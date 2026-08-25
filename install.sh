#!/bin/sh
# OpenEvo online installer bootstrap
set -eu
umask 077

ARCHIVE_NAME=openevo-launcher.tar.gz
CHECKSUM_NAME="$ARCHIVE_NAME.sha256"
DEFAULT_REPOSITORY=KDBRonaldo/OpenEvo

usage() {
  cat <<'EOF'
Usage: sh install.sh [--version TAG] [--prefix ABSOLUTE_PATH]

Downloads a verified OpenEvo launcher release and installs it for the current
user. The newest published release is used unless --version is supplied.

Environment overrides:
  OPENEVO_GITHUB_REPOSITORY   GitHub owner/repository
  OPENEVO_RELEASE_BASE_URL    Complete releases base URL (useful for mirrors)
EOF
}

version=latest
prefix=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --version)
      if [ "$#" -lt 2 ]; then
        echo "--version requires a value" >&2
        exit 2
      fi
      version=$2
      shift 2
      ;;
    --prefix)
      if [ "$#" -lt 2 ]; then
        echo "--prefix requires a value" >&2
        exit 2
      fi
      prefix=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown installer argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$version" in
  latest) ;;
  ''|*[!A-Za-z0-9._+-]*)
    echo "release tag contains unsupported characters: $version" >&2
    exit 2
    ;;
esac

for command_name in curl tar python3 ssh; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "$command_name is required to install OpenEvo." >&2
    exit 10
  fi
done
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Python 3.11 or newer is required to install OpenEvo." >&2
  exit 10
fi

repository=${OPENEVO_GITHUB_REPOSITORY:-$DEFAULT_REPOSITORY}
case "$repository" in
  */*) ;;
  *)
    echo "OPENEVO_GITHUB_REPOSITORY must be in owner/repository form." >&2
    exit 2
    ;;
esac
base_url=${OPENEVO_RELEASE_BASE_URL:-"https://github.com/$repository/releases"}
if [ "$version" = latest ]; then
  download_root="$base_url/latest/download"
else
  download_root="$base_url/download/$version"
fi

temporary_root=$(mktemp -d "${TMPDIR:-/tmp}/openevo-online-install.XXXXXX")
cleanup() {
  rm -rf -- "$temporary_root"
}
trap cleanup EXIT HUP INT TERM

archive_path="$temporary_root/$ARCHIVE_NAME"
checksum_path="$temporary_root/$CHECKSUM_NAME"
curl_flags='--fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 300'

echo "Downloading OpenEvo launcher from $download_root ..."
# shellcheck disable=SC2086
curl $curl_flags --output "$archive_path" "$download_root/$ARCHIVE_NAME"
# shellcheck disable=SC2086
curl $curl_flags --output "$checksum_path" "$download_root/$CHECKSUM_NAME"

if ! expected=$(python3 - "$checksum_path" "$ARCHIVE_NAME" <<'PY'
from pathlib import Path
import re
import sys

try:
    lines = Path(sys.argv[1]).read_text(encoding="ascii").splitlines()
except (OSError, UnicodeError):
    raise SystemExit(1)
if len(lines) != 1:
    raise SystemExit(1)
parts = lines[0].split()
if (
    len(parts) != 2
    or re.fullmatch(r"[0-9a-f]{64}", parts[0]) is None
    or parts[1].removeprefix("*") != sys.argv[2]
):
    raise SystemExit(1)
print(parts[0])
PY
); then
  echo "OpenEvo release checksum is malformed." >&2
  exit 20
fi

actual=$(python3 - "$archive_path" <<'PY'
import hashlib
from pathlib import Path
import sys

digest = hashlib.sha256()
with Path(sys.argv[1]).open("rb") as source:
    while chunk := source.read(1024 * 1024):
        digest.update(chunk)
print(digest.hexdigest())
PY
)
if [ "$actual" != "$expected" ]; then
  echo "OpenEvo launcher checksum verification failed." >&2
  exit 21
fi

tar -xzf "$archive_path" -C "$temporary_root"
package_installer="$temporary_root/openevo-launcher/install.sh"
if [ ! -f "$package_installer" ] || [ -L "$package_installer" ]; then
  echo "OpenEvo launcher archive does not contain a safe installer." >&2
  exit 22
fi

if [ -n "$prefix" ]; then
  sh "$package_installer" --prefix "$prefix"
  command_path="$prefix/bin/openevo"
else
  sh "$package_installer"
  command_path="$HOME/.local/bin/openevo"
fi

echo "OpenEvo is installed. Run $command_path webui to start it."
