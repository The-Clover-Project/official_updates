import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = "https://api.github.com"
USER_AGENT = "cloverbot-pr-check/1.0 (+https://thecloverproject.com)"
OTA_HOST = "ota.thecloverproject.com"
MARKER = "<!-- clover-pr-check -->"
LABEL_OK = "format-ok"
LABEL_BAD = "format-bad"
LABELS = {
    LABEL_OK: ("0e8a16", "The release PR format check passed"),
    LABEL_BAD: ("d93f0b", "The release PR format check failed"),
}
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}
KEYS = ["datetime", "filename", "id", "md5", "size", "url", "version", "romtype", "devicename", "maintainer"]
OPTIONAL_KEYS = ["os_sdk_level", "os_patch_level", "ota_property_files"]
DEVICE_PATH = re.compile(r"^devices/(gms|vanilla)/([A-Za-z0-9][A-Za-z0-9_.-]*)\.json$")
CHANGELOG_PATH = re.compile(r"^changelogs/changelog_([A-Za-z0-9][A-Za-z0-9_.-]*)\.txt$")
SITE_PATH = re.compile(r"^site/devices/([a-z0-9][a-z0-9_.-]*)\.json$")
COMPAT_PATH = "ota/compatibility.json"
COMPAT_KEYS = ["ver_clean_required", "ver_dirty_allowed", "devices"]
CODENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
VERSION = re.compile(r"^(\d+)(?:\.(\d+))?(?:-HOTFIX(\d*))?$")
VERSION_HELP = "a version must look like `4.0`, `4.6-HOTFIX` or `4.6-HOTFIX2`"
FILENAME = re.compile(
    r"^CloverProject-(?P<version>[^-]+(?:-HOTFIX\d*)?)-(?P<date>\d{8}(?:_\d{6})?)-(?P<romtype>[A-Za-z]+)-(?P<codename>.+)-(?P<variant>GMS|VANILLA)\.zip$"
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
PATCH_LEVEL = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROPERTY_FILE = re.compile(r"^[A-Za-z0-9_.-]+:\d+:\d+$")
MAX_TEXT = 64 * 1024
MAX_ROM = 8 * 1024 * 1024 * 1024
FUTURE_SLACK = 24 * 3600


def clean(value, limit=200):
    text = str(value).replace("`", "'").replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def human_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def github_user(url):
    parts = urllib.parse.urlparse((url or "").strip()).path.strip("/").split("/")
    return parts[0].lower() if parts and parts[0] else ""


def read_local(path):
    target = ROOT / path
    if not target.is_file():
        return None
    return target.read_text(encoding="utf-8", errors="replace")


def version_key(text):
    match = VERSION.match(text) if isinstance(text, str) else None
    if not match:
        return None
    hotfix = match[3]
    return (int(match[1]), int(match[2] or 0), 0 if hotfix is None else int(hotfix or 1))


def version_set(value):
    if not isinstance(value, list):
        return set()
    return {key for key in map(version_key, value) if key is not None}


def device_clean_set(compat, codename):
    devices = compat.get("devices") if isinstance(compat, dict) else None
    if not isinstance(devices, dict):
        return set()
    result = set()
    for name, device in devices.items():
        if name.lower() == codename.lower() and isinstance(device, dict):
            result |= version_set(device.get("ver_clean_required"))
    return result


def case_conflicts(path):
    target = ROOT / path
    if not target.parent.is_dir():
        return []
    return sorted(
        f"{Path(path).parent.as_posix()}/{other.name}"
        for other in target.parent.iterdir()
        if other.name != target.name and other.name.lower() == target.name.lower()
    )


def parse_compat(text):
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def site_device(codename):
    text = read_local(f"site/devices/{codename.lower()}.json")
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.checked = []

    def error(self, path, message):
        self.errors.append(f"`{clean(path)}`: {message}" if path else message)

    def warn(self, path, message):
        self.warnings.append(f"`{clean(path)}`: {message}" if path else message)

    @property
    def ok(self):
        return not self.errors


def verify_download(url, size, sha256, md5, attempts=5):
    sha = hashlib.sha256()
    digest = hashlib.md5()
    total = 0
    last = ""
    for attempt in range(attempts):
        if attempt:
            time.sleep(5 * attempt)
        headers = {"User-Agent": USER_AGENT}
        if total:
            headers["Range"] = f"bytes={total}-"
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=120) as response:
                if response.status == 200 and total:
                    sha = hashlib.sha256()
                    digest = hashlib.md5()
                    total = 0
                elif response.status not in (200, 206):
                    return f"download returned HTTP {response.status}"
                while True:
                    chunk = response.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > size:
                        return f"download is bigger than size {size}"
                    sha.update(chunk)
                    digest.update(chunk)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return "download returned HTTP 404 (file is not on the OTA server)"
            last = f"download returned HTTP {exc.code}"
            continue
        except Exception as exc:
            last = f"download failed at {total} of {size} bytes: {clean(exc)}"
            continue
        if total == size:
            break
        last = f"download stopped at {total} of {size} bytes"
    if total != size:
        return last
    if sha.hexdigest() != sha256:
        return "download SHA-256 is not the same as `id`"
    if digest.hexdigest() != md5:
        return "download MD5 is not the same as `md5`"
    return None


