"""Build and verify the repository-free EvoLab launcher distribution."""

from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile
from typing import Any
import zipfile

from openevo.release_bundle import (
    BUNDLE_SUFFIX,
    COMMIT_PATTERN,
    RELEASE_ID_PATTERN,
    VERSION_PATTERN,
    ReleaseBundleError,
    build_release_bundle,
)


LAUNCHER_DISTRIBUTION_SCHEMA_VERSION = "1"
LAUNCHER_DISTRIBUTION_ROOT = "evolab-launcher"
LAUNCHER_DISTRIBUTION_SUFFIXES = (".tar.gz", ".zip")
MAX_DISTRIBUTION_FILE_BYTES = 512 * 1024 * 1024
MAX_DISTRIBUTION_BYTES = 600 * 1024 * 1024
REQUIRED_DISTRIBUTION_FILES = frozenset(
    {
        "LICENSE",
        "README.txt",
        "install.ps1",
        "install.sh",
        "openevo.pyz",
        f"openevo-server{BUNDLE_SUFFIX}",
    }
)
LAUNCHER_SOURCE_PATHS = (
    "src/openevo/__init__.py",
    "src/openevo/cli.py",
    "src/openevo/launcher.py",
    "src/openevo/release_bundle.py",
)
MANAGED_WRAPPER_MARKER = "# managed by EvoLab launcher installer"
MANAGED_WINDOWS_WRAPPER_MARKER = "REM managed by EvoLab launcher installer"


class LauncherDistributionError(RuntimeError):
    """The ordinary-user launcher distribution is invalid or incomplete."""


@dataclass(frozen=True)
class LauncherDistributionReceipt:
    path: Path
    distribution_id: str
    product_version: str
    source_commit: str
    server_release_id: str
    sha256: str
    byte_size: int
    file_count: int


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repository_root: Path, *arguments: str, text: bool = True) -> str | bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=text,
        timeout=120,
    )
    if completed.returncode != 0:
        stderr = completed.stderr
        detail = stderr.strip() if isinstance(stderr, str) else stderr.decode(errors="replace").strip()
        raise LauncherDistributionError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout


def _git_file(repository_root: Path, commit: str, path: str) -> bytes:
    value = _git(repository_root, "show", f"{commit}:{path}", text=False)
    assert isinstance(value, bytes)
    return value


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (0o100644 & 0xFFFF) << 16
    return info


