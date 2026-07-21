#!/usr/bin/env bash
set -euo pipefail

icons_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tauri_cli="$icons_dir/../../node_modules/.bin/tauri"
repo_root="$(cd "$icons_dir/../../.." && pwd)"
source_icon="$repo_root/assets/openevo-favicon.svg"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

if (( $# > 1 )) || { (( $# == 1 )) && [[ $1 != "--check" ]]; }; then
  echo "Usage: $0 [--check]" >&2
  exit 2
fi

if [[ ! -x "$tauri_cli" ]]; then
  echo "Tauri CLI not found at $tauri_cli" >&2
  exit 1
fi

"$tauri_cli" icon "$source_icon" --output "$tmp_dir" --png 32,64,128,256,512,1024

python3 - "$tmp_dir" <<'PY'
from pathlib import Path
import struct
import sys

png_dir = Path(sys.argv[1])
output = png_dir / "icon.icns"
entries = (
    (b"ic11", "32x32.png"),
    (b"ic12", "64x64.png"),
    (b"ic07", "128x128.png"),
    (b"ic13", "256x256.png"),
    (b"ic08", "256x256.png"),
    (b"ic14", "512x512.png"),
    (b"ic09", "512x512.png"),
    (b"ic10", "1024x1024.png"),
)

chunks = []
for kind, name in entries:
    payload = (png_dir / name).read_bytes()
    chunks.append(kind + struct.pack(">I", len(payload) + 8) + payload)

body = b"".join(chunks)
output.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)
PY

expected_outputs=(
  "32x32.png:32x32.png"
  "128x128.png:128x128.png"
  "128x128@2x.png:256x256.png"
  "256x256.png:256x256.png"
  "512x512.png:512x512.png"
  "1024x1024.png:1024x1024.png"
  "icon.png:1024x1024.png"
  "icon.icns:icon.icns"
)

if [[ ${1:-} == "--check" ]]; then
  for output in "${expected_outputs[@]}"; do
    output_name="${output%%:*}"
    source_name="${output#*:}"
    if ! cmp -s "$tmp_dir/$source_name" "$icons_dir/$output_name"; then
      echo "Stale icon output: $icons_dir/$output_name" >&2
      exit 1
    fi
  done
else
  for output in "${expected_outputs[@]}"; do
    output_name="${output%%:*}"
    source_name="${output#*:}"
    install -m 0644 "$tmp_dir/$source_name" "$icons_dir/$output_name"
  done
fi

python3 - "$icons_dir" <<'PY'
from pathlib import Path
import struct
import sys

icons_dir = Path(sys.argv[1])
png_sizes = {
    "32x32.png": 32,
    "128x128.png": 128,
    "128x128@2x.png": 256,
    "256x256.png": 256,
    "512x512.png": 512,
    "1024x1024.png": 1024,
    "icon.png": 1024,
}

for name, expected_size in png_sizes.items():
    data = (icons_dir / name).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"{name} is not a PNG")
    length = struct.unpack(">I", data[8:12])[0]
    if length != 13 or data[12:16] != b"IHDR":
        raise SystemExit(f"{name} has no valid IHDR")
    width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
        ">IIBBBBB", data[16:29]
    )
    if (width, height, bit_depth, color_type, compression, filter_method, interlace) != (
        expected_size,
        expected_size,
        8,
        6,
        0,
        0,
        0,
    ):
        raise SystemExit(f"{name} has unexpected PNG metadata")

data = (icons_dir / "icon.icns").read_bytes()
if len(data) < 8 or data[:4] != b"icns" or struct.unpack(">I", data[4:8])[0] != len(data):
    raise SystemExit("icon.icns has an invalid header")

expected_kinds = (b"ic11", b"ic12", b"ic07", b"ic13", b"ic08", b"ic14", b"ic09", b"ic10")
offset = 8
kinds = []
while offset < len(data):
    if offset + 8 > len(data):
        raise SystemExit("icon.icns has a truncated chunk header")
    kind = data[offset : offset + 4]
    chunk_length = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
    if chunk_length < 8 or offset + chunk_length > len(data):
        raise SystemExit("icon.icns has an invalid chunk length")
    payload = data[offset + 8 : offset + chunk_length]
    if payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"icon.icns chunk {kind.decode('ascii')} is not PNG data")
    kinds.append(kind)
    offset += chunk_length

if offset != len(data) or tuple(kinds) != expected_kinds:
    raise SystemExit("icon.icns has an unexpected chunk inventory")
PY