def check_entry_types(report, path, entry):
    if not isinstance(entry, dict):
        report.error(path, "each item in `response` must be an object")
        return False
    keys = list(entry.keys())
    missing = [k for k in KEYS if k not in entry]
    extra = [k for k in keys if k not in KEYS and k not in OPTIONAL_KEYS]
    if missing:
        report.error(path, "missing keys: " + ", ".join(f"`{k}`" for k in missing))
    if extra:
        report.error(path, "unknown keys: " + ", ".join(f"`{clean(k, 40)}`" for k in extra))
    if missing or extra:
        return False
    if keys != [k for k in KEYS + OPTIONAL_KEYS if k in entry]:
        report.warn(path, "keys are not in the usual order")
    good = True
    for key in ("datetime", "size"):
        value = entry[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            report.error(path, f"`{key}` must be a positive whole number")
            good = False
    for key in ("filename", "id", "md5", "url", "version", "romtype", "devicename", "maintainer"):
        value = entry[key]
        if not isinstance(value, str) or not value.strip():
            report.error(path, f"`{key}` must be a text value that is not empty")
            good = False
        elif value != value.strip():
            report.error(path, f"`{key}` has spaces at the start or end")
            good = False
    if "os_sdk_level" in entry:
        value = entry["os_sdk_level"]
        if isinstance(value, bool) or not isinstance(value, int) or not 21 <= value <= 99:
            report.error(path, "`os_sdk_level` must be a whole number, for example `36`")
            good = False
    if "os_patch_level" in entry:
        value = entry["os_patch_level"]
        try:
            if not isinstance(value, str) or not PATCH_LEVEL.match(value):
                raise ValueError
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            report.error(path, "`os_patch_level` must be a date like `2026-10-01`")
            good = False
    if "ota_property_files" in entry:
        value = entry["ota_property_files"]
        parts = value.strip().split(",") if isinstance(value, str) else []
        if not parts or not all(PROPERTY_FILE.match(p) for p in parts):
            report.error(path, "`ota_property_files` must look like `payload.bin:4662:1926274191,...` (the `ota-property-files` value of the OTA metadata)")
            good = False
        elif not any(p.startswith("payload.bin:") for p in parts):
            report.warn(path, "`ota_property_files` has no `payload.bin`, so the app cannot stream this update")
    return good


def check_device_json(report, path, variant, codename, text, download=True):
    if len(text.encode("utf-8")) > MAX_TEXT:
        report.error(path, "file is too big for an OTA JSON")
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        report.error(path, f"not valid JSON: {clean(exc)}")
        return None
    if not isinstance(data, dict) or list(data.keys()) != ["response"]:
        report.error(path, "the top level must be an object with only the key `response`")
        return None
    response = data["response"]
    if not isinstance(response, list) or len(response) != 1:
        report.error(path, "`response` must be a list with exactly one build")
        return None
    entry = response[0]
    if not check_entry_types(report, path, entry):
        return None
    expected = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if text != expected:
        report.warn(path, "format is not the same as the bot output (indent 2, newline at the end)")

    name = FILENAME.match(entry["filename"])
    if not name:
        report.error(path, "`filename` must look like `CloverProject-<version>[-HOTFIX[n]]-<yyyymmdd[_hhmmss]>-OFFICIAL-<codename>-<GMS|VANILLA>.zip`")
        return None
    if name["codename"].lower() != codename.lower():
        report.error(path, f"`filename` is for `{clean(name['codename'])}`, but the file is `{codename}.json`")
    elif name["codename"] != codename:
        report.error(path, f"the file must be `devices/{variant}/{clean(name['codename'])}.json`; the phone asks for the codename with the same upper and lower case letters as in `filename`")
    if name["variant"].lower() != variant:
        report.error(path, f"`filename` is a {name['variant']} build, but the file is in `devices/{variant}/`")
    if entry["romtype"].lower() != name["romtype"].lower():
        report.error(path, f"`romtype` is `{clean(entry['romtype'])}`, but `filename` has `{clean(name['romtype'])}`")
    if entry["romtype"] != entry["romtype"].lower():
        report.warn(path, "`romtype` is usually lower case")
    if version_key(entry["version"]) is None:
        report.error(path, f"`version` is `{clean(entry['version'])}`; {VERSION_HELP}")
    elif entry["version"] != name["version"]:
        report.error(path, f"`version` is `{clean(entry['version'])}`, but `filename` has `{clean(name['version'])}`")

    if not HEX64.match(entry["id"]):
        report.error(path, "`id` must be the SHA-256 of the zip (64 lower case hex characters)")
    if not HEX32.match(entry["md5"]):
        report.error(path, "`md5` must be 32 lower case hex characters")
    if entry["size"] > MAX_ROM:
        report.error(path, "`size` is too big")

    url = urllib.parse.urlparse(entry["url"])
    url_ok = True
    if url.scheme != "https" or url.hostname != OTA_HOST or url.port is not None or url.query or url.fragment or url.username:
        report.error(path, f"`url` must start with `https://{OTA_HOST}/` and have no query")
        url_ok = False
    else:
        parts = [urllib.parse.unquote(p) for p in url.path.split("/")]
        if len(parts) < 4 or parts[1].lower() != codename.lower() or parts[-1] != entry["filename"] or any(p in ("", ".", "..") for p in parts[1:]):
            report.error(path, f"`url` must be `https://{OTA_HOST}/{codename.lower()}/<version>/<filename>`")
            url_ok = False

    now = int(time.time())
    if entry["datetime"] > now + FUTURE_SLACK:
        report.error(path, "`datetime` is in the future")
    stamp = datetime.fromtimestamp(entry["datetime"], tz=timezone.utc)
    try:
        built = datetime.strptime(name["date"][:8], "%Y%m%d").replace(tzinfo=timezone.utc)
        if abs((stamp - built).total_seconds()) > 3 * 86400:
            report.warn(path, f"`datetime` ({stamp:%Y-%m-%d}) is not near the date in `filename` ({built:%Y-%m-%d})")
    except ValueError:
        report.error(path, "the date in `filename` is not a real date")

    base = read_local(path)
    if base is not None:
        try:
            old = (json.loads(base).get("response") or [None])[0] or {}
        except (ValueError, AttributeError):
            old = {}
        if old.get("id") == entry["id"] or old.get("filename") == entry["filename"]:
            report.error(path, "this build is already released")
        old_time = old.get("datetime")
        if isinstance(old_time, int) and entry["datetime"] <= old_time:
            report.error(path, "`datetime` is not newer than the current release")

    device = site_device(codename)
    if device is None:
        report.warn(path, f"`site/devices/{codename.lower()}.json` does not exist; the website and the post use fallback data")
    else:
        if device.get("name") and device["name"] != entry["devicename"]:
            report.warn(path, f"`devicename` is not the same as the site name `{clean(device['name'])}`")
        site_maintainer = (device.get("maintainer") or {}).get("name")
        if site_maintainer and site_maintainer != entry["maintainer"]:
            report.warn(path, f"`maintainer` is not the same as the site maintainer `{clean(site_maintainer)}`")

    result = f"{clean(entry['version'])} {name['variant']}, {human_size(entry['size'])}"
    if download and url_ok and HEX64.match(entry["id"]) and HEX32.match(entry["md5"]) and entry["size"] <= MAX_ROM:
        problem = verify_download(entry["url"], entry["size"], entry["id"], entry["md5"])
        if problem:
            report.error(path, problem)
            result += ", download failed"
        else:
            result += ", download OK"
    report.checked.append(f"`{path}`: {result}")
    return entry


def check_changelog(report, path, text):
    if len(text.encode("utf-8")) > MAX_TEXT:
        report.error(path, "changelog is too big")
    elif not text.strip():
        report.error(path, "changelog is empty")
    elif "\x00" in text:
        report.error(path, "changelog is not a text file")
    else:
        report.checked.append(f"`{path}`: changelog")


def check_site_device(report, path, codename, text, released):
    if codename not in released:
        report.error(path, f"a release PR can change `site/devices/{codename}.json` only when it also releases `{codename}`")
        return
    if len(text.encode("utf-8")) > MAX_TEXT:
        report.error(path, "file is too big for a website device page")
        return
    try:
        data = json.loads(text)
    except ValueError as exc:
        report.error(path, f"not valid JSON: {clean(exc)}")
        return
    if not isinstance(data, dict):
        report.error(path, "the top level must be an object")
        return
    old = site_device(codename)
    if old is not None and old.get("maintainer") != data.get("maintainer"):
        report.warn(path, "changes the maintainer; an admin must check it")
    report.warn(path, "changes the website device page; an admin must review it")
    report.checked.append(f"`{path}`: website device page")


def check_version_list(report, path, name, value):
    if not isinstance(value, list):
        report.error(path, f"`{name}` must be a list of versions")
        return {}
    keys = {}
    for item in value:
        key = version_key(item)
        if key is None:
            report.error(path, f"`{name}` has `{clean(item, 40)}`; {VERSION_HELP}")
        elif key in keys:
            report.error(path, f"`{name}` has `{clean(item, 40)}` more than one time")
        else:
            keys[key] = item
    return keys


def check_compatibility(report, path, text, trusted, released):
    if len(text.encode("utf-8")) > MAX_TEXT:
        report.error(path, "file is too big")
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        report.error(path, f"not valid JSON: {clean(exc)}")
        return None
    if not isinstance(data, dict) or sorted(data.keys()) != sorted(COMPAT_KEYS):
        report.error(path, "the top level must be an object with only the keys " + ", ".join(f"`{k}`" for k in COMPAT_KEYS))
        return None

    errors = len(report.errors)
    clean_required = check_version_list(report, path, "ver_clean_required", data["ver_clean_required"])
    dirty_allowed = check_version_list(report, path, "ver_dirty_allowed", data["ver_dirty_allowed"])
    for key in sorted(clean_required.keys() & dirty_allowed.keys()):
        report.error(path, f"`{clean(clean_required[key], 40)}` is in `ver_clean_required` and in `ver_dirty_allowed`")
    devices = data["devices"]
    if not isinstance(devices, dict):
        report.error(path, "`devices` must be an object")
        return None
    seen = {}
    for codename, device in devices.items():
        label = f"devices.{clean(codename, 40)}"
        if not CODENAME.match(codename):
            report.error(path, f"`{label}`: the codename is not valid")
        elif codename.lower() in seen:
            report.error(path, f"`{label}` and `devices.{clean(seen[codename.lower()], 40)}` are the same device; use one key with the same case as `ro.clover.device`")
        else:
            seen[codename.lower()] = codename
        if not isinstance(device, dict) or list(device.keys()) != ["ver_clean_required"]:
            report.error(path, f"`{label}` must be an object with only the key `ver_clean_required`")
            continue
        keys = check_version_list(report, path, f"{label}.ver_clean_required", device["ver_clean_required"])
        for key in sorted(keys.keys() & clean_required.keys()):
            report.warn(path, f"`{label}.ver_clean_required` has `{clean(keys[key], 40)}`, but it is already in the global `ver_clean_required`")
        if site_device(codename) is None:
            report.warn(path, f"`{label}`: no `site/devices/{clean(codename, 40)}.json` for this device")
    if json.dumps(data, indent=2, ensure_ascii=False) + "\n" != text:
        report.warn(path, "format is not indent 2 with a newline at the end")

    if not trusted:
        base = parse_compat(read_local(path))
        if base is None:
            report.error(path, "only an admin can add this file")
            return None
        if (version_set(base.get("ver_clean_required")) != set(clean_required)
                or version_set(base.get("ver_dirty_allowed")) != set(dirty_allowed)):
            report.error(path, "only an admin can change `ver_clean_required` and `ver_dirty_allowed`; add versions for your device to `devices.<codename>.ver_clean_required`")
        old_devices = base.get("devices") if isinstance(base.get("devices"), dict) else {}
        for codename in sorted(set(devices) | set(old_devices)):
            old = device_clean_set(base, codename)
            new = device_clean_set(data, codename)
            if old == new:
                continue
            label = f"devices.{clean(codename, 40)}"
            if codename.lower() not in released:
                report.error(path, f"a release PR can change `{label}` only when it also releases `{clean(codename, 40)}`")
            elif not old <= new:
                report.error(path, f"`{label}.ver_clean_required`: you can only add versions; only an admin can remove them")

    if len(report.errors) == errors:
        report.checked.append(f"`{path}`: OTA compatibility rules")
    return data


def check_release_versions(report, compat, releases):
    if compat is None:
        report.warn("", f"no valid `{COMPAT_PATH}`; the app allows a dirty flash for every version")
        return
    clean_required = version_set(compat.get("ver_clean_required"))
    dirty_allowed = version_set(compat.get("ver_dirty_allowed"))
    for codename, version in sorted(releases):
        key = version_key(version)
        if key is None:
            continue
        if key in clean_required or key in device_clean_set(compat, codename):
            report.checked.append(f"`{codename}` {clean(version, 40)}: clean flash required")
        elif key in dirty_allowed:
            report.checked.append(f"`{codename}` {clean(version, 40)}: dirty flash allowed")
        else:
            report.warn("", f"`{clean(version, 40)}` for `{codename}` is not in `{COMPAT_PATH}`, so the app allows a dirty flash. If it needs a clean flash, add it to `devices.{codename}.ver_clean_required`, or ask an admin to add it to a global list")


def check_files(report, files, author, association, download=True):
    codenames = set()
    released = set()
    device_names = {}
    changelogs = []
    releases = set()
    sites = []
    compat_files = []
    device_files = 0
    trusted = association in TRUSTED
    pr_paths = {}
    for item in files:
        if item["status"] in ("added", "modified"):
            pr_paths.setdefault(item["path"].lower(), []).append(item["path"])
    for item in files:
        path, status, text = item["path"], item["status"], item.get("text")
        device = DEVICE_PATH.match(path)
        changelog = CHANGELOG_PATH.match(path)
        site = SITE_PATH.match(path)
        compat = path == COMPAT_PATH
        if (device or changelog) and status in ("added", "modified"):
            others = sorted(set(case_conflicts(path) + pr_paths.get(path.lower(), [])) - {path})
            if others:
                report.error(path, "a file for the same device with other upper and lower case letters exists: "
                             + ", ".join(f"`{clean(o)}`" for o in others)
                             + "; keep the codename as `ro.clover.device` has it and use only one file")
        if not device and not changelog and not site and not compat:
            report.error(path, f"a release PR can only change `devices/<gms|vanilla>/<codename>.json`, `changelogs/changelog_<codename>.txt`, `site/devices/<codename>.json` and `{COMPAT_PATH}`")
            continue
        if status not in ("added", "modified"):
            report.error(path, f"file is {clean(status, 20)}; a release PR can only add or change files")
            continue
        if text is None:
            report.error(path, "cannot read the file from the PR")
            continue
        if compat:
            compat_files.append((path, text))
        elif site:
            codenames.add(site[1])
            sites.append((path, site[1], text))
        elif device:
            device_files += 1
            codename = device[2]
            codenames.add(codename.lower())
            released.add(codename.lower())
            device_names.setdefault(codename.lower(), codename)
            entry = check_device_json(report, path, device[1], codename, text, download)
            if entry:
                releases.add((codename, entry["version"]))
        else:
            codename = changelog[1]
            codenames.add(codename.lower())
            changelogs.append((path, codename))
            if site_device(codename) is None and read_local(path) is None:
                report.warn(path, f"no `site/devices/{codename.lower()}.json` for this changelog")
            check_changelog(report, path, text)
    for path, codename in changelogs:
        name = device_names.get(codename.lower())
        if name and name != codename:
            report.error(path, f"the changelog must be `changelogs/changelog_{clean(name)}.txt`, with the same codename as the OTA JSON")
    for path, codename, text in sites:
        check_site_device(report, path, codename, text, released)
    compat_data = parse_compat(read_local(COMPAT_PATH))
    for path, text in compat_files:
        compat_data = check_compatibility(report, path, text, trusted, released)
    if releases:
        check_release_versions(report, compat_data, releases)
    if device_files == 0 and files:
        report.warn("", "this PR has no OTA JSON, so it releases nothing")
    if not files:
        report.error("", "this PR changes no files")

    if trusted:
        return
    login = (author or "").lower()
    for codename in sorted(codenames):
        device = site_device(codename) or {}
        maintainer = github_user((device.get("maintainer") or {}).get("githubUrl"))
        if not maintainer or maintainer != login:
            report.error("", f"@{clean(author, 40)} is not the maintainer of `{codename}` in `site/devices/{codename}.json` and has no write access")


def render_comment(report, sha):
    lines = [MARKER]
    if report.ok:
        lines.append("### ✅ Release format check passed")
    else:
        lines.append("### ❌ Release format check failed")
    lines.append(f"Checked commit `{sha[:7]}`.")
    if report.errors:
        lines += ["", "**Problems**"] + [f"- {e}" for e in report.errors]
    if report.warnings:
        lines += ["", "**Warnings**"] + [f"- {w}" for w in report.warnings]
    if report.checked:
        lines += ["", "**Checked files**"] + [f"- {c}" for c in report.checked]
    lines += ["", "_This check never merges. An admin reviews and merges the PR._"]
    return "\n".join(lines) + "\n"


def markdown_to_text(item):
    return item.replace("`", "")


def render_telegram(report, pr, repo):
    head = "✅ <b>Release PR: format OK</b>" if report.ok else "❌ <b>Release PR: format bad</b>"
    lines = [
        "#PR_CHECK",
        head,
        f"<b>Repo:</b> {html.escape(repo)}",
        f"<b>PR:</b> <a href=\"{html.escape(pr['html_url'], quote=True)}\">#{pr['number']}</a> {html.escape(clean(pr.get('title') or '', 120))}",
        f"<b>User:</b> {html.escape(pr['user']['login'])}",
    ]
    for item in report.checked[:6]:
        lines.append("• " + html.escape(markdown_to_text(item)))
    if report.errors:
        lines.append("<b>Problems:</b>")
        for item in report.errors[:8]:
            lines.append("• " + html.escape(markdown_to_text(item)))
        if len(report.errors) > 8:
            lines.append(f"• and {len(report.errors) - 8} more")
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3990] + "…"


