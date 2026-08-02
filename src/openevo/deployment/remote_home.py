"""Private, generation-bound authority for one remote SSH account home."""

from __future__ import annotations

from dataclasses import dataclass, field
import posixpath
import re
import shlex


REMOTE_HOME_PROBE_OUTPUT_LIMIT = 8_192

_MAX_HOME_BYTES = 4_096
_MAX_REMOTE_COMMAND_BYTES = 1_048_576
_MAX_SAFE_GENERATION = 9_007_199_254_740_991
_MAX_LINUX_UID = 4_294_967_294
_PROFILE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_REMOTE_USER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]{0,127}\Z", re.ASCII)
_HOME_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9._@%+=,-]+\Z", re.ASCII)
_UID_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z", re.ASCII)
_PROBE_VERSION = "openevo-remote-home-v1"
_WORKSPACE_SUFFIX = "/.openevo/workspaces"
_DAEMON_BUNDLE_SUFFIX = "/.openevo/daemon-bundles"
_AUTHORITY_SEAL = object()

_PROBE_INVALID_MESSAGE = "Remote account home probe is invalid."
_AUTHORITY_INVALID_MESSAGE = "Remote account authority is invalid."
_BINDING_INVALID_MESSAGE = "Remote account authority binding is invalid."
_COMMAND_INVALID_MESSAGE = "Remote account command is invalid."


class RemoteHomeAuthorityError(ValueError):
    """A closed error that never retains rejected remote account data."""


@dataclass(frozen=True, slots=True, repr=False, init=False)
class RemoteHomeAuthority:
    """Process-local authority sealed from one private remote account probe."""

    profile_id: str
    connection_generation: int
    remote_user: str
    uid: int
    _home: str = field(repr=False)
    _seal: object = field(repr=False, compare=False)

    def __new__(
        cls,
        *,
        _factory_token: object | None = None,
    ) -> RemoteHomeAuthority:
        if cls is not RemoteHomeAuthority or _factory_token is not _AUTHORITY_SEAL:
            raise RemoteHomeAuthorityError(_AUTHORITY_INVALID_MESSAGE)
        return object.__new__(cls)

    def __init__(self, *, _factory_token: object | None = None) -> None:
        if _factory_token is not _AUTHORITY_SEAL:
            raise RemoteHomeAuthorityError(_AUTHORITY_INVALID_MESSAGE)

    @property
    def workspace_root(self) -> str:
        self._require_sealed()
        return f"{self._home}{_WORKSPACE_SUFFIX}"

    @property
    def daemon_bundle_root(self) -> str:
        self._require_sealed()
        return f"{self._home}{_DAEMON_BUNDLE_SUFFIX}"

    def matches(
        self,
        *,
        profile_id: str,
        connection_generation: int,
        remote_user: str,
    ) -> bool:
        self._require_sealed()
        return (
            type(profile_id) is str
            and type(connection_generation) is int
            and type(remote_user) is str
            and profile_id == self.profile_id
            and connection_generation == self.connection_generation
            and remote_user == self.remote_user
        )

    def require_binding(
        self,
        *,
        profile_id: str,
        connection_generation: int,
        remote_user: str,
        workspace_root: str,
    ) -> None:
        if not self.matches(
            profile_id=profile_id,
            connection_generation=connection_generation,
            remote_user=remote_user,
        ) or type(workspace_root) is not str or workspace_root != self.workspace_root:
            raise RemoteHomeAuthorityError(_BINDING_INVALID_MESSAGE)

    def _require_sealed(self) -> None:
        if self._seal is not _AUTHORITY_SEAL:
            raise RemoteHomeAuthorityError(_AUTHORITY_INVALID_MESSAGE)

    def __repr__(self) -> str:
        return "RemoteHomeAuthority(<sealed>)"

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("Remote account authority cannot be serialized.")


def parse_remote_home_probe(
    *,
    profile_id: str,
    connection_generation: int,
    return_code: int,
    stdout: bytes,
    stderr: bytes,
) -> RemoteHomeAuthority:
    """Parse one exact private probe record and seal its local binding."""

    try:
        if (
            type(stdout) is not bytes
            or type(stderr) is not bytes
            or type(return_code) is not int
            or isinstance(return_code, bool)
            or return_code != 0
            or len(stdout) + len(stderr) > REMOTE_HOME_PROBE_OUTPUT_LIMIT
            or not stdout
            or not stdout.endswith(b"\n")
            or not _valid_profile_id(profile_id)
            or not _valid_generation(connection_generation)
        ):
            raise ValueError
        decoded = stdout.decode("utf-8", errors="strict")
        fields = decoded[:-1].split("\n")
        if len(fields) != 9 or any(_contains_control(field) for field in fields):
            raise ValueError
        (
            version,
            id_user,
            id_uid_text,
            nss_user,
            nss_uid_text,
            home,
            physical_home,
            owner_uid_text,
            writable,
        ) = fields
        id_uid = _parse_uid(id_uid_text)
        nss_uid = _parse_uid(nss_uid_text)
        owner_uid = _parse_uid(owner_uid_text)
        if (
            version != _PROBE_VERSION
            or not _valid_remote_user(id_user)
            or nss_user != id_user
            or nss_uid != id_uid
            or owner_uid != id_uid
            or not _valid_home(home)
            or physical_home != home
            or writable != "1"
        ):
            raise ValueError
        return _new_authority(
            profile_id=profile_id,
            connection_generation=connection_generation,
            remote_user=id_user,
            uid=id_uid,
            home=home,
        )
    except (RemoteHomeAuthorityError, TypeError, UnicodeError, ValueError, OverflowError):
        raise RemoteHomeAuthorityError(_PROBE_INVALID_MESSAGE) from None


def build_remote_home_probe_command() -> str:
    """Return the fixed, private account probe for one authenticated master."""

    return shlex.join(
        [
            "/bin/sh",
            "-c",
            _REMOTE_HOME_PROBE_SCRIPT,
            "openevo-remote-home-probe-v1",
        ]
    )


def build_remote_home_guarded_command(
    authority: RemoteHomeAuthority,
    remote_command: str,
) -> str:
    """Guard one trusted rich command with the sealed remote account binding."""

    if (
        type(authority) is not RemoteHomeAuthority
        or authority._seal is not _AUTHORITY_SEAL
        or not _valid_remote_command(remote_command)
    ):
        raise RemoteHomeAuthorityError(_COMMAND_INVALID_MESSAGE)
    return shlex.join(
        [
            "/bin/sh",
            "-c",
            _REMOTE_HOME_GUARD_SCRIPT,
            "openevo-remote-home-guard-v1",
            authority.remote_user,
            str(authority.uid),
            authority._home,
            remote_command,
        ]
    )


def _valid_profile_id(value: object) -> bool:
    return type(value) is str and _PROFILE_ID_PATTERN.fullmatch(value) is not None


def _valid_remote_command(value: object) -> bool:
    if type(value) is not str or not value or "\x00" in value:
        return False
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return len(encoded) <= _MAX_REMOTE_COMMAND_BYTES


def _new_authority(
    *,
    profile_id: str,
    connection_generation: int,
    remote_user: str,
    uid: int,
    home: str,
) -> RemoteHomeAuthority:
    if (
        not _valid_profile_id(profile_id)
        or not _valid_generation(connection_generation)
        or not _valid_remote_user(remote_user)
        or not _valid_uid(uid)
        or not _valid_home(home)
    ):
        raise RemoteHomeAuthorityError(_AUTHORITY_INVALID_MESSAGE)
    authority = RemoteHomeAuthority(_factory_token=_AUTHORITY_SEAL)
    object.__setattr__(authority, "profile_id", profile_id)
    object.__setattr__(authority, "connection_generation", connection_generation)
    object.__setattr__(authority, "remote_user", remote_user)
    object.__setattr__(authority, "uid", uid)
    object.__setattr__(authority, "_home", home)
    object.__setattr__(authority, "_seal", _AUTHORITY_SEAL)
    return authority