def _build_launcher_zipapp(repository_root: Path, commit: str) -> bytes:
    output = io.BytesIO()
    output.write(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(output, "a", allowZip64=False) as archive:
        archive.writestr(
            _zip_info("__main__.py"),
            b"from openevo.cli import main\n\nraise SystemExit(main())\n",
        )
        for source_path in LAUNCHER_SOURCE_PATHS:
            archive_path = source_path.removeprefix("src/")
            archive.writestr(
                _zip_info(archive_path),
                _git_file(repository_root, commit, source_path),
            )
    return output.getvalue()


def _installer_readme(version: str) -> bytes:
    return f"""EvoLab launcher {version}

Requirements on this computer:
- Python 3.11 or newer
- system OpenSSH client
- a configured ~/.ssh/config host

Linux, macOS, or WSL installation:
  sh install.sh

Choose another per-user prefix:
  sh install.sh --prefix /absolute/path

Windows PowerShell installation:
  powershell -ExecutionPolicy Bypass -File .\\install.ps1

After installation:
  evolab webui

The launcher contains the matching server Release Bundle. Git, uv, npm, and
the EvoLab repository are not required on this computer. The legacy
`openevo webui` command remains available for compatibility.
""".encode("utf-8")


INSTALLER_SCRIPT = r'''#!/bin/sh
# EvoLab repository-free per-user installer
set -eu
umask 077

usage() {
  cat <<'EOF'
Usage: sh install.sh [--prefix ABSOLUTE_PATH]

Installs the versioned EvoLab launcher under ~/.local by default.
EOF
}

prefix=${OPENEVO_INSTALL_PREFIX:-"$HOME/.local"}
while [ "$#" -gt 0 ]; do
  case "$1" in
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

case "$prefix" in
  /*) ;;
  *) echo "installation prefix must be an absolute path" >&2; exit 2 ;;
esac
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.11 or newer is required on this computer." >&2
  exit 10
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Python 3.11 or newer is required on this computer." >&2
  exit 10
fi
if ! command -v ssh >/dev/null 2>&1; then
  echo "The system OpenSSH client is required on this computer." >&2
  exit 11
fi

package_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
python3 - "$package_root" "$prefix" <<'PY'
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile

SCHEMA_VERSION = "1"
MAX_FILE_BYTES = 536870912
MAX_TOTAL_BYTES = 629145600
HEX64 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40,64}")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
REQUIRED = {
    "LICENSE",
    "README.txt",
    "install.ps1",
    "install.sh",
    "openevo.pyz",
    "openevo-server.oevobundle",
}
WRAPPER_MARKER = "# managed by EvoLab launcher installer"

def report_error(error_type, error, traceback):
    if issubclass(error_type, KeyboardInterrupt):
        sys.__excepthook__(error_type, error, traceback)
        return
    print(f"EvoLab installation failed: {error}", file=sys.stderr)

sys.excepthook = report_error

package_root = Path(sys.argv[1])
prefix = Path(sys.argv[2])

def canonical(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()

def safe_name(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise RuntimeError("launcher manifest contains an invalid path")
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) != 1 or path.as_posix() != value:
        raise RuntimeError("launcher manifest contains an unsafe path")
    return value

def digest_file(path, expected_size):
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"launcher file is missing or unsafe: {path.name}")
    if path.stat().st_size != expected_size:
        raise RuntimeError(f"launcher file size changed: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1048576):
            digest.update(chunk)
    return digest.hexdigest()

def load_and_verify(root):
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RuntimeError("launcher manifest is missing or unsafe")
    raw_manifest = manifest_path.read_bytes()
    manifest = json.loads(raw_manifest)
    required_keys = {
        "schema_version",
        "distribution_id",
        "product_version",
        "source_commit",
        "server_release_id",
        "files",
    }
    if not isinstance(manifest, dict) or set(manifest) != required_keys:
        raise RuntimeError("launcher manifest does not match the closed schema")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise RuntimeError("launcher manifest schema is unsupported")
    if not isinstance(manifest["distribution_id"], str) or not HEX64.fullmatch(manifest["distribution_id"]):
        raise RuntimeError("launcher distribution ID is invalid")
    if not isinstance(manifest["server_release_id"], str) or not HEX64.fullmatch(manifest["server_release_id"]):
        raise RuntimeError("server release ID is invalid")
    if not isinstance(manifest["source_commit"], str) or not COMMIT.fullmatch(manifest["source_commit"]):
        raise RuntimeError("launcher source commit is invalid")
    if not isinstance(manifest["product_version"], str) or not VERSION.fullmatch(manifest["product_version"]):
        raise RuntimeError("launcher product version is invalid")
    files = manifest["files"]
    if not isinstance(files, list) or len(files) != len(REQUIRED):
        raise RuntimeError("launcher file inventory is invalid")
    normalized = []
    seen = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "byte_size", "sha256"}:
            raise RuntimeError("launcher file record is invalid")
        path = safe_name(item["path"])
        size = item["byte_size"]
        digest = item["sha256"]
        if path in seen or not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_FILE_BYTES:
            raise RuntimeError("launcher file identity is invalid")
        if not isinstance(digest, str) or not HEX64.fullmatch(digest):
            raise RuntimeError("launcher file digest is invalid")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise RuntimeError("launcher distribution exceeds its byte limit")
        if digest_file(root / path, size) != digest:
            raise RuntimeError(f"launcher file digest changed: {path}")
        seen.add(path)
        normalized.append({"path": path, "byte_size": size, "sha256": digest})
    if seen != REQUIRED or [item["path"] for item in normalized] != sorted(seen):
        raise RuntimeError("launcher file inventory is incomplete or unsorted")
    actual_entries = {entry.name for entry in root.iterdir()}
    if actual_entries != REQUIRED | {"manifest.json"}:
        raise RuntimeError("launcher directory contains unexpected files")
    identity = {
        "schema_version": SCHEMA_VERSION,
        "product_version": manifest["product_version"],
        "source_commit": manifest["source_commit"],
        "server_release_id": manifest["server_release_id"],
        "files": normalized,
    }
    if hashlib.sha256(canonical(identity)).hexdigest() != manifest["distribution_id"]:
        raise RuntimeError("launcher distribution identity is invalid")
    if raw_manifest != canonical(manifest) + b"\n":
        raise RuntimeError("launcher manifest is not canonical")
    return manifest, normalized

manifest, files = load_and_verify(package_root)
distribution_id = manifest["distribution_id"]
install_base = prefix / "share" / "openevo"
releases_root = install_base / "releases"
release_root = releases_root / distribution_id
bin_root = prefix / "bin"
releases_root.mkdir(mode=0o700, parents=True, exist_ok=True)
bin_root.mkdir(mode=0o700, parents=True, exist_ok=True)

if release_root.exists():
    existing_manifest, _ = load_and_verify(release_root)
    if existing_manifest != manifest:
        raise RuntimeError("installed launcher release has conflicting contents")
else:
    temporary = Path(tempfile.mkdtemp(prefix=".install-", dir=releases_root))
    candidate = temporary / "release"
    candidate.mkdir(mode=0o700)
    try:
        shutil.copyfile(package_root / "manifest.json", candidate / "manifest.json")
        os.chmod(candidate / "manifest.json", 0o600)
        for item in files:
            destination = candidate / item["path"]
            shutil.copyfile(package_root / item["path"], destination)
            os.chmod(destination, 0o700 if item["path"] == "install.sh" else 0o600)
        load_and_verify(candidate)
        try:
            os.rename(candidate, release_root)
        except FileExistsError:
            existing_manifest, _ = load_and_verify(release_root)
            if existing_manifest != manifest:
                raise RuntimeError("another installer published conflicting contents")
    finally:
        shutil.rmtree(temporary, ignore_errors=True)

wrapper_path = bin_root / "openevo"
wrapper = f"""#!/bin/sh
{WRAPPER_MARKER}
set -eu
bin_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
install_base=$(CDPATH= cd -- "$bin_root/../share/openevo" && pwd -P)
distribution_id=$(cat "$install_base/active-launcher-v1")
case "$distribution_id" in
  ''|*[!0-9a-f]*) echo "EvoLab active launcher receipt is invalid." >&2; exit 70 ;;
esac
release_root="$install_base/releases/$distribution_id"
if [ ! -f "$release_root/openevo.pyz" ] || [ ! -f "$release_root/openevo-server.oevobundle" ]; then
  echo "EvoLab active launcher release is incomplete." >&2
  exit 71
fi
exec python3 "$release_root/openevo.pyz" "$@" --release-bundle "$release_root/openevo-server.oevobundle"
""".encode()
if wrapper_path.exists() or wrapper_path.is_symlink():
    if wrapper_path.is_symlink() or not wrapper_path.is_file():
        raise RuntimeError(f"refusing to replace unmanaged command: {wrapper_path}")
    existing = wrapper_path.read_bytes()
    expected_prefix = f"#!/bin/sh\n{WRAPPER_MARKER}\n".encode()
    if not existing.startswith(expected_prefix):
        raise RuntimeError(f"refusing to replace unmanaged command: {wrapper_path}")
wrapper_temporary = bin_root / f".openevo.tmp.{os.getpid()}"
wrapper_temporary.write_bytes(wrapper)
os.chmod(wrapper_temporary, 0o755)
os.replace(wrapper_temporary, wrapper_path)

evolab_wrapper_path = bin_root / "evolab"
if evolab_wrapper_path.exists() or evolab_wrapper_path.is_symlink():
    if evolab_wrapper_path.is_symlink() or not evolab_wrapper_path.is_file():
        raise RuntimeError(f"refusing to replace unmanaged command: {evolab_wrapper_path}")
    existing = evolab_wrapper_path.read_bytes()
    expected_prefix = f"#!/bin/sh\n{WRAPPER_MARKER}\n".encode()
    if not existing.startswith(expected_prefix):
        raise RuntimeError(f"refusing to replace unmanaged command: {evolab_wrapper_path}")
evolab_wrapper_temporary = bin_root / f".evolab.tmp.{os.getpid()}"
evolab_wrapper_temporary.write_bytes(wrapper)
os.chmod(evolab_wrapper_temporary, 0o755)
os.replace(evolab_wrapper_temporary, evolab_wrapper_path)

marker = install_base / "active-launcher-v1"
marker_temporary = install_base / f".active-launcher.tmp.{os.getpid()}"
marker_temporary.write_text(distribution_id + "\n", encoding="ascii")
os.chmod(marker_temporary, 0o600)
os.replace(marker_temporary, marker)

print(f"Installed EvoLab {manifest['product_version']} ({distribution_id[:12]}).")
print(f"Command: {evolab_wrapper_path}")
if os.fspath(bin_root) not in os.environ.get("PATH", "").split(os.pathsep):
    print(f"Add {bin_root} to PATH, then run: evolab webui")
else:
    print("Run: evolab webui")
PY
'''


INSTALLER_POWERSHELL = r'''# EvoLab repository-free per-user installer
[CmdletBinding()]
param(
    [string]$Prefix = (Join-Path $env:LOCALAPPDATA "EvoLab"),
    [switch]$NoPathUpdate
)

$ErrorActionPreference = "Stop"
$requiredFiles = @(
    "LICENSE",
    "README.txt",
    "install.ps1",
    "install.sh",
    "openevo.pyz",
    "openevo-server.oevobundle"
)
$wrapperMarker = "REM managed by EvoLab launcher installer"

function Find-Python311 {
    $candidates = @(
        @{ Command = "py"; Arguments = @("-3.11") },
        @{ Command = "python"; Arguments = @() },
        @{ Command = "python3"; Arguments = @() }
    )
    foreach ($candidate in $candidates) {
        $resolved = Get-Command $candidate.Command -ErrorAction SilentlyContinue
        if ($null -eq $resolved) { continue }
        $previousErrorPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $resolved.Source @($candidate.Arguments) -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
            $candidateExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorPreference
        }
        if ($candidateExitCode -eq 0) {
            return @{
                Executable = $resolved.Source
                Arguments = @($candidate.Arguments)
            }
        }
    }
    throw "Python 3.11 or newer is required on this computer."
}

function Get-FileDigest([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-Distribution([string]$Root, $ExpectedManifest = $null) {
    $manifestPath = Join-Path $Root "manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "launcher manifest is missing"
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema_version -ne "1") { throw "launcher manifest schema is unsupported" }
    if ($manifest.distribution_id -notmatch '^[0-9a-f]{64}$') {
        throw "launcher distribution ID is invalid"
    }
    if ($manifest.source_commit -notmatch '^[0-9a-f]{40,64}$') {
        throw "launcher source commit is invalid"
    }
    $records = @($manifest.files)
    if ($records.Count -ne $requiredFiles.Count) {
        throw "launcher file inventory is invalid"
    }
    $seen = @{}
    foreach ($record in $records) {
        $name = [string]$record.path
        if ([string]::IsNullOrWhiteSpace($name) -or [IO.Path]::GetFileName($name) -ne $name) {
            throw "launcher manifest contains an unsafe path"
        }
        if ($seen.ContainsKey($name)) { throw "launcher manifest contains a duplicate path" }
        $seen[$name] = $true
        $path = Join-Path $Root $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "launcher file is missing: $name"
        }
        $size = (Get-Item -LiteralPath $path).Length
        if ($size -ne [int64]$record.byte_size) { throw "launcher file size changed: $name" }
        if ((Get-FileDigest $path) -ne [string]$record.sha256) {
            throw "launcher file digest changed: $name"
        }
    }
    foreach ($name in $requiredFiles) {
        if (-not $seen.ContainsKey($name)) { throw "launcher file inventory is incomplete" }
    }
    $actualNames = @(Get-ChildItem -LiteralPath $Root -File | ForEach-Object { $_.Name } | Sort-Object)
    $expectedNames = @($requiredFiles + "manifest.json" | Sort-Object)
    if (($actualNames -join "`n") -ne ($expectedNames -join "`n")) {
        throw "launcher directory contains unexpected files"
    }
    if ($null -ne $ExpectedManifest) {
        $actualManifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8
        if ($actualManifest -ne $ExpectedManifest) {
            throw "installed launcher release has conflicting contents"
        }
    }
    return $manifest
}

function Quote-Cmd([string]$Value) {
    return '"' + $Value.Replace('"', '""') + '"'
}

if (-not [IO.Path]::IsPathRooted($Prefix)) {
    throw "installation prefix must be an absolute path"
}
if ($null -eq (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "The system OpenSSH client is required on this computer."
}
$python = Find-Python311
$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$manifest = Assert-Distribution $packageRoot
$manifestText = Get-Content -LiteralPath (Join-Path $packageRoot "manifest.json") -Raw -Encoding UTF8
$distributionId = [string]$manifest.distribution_id
$installBase = Join-Path $Prefix "share\openevo"
$releasesRoot = Join-Path $installBase "releases"
$releaseRoot = Join-Path $releasesRoot $distributionId
$binRoot = Join-Path $Prefix "bin"
New-Item -ItemType Directory -Force -Path $releasesRoot, $binRoot | Out-Null

if (Test-Path -LiteralPath $releaseRoot) {
    [void](Assert-Distribution $releaseRoot $manifestText)
} else {
    $temporary = Join-Path $releasesRoot (".install-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $temporary | Out-Null
    try {
        Copy-Item -LiteralPath (Join-Path $packageRoot "manifest.json") -Destination $temporary
        foreach ($record in @($manifest.files)) {
            Copy-Item -LiteralPath (Join-Path $packageRoot ([string]$record.path)) -Destination $temporary
        }
        [void](Assert-Distribution $temporary $manifestText)
        Move-Item -LiteralPath $temporary -Destination $releaseRoot
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Recurse -Force
        }
    }
}

$pythonParts = @((Quote-Cmd $python.Executable)) + @($python.Arguments)
$pythonPrefix = $pythonParts -join " "
$wrapper = @"
@echo off
$wrapperMarker
setlocal
set /p "EVOLAB_DISTRIBUTION_ID="<"%~dp0..\share\openevo\active-launcher-v1"
set "EVOLAB_RELEASE_ROOT=%~dp0..\share\openevo\releases\%EVOLAB_DISTRIBUTION_ID%"
$pythonPrefix "%EVOLAB_RELEASE_ROOT%\openevo.pyz" %* --release-bundle "%EVOLAB_RELEASE_ROOT%\openevo-server.oevobundle"
"@
foreach ($commandName in @("evolab.cmd", "openevo.cmd")) {
    $commandPath = Join-Path $binRoot $commandName
    if (Test-Path -LiteralPath $commandPath) {
        $existing = Get-Content -LiteralPath $commandPath -Raw
        if (-not $existing.StartsWith("@echo off`r`n$wrapperMarker") -and
            -not $existing.StartsWith("@echo off`n$wrapperMarker")) {
            throw "refusing to replace unmanaged command: $commandPath"
        }
    }
    Set-Content -LiteralPath $commandPath -Value $wrapper -Encoding ASCII
}
Set-Content -LiteralPath (Join-Path $installBase "active-launcher-v1") -Value $distributionId -Encoding ASCII

if (-not $NoPathUpdate) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @($userPath -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if (-not ($entries | Where-Object { $_.TrimEnd('\') -ieq $binRoot.TrimEnd('\') })) {
        $updatedPath = (@($entries) + $binRoot) -join ';'
        [Environment]::SetEnvironmentVariable("Path", $updatedPath, "User")
    }
    if (-not (($env:Path -split ';') | Where-Object { $_.TrimEnd('\') -ieq $binRoot.TrimEnd('\') })) {
        $env:Path = "$env:Path;$binRoot"
    }
}

Write-Host "Installed EvoLab $($manifest.product_version) ($($distributionId.Substring(0, 12)))."
Write-Host "Open a new terminal, then run: evolab webui"
Write-Host "Compatibility command: openevo webui"
'''


def _distribution_identity(
    *,
    product_version: str,
    source_commit: str,
    server_release_id: str,
    files: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": LAUNCHER_DISTRIBUTION_SCHEMA_VERSION,
        "product_version": product_version,
        "source_commit": source_commit,
        "server_release_id": server_release_id,
        "files": files,
    }


def _tar_info(path: str, size: int, *, executable: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(path)
    info.size = size
    info.mode = 0o755 if executable else 0o644
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def build_launcher_distribution(
    repository_root: Path,
    output_path: Path,
    *,
    commit: str = "HEAD",
) -> LauncherDistributionReceipt:
    """Build one deterministic ordinary-user archive from an exact commit."""

    root = repository_root.resolve()
    resolved_commit = _git(root, "rev-parse", f"{commit}^{{commit}}")
    assert isinstance(resolved_commit, str)
    resolved_commit = resolved_commit.strip()
    if not COMMIT_PATTERN.fullmatch(resolved_commit):
        raise LauncherDistributionError("git returned an invalid source commit")
    target = output_path.resolve()
    archive_kind = next(
        (suffix for suffix in LAUNCHER_DISTRIBUTION_SUFFIXES if target.name.endswith(suffix)),
        None,
    )
    if archive_kind is None:
        raise LauncherDistributionError(
            "launcher distribution output must end with "
            + " or ".join(LAUNCHER_DISTRIBUTION_SUFFIXES)
        )
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="openevo-launcher-build-") as temporary_name:
        temporary_root = Path(temporary_name)
        server_path = temporary_root / f"openevo-server{BUNDLE_SUFFIX}"
        try:
            server = build_release_bundle(root, server_path, commit=resolved_commit)
        except ReleaseBundleError as exc:
            raise LauncherDistributionError(f"server release build failed: {exc}") from exc
        payload: dict[str, bytes] = {
            "LICENSE": _git_file(root, resolved_commit, "LICENSE"),
            "README.txt": _installer_readme(server.product_version),
            "install.ps1": INSTALLER_POWERSHELL.encode("utf-8"),
            "install.sh": INSTALLER_SCRIPT.encode("utf-8"),
            "openevo.pyz": _build_launcher_zipapp(root, resolved_commit),
            f"openevo-server{BUNDLE_SUFFIX}": server_path.read_bytes(),
        }
        files = [
            {
                "path": path,
                "byte_size": len(data),
                "sha256": _sha256_bytes(data),
            }
            for path, data in sorted(payload.items())
        ]
        identity = _distribution_identity(
            product_version=server.product_version,
            source_commit=resolved_commit,
            server_release_id=server.release_id,
            files=files,
        )
        distribution_id = _sha256_bytes(_canonical_json(identity))
        manifest = {**identity, "distribution_id": distribution_id}
        archive_payload = {"manifest.json": _canonical_json(manifest) + b"\n", **payload}

        fd, temporary_output_name = tempfile.mkstemp(
            prefix="openevo-launcher-",
            suffix=".tmp",
            dir=target.parent,
        )
        os.close(fd)
        temporary_output = Path(temporary_output_name)
        try:
            if archive_kind == ".tar.gz":
                tar_path = temporary_root / "launcher.tar"
                with tarfile.open(tar_path, "w", format=tarfile.USTAR_FORMAT) as archive:
                    for path, data in sorted(archive_payload.items()):
                        archive_path = f"{LAUNCHER_DISTRIBUTION_ROOT}/{path}"
                        archive.addfile(
                            _tar_info(archive_path, len(data), executable=path == "install.sh"),
                            io.BytesIO(data),
                        )
                with tar_path.open("rb") as source, temporary_output.open("wb") as raw_output:
                    with gzip.GzipFile(
                        filename="", mode="wb", fileobj=raw_output, mtime=0
                    ) as compressed:
                        while chunk := source.read(1024 * 1024):
                            compressed.write(chunk)
            else:
                with zipfile.ZipFile(temporary_output, "w", allowZip64=False) as archive:
                    for path, data in sorted(archive_payload.items()):
                        archive.writestr(
                            _zip_info(f"{LAUNCHER_DISTRIBUTION_ROOT}/{path}"),
                            data,
                        )
            os.replace(temporary_output, target)
        finally:
            temporary_output.unlink(missing_ok=True)
    return verify_launcher_distribution(target)


def _safe_distribution_path(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise LauncherDistributionError(f"invalid launcher file path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) != 1 or path.as_posix() != value:
        raise LauncherDistributionError(f"unsafe launcher file path: {value!r}")
    return value


def _parse_manifest(value: Any) -> tuple[dict[str, object], list[dict[str, object]]]:
    required_keys = {
        "schema_version",
        "distribution_id",
        "product_version",
        "source_commit",
        "server_release_id",
        "files",
    }
    if not isinstance(value, dict) or set(value) != required_keys:
        raise LauncherDistributionError("launcher manifest does not match the closed schema")
    if value["schema_version"] != LAUNCHER_DISTRIBUTION_SCHEMA_VERSION:
        raise LauncherDistributionError("launcher manifest schema is unsupported")
    if not isinstance(value["distribution_id"], str) or not RELEASE_ID_PATTERN.fullmatch(
        value["distribution_id"]
    ):
        raise LauncherDistributionError("launcher distribution ID is invalid")
    if not isinstance(value["server_release_id"], str) or not RELEASE_ID_PATTERN.fullmatch(
        value["server_release_id"]
    ):
        raise LauncherDistributionError("launcher server release ID is invalid")
    if not isinstance(value["source_commit"], str) or not COMMIT_PATTERN.fullmatch(
        value["source_commit"]
    ):
        raise LauncherDistributionError("launcher source commit is invalid")
    if not isinstance(value["product_version"], str) or not VERSION_PATTERN.fullmatch(
        value["product_version"]
    ):
        raise LauncherDistributionError("launcher product version is invalid")
    raw_files = value["files"]
    if not isinstance(raw_files, list) or len(raw_files) != len(REQUIRED_DISTRIBUTION_FILES):
        raise LauncherDistributionError("launcher file inventory is invalid")
    files: list[dict[str, object]] = []
    seen: set[str] = set()
    total = 0
    for item in raw_files:
        if not isinstance(item, dict) or set(item) != {"path", "byte_size", "sha256"}:
            raise LauncherDistributionError("launcher manifest contains an invalid file record")
        path = _safe_distribution_path(item["path"] if isinstance(item["path"], str) else "")
        size = item["byte_size"]
        digest = item["sha256"]
        if path in seen or not isinstance(size, int) or isinstance(size, bool):
            raise LauncherDistributionError("launcher manifest file identity is invalid")
        if not 0 <= size <= MAX_DISTRIBUTION_FILE_BYTES:
            raise LauncherDistributionError("launcher manifest file size is invalid")
        if not isinstance(digest, str) or not RELEASE_ID_PATTERN.fullmatch(digest):
            raise LauncherDistributionError("launcher manifest file digest is invalid")
        total += size
        if total > MAX_DISTRIBUTION_BYTES:
            raise LauncherDistributionError("launcher distribution exceeds its byte limit")
        seen.add(path)
        files.append({"path": path, "byte_size": size, "sha256": digest})
    if seen != REQUIRED_DISTRIBUTION_FILES or [item["path"] for item in files] != sorted(seen):
        raise LauncherDistributionError("launcher file inventory is incomplete or unsorted")
    identity = _distribution_identity(
        product_version=str(value["product_version"]),
        source_commit=str(value["source_commit"]),
        server_release_id=str(value["server_release_id"]),
        files=files,
    )
    if _sha256_bytes(_canonical_json(identity)) != value["distribution_id"]:
        raise LauncherDistributionError("launcher distribution identity is invalid")
    return value, files


def verify_launcher_distribution(path: Path) -> LauncherDistributionReceipt:
    """Verify archive shape, identity, and all ordinary-user payload bytes."""

    archive_path = path.resolve()
    if archive_path.stat().st_size > MAX_DISTRIBUTION_BYTES:
        raise LauncherDistributionError("launcher archive exceeds its byte limit")
    manifest_name = f"{LAUNCHER_DISTRIBUTION_ROOT}/manifest.json"
    try:
        if archive_path.name.endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                if len(names) != len(set(names)) or names.count(manifest_name) != 1:
                    raise LauncherDistributionError(
                        "launcher archive contains duplicate entries or no unique manifest"
                    )
                manifest_info = archive.getinfo(manifest_name)
                if manifest_info.is_dir() or manifest_info.file_size > 1024 * 1024:
                    raise LauncherDistributionError("launcher manifest entry is invalid")
                manifest_bytes = archive.read(manifest_info)
                manifest, files = _parse_launcher_manifest_bytes(manifest_bytes)
                _verify_archive_names(names, manifest_name, files)
                for item in files:
                    name = f"{LAUNCHER_DISTRIBUTION_ROOT}/{item['path']}"
                    info = archive.getinfo(name)
                    if info.is_dir() or info.file_size != item["byte_size"]:
                        raise LauncherDistributionError(
                            f"launcher archive file is invalid: {name}"
                        )
                    with archive.open(info, "r") as source:
                        _verify_archive_stream(source, item, name)
        else:
            with tarfile.open(archive_path, "r:gz") as archive:
                members = archive.getmembers()
                names = [member.name for member in members]
                if len(names) != len(set(names)) or names.count(manifest_name) != 1:
                    raise LauncherDistributionError(
                        "launcher archive contains duplicate entries or no unique manifest"
                    )
                manifest_member = archive.getmember(manifest_name)
                if not manifest_member.isfile() or manifest_member.size > 1024 * 1024:
                    raise LauncherDistributionError("launcher manifest entry is invalid")
                manifest_source = archive.extractfile(manifest_member)
                if manifest_source is None:
                    raise LauncherDistributionError("launcher manifest could not be read")
                manifest_bytes = manifest_source.read(1024 * 1024 + 1)
                manifest, files = _parse_launcher_manifest_bytes(manifest_bytes)
                _verify_archive_names(names, manifest_name, files)
                for item in files:
                    name = f"{LAUNCHER_DISTRIBUTION_ROOT}/{item['path']}"
                    member = archive.getmember(name)
                    if not member.isfile() or member.size != item["byte_size"]:
                        raise LauncherDistributionError(
                            f"launcher archive file is invalid: {name}"
                        )
                    source = archive.extractfile(member)
                    if source is None:
                        raise LauncherDistributionError(
                            f"launcher archive file could not be read: {name}"
                        )
                    _verify_archive_stream(source, item, name)
    except (OSError, tarfile.TarError, zipfile.BadZipFile, KeyError) as exc:
        raise LauncherDistributionError(f"could not read launcher distribution: {exc}") from exc
    return LauncherDistributionReceipt(
        path=archive_path,
        distribution_id=str(manifest["distribution_id"]),
        product_version=str(manifest["product_version"]),
        source_commit=str(manifest["source_commit"]),
        server_release_id=str(manifest["server_release_id"]),
        sha256=_sha256_file(archive_path),
        byte_size=archive_path.stat().st_size,
        file_count=len(files),
    )


def _parse_launcher_manifest_bytes(
    manifest_bytes: bytes,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    try:
        manifest_value = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LauncherDistributionError("launcher manifest is not valid JSON") from exc
    manifest, files = _parse_manifest(manifest_value)
    if manifest_bytes != _canonical_json(manifest) + b"\n":
        raise LauncherDistributionError("launcher manifest is not canonical")
    return manifest, files


def _verify_archive_names(
    names: list[str],
    manifest_name: str,
    files: list[dict[str, object]],
) -> None:
    expected_names = {
        manifest_name,
        *(f"{LAUNCHER_DISTRIBUTION_ROOT}/{item['path']}" for item in files),
    }
    if set(names) != expected_names:
        raise LauncherDistributionError("launcher archive entries do not match its manifest")


def _verify_archive_stream(source: Any, item: dict[str, object], name: str) -> None:
    digest = hashlib.sha256()
    read_bytes = 0
    while chunk := source.read(1024 * 1024):
        read_bytes += len(chunk)
        if read_bytes > item["byte_size"]:
            raise LauncherDistributionError(f"launcher archive file expanded: {name}")
        digest.update(chunk)
    if digest.hexdigest() != item["sha256"]:
        raise LauncherDistributionError(f"launcher archive digest mismatch: {name}")


__all__ = [
    "LAUNCHER_DISTRIBUTION_ROOT",
    "LAUNCHER_DISTRIBUTION_SUFFIXES",
    "LauncherDistributionError",
    "LauncherDistributionReceipt",
    "build_launcher_distribution",
    "verify_launcher_distribution",
]
