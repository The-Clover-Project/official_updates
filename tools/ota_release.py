#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

BUCKET_NAME = "clover-ota"
PUBLIC_BASE_URL = "https://ota.thecloverproject.com"
AUTH_URL = "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"
DEFAULT_ENV_FILE = "~/.config/clover/b2.env"
FILENAME_PATTERN = re.compile(
    r"^CloverProject-(?P<version>[^-]+)-(?P<date>\d{8}(?:_\d{6})?)-(?P<romtype>[A-Za-z]+)-(?P<codename>.+)-(?P<variant>GMS|VANILLA)\.zip$",
    re.IGNORECASE,
)
RETRY_STATUSES = {401, 408, 429, 500, 503}
USER_AGENT = "clover-ota-release/1.0"
HASH_CHUNK = 8 * 1024 * 1024


class B2Error(Exception):
    def __init__(self, status, code, message):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code


def log(message):
    print(message, file=sys.stderr, flush=True)


def progress(label, done, total):
    percent = done * 100 // total if total else 100
    print(f"\r  {label}: {percent:3d}% ({done / 1024 ** 3:.2f}/{total / 1024 ** 3:.2f} GB)", end="", file=sys.stderr, flush=True)
    if done >= total:
        print(file=sys.stderr, flush=True)


def http_json(url, payload=None, headers=None, data=None, method=None):
    body = data if data is not None else (json.dumps(payload).encode() if payload is not None else None)
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = json.load(error)
        except ValueError:
            detail = {}
        raise B2Error(error.code, detail.get("code", ""), detail.get("message", error.reason)) from None


def load_credentials(env_file):
    key_id = os.environ.get("B2_KEY_ID")
    app_key = os.environ.get("B2_APP_KEY")
    path = Path(env_file).expanduser()
    if (not key_id or not app_key) and path.is_file():
        values = {}
        for line in path.read_text().splitlines():
            for separator in ("=", ":"):
                if separator in line:
                    name, value = line.split(separator, 1)
                    values[name.strip().lower()] = value.strip().strip("\"'")
                    break
        key_id = key_id or values.get("b2_key_id") or values.get("keyid")
        app_key = app_key or values.get("b2_app_key") or values.get("applicationkey")
    if not key_id or not app_key:
        sys.exit(f"No B2 key found. Set B2_KEY_ID and B2_APP_KEY, or write them to {env_file}.")
    return key_id, app_key


