import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
VARIANTS = ("gms", "vanilla")


def load(path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def find_file(folder, name):
    for path in folder.iterdir():
        if path.name.lower() == name.lower():
            return path.name
    return None


def latest_build(variant, codename):
    path = ROOT / "devices" / variant / f"{codename}.json"
    if not path.is_file():
        return None
    response = load(path).get("response") or []
    return response[0] if response else None


def relative_image(path):
    return path.relative_to(SITE).as_posix() if path.is_file() else None


def build_entry(codename, info):
    builds = {variant: latest_build(variant, codename) for variant in VARIANTS}
    builds = {variant: build for variant, build in builds.items() if build}
    first = next(iter(builds.values()), {})
    maintainer = info.get("maintainer")
    if not maintainer and first.get("maintainer"):
        maintainer = {
            "name": first["maintainer"],
            "githubUrl": f"https://github.com/{first['maintainer']}",
        }
    return {
        "codename": codename,
        "name": info.get("name") or first.get("devicename") or codename,
        "brand": info.get("brand") or "Other",
        "maintainer": maintainer or {"name": "Unknown", "githubUrl": ""},
        "image": relative_image(SITE / "images" / "devices" / f"{codename}.png"),
        "heroImage": relative_image(SITE / "images" / "devices" / "download" / f"{codename}.png"),
        "released": bool(builds),
        "version": first.get("version"),
        "datetime": first.get("datetime"),
        "variants": list(builds),
        "changelog": find_file(ROOT / "changelogs", f"changelog_{codename}.txt"),
    }


def main():
    infos = {path.stem.lower(): load(path) for path in (SITE / "devices").glob("*.json")}
    released = {
        path.stem.lower()
        for variant in VARIANTS
        for path in (ROOT / "devices" / variant).glob("*.json")
    }
    devices = [build_entry(codename, infos.get(codename, {})) for codename in sorted(set(infos) | released)]
    devices.sort(key=lambda device: (not device["released"], device["name"].lower()))
    output = SITE / "devices.json"
    output.write_text(json.dumps(devices, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{output.relative_to(ROOT)}: {len(devices)} devices, {sum(d['released'] for d in devices)} released")


if __name__ == "__main__":
    main()
