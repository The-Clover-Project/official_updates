#!/bin/bash
#
# Copyright (C) 2024 The Clover Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# Usage: Update the user-defined variables with appropriate values and execute the script using the command:
#        bash createjson.sh

# -----------------------------
# User-defined Values
# -----------------------------
codename=""       # e.g., miatoll
devicename=""     # e.g., POCO M2 Pro / Redmi Note 9S / Redmi Note 9 Pro / Redmi Note 9 Pro Max / Redmi Note 10 Lite
maintainer=""     # e.g., RiteshSahany
zip=""            # e.g., CloverProject-v2.0-miatoll-OFFICIAL-20241110-1248.zip

# -----------------------------
# Auto-generated Values
# -----------------------------
script_path="${PWD}/.."
zip_name="${script_path}/out/target/product/${codename}/${zip}"
buildprop="${script_path}/out/target/product/${codename}/system/build.prop"
output_json="${script_path}/official_updates/devices/${codename}.json"
device_folder="${script_path}/official_updates/devices"

# Create device folder if it doesn't exist
if [ ! -d "$device_folder" ]; then
  mkdir -p "$device_folder"
fi

# Clean up existing JSON file if it exists
if [ -f "$output_json" ]; then
  rm "$output_json"
fi

# Function to display progress bar
show_progress() {
  local duration=$1
  local interval=0.1
  local completed=0
  local total_ticks=$(echo "$duration / $interval" | bc)

  while [ $completed -le $total_ticks ]; do
    local progress=$(echo "$completed * 100 / $total_ticks" | bc)
    printf "\rProgress: [%-50s] %d%%" $(printf '%0.s=' $(seq 1 $(($progress / 2)))) $progress
    sleep $interval
    completed=$(($completed + 1))
  done
  printf "\n"
}

# Extract values from build.prop and calculate file properties
linenr=$(grep -n "ro.system.build.date.utc" "$buildprop" | cut -d':' -f1)
timestamp=$(sed -n "${linenr}p" < "$buildprop" | cut -d'=' -f2)
zip_only=$(basename "$zip_name")
sha256=$(sha256sum "$zip_name" | cut -d' ' -f1)
size=$(stat -c "%s" "$zip_name")
version=$(echo "$zip_only" | cut -d'-' -f2)

# Simulate processing time for the progress bar (e.g., 5 seconds)
show_progress 5

# Generate JSON content
echo "done."
cat <<EOF >>"$output_json"
{
  "response": [
    {
      "datetime": $timestamp,
      "filename": "$zip_only",
      "id": "$sha256",
      "size": $size,
      "url": "https://dl.genesisos.dev/0:/$codename/$zip_only",
      "version": "$version",
      "devicename": "$devicename",
      "maintainer": "$maintainer"
    }
  ]
}
EOF