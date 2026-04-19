#!/usr/bin/env python3
"""
sync_device.py
Reads {codename}.json and changelog_{codename}.txt from the OTA repo
and updates data/devices/{codename}.json in the website repo.

Usage:
    python scripts/sync_device.py <codename> [android_branch] [--dry-run] [--keep-builds N]

Arguments:
    codename          Device codename (e.g. "miatoll")
    android_branch    Git branch name; plain integers are used as the Android
                      version number (e.g. "16" → androidVersion: "16").
                      Non-integer branch names (e.g. "lineage-21") set
                      androidVersion to "unknown". Defaults to "16".

Options:
    --dry-run         Parse and validate everything, print what would change,
                      but do NOT write the website JSON back to disk.
    --keep-builds N   Keep the N most recent build entries in the "builds"
                      array. Defaults to 1 (only the latest build, matching
                      the original behaviour). Set higher to preserve history.

Environment variables:
    OTA_REPO_PATH      Path to the checked-out OTA repo (default: ".")
    WEBSITE_REPO_PATH  Path to the checked-out website repo (default: "../website-repo")

Exit codes:
    0  Success (or --dry-run completed without errors)
    1  Fatal error — missing files, malformed JSON, unrecoverable failure.
       The CI step will fail and the workflow annotation will show the reason.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Helpers

def _die(message: str) -> None:
    """Print an error to stderr and exit with code 1."""
    print(f"[ERROR] {message}", file=sys.stderr)
    sys.exit(1)

def bytes_to_human(size_bytes: int) -> str:
    """Convert bytes to a rounded-down GB string, e.g. 3041415631 → '2.8 GB'."""
    gb = size_bytes / (1024 ** 3)
    floored = math.floor(gb * 10) / 10
    return f"{floored:.1f} GB"

def unix_to_ddmmyyyy(timestamp: int) -> str:
    """Convert a UNIX timestamp to DD-MM-YYYY (UTC)."""
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.strftime("%d-%m-%Y")

def ddmmyyyy_to_monthyear(date_str: str) -> str:
    """'10-04-2026' → 'April 2026'"""
    dt = datetime.strptime(date_str, "%d-%m-%Y")
    return dt.strftime("%B %Y")

def folder_url_from_file_url(file_url: str) -> str:
    """
    Strip the filename from a direct download URL to get the folder URL.
    'https://host/folder/miatoll/3.x/3.9/CloverProject-v3.9-...zip'
     → 'https://host/folder/miatoll/3.x/3.9'
    """
    return file_url.rsplit("/", 1)[0]

def status_from_filename(filename: str) -> str:
    """Infer build status from the filename convention."""
    upper = filename.upper()
    if "OFFICIAL" in upper:
        return "Stable"
    if "UNOFFICIAL" in upper:
        return "Unofficial"
    return "Unknown"

def android_version_from_branch(branch: str) -> str:
    """
    Return the Android version string for a given branch name.
    Plain integers ("16", "15") map directly.
    Anything else (e.g. "lineage-21", "main") returns "unknown".
    """
    return branch if branch.isdigit() else "unknown"

# OTA JSON validation

_REQUIRED_FIELDS: list[str] = [
    "datetime", "filename", "md5", "size", "url", "version",
]

def validate_ota_entry(entry: dict[str, Any], path: str) -> None:
    """Raise SystemExit(1) if any required field is missing or has wrong type."""
    missing = [f for f in _REQUIRED_FIELDS if f not in entry]
    if missing:
        _die(f"OTA JSON at {path} is missing required fields: {missing}")

    if not isinstance(entry["datetime"], int):
        _die(f"OTA JSON 'datetime' must be an integer, got {type(entry['datetime'])!r}")
    if not isinstance(entry["size"], int):
        _die(f"OTA JSON 'size' must be an integer, got {type(entry['size'])!r}")

# Changelog parsing

# Matches headers like "===== 10 April 2026 ====="
_SECTION_RE = re.compile(r"={3,}\s*(\d{1,2}\s+\w+\s+\d{4})\s*={3,}")

# Lines that are metadata, not bullet-point entries
_METADATA_PREFIXES: tuple[str, ...] = (
    "Highlights",
    "Build type:",
    "Device:",
    "Device maintainer:",
)

def _parse_section_content(raw: str) -> list[str]:
    """Extract bullet-point lines from a changelog section body."""
    entries: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if any(line.startswith(p) for p in _METADATA_PREFIXES):
            continue
        # Strip leading "* " bullet marker
        if line.startswith("* "):
            line = line[2:].strip()
        if line:
            entries.append(line)
    return entries


def parse_changelog(changelog_text: str, target_date_str: str) -> list[str]:
    """
    Parse the full changelog and return entries for the section whose date
    matches target_date_str (DD-MM-YYYY).

    Returns an empty list if the section is not found; the caller decides
    whether that is a warning or a hard error.
    """
    target_dt = datetime.strptime(target_date_str, "%d-%m-%Y").date()

    # Split: [preamble, date_header, content, date_header, content, …]
    parts = _SECTION_RE.split(changelog_text)

    for i in range(1, len(parts), 2):
        header_date_str = parts[i].strip()
        content = parts[i + 1] if i + 1 < len(parts) else ""

        try:
            # Header format: "10 April 2026"
            section_dt = datetime.strptime(header_date_str, "%d %B %Y").date()
        except ValueError:
            continue

        if section_dt == target_dt:
            return _parse_section_content(content)

    return []

# Main sync logic

def sync_device(codename: str, android_branch: str, dry_run: bool, keep_builds: int) -> None:
    ota_root = Path(os.environ.get("OTA_REPO_PATH", "."))
    web_root = Path(os.environ.get("WEBSITE_REPO_PATH", "../website-repo"))

    ota_json_path    = ota_root / "devices"    / f"{codename}.json"
    changelog_path   = ota_root / "changelogs" / f"changelog_{codename}.txt"
    website_json_path = web_root / "data" / "devices" / f"{codename}.json"

    # Hard stop if any required file is missing — return silently was masking
    # real problems before (the CI step would succeed even on missing input).
    for path in (ota_json_path, changelog_path, website_json_path):
        if not path.exists():
            _die(f"Required file not found: {path}")

    # --- Read and validate OTA JSON ---
    try:
        ota_data: dict = json.loads(ota_json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _die(f"Failed to parse OTA JSON at {ota_json_path}: {exc}")

    if not ota_data.get("response"):
        _die(f"OTA JSON at {ota_json_path} has an empty 'response' array.")

    latest: dict = ota_data["response"][0]
    validate_ota_entry(latest, str(ota_json_path))

    version: str    = latest["version"]
    md5: str        = latest["md5"]
    filename: str   = latest["filename"]
    file_url: str   = latest["url"]
    size_bytes: int = latest["size"]
    timestamp: int  = latest["datetime"]

    date_str       = unix_to_ddmmyyyy(timestamp)
    security_patch = ddmmyyyy_to_monthyear(date_str)
    file_size      = bytes_to_human(size_bytes)
    status         = status_from_filename(filename)
    download_url   = folder_url_from_file_url(file_url)
    android_ver    = android_version_from_branch(android_branch)

    # --- Parse changelog ---
    changelog_text = changelog_path.read_text(encoding="utf-8")
    changelog_lines = parse_changelog(changelog_text, date_str)

    if not changelog_lines:
        # Warn but do not abort: changelog omissions are common during hotfix
        # releases and should not block the build metadata from syncing.
        print(f"[WARN] No changelog section found for {date_str} in {changelog_path}",
              file=sys.stderr)

    # --- Read website JSON ---
    try:
        website_data: dict = json.loads(website_json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _die(f"Failed to parse website JSON at {website_json_path}: {exc}")

    changes_made = False

    # --- Update builds ---
    # We prepend the new build and trim to `keep_builds` entries so that
    # historical builds can be preserved when the caller passes --keep-builds > 1.
    # Default (keep_builds=1) matches the original behaviour of one entry only.
    existing_versions = {b["version"] for b in website_data.get("builds", [])}
    if version not in existing_versions:
        new_build = {
            "version":       version,
            "androidVersion": android_ver,
            "status":        status,
            "date":          date_str,
            "securityPatch": security_patch,
            "fileSize":      file_size,
            "md5":           md5,
            "downloadUrl":   download_url,
        }
        builds: list = website_data.setdefault("builds", [])
        builds.insert(0, new_build)
        # Trim to the requested history depth
        website_data["builds"] = builds[:keep_builds]
        print(f"  [builds]          added v{version} (keeping last {keep_builds})")
        changes_made = True
    else:
        print(f"  [builds]          v{version} already present, skipped")

    # --- Update deviceChangelog ---
    existing_cl_versions = {c["version"] for c in website_data.get("deviceChangelog", [])}
    if version not in existing_cl_versions:
        new_cl_entry = {
            "version": version,
            "date":    date_str,
            "log":     changelog_lines,
        }
        website_data.setdefault("deviceChangelog", []).insert(0, new_cl_entry)
        print(f"  [deviceChangelog] prepended v{version} ({len(changelog_lines)} entries)")
        changes_made = True
    else:
        print(f"  [deviceChangelog] v{version} already present, skipped")

    # --- Summary ---
    print()
    print(f"  version        : {version}")
    print(f"  date           : {date_str}")
    print(f"  securityPatch  : {security_patch}")
    print(f"  fileSize       : {file_size}")
    print(f"  status         : {status}")
    print(f"  androidVersion : {android_ver}")
    print(f"  changelog lines: {len(changelog_lines)}")

    if not changes_made:
        print(f"\n  Nothing changed — {codename}.json is already up to date.")
        return

    # --- Write back ---
    if dry_run:
        print(f"\n[DRY-RUN] Would write {website_json_path} — skipping.")
        return

    website_json_path.write_text(
        json.dumps(website_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\n✓ {codename}.json saved to {website_json_path}")

# Entry point

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync OTA device data into the website repo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "codename",
        help="Device codename, e.g. miatoll",
    )
    parser.add_argument(
        "android_branch",
        nargs="?",
        default="16",
        help="Git branch name (plain integers used as Android version). Default: 16",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print changes without writing to disk.",
    )
    parser.add_argument(
        "--keep-builds",
        type=int,
        default=1,
        metavar="N",
        help="Number of build history entries to retain (default: 1).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.keep_builds < 1:
        print("[ERROR] --keep-builds must be at least 1", file=sys.stderr)
        sys.exit(1)

    print(f"── Syncing {args.codename} (branch: {args.android_branch}) ──")
    if args.dry_run:
        print("[DRY-RUN] No files will be modified.\n")

    sync_device(
        codename=args.codename,
        android_branch=args.android_branch,
        dry_run=args.dry_run,
        keep_builds=args.keep_builds,
    )