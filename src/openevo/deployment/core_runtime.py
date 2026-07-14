from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import shlex

from pydantic import SecretStr


_BOOTSTRAP_PYTHON = "/usr/bin/python3"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REMOTE_PATH = re.compile(r"/(?:[A-Za-z0-9._@%+=,-]+/)*[A-Za-z0-9._@%+=,-]+\Z")
_MAX_RESPONSE_BYTES = 4096
_MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
_REASONS = {
    "ready",
    "unsupported_platform",
    "process_identity_unavailable",
    "kernel_syscall_unsupported",
    "no_supported_python",
    "python_provision_failed",
}


@dataclass(frozen=True, slots=True, repr=False)
class CorePythonRuntimeAuthority:
    authority_id: str
    executable_path: str
    executable_sha256: str
    device: int
    inode: int
    uid: int
    mode: int
    byte_size: int
    mtime_ns: int
    ctime_ns: int
    version: tuple[int, int, int]

    def __post_init__(self) -> None:
        fields = (
            self.device,
            self.inode,
            self.uid,
            self.mode,
            self.byte_size,
            self.mtime_ns,
            self.ctime_ns,
        )
        if (
            not isinstance(self.authority_id, str)
            or _DIGEST.fullmatch(self.authority_id) is None
            or not isinstance(self.executable_path, str)
            or _REMOTE_PATH.fullmatch(self.executable_path) is None
            or any(part in {"", ".", ".."} for part in self.executable_path.split("/")[1:])
            or not isinstance(self.executable_sha256, str)
            or _DIGEST.fullmatch(self.executable_sha256) is None
            or any(type(value) is not int or value < 0 for value in fields)
            or self.inode <= 0
            or not 0 < self.byte_size <= _MAX_EXECUTABLE_BYTES
            or not 0 <= self.mode <= 0o7777
            or self.mode & 0o111 == 0
            or not isinstance(self.version, tuple)
            or len(self.version) != 3
            or any(type(value) is not int or value < 0 for value in self.version)
            or self.version < (3, 11, 0)
            or self.authority_id != _runtime_authority_id(self)
        ):
            raise ValueError("Core Python runtime authority is invalid")


@dataclass(frozen=True, slots=True)
class CorePythonRuntimeSelection:
    reason: str
    authority: CorePythonRuntimeAuthority | None = None

    def __post_init__(self) -> None:
        if self.reason not in _REASONS or (self.reason == "ready") is not (
            self.authority is not None
        ):
            raise ValueError("Core Python runtime selection is invalid")


def build_core_supervisor_runtime_preflight_command(*, timeout_seconds: float = 300.0) -> str:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= 300
    ):
        raise ValueError("Core Python runtime selection deadline is invalid")
    remote_timeout = max(1, min(300, int(timeout_seconds)))
    return " ".join(
        (
            _BOOTSTRAP_PYTHON,
            "-I",
            "-c",
            shlex.quote(_REMOTE_SELECTION_SCRIPT),
            str(remote_timeout),
        )
    )


def parse_core_supervisor_runtime_preflight(
    payload: SecretStr,
) -> CorePythonRuntimeSelection:
    if not isinstance(payload, SecretStr):
        raise ValueError("Core Python runtime selection response is invalid")
    try:
        encoded = payload.get_secret_value().encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Core Python runtime selection response is invalid") from exc
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise ValueError("Core Python runtime selection response is invalid")
    try:
        value = json.loads(encoded, object_pairs_hook=_closed_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Core Python runtime selection response is invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"authority", "reason", "schema_version"}
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 2
        or not isinstance(value.get("reason"), str)
        or value["reason"] not in _REASONS
    ):
        raise ValueError("Core Python runtime selection response is invalid")
    if value["reason"] != "ready":
        if value.get("authority") is not None:
            raise ValueError("Core Python runtime selection response is invalid")
        return CorePythonRuntimeSelection(reason=value["reason"])
    authority = value.get("authority")
    expected = {
        "authority_id",
        "byte_size",
        "ctime_ns",
        "device",
        "executable_path",
        "executable_sha256",
        "inode",
        "mode",
        "mtime_ns",
        "uid",
        "version",
    }
    if not isinstance(authority, dict) or set(authority) != expected:
        raise ValueError("Core Python runtime selection response is invalid")
    integer_fields = (
        "byte_size",
        "ctime_ns",
        "device",
        "inode",
        "mode",
        "mtime_ns",
        "uid",
    )
    version = authority.get("version")
    if (
        any(type(authority.get(field)) is not int for field in integer_fields)
        or not isinstance(version, list)
        or len(version) != 3
        or any(type(part) is not int for part in version)
    ):
        raise ValueError("Core Python runtime selection response is invalid")
    try:
        parsed = CorePythonRuntimeAuthority(
            authority_id=authority["authority_id"],
            executable_path=authority["executable_path"],
            executable_sha256=authority["executable_sha256"],
            device=authority["device"],
            inode=authority["inode"],
            uid=authority["uid"],
            mode=authority["mode"],
            byte_size=authority["byte_size"],
            mtime_ns=authority["mtime_ns"],
            ctime_ns=authority["ctime_ns"],
            version=tuple(version),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Core Python runtime selection response is invalid") from exc
    return CorePythonRuntimeSelection(reason="ready", authority=parsed)


def build_verified_python_command(
    authority: CorePythonRuntimeAuthority,
    script: str,
    *arguments: str,
) -> str:
    if not isinstance(authority, CorePythonRuntimeAuthority):
        raise ValueError("Core Python runtime authority is invalid")
    authority.__post_init__()
    if (
        not isinstance(script, str)
        or not script
        or "\x00" in script
        or any(not isinstance(value, str) or "\x00" in value for value in arguments)
    ):
        raise ValueError("Core Python runtime invocation is invalid")
    verifier_arguments = (
        authority.authority_id,
        authority.executable_path,
        authority.executable_sha256,
        str(authority.device),
        str(authority.inode),
        str(authority.uid),
        str(authority.mode),
        str(authority.byte_size),
        str(authority.mtime_ns),
        str(authority.ctime_ns),
        ".".join(str(value) for value in authority.version),
        "-I",
        "-c",
        script,
        *arguments,
    )
    return " ".join(
        (_BOOTSTRAP_PYTHON, "-I", "-c", shlex.quote(_REMOTE_EXECUTOR_SCRIPT))
        + tuple(shlex.quote(value) for value in verifier_arguments)
    )


def _runtime_authority_id(authority: CorePythonRuntimeAuthority) -> str:
    canonical = json.dumps(
        {
            "byte_size": authority.byte_size,
            "ctime_ns": authority.ctime_ns,
            "device": authority.device,
            "executable_path": authority.executable_path,
            "executable_sha256": authority.executable_sha256,
            "inode": authority.inode,
            "mode": authority.mode,
            "mtime_ns": authority.mtime_ns,
            "schema_version": 1,
            "uid": authority.uid,
            "version": list(authority.version),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"openevo-core-python-runtime-v1\0" + canonical).hexdigest()


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


_REMOTE_SELECTION_SCRIPT = r"""
import ctypes, errno, hashlib, io, json, os, pwd, shutil, signal, stat, subprocess, sys, tarfile, tempfile, time, urllib.error, urllib.request

deadline = time.monotonic() + int(sys.argv[1])
uid = os.geteuid()
max_output = 4096
max_executable = 256 * 1024 * 1024
max_uv_archive = 64 * 1024 * 1024
allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@%+=,-")
current_child = None

def emit(reason, authority=None):
    print(json.dumps({"schema_version": 2, "reason": reason, "authority": authority}, sort_keys=True, separators=(",", ":")))
    raise SystemExit(0)

def terminate_child(_signum, _frame):
    global current_child
    child = current_child
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    raise SystemExit(124)

signal.signal(signal.SIGTERM, terminate_child)
signal.signal(signal.SIGINT, terminate_child)
if hasattr(signal, "SIGHUP"):
    signal.signal(signal.SIGHUP, terminate_child)

def remaining(cap):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError
    return min(cap, value)

def closed_absolute(path):
    if not isinstance(path, str) or len(path.encode("utf-8")) > 4096 or not path.startswith("/"):
        return False
    parts = path.split("/")[1:]
    return bool(parts) and all(part and part not in {".", ".."} and all(c in allowed for c in part) for part in parts)

flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
dir_flags = flags | os.O_DIRECTORY

def open_absolute(path):
    parts = path.split("/")[1:]
    parent = os.open("/", dir_flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, dir_flags, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], flags, dir_fd=parent)
        return parent, fd, parts[-1]
    except BaseException:
        os.close(parent)
        raise

def digest_fd(fd, size):
    os.lseek(fd, 0, os.SEEK_SET)
    value = hashlib.sha256()
    remaining_bytes = size
    while remaining_bytes:
        chunk = os.read(fd, min(1024 * 1024, remaining_bytes))
        if not chunk:
            raise ValueError
        value.update(chunk)
        remaining_bytes -= len(chunk)
    if os.read(fd, 1):
        raise ValueError
    return value.hexdigest()

def secure_executable(raw):
    if not isinstance(raw, str) or "\n" in raw or "\r" in raw or "\0" in raw:
        raise ValueError
    path = os.path.realpath(raw)
    if not closed_absolute(path):
        raise ValueError
    parent, fd, name = open_absolute(path)
    try:
        before = os.fstat(fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        mode = stat.S_IMODE(before.st_mode)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid not in {0, uid}
                or before.st_nlink != 1 or mode & 0o022 or mode & 0o111 == 0
                or not 0 < before.st_size <= max_executable
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError
        digest = digest_fd(fd, before.st_size)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (any(getattr(before, field) != getattr(after, field) for field in fields)
                or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError
        return path, fd, before, mode, digest
    except BaseException:
        os.close(fd)
        raise
    finally:
        os.close(parent)

def run_fd(fd, arguments, timeout):
    global current_child
    os.set_inheritable(fd, True)
    with tempfile.TemporaryFile() as output:
        child = subprocess.Popen(
            ["/proc/self/fd/" + str(fd), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(fd,),
            env=child_environment,
        )
        current_child = child
        try:
            code = child.wait(timeout=remaining(timeout))
        except (subprocess.TimeoutExpired, TimeoutError):
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            raise TimeoutError
        finally:
            current_child = None
        size = output.tell()
        if size > max_output:
            raise ValueError
        output.seek(0)
        return code, output.read(max_output + 1)

home = pwd.getpwuid(uid).pw_dir
child_environment = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"):
    value = os.environ.get(key)
    if value:
        child_environment[key] = value

if sys.platform != "linux":
    emit("unsupported_platform")
try:
    with open("/proc/sys/kernel/random/boot_id", "r", encoding="ascii") as handle:
        boot_id = handle.read(64).strip()
    groups = boot_id.split("-")
    if ([len(group) for group in groups] != [8, 4, 4, 4, 12]
            or any(any(character not in "0123456789abcdef" for character in group) for group in groups)):
        emit("process_identity_unavailable")
except (OSError, UnicodeError):
    emit("process_identity_unavailable")

try:
    machine = os.uname().machine.lower()
    numbers = {"aarch64": (434, 424), "arm64": (434, 424), "amd64": (434, 424), "x86_64": (434, 424)}.get(machine)
    if numbers is None:
        raise OSError(errno.ENOSYS, "unsupported syscall ABI")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    pidfd = libc.syscall(ctypes.c_long(numbers[0]), ctypes.c_int(os.getpid()), ctypes.c_uint(0))
    if pidfd == -1:
        raise OSError(ctypes.get_errno() or errno.EIO, "pidfd_open failed")
    try:
        result = libc.syscall(ctypes.c_long(numbers[1]), ctypes.c_int(pidfd), ctypes.c_int(0), ctypes.c_void_p(), ctypes.c_uint(0))
        if result == -1:
            raise OSError(ctypes.get_errno() or errno.EIO, "pidfd_send_signal failed")
    finally:
        os.close(pidfd)
except OSError:
    emit("kernel_syscall_unsupported")

probe = "import json,sys;print(json.dumps({'platform':sys.platform,'version':list(sys.version_info[:3])},sort_keys=True,separators=(',',':')))"

def inspect_python(raw):
    try:
        path, fd, metadata, mode, digest = secure_executable(raw)
        try:
            code, output = run_fd(fd, ["-I", "-c", probe], 20)
            current = os.stat(path, follow_symlinks=False)
            if code != 0 or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
                return None
            value = json.loads(output)
            if (not isinstance(value, dict) or set(value) != {"platform", "version"}
                    or value.get("platform") != "linux" or not isinstance(value.get("version"), list)
                    or len(value["version"]) != 3 or any(type(part) is not int for part in value["version"])
                    or tuple(value["version"]) < (3, 11, 0)):
                return None
            authority = {
                "executable_path": path,
                "executable_sha256": digest,
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "uid": metadata.st_uid,
                "mode": mode,
                "byte_size": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "ctime_ns": metadata.st_ctime_ns,
                "version": value["version"],
            }
            canonical = json.dumps({"schema_version": 1, **authority}, sort_keys=True, separators=(",", ":")).encode("utf-8")
            authority["authority_id"] = hashlib.sha256(b"openevo-core-python-runtime-v1\0" + canonical).hexdigest()
            return authority
        finally:
            os.close(fd)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, TimeoutError):
        return None

candidates = []
for name in ("python3.13", "python3.12", "python3.11"):
    found = shutil.which(name, path=child_environment["PATH"])
    if found:
        candidates.append(found)
for prefix in ("/usr/local/bin", "/usr/bin", home + "/.local/bin"):
    for name in ("python3.13", "python3.12", "python3.11"):
        candidates.append(prefix + "/" + name)

seen = set()
for candidate in candidates:
    canonical = os.path.realpath(candidate)
    if canonical in seen:
        continue
    seen.add(canonical)
    authority = inspect_python(candidate)
    if authority is not None:
        emit("ready", authority)

uv_candidates = []
found_uv = shutil.which("uv", path=child_environment["PATH"])
if found_uv:
    uv_candidates.append(found_uv)
uv_candidates.extend((home + "/.local/bin/uv", home + "/.cargo/bin/uv"))
uv_seen = set()
validated_uv = []
for candidate in uv_candidates:
    canonical = os.path.realpath(candidate)
    if canonical in uv_seen:
        continue
    uv_seen.add(canonical)
    try:
        path, fd, _metadata, _mode, _digest = secure_executable(candidate)
        validated_uv.append((path, fd))
    except (OSError, ValueError):
        continue

if not validated_uv:
    uv_archives = {
        "amd64": (
            "https://github.com/astral-sh/uv/releases/download/0.11.28/uv-x86_64-unknown-linux-gnu.tar.gz",
            "e490a6464492183c5d4534a5527fb4440f7f2bb2f228162ad7e4afe076dc0224",
            "uv-x86_64-unknown-linux-gnu/uv",
        ),
        "x86_64": (
            "https://github.com/astral-sh/uv/releases/download/0.11.28/uv-x86_64-unknown-linux-gnu.tar.gz",
            "e490a6464492183c5d4534a5527fb4440f7f2bb2f228162ad7e4afe076dc0224",
            "uv-x86_64-unknown-linux-gnu/uv",
        ),
        "aarch64": (
            "https://github.com/astral-sh/uv/releases/download/0.11.28/uv-aarch64-unknown-linux-gnu.tar.gz",
            "03e9fe0a81b0718d0bc84625de3885df6cc3f89a8b6af6121d6b9f6113fb6533",
            "uv-aarch64-unknown-linux-gnu/uv",
        ),
        "arm64": (
            "https://github.com/astral-sh/uv/releases/download/0.11.28/uv-aarch64-unknown-linux-gnu.tar.gz",
            "03e9fe0a81b0718d0bc84625de3885df6cc3f89a8b6af6121d6b9f6113fb6533",
            "uv-aarch64-unknown-linux-gnu/uv",
        ),
    }
    archive = uv_archives.get(machine)
    if archive is None:
        emit("no_supported_python")
    url, expected_archive_digest, member_name = archive
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "OpenEvo-Desktop/1"})
        with urllib.request.urlopen(request, timeout=remaining(60)) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > max_uv_archive:
                raise ValueError
            archive_bytes = response.read(max_uv_archive + 1)
        if (len(archive_bytes) > max_uv_archive
                or hashlib.sha256(archive_bytes).hexdigest() != expected_archive_digest):
            raise ValueError
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as bundle:
            members = bundle.getmembers()
            if len(members) > 16:
                raise ValueError
            matches = [item for item in members if item.name == member_name]
            if (len(matches) != 1 or not matches[0].isfile()
                    or not 0 < matches[0].size <= max_executable):
                raise ValueError
            extracted = bundle.extractfile(matches[0])
            if extracted is None:
                raise ValueError
            uv_bytes = extracted.read(max_executable + 1)
            if len(uv_bytes) != matches[0].size:
                raise ValueError
        with tempfile.TemporaryDirectory(prefix="openevo-uv-") as temporary_root:
            temporary_path = os.path.join(temporary_root, "uv")
            write_fd = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o700,
            )
            try:
                offset = 0
                while offset < len(uv_bytes):
                    written = os.write(write_fd, uv_bytes[offset:])
                    if written <= 0:
                        raise OSError(errno.EIO, "uv bootstrap write failed")
                    offset += written
                os.fsync(write_fd)
            finally:
                os.close(write_fd)
            read_fd = os.open(temporary_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            metadata = os.fstat(read_fd)
            current = os.stat(temporary_path, follow_symlinks=False)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid
                    or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o700
                    or metadata.st_size != len(uv_bytes)
                    or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)):
                os.close(read_fd)
                raise ValueError
            validated_uv.append(("verified uv 0.11.28", read_fd))
    except (EOFError, OSError, TimeoutError, ValueError, tarfile.TarError, urllib.error.URLError):
        emit("python_provision_failed")

provision_attempted = False
try:
    for _uv_path, uv_fd in validated_uv:
        try:
            try:
                code, output = run_fd(uv_fd, ["python", "find", "3.11"], 30)
            except (ValueError, TimeoutError):
                code, output = 1, b""
            if code == 0:
                try:
                    candidate = output.decode("utf-8").strip()
                except UnicodeError:
                    candidate = ""
                authority = inspect_python(candidate)
                if authority is not None:
                    emit("ready", authority)
            provision_attempted = True
            try:
                code, _output = run_fd(uv_fd, ["python", "install", "3.11", "--no-progress"], 240)
            except (ValueError, TimeoutError):
                code = 1
            if code != 0:
                continue
            try:
                code, output = run_fd(uv_fd, ["python", "find", "3.11"], 30)
            except (ValueError, TimeoutError):
                continue
            if code != 0:
                continue
            try:
                candidate = output.decode("utf-8").strip()
            except UnicodeError:
                continue
            authority = inspect_python(candidate)
            if authority is not None:
                emit("ready", authority)
        finally:
            os.close(uv_fd)
finally:
    for _uv_path, uv_fd in validated_uv:
        try:
            os.close(uv_fd)
        except OSError:
            pass

emit("python_provision_failed" if provision_attempted else "no_supported_python")
""".strip()


_REMOTE_EXECUTOR_SCRIPT = r"""
import hashlib, json, os, stat, sys

(authority_id, path, expected_digest, device, inode, uid, mode, size,
 mtime_ns, ctime_ns, version, *target) = sys.argv[1:]
values = [device, inode, uid, mode, size, mtime_ns, ctime_ns]
if (len(authority_id) != 64 or any(c not in "0123456789abcdef" for c in authority_id)
        or len(expected_digest) != 64 or any(c not in "0123456789abcdef" for c in expected_digest)
        or len(target) < 3 or target[:2] != ["-I", "-c"]):
    raise SystemExit(70)
try:
    device, inode, uid, mode, size, mtime_ns, ctime_ns = [int(value) for value in values]
    version_parts = [int(value) for value in version.split(".")]
except ValueError:
    raise SystemExit(70)
if len(version_parts) != 3 or tuple(version_parts) < (3, 11, 0):
    raise SystemExit(70)

allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@%+=,-")
parts = path.split("/")[1:]
if (not path.startswith("/") or not parts
        or any(not part or part in {".", ".."} or any(c not in allowed for c in part) for part in parts)):
    raise SystemExit(70)
flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
dir_flags = flags | os.O_DIRECTORY
parent = os.open("/", dir_flags)
fd = -1
try:
    for part in parts[:-1]:
        child = os.open(part, dir_flags, dir_fd=parent)
        os.close(parent)
        parent = child
    fd = os.open(parts[-1], flags, dir_fd=parent)
    before = os.fstat(fd)
    current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
    expected = (device, inode, uid, mode, size, mtime_ns, ctime_ns)
    actual = (before.st_dev, before.st_ino, before.st_uid, stat.S_IMODE(before.st_mode), before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid not in {0, os.geteuid()}
            or before.st_nlink != 1 or actual != expected
            or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
            or mode & 0o022 or mode & 0o111 == 0):
        raise SystemExit(71)
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            raise SystemExit(72)
        digest.update(chunk)
        remaining -= len(chunk)
    if os.read(fd, 1) or digest.hexdigest() != expected_digest:
        raise SystemExit(72)
    after = os.fstat(fd)
    current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
    actual_after = (after.st_dev, after.st_ino, after.st_uid, stat.S_IMODE(after.st_mode), after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    canonical = json.dumps({
        "schema_version": 1, "executable_path": path, "executable_sha256": expected_digest,
        "device": device, "inode": inode, "uid": uid, "mode": mode, "byte_size": size,
        "mtime_ns": mtime_ns, "ctime_ns": ctime_ns, "version": version_parts,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    computed = hashlib.sha256(b"openevo-core-python-runtime-v1\0" + canonical).hexdigest()
    if (actual_after != expected or computed != authority_id
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)):
        raise SystemExit(73)
    os.set_inheritable(fd, True)
    os.execve("/proc/self/fd/" + str(fd), [path, *target], os.environ)
finally:
    if fd >= 0:
        os.close(fd)
    os.close(parent)
""".strip()


__all__ = (
    "CorePythonRuntimeAuthority",
    "CorePythonRuntimeSelection",
    "build_core_supervisor_runtime_preflight_command",
    "build_verified_python_command",
    "parse_core_supervisor_runtime_preflight",
)
