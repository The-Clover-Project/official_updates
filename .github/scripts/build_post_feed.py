import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
OUTPUT = ROOT / ".post"
VARIANTS = {"gms": "GMS", "vanilla": "Vanilla"}


def load(path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def latest_build(variant, codename):
    path = ROOT / "devices" / variant / f"{codename}.json"
    if not path.is_file():
        return None
    response = load(path).get("response") or []
    return response[0] if response else None


def normalize(url):
    return (url or "").strip().rstrip("/").lower()


def main():
    team = load(SITE / "team.json")
    team_by_github = {normalize(member.get("github")): member for member in team if member.get("github")}
    team_by_name = {member["name"].lower(): member for member in team if member.get("name")}

    codenames = sorted({path.stem.lower() for variant in VARIANTS for path in (ROOT / "devices" / variant).glob("*.json")})
    shutil.rmtree(OUTPUT, ignore_errors=True)
    OUTPUT.mkdir()

    for codename in codenames:
        builds = {variant: latest_build(variant, codename) for variant in VARIANTS}
        builds = {variant: build for variant, build in builds.items() if build}
        if not builds:
            continue
        entry = dict(next(iter(builds.values())))
        info_path = SITE / "devices" / f"{codename}.json"
        info = load(info_path) if info_path.is_file() else {}
        maintainer = info.get("maintainer") or {}
        member = team_by_github.get(normalize(maintainer.get("githubUrl"))) or team_by_name.get(
            (maintainer.get("name") or entry.get("maintainer") or "").lower()
        )
        entry["devicename"] = info.get("name") or entry.get("devicename") or codename
        entry["maintainer"] = maintainer.get("name") or entry.get("maintainer") or "Unknown"
        entry["telegram"] = (member or {}).get("telegram", "")
        entry["variants"] = " & ".join(VARIANTS[variant] for variant in builds)
        (OUTPUT / f"{codename}.json").write_text(json.dumps({"response": [entry]}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"{OUTPUT.relative_to(ROOT)}: {len(list(OUTPUT.glob('*.json')))} devices")


if __name__ == "__main__":
    main()
