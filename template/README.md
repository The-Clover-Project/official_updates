# Release template

One pull request can release GMS, Vanilla, or both.

1. Copy `devices/` and `changelogs/` from this folder.
2. Rename `codename` in each file name to your device codename, for example `peridot.json` and `changelog_peridot.txt`. Use the codename exactly as it is in the zip name and in `ro.clover.device`, with the same upper and lower case letters, for example `Pong.json` and `changelog_Pong.txt`.
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
| `url` | `https://ota.thecloverproject.com/<codename in lower case>/<version>/<filename>` |
| `version` | The version in `filename`, for example `4.0` |
| `romtype` | `official` |
| `devicename` | `name` in `site/devices/<codename in lower case>.json` |
| `maintainer` | `maintainer.name` in `site/devices/<codename in lower case>.json` |
| `os_sdk_level` | Optional. `post-sdk-level` from the metadata, as a number |
| `os_patch_level` | Optional. `post-security-patch-level` from the metadata |
| `ota_property_files` | Optional. `ota-property-files` from the metadata. The app needs it to stream the update |

## Clean flash

`ota/compatibility.json` tells the Updater app which versions need a clean flash.
The app blocks the OTA when a version in `ver_clean_required` is newer than the
installed version and not newer than the new build.

If your release needs a clean flash on your device only, add its version to
`devices.<codename>.ver_clean_required` in the same pull request:

```json
"devices": {
  "peridot": {
    "ver_clean_required": ["4.1"]
  }
}
```

You can only add versions for the device that you release. Only an admin can
change `ver_clean_required`, `ver_dirty_allowed` or remove a version. If your
version is in no list, the check shows a warning and the app allows a dirty flash.

Cloverbot gives you filled JSON files after the upload. You can use them in place of the blank files.