class B2:
    def __init__(self, key_id, app_key):
        token = base64.b64encode(f"{key_id}:{app_key}".encode()).decode()
        data = http_json(AUTH_URL, headers={"Authorization": f"Basic {token}"})
        api = data["apiInfo"]["storageApi"]
        self.token = data["authorizationToken"]
        self.api_url = api["apiUrl"]
        self.recommended_part_size = api["recommendedPartSize"]
        self.minimum_part_size = api["absoluteMinimumPartSize"]
        self.name_prefix = api.get("namePrefix") or ""
        if api.get("bucketName") and api["bucketName"] != BUCKET_NAME:
            sys.exit(f"This key is for bucket {api['bucketName']}, not {BUCKET_NAME}.")
        self.bucket_id = api.get("bucketId") or self._find_bucket(data["accountId"])
        self.local = threading.local()

    def call(self, name, payload):
        return http_json(
            f"{self.api_url}/b2api/v3/{name}",
            payload=payload,
            headers={"Authorization": self.token, "Content-Type": "application/json"},
        )

    def _find_bucket(self, account_id):
        buckets = self.call("b2_list_buckets", {"accountId": account_id, "bucketName": BUCKET_NAME})["buckets"]
        if not buckets:
            sys.exit(f"Bucket {BUCKET_NAME} not found.")
        return buckets[0]["bucketId"]

    def find_file(self, name):
        files = self.call("b2_list_file_names", {"bucketId": self.bucket_id, "startFileName": name, "maxFileCount": 1})["files"]
        return files[0] if files and files[0]["fileName"] == name else None

    def list_versions(self, prefix):
        payload = {"bucketId": self.bucket_id, "prefix": prefix, "maxFileCount": 1000}
        while True:
            result = self.call("b2_list_file_versions", payload)
            yield from result["files"]
            if not result.get("nextFileName"):
                return
            payload["startFileName"] = result["nextFileName"]
            payload["startFileId"] = result.get("nextFileId")

    def delete_version(self, name, file_id):
        self.call("b2_delete_file_version", {"fileName": name, "fileId": file_id})

    def cancel_large_file(self, file_id):
        self.call("b2_cancel_large_file", {"fileId": file_id})

    def upload_small(self, path, name, sha1, info):
        target = self.call("b2_get_upload_url", {"bucketId": self.bucket_id})
        headers = {
            "Authorization": target["authorizationToken"],
            "X-Bz-File-Name": urllib.parse.quote(name, safe="/"),
            "Content-Type": "application/zip",
            "X-Bz-Content-Sha1": sha1,
        }
        for key, value in info.items():
            headers[f"X-Bz-Info-{key}"] = urllib.parse.quote(value, safe="")
        http_json(target["uploadUrl"], data=Path(path).read_bytes(), headers=headers, method="POST")

    def find_unfinished(self, name, sha1):
        payload = {"bucketId": self.bucket_id, "namePrefix": name, "maxFileCount": 100}
        for item in self.call("b2_list_unfinished_large_files", payload)["files"]:
            if item["fileName"] == name and item.get("fileInfo", {}).get("large_file_sha1") == sha1:
                return item["fileId"]
        return None

    def list_parts(self, file_id):
        parts = {}
        payload = {"fileId": file_id, "maxPartCount": 1000}
        while True:
            result = self.call("b2_list_parts", payload)
            for part in result["parts"]:
                parts[part["partNumber"]] = part["contentSha1"]
            if not result.get("nextPartNumber"):
                return parts
            payload["startPartNumber"] = result["nextPartNumber"]

    def _part_target(self, file_id, renew=False):
        if renew or getattr(self.local, "file_id", None) != file_id:
            target = self.call("b2_get_upload_part_url", {"fileId": file_id})
            self.local.file_id = file_id
            self.local.target = target
        return self.local.target

    def upload_part(self, file_id, number, data, sha1):
        for attempt in range(6):
            target = self._part_target(file_id, renew=attempt > 0)
            try:
                http_json(
                    target["uploadUrl"],
                    data=data,
                    headers={
                        "Authorization": target["authorizationToken"],
                        "X-Bz-Part-Number": str(number),
                        "X-Bz-Content-Sha1": sha1,
                    },
                    method="POST",
                )
                return
            except (B2Error, urllib.error.URLError, OSError) as error:
                status = getattr(error, "status", None)
                if attempt == 5 or (status is not None and status not in RETRY_STATUSES):
                    raise
                log(f"\n  part {number}: {error}; retry {attempt + 1}/5")
                time.sleep(min(2 ** attempt, 30))

    def upload_large(self, path, name, size, sha1, info, part_size, threads):
        file_id = self.find_unfinished(name, sha1)
        done_parts = {}
        if file_id:
            done_parts = self.list_parts(file_id)
            log(f"  resuming unfinished upload: {len(done_parts)} parts already uploaded")
        else:
            file_info = dict(info, large_file_sha1=sha1)
            file_id = self.call(
                "b2_start_large_file",
                {"bucketId": self.bucket_id, "fileName": name, "contentType": "application/zip", "fileInfo": file_info},
            )["fileId"]

        count = (size + part_size - 1) // part_size
        sha1s = [None] * count
        sent = {"bytes": 0}
        lock = threading.Lock()

        def send(number):
            offset = (number - 1) * part_size
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(part_size)
            digest = hashlib.sha1(data).hexdigest()
            if done_parts.get(number) != digest:
                self.upload_part(file_id, number, data, digest)
            sha1s[number - 1] = digest
            with lock:
                sent["bytes"] += len(data)
                progress("upload", sent["bytes"], size)

        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
            for future in concurrent.futures.as_completed([pool.submit(send, n) for n in range(1, count + 1)]):
                future.result()
        self.call("b2_finish_large_file", {"fileId": file_id, "partSha1Array": sha1s})


def parse_zip_name(path):
    match = FILENAME_PATTERN.match(Path(path).name)
    if not match:
        sys.exit(f"{Path(path).name}: name must look like CloverProject-<version>-<yyyymmdd[_hhmmss]>-OFFICIAL-<codename>-<GMS|VANILLA>.zip")
    return match.groupdict()


def read_ota_metadata(path):
    with zipfile.ZipFile(path) as archive:
        try:
            text = archive.read("META-INF/com/android/metadata").decode()
        except KeyError:
            return {}
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def optional_metadata(metadata):
    values = {}
    sdk = metadata.get("post-sdk-level", "").strip()
    if sdk.isdigit():
        values["os_sdk_level"] = int(sdk)
    patch = metadata.get("post-security-patch-level", "").strip()
    if patch:
        values["os_patch_level"] = patch
    files = metadata.get("ota-property-files", "").strip()
    if files:
        values["ota_property_files"] = files
    return values


def hash_file(path, size):
    md5, sha1, sha256 = hashlib.md5(), hashlib.sha1(), hashlib.sha256()
    done = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            md5.update(chunk)
            sha1.update(chunk)
            sha256.update(chunk)
            done += len(chunk)
            progress("hash", done, size)
    return md5.hexdigest(), sha1.hexdigest(), sha256.hexdigest()


def remote_sha1(item):
    if not item:
        return None
    return item.get("fileInfo", {}).get("large_file_sha1") or item.get("contentSha1")


def verify_public(url, size):
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                length = int(response.headers.get("Content-Length", "-1"))
                if length == size:
                    return True
                log(f"  public URL size is {length}, expected {size}")
                return False
        except urllib.error.URLError as error:
            if attempt == 4:
                log(f"  public URL check failed: {error}")
                return False
            time.sleep(3)
    return False


def device_details(repo, codename, device, variant, args):
    info_path = repo / "site" / "devices" / f"{codename}.json"
    info = json.loads(info_path.read_text()) if info_path.is_file() else {}
    current = {}
    for name in (variant, "gms", "vanilla"):
        path = repo / "devices" / name / f"{device}.json"
        if path.is_file():
            current = (json.loads(path.read_text()).get("response") or [{}])[0]
            break
    devicename = args.devicename or current.get("devicename") or info.get("name")
    maintainer = args.maintainer or current.get("maintainer") or (info.get("maintainer") or {}).get("name")
    if not devicename or not maintainer:
        sys.exit(f"No device name or maintainer for {codename}. Add site/devices/{codename}.json or use --devicename and --maintainer.")
    return devicename, maintainer


def prune(b2, codename, keep_versions, current_versions, dry_run):
    folders = {}
    for item in b2.list_versions(f"{codename}/"):
        parts = item["fileName"].split("/")
        if len(parts) < 3:
            continue
        folder = folders.setdefault(parts[1], {"latest": 0, "files": []})
        if item["action"] == "upload":
            folder["latest"] = max(folder["latest"], item["uploadTimestamp"])
        folder["files"].append(item)
    others = sorted(
        (version for version in folders if version not in current_versions),
        key=lambda version: folders[version]["latest"],
        reverse=True,
    )
    keep = set(current_versions) | set(others[:max(keep_versions - len(current_versions), 0)])
    for version in others:
        if version in keep:
            continue
        for item in folders[version]["files"]:
            log(f"  {'would delete' if dry_run else 'delete'} {item['fileName']}")
            if dry_run:
                continue
            if item["action"] == "start":
                b2.cancel_large_file(item["fileId"])
            else:
                b2.delete_version(item["fileName"], item["fileId"])


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Upload Clover Project OTA zips to Backblaze B2 (served at "
            f"{PUBLIC_BASE_URL}) and write devices/<variant>/<codename>.json."
        ),
        epilog=(
            "Files are stored as <codename>/<version>/<zip name>. "
            "B2 key: B2_KEY_ID and B2_APP_KEY environment variables, or the --env-file file."
        ),
    )
    parser.add_argument("zips", nargs="+", help="OTA zip files (GMS and/or Vanilla)")
    parser.add_argument("--version", help="version for the folder and the JSON; it must be the same as the version in the zip name, for example 4.0")
    parser.add_argument("--devicename", help="device name for the JSON (default: site/devices/<codename>.json)")
    parser.add_argument("--maintainer", help="maintainer for the JSON (default: current JSON or site/devices/<codename>.json)")
    parser.add_argument("--repo", default=Path(__file__).resolve().parents[1], type=Path, help="official_updates checkout (default: this repo)")
    parser.add_argument("--keep", type=int, default=2, help="release folders to keep per device in B2; older ones are deleted (default: 2, 0 = do not delete)")
    parser.add_argument("--threads", type=int, default=4, help="parallel part uploads (default: 4)")
    parser.add_argument("--part-size", type=int, help="part size in MB (default: B2 recommended size)")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE, help=f"file with the B2 key (default: {DEFAULT_ENV_FILE})")
    parser.add_argument("--dry-run", action="store_true", help="hash and show the plan, but do not upload, delete or write files")
    args = parser.parse_args()

    releases = []
    for zip_path in args.zips:
        path = Path(zip_path).resolve()
        if not path.is_file():
            sys.exit(f"{zip_path}: file not found")
        parsed = parse_zip_name(path)
        device = parsed["codename"]
        codename = device.lower()
        variant = parsed["variant"].lower()
        metadata = read_ota_metadata(path)
        if not metadata.get("post-timestamp"):
            sys.exit(f"{path.name}: META-INF/com/android/metadata has no post-timestamp")
        devices = {device.lower() for device in metadata.get("pre-device", codename).split(",")}
        if codename not in devices:
            sys.exit(f"{path.name}: zip is for {metadata.get('pre-device')}, not {codename}")
        version = args.version or parsed["version"]
        if version != parsed["version"]:
            sys.exit(f"{path.name}: --version {version} is not the version in the zip name ({parsed['version']})")
        devicename, maintainer = device_details(args.repo, codename, device, variant, args)
        releases.append({
            "path": path,
            "codename": codename,
            "device": device,
            "variant": variant,
            "version": version,
            "romtype": parsed["romtype"].lower(),
            "datetime": int(metadata["post-timestamp"]),
            "metadata": metadata,
            "devicename": devicename,
            "maintainer": maintainer,
            "remote": f"{codename}/{version}/{path.name}",
        })

    b2 = B2(*load_credentials(args.env_file))
    part_size = (args.part_size * 1024 * 1024) if args.part_size else b2.recommended_part_size
    part_size = max(part_size, b2.minimum_part_size)

    for release in releases:
        path = release["path"]
        size = path.stat().st_size
        url = f"{PUBLIC_BASE_URL}/{urllib.parse.quote(release['remote'], safe='/')}"
        log(f"{path.name} -> {release['remote']}")
        if b2.name_prefix and not release["remote"].startswith(b2.name_prefix):
            sys.exit(f"  this key can only write under {b2.name_prefix}")
        md5, sha1, sha256 = hash_file(path, size)
        info = {
            "b2-content-disposition": f'attachment; filename="{path.name}"',
            "b2-cache-control": "public, max-age=31536000, immutable",
        }
        if args.dry_run:
            log("  dry run: no upload")
        elif remote_sha1(b2.find_file(release["remote"])) == sha1:
            log("  already in B2 with the same SHA-1, no upload")
        elif size <= part_size:
            b2.upload_small(path, release["remote"], sha1, info)
            log("  uploaded")
        else:
            b2.upload_large(path, release["remote"], size, sha1, info, part_size, args.threads)
            log("  uploaded")
        if not args.dry_run and not verify_public(url, size):
            sys.exit(f"  {url} is not correct, JSON not written")
        entry = {
            "datetime": release["datetime"],
            "filename": path.name,
            "id": sha256,
            "md5": md5,
            "size": size,
            "url": url,
            "version": release["version"],
            "romtype": release["romtype"],
            "devicename": release["devicename"],
            "maintainer": release["maintainer"],
        }
        entry.update(optional_metadata(release["metadata"]))
        output = args.repo / "devices" / release["variant"] / f"{release['device']}.json"
        if args.dry_run:
            log(f"  dry run: would write {output}")
            log(json.dumps(entry, indent=2))
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"response": [entry]}, indent=2, ensure_ascii=False) + "\n")
            log(f"  wrote {output.relative_to(args.repo) if output.is_relative_to(args.repo) else output}")

    if args.keep > 0:
        for codename in sorted({release["codename"] for release in releases}):
            current = {release["version"] for release in releases if release["codename"] == codename}
            log(f"cleanup {codename}/: keep {args.keep} release folder(s), including {', '.join(sorted(current))}")
            prune(b2, codename, args.keep, current, args.dry_run)

    if not args.dry_run:
        changed = sorted({f"devices/{r['variant']}/{r['device']}.json" for r in releases})
        log(f"\nNext: update changelogs/changelog_{releases[0]['device']}.txt, then commit and push to branch 17:")
        log(f"  git add {' '.join(changed)} changelogs/")
        log(f"  git commit -m \"{releases[0]['device']}: {releases[0]['version']}\"")
        log("  git push origin 17")


if __name__ == "__main__":
    main()
