from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
import time
from typing import Literal, Protocol

from desktop.sidecar.contracts.v1.models import HostKeyAcceptV1, RemoteProfileV1
from openevo.deployment.host_keys import (
    HostKeyCandidate,
    HostKeyAlgorithm,
    PendingHostKeyProbe,
    ProviderKnownHostStore,
    TrustedKnownHostsBinding,
)
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.profile import ProxySettings, RemoteProfileConfig, SSHAuthConfig
from openevo.deployment.ssh import SshRemoteExecutorTransport


class RemoteLifecycleError(RuntimeError):
    """A release-safe remote lifecycle failure."""


class RemoteCredentialUnavailableError(RemoteLifecycleError):
    """The selected authentication mode has no native credential material."""


class RemoteLifecycleSupersededError(RemoteLifecycleError):
    """A newer lifecycle mutation superseded an in-flight operation."""


class RemoteConnectionFailedError(RemoteLifecycleError):
    """The trusted SSH connection could not be established."""


class _HostKeyStore(Protocol):
    def probe(
        self, profile: RemoteProfileConfig, *, timeout_seconds: float = 10.0
    ) -> PendingHostKeyProbe: ...

    def confirm(
        self,
        pending: PendingHostKeyProbe,
        *,
        profile: RemoteProfileConfig,
        algorithm: HostKeyAlgorithm,
        fingerprint: str,
        timeout_seconds: float = 10.0,
    ) -> TrustedKnownHostsBinding: ...

    def load(
        self, profile: RemoteProfileConfig, *, expected_fingerprint: str
    ) -> TrustedKnownHostsBinding | None: ...


class _RemoteTransport(Protocol):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult: ...

    def close(self) -> None: ...


AuthResolver = Callable[[RemoteProfileV1], SSHAuthConfig]
TransportFactory = Callable[[RemoteProfileConfig, TrustedKnownHostsBinding], _RemoteTransport]
_MAX_CONNECTION_DEADLINE_SECONDS = 12.0


@dataclass(frozen=True)
class RemoteLifecycleSnapshot:
    profile_id: str | None
    state: Literal["disconnected", "connecting", "host_key_required", "connected", "failed"]
    host_key_candidate: HostKeyCandidate | None = None
    failure_code: str | None = None


@dataclass(frozen=True)
class RemoteConnectionResult:
    profile_id: str
    state: Literal["host_key_required", "connected"]
    host_key_candidate: HostKeyCandidate | None = None


@dataclass
class _ActiveRemote:
    profile: RemoteProfileConfig
    binding: TrustedKnownHostsBinding
    transport: _RemoteTransport


def ssh_agent_auth_for_release(profile: RemoteProfileV1) -> SSHAuthConfig:
    if profile.authentication_kind != "ssh_agent":
        raise RemoteCredentialUnavailableError(
            "The selected SSH authentication mode requires the native credential broker."
        )
    return SSHAuthConfig(method="ssh_agent")


def remote_profile_config(
    profile: RemoteProfileV1,
    *,
    auth_resolver: AuthResolver = ssh_agent_auth_for_release,
) -> RemoteProfileConfig:
    return RemoteProfileConfig(
        id=profile.profile_id,
        name=profile.name,
        host=profile.host,
        port=profile.port,
        user=profile.user,
        auth=auth_resolver(profile),
        proxy=ProxySettings(
            http_proxy=profile.proxy.http_url,
            https_proxy=profile.proxy.https_url,
            no_proxy=",".join(profile.proxy.no_proxy) or None,
        ),
    )


def _default_transport_factory(
    profile: RemoteProfileConfig,
    binding: TrustedKnownHostsBinding,
) -> _RemoteTransport:
    return SshRemoteExecutorTransport(profile, trusted_host=binding)