class GitHub:
    def __init__(self, token, repo):
        self.token = token
        self.repo = repo

    def call(self, method, path, body=None, accept="application/vnd.github+json", ok=(200, 201, 204)):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(API + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", accept)
        request.add_header("User-Agent", USER_AGENT)
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        if status not in ok:
            raise RuntimeError(f"GitHub {method} {path}: HTTP {status}")
        return status, raw

    def json(self, method, path, body=None, ok=(200, 201, 204)):
        _, raw = self.call(method, path, body, ok=ok)
        return json.loads(raw) if raw else None

    def pr_files(self, number):
        files = []
        page = 1
        while True:
            batch = self.json("GET", f"/repos/{self.repo}/pulls/{number}/files?per_page=100&page={page}")
            files += batch
            if len(batch) < 100 or page >= 30:
                return files
            page += 1

    def file_text(self, repo, path, ref):
        quoted = urllib.parse.quote(path)
        try:
            _, raw = self.call("GET", f"/repos/{repo}/contents/{quoted}?ref={urllib.parse.quote(ref)}", accept="application/vnd.github.raw")
        except RuntimeError:
            return None
        if len(raw) > MAX_TEXT * 4:
            return raw[: MAX_TEXT * 4].decode("utf-8", errors="replace")
        return raw.decode("utf-8", errors="replace")

    def upsert_comment(self, number, body):
        page = 1
        while True:
            comments = self.json("GET", f"/repos/{self.repo}/issues/{number}/comments?per_page=100&page={page}")
            for comment in comments:
                if MARKER in (comment.get("body") or "") and (comment.get("user") or {}).get("type") == "Bot":
                    self.json("PATCH", f"/repos/{self.repo}/issues/comments/{comment['id']}", {"body": body})
                    return
            if len(comments) < 100 or page >= 10:
                break
            page += 1
        self.json("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})

    def set_label(self, number, label):
        for name, (color, description) in LABELS.items():
            self.call("POST", f"/repos/{self.repo}/labels", {"name": name, "color": color, "description": description}, ok=(201, 422))
        other = LABEL_BAD if label == LABEL_OK else LABEL_OK
        self.call("DELETE", f"/repos/{self.repo}/issues/{number}/labels/{other}", ok=(200, 204, 404))
        self.json("POST", f"/repos/{self.repo}/issues/{number}/labels", {"labels": [label]})


def send_telegram(text):
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat = os.environ.get("PRIV_CHAT_ID", "").strip()
    if not token or not chat:
        print("Telegram is not configured; no message sent")
        return
    body = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30):
            pass
    except Exception as exc:
        print(f"Telegram message failed: {type(exc).__name__}")


def main():
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not token or not repo or not event_path:
        print("GITHUB_TOKEN, GITHUB_REPOSITORY and GITHUB_EVENT_PATH are required")
        return 2
    with open(event_path, encoding="utf-8") as handle:
        event = json.load(handle)
    pr = event["pull_request"]
    gh = GitHub(token, repo)
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name") or repo
    head_sha = pr["head"]["sha"]

    files = []
    for item in gh.pr_files(pr["number"]):
        path = item["filename"]
        text = None
        if item["status"] in ("added", "modified") and (DEVICE_PATH.match(path) or CHANGELOG_PATH.match(path) or SITE_PATH.match(path) or path == COMPAT_PATH):
            text = gh.file_text(head_repo, path, head_sha)
        files.append({"path": path, "status": item["status"], "text": text})

    report = Report()
    download = os.environ.get("CHECK_DOWNLOAD", "1") != "0"
    check_files(report, files, pr["user"]["login"], pr.get("author_association", ""), download)

    comment = render_comment(report, head_sha)
    print(comment)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(comment)
    gh.upsert_comment(pr["number"], comment)
    gh.set_label(pr["number"], LABEL_OK if report.ok else LABEL_BAD)
    send_telegram(render_telegram(report, pr, repo))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
