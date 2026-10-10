# Release template

One pull request can release GMS, Vanilla, or both.

1. Copy `devices/` and `changelogs/` from this folder.
2. Rename `codename` in each file name to your device codename, for example `peridot.json` and `changelog_peridot.txt`.
3. Fill the files. Delete the variant that you do not release.
4. On GitHub, open **Add file → Upload files** at the repo root of branch `17`. Drop the `devices` and `changelogs` folders together.
5. Select **Create a new branch and start a pull request**.
6. The check adds `format-ok` or `format-bad`. An admin merges.

| Key | Value |
| --- | --- |
| `datetime` | `post-timestamp` from `META-INF/com/android/metadata` in the zip |
| `filename` | `CloverProject-<version>-<yyyymmdd>-OFFICIAL-<codename>-<GMS\|VANILLA>.zip` |
| `id` | SHA-256 of the zip |
| `md5` | MD5 of the zip |
| `size` | Zip size in bytes |
| `url` | `https://ota.thecloverproject.com/<codename>/<version>/<filename>` |
| `version` | For example `4.0` |
| `romtype` | `official` |
| `devicename` | `name` in `site/devices/<codename>.json` |
| `maintainer` | `maintainer.name` in `site/devices/<codename>.json` |

Cloverbot gives you filled JSON files after the upload. You can use them in place of the blank files.