class DesktopRemoteLifecycle:
    """Own one trusted remote SSH transport for the release sidecar."""

    def __init__(
        self,
        host_keys: ProviderKnownHostStore,
        *,
        auth_resolver: AuthResolver = ssh_agent_auth_for_release,
        transport_factory: TransportFactory = _default_transport_factory,
        connection_timeout_seconds: float = _MAX_CONNECTION_DEADLINE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 < connection_timeout_seconds <= _MAX_CONNECTION_DEADLINE_SECONDS:
            raise ValueError("connection timeout must be between zero and 12 seconds")
        self._host_keys: _HostKeyStore = host_keys
        self._auth_resolver = auth_resolver
        self._transport_factory = transport_factory
        self._connection_timeout_seconds = connection_timeout_seconds
        self._monotonic = monotonic
        self._lock = Lock()
        self._generation = 0
        self._snapshot = RemoteLifecycleSnapshot(None, "disconnected")
        self._pending: dict[str, tuple[RemoteProfileConfig, PendingHostKeyProbe]] = {}
        self._active: _ActiveRemote | None = None
        self._candidate: tuple[int, _ActiveRemote] | None = None

    def snapshot(self) -> RemoteLifecycleSnapshot:
        with self._lock:
            return self._snapshot

    def active_transport(self, profile_id: str) -> _RemoteTransport:
        with self._lock:
            active = self._active
            if active is None or active.profile.id != profile_id:
                raise RemoteConnectionFailedError("The requested remote profile is not connected.")
            return active.transport

    def connect(self, profile: RemoteProfileV1) -> RemoteConnectionResult:
        deadline = self._deadline()
        generation, displaced = self._begin(profile.profile_id)
        self._close_remotes(displaced)
        try:
            config = remote_profile_config(profile, auth_resolver=self._auth_resolver)
            self._remaining(deadline)
            binding = self._load_binding(config, profile.host_key_fingerprint)
            self._remaining(deadline)
            if binding is None:
                pending = self._host_keys.probe(config, timeout_seconds=self._remaining(deadline))
                self._remaining(deadline)
                candidate = self._preferred_candidate(pending)
                self._publish_pending(generation, config, pending, candidate)
                return RemoteConnectionResult(
                    profile_id=profile.profile_id,
                    state="host_key_required",
                    host_key_candidate=candidate,
                )
            return self._connect_with_binding(generation, config, binding, deadline=deadline)
        except RemoteLifecycleSupersededError:
            raise
        except Exception as exc:
            self._publish_failure(generation, profile.profile_id, "ssh_connection_failed")
            if isinstance(exc, RemoteLifecycleError):
                raise
            raise RemoteConnectionFailedError(
                "The SSH connection could not be established."
            ) from exc

    def accept_host_key(
        self,
        profile: RemoteProfileV1,
        request: HostKeyAcceptV1,
    ) -> RemoteConnectionResult:
        deadline = self._deadline()
        with self._lock:
            pending_entry = self._pending.get(profile.profile_id)
            if pending_entry is None or self._snapshot.profile_id != profile.profile_id:
                raise RemoteConnectionFailedError("The pending SSH host key is no longer current.")
            self._generation += 1
            generation = self._generation
            displaced = self._owned_remotes_locked()
            self._active = None
            self._candidate = None
            self._snapshot = RemoteLifecycleSnapshot(profile.profile_id, "connecting")
        self._close_remotes(displaced)
        try:
            config = remote_profile_config(profile, auth_resolver=self._auth_resolver)
            self._remaining(deadline)
            if pending_entry[0] != config:
                raise RemoteConnectionFailedError("The pending SSH host key is no longer current.")
            binding = self._host_keys.confirm(
                pending_entry[1],
                profile=config,
                algorithm=request.algorithm,
                fingerprint=request.fingerprint,
                timeout_seconds=self._remaining(deadline),
            )
            self._remaining(deadline)
            return self._connect_with_binding(generation, config, binding, deadline=deadline)
        except RemoteLifecycleSupersededError:
            raise
        except Exception as exc:
            self._publish_failure(generation, profile.profile_id, "ssh_connection_failed")
            if isinstance(exc, RemoteLifecycleError):
                raise
            raise RemoteConnectionFailedError("The SSH host key could not be confirmed.") from exc

    def disconnect(self, profile_id: str | None = None) -> None:
        with self._lock:
            if profile_id is not None and self._snapshot.profile_id not in {None, profile_id}:
                raise RemoteConnectionFailedError("Another remote profile owns the connection.")
            self._generation += 1
            displaced = self._owned_remotes_locked()
            self._active = None
            self._candidate = None
            self._pending.clear()
            self._snapshot = RemoteLifecycleSnapshot(None, "disconnected")
        self._close_remotes(displaced)

    close = disconnect

    def _begin(self, profile_id: str) -> tuple[int, tuple[_ActiveRemote, ...]]:
        with self._lock:
            self._generation += 1
            generation = self._generation
            displaced = self._owned_remotes_locked()
            self._active = None
            self._candidate = None
            self._pending.clear()
            self._snapshot = RemoteLifecycleSnapshot(profile_id, "connecting")
            return generation, displaced

    def _load_binding(
        self,
        profile: RemoteProfileConfig,
        fingerprint: str | None,
    ) -> TrustedKnownHostsBinding | None:
        if fingerprint is None:
            return None
        return self._host_keys.load(profile, expected_fingerprint=fingerprint)

    @staticmethod
    def _preferred_candidate(pending: PendingHostKeyProbe) -> HostKeyCandidate:
        if not pending.candidates:
            raise RemoteConnectionFailedError(
                "The server did not present a supported SSH host key."
            )
        priorities = {
            "ssh-ed25519": 0,
            "ecdsa-sha2-nistp256": 1,
            "rsa-sha2-512": 2,
        }
        return min(
            pending.candidates,
            key=lambda candidate: (priorities[candidate.algorithm], candidate.fingerprint),
        )

    def _publish_pending(
        self,
        generation: int,
        profile: RemoteProfileConfig,
        pending: PendingHostKeyProbe,
        candidate: HostKeyCandidate,
    ) -> None:
        with self._lock:
            self._require_generation(generation)
            self._pending[profile.id] = (profile, pending)
            self._snapshot = RemoteLifecycleSnapshot(
                profile.id,
                "host_key_required",
                host_key_candidate=candidate,
            )

    def _connect_with_binding(
        self,
        generation: int,
        profile: RemoteProfileConfig,
        binding: TrustedKnownHostsBinding,
        *,
        deadline: float,
    ) -> RemoteConnectionResult:
        self._remaining(deadline)
        transport = self._transport_factory(profile, binding)
        candidate = _ActiveRemote(profile=profile, binding=binding, transport=transport)
        try:
            with self._lock:
                self._require_generation(generation)
                self._candidate = (generation, candidate)
            result = transport.run("true", timeout_seconds=self._remaining(deadline))
            self._remaining(deadline)
            if not result.ok:
                raise RemoteConnectionFailedError("The SSH connectivity check failed.")
            with self._lock:
                self._require_generation(generation)
                bound_candidate = self._candidate
                if (
                    bound_candidate is None
                    or bound_candidate[0] != generation
                    or bound_candidate[1] is not candidate
                ):
                    raise RemoteLifecycleSupersededError(
                        "A newer remote lifecycle operation superseded this result."
                    )
                self._candidate = None
                self._pending.pop(profile.id, None)
                self._active = candidate
                self._snapshot = RemoteLifecycleSnapshot(profile.id, "connected")
            return RemoteConnectionResult(profile.id, "connected")
        except BaseException:
            with self._lock:
                superseded = generation != self._generation
                bound_candidate = self._candidate
                if (
                    bound_candidate is not None
                    and bound_candidate[0] == generation
                    and bound_candidate[1] is candidate
                ):
                    self._candidate = None
            self._close_transport(transport)
            if superseded:
                raise RemoteLifecycleSupersededError(
                    "A newer remote lifecycle operation superseded this result."
                ) from None
            raise

    def _deadline(self) -> float:
        return self._monotonic() + self._connection_timeout_seconds

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise RemoteConnectionFailedError("The SSH connection deadline expired.")
        return remaining

    def _publish_failure(self, generation: int, profile_id: str, code: str) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._active = None
            self._snapshot = RemoteLifecycleSnapshot(
                profile_id,
                "failed",
                failure_code=code,
            )

    def _require_generation(self, generation: int) -> None:
        if generation != self._generation:
            raise RemoteLifecycleSupersededError(
                "A newer remote lifecycle operation superseded this result."
            )

    def _owned_remotes_locked(self) -> tuple[_ActiveRemote, ...]:
        remotes: list[_ActiveRemote] = []
        if self._active is not None:
            remotes.append(self._active)
        if self._candidate is not None and self._candidate[1] not in remotes:
            remotes.append(self._candidate[1])
        return tuple(remotes)

    @classmethod
    def _close_remotes(cls, remotes: tuple[_ActiveRemote, ...]) -> None:
        for remote in remotes:
            cls._close_transport(remote.transport)

    @staticmethod
    def _close_transport(transport: _RemoteTransport) -> None:
        close = getattr(transport, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


__all__ = (
    "DesktopRemoteLifecycle",
    "RemoteConnectionFailedError",
    "RemoteConnectionResult",
    "RemoteCredentialUnavailableError",
    "RemoteLifecycleSnapshot",
    "RemoteLifecycleSupersededError",
    "remote_profile_config",
    "ssh_agent_auth_for_release",
)