def _valid_generation(value: object) -> bool:
    return type(value) is int and 1 <= value <= _MAX_SAFE_GENERATION


def _valid_remote_user(value: object) -> bool:
    return type(value) is str and _REMOTE_USER_PATTERN.fullmatch(value) is not None


def _valid_uid(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_LINUX_UID


def _parse_uid(value: str) -> int:
    if _UID_PATTERN.fullmatch(value) is None:
        raise ValueError
    parsed = int(value)
    if not _valid_uid(parsed):
        raise ValueError
    return parsed


def _valid_home(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    if (
        not encoded
        or len(encoded) > _MAX_HOME_BYTES
        or not value.startswith("/")
        or value == "/"
        or value.endswith("/")
        or posixpath.normpath(value) != value
        or _contains_control(value)
    ):
        return False
    components = value[1:].split("/")
    return bool(components) and all(
        component not in {"", ".", ".."}
        and _HOME_COMPONENT_PATTERN.fullmatch(component) is not None
        for component in components
    )


def _contains_control(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


_REMOTE_HOME_PROBE_SCRIPT = r"""
set -eu
set -f
LC_ALL=C
export LC_ALL

user=$(id -un 2>/dev/null)
uid=$(id -u 2>/dev/null)
passwd_record=$(getent passwd "$uid" 2>/dev/null)
case "$passwd_record" in
  *'
'*) exit 64 ;;
esac

old_ifs=$IFS
IFS=:
set -- $passwd_record
IFS=$old_ifs
[ "$#" -eq 7 ] || exit 64
nss_user=$1
nss_uid=$3
home=$6
[ "$nss_user" = "$user" ] || exit 64
[ "$nss_uid" = "$uid" ] || exit 64
[ -n "$home" ] || exit 64
[ -d "$home" ] || exit 64
[ -w "$home" ] || exit 64

physical_home=$(
  CDPATH=
  export CDPATH
  cd -P "$home" 2>/dev/null
  pwd -P
)
owner_uid=$(stat -c %u -- "$home" 2>/dev/null)

printf '%s\n' \
  openevo-remote-home-v1 \
  "$user" \
  "$uid" \
  "$nss_user" \
  "$nss_uid" \
  "$home" \
  "$physical_home" \
  "$owner_uid" \
  1
""".strip()


_REMOTE_HOME_GUARD_SCRIPT = r"""
set -eu
set -f
LC_ALL=C
export LC_ALL

expected_user=$1
expected_uid=$2
expected_home=$3
remote_command=$4

user=$(id -un 2>/dev/null)
uid=$(id -u 2>/dev/null)
[ "$user" = "$expected_user" ] || exit 64
[ "$uid" = "$expected_uid" ] || exit 64

passwd_record=$(getent passwd "$uid" 2>/dev/null)
case "$passwd_record" in
  *'
'*) exit 64 ;;
esac
old_ifs=$IFS
IFS=:
set -- $passwd_record
IFS=$old_ifs
[ "$#" -eq 7 ] || exit 64
nss_user=$1
nss_uid=$3
home=$6
[ "$nss_user" = "$expected_user" ] || exit 64
[ "$nss_uid" = "$expected_uid" ] || exit 64
[ "$home" = "$expected_home" ] || exit 64
[ -d "$home" ] || exit 64
[ -w "$home" ] || exit 64

physical_home=$(
  CDPATH=
  export CDPATH
  cd -P "$home" 2>/dev/null
  pwd -P
)
[ "$physical_home" = "$expected_home" ] || exit 64
owner_uid=$(stat -c %u -- "$home" 2>/dev/null)
[ "$owner_uid" = "$expected_uid" ] || exit 64

exec /bin/sh -c "$remote_command"
""".strip()


__all__ = (
    "REMOTE_HOME_PROBE_OUTPUT_LIMIT",
    "RemoteHomeAuthority",
    "RemoteHomeAuthorityError",
    "build_remote_home_guarded_command",
    "build_remote_home_probe_command",
    "parse_remote_home_probe",
)
