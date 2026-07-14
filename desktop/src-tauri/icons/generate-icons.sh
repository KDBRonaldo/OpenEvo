#!/usr/bin/env bash
set -euo pipefail

icons_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tauri_cli="$icons_dir/../../node_modules/.bin/tauri"
source_icon="$icons_dir/openevo-icon.svg"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

if [[ ! -x "$tauri_cli" ]]; then
  echo "Tauri CLI not found at $tauri_cli" >&2
  exit 1
fi

"$tauri_cli" icon "$source_icon" --output "$tmp_dir" --png 32,64,128,256,512,1024

for size in 32 128 256 512 1024; do
  install -m 0644 "$tmp_dir/${size}x${size}.png" "$icons_dir/${size}x${size}.png"
done
install -m 0644 "$tmp_dir/256x256.png" "$icons_dir/128x128@2x.png"
install -m 0644 "$tmp_dir/1024x1024.png" "$icons_dir/icon.png"

python3 - "$tmp_dir" "$icons_dir/icon.icns" <<'PY'
from pathlib import Path
import struct
import sys

png_dir = Path(sys.argv[1])
output = Path(sys.argv[2])
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
