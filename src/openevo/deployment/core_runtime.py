from __future__ import annotations

import json
import shlex

from pydantic import SecretStr


_MAX_RESPONSE_BYTES = 512
_REASONS = {
    "ready",
    "unsupported_platform",
    "python_version_unsupported",
    "missing_python_pidfd_api",
    "process_identity_unavailable",
    "pidfd_probe_failed",
}


def build_core_supervisor_runtime_preflight_command() -> str:
    return f"python3 -I -c {shlex.quote(_REMOTE_PREFLIGHT_SCRIPT)}"


def parse_core_supervisor_runtime_preflight(payload: SecretStr) -> str:
    if not isinstance(payload, SecretStr):
        raise ValueError("Core supervisor preflight response is invalid")
    try:
        encoded = payload.get_secret_value().encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Core supervisor preflight response is invalid") from exc
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise ValueError("Core supervisor preflight response is invalid")
    try:
        value = json.loads(encoded, object_pairs_hook=_closed_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Core supervisor preflight response is invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"reason", "schema_version", "supported"}
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or type(value.get("supported")) is not bool
        or not isinstance(value.get("reason"), str)
        or value["reason"] not in _REASONS
        or value["supported"] is not (value["reason"] == "ready")
    ):
        raise ValueError("Core supervisor preflight response is invalid")
    return value["reason"]


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


_REMOTE_PREFLIGHT_SCRIPT = r"""
import json, os, signal, sys

reason = "ready"
if sys.platform != "linux":
    reason = "unsupported_platform"
elif sys.version_info < (3, 11):
    reason = "python_version_unsupported"
elif not callable(getattr(os, "pidfd_open", None)) or not callable(getattr(signal, "pidfd_send_signal", None)):
    reason = "missing_python_pidfd_api"
else:
    try:
        with open("/proc/sys/kernel/random/boot_id", "r", encoding="ascii") as boot_id_file:
            boot_id = boot_id_file.read(64).strip()
        groups = boot_id.split("-")
        if ([len(group) for group in groups] != [8, 4, 4, 4, 12]
                or any(any(character not in "0123456789abcdef" for character in group) for group in groups)):
            reason = "process_identity_unavailable"
    except (OSError, UnicodeError):
        reason = "process_identity_unavailable"
if reason == "ready":
    pidfd = -1
    try:
        pidfd = os.pidfd_open(os.getpid(), 0)
        signal.pidfd_send_signal(pidfd, 0)
    except (OSError, TypeError, ValueError):
        reason = "pidfd_probe_failed"
    finally:
        if pidfd >= 0:
            os.close(pidfd)

print(json.dumps({
    "schema_version": 1,
    "supported": reason == "ready",
    "reason": reason,
}, sort_keys=True, separators=(",", ":")))
""".strip()


__all__ = (
    "build_core_supervisor_runtime_preflight_command",
    "parse_core_supervisor_runtime_preflight",
)
