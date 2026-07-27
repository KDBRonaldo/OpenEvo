from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
import re
import time
from typing import Literal, Protocol

from desktop.sidecar.contracts.v1.models import HostKeyAcceptV1, RemoteProfileV1
from desktop.sidecar.contracts.v2 import models as local_v2
from desktop.sidecar.askpass_broker import AskpassPromptObservation
from desktop.sidecar.system_ssh_session import (
    AskpassHelperAuthority,
    SystemOpenSshFollowerTransportAuthority,
    SystemOpenSshHostTrust,
    SystemOpenSshSession,
    SystemOpenSshSessionError,
    SystemOpenSshSessionOwner,
)
from desktop.sidecar.lifecycle_logs_v2 import LifecycleRawOutputObserverV2
from openevo.deployment.host_keys import (
    HostKeyCandidate,
    HostKeyAlgorithm,
    PendingSystemHostKeyReview,
    PendingHostKeyProbe,
    ProviderKnownHostStore,
    TrustedKnownHostsBinding,
)
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.profile import (
    ProxySettings,
    RemoteProfileConfig,
    SSHAuthConfig,
    SystemOpenSshAliasProfile,
)
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
_CREDENTIAL_UNAVAILABLE_MESSAGE = (
    "The selected SSH authentication mode requires the native credential broker."
)
_SUPERSEDED_MESSAGE = "A newer remote lifecycle operation superseded this result."
_SAFE_CONNECTION_FAILURE_MESSAGES = frozenset(
    {
        "The requested remote profile is not connected.",
        "The SSH connection could not be established.",
        "The pending SSH host key is no longer current.",
        "The SSH host key could not be confirmed.",
        "Another remote profile owns the connection.",
        "The server did not present a supported SSH host key.",
        "The SSH connectivity check failed.",
        "The SSH connection deadline expired.",
    }
)


def _detached_lifecycle_error(
    error: RemoteLifecycleError,
    *,
    fallback: str,
) -> RemoteLifecycleError:
    if type(error) is RemoteCredentialUnavailableError:
        return RemoteCredentialUnavailableError(_CREDENTIAL_UNAVAILABLE_MESSAGE)
    if type(error) is RemoteLifecycleSupersededError:
        return RemoteLifecycleSupersededError(_SUPERSEDED_MESSAGE)
    message = error.args[0] if len(error.args) == 1 else None
    if (
        type(error) is RemoteConnectionFailedError
        and type(message) is str
        and message in _SAFE_CONNECTION_FAILURE_MESSAGES
    ):
        return RemoteConnectionFailedError(message)
    return RemoteConnectionFailedError(fallback)


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
        raise RemoteCredentialUnavailableError(_CREDENTIAL_UNAVAILABLE_MESSAGE)
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
        failure: RemoteLifecycleError | None = None
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
        except RemoteLifecycleSupersededError as error:
            failure = _detached_lifecycle_error(
                error,
                fallback="The SSH connection could not be established.",
            )
        except RemoteLifecycleError as error:
            self._publish_failure(generation, profile.profile_id, "ssh_connection_failed")
            failure = _detached_lifecycle_error(
                error,
                fallback="The SSH connection could not be established.",
            )
        except Exception:
            self._publish_failure(generation, profile.profile_id, "ssh_connection_failed")
        if failure is not None:
            raise failure
        raise RemoteConnectionFailedError("The SSH connection could not be established.")

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
        failure: RemoteLifecycleError | None = None
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
        except RemoteLifecycleSupersededError as error:
            failure = _detached_lifecycle_error(
                error,
                fallback="The SSH host key could not be confirmed.",
            )
        except RemoteLifecycleError as error:
            self._publish_failure(generation, profile.profile_id, "ssh_connection_failed")
            failure = _detached_lifecycle_error(
                error,
                fallback="The SSH host key could not be confirmed.",
            )
        except Exception:
            self._publish_failure(generation, profile.profile_id, "ssh_connection_failed")
        if failure is not None:
            raise failure
        raise RemoteConnectionFailedError("The SSH host key could not be confirmed.")

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
            raise RemoteLifecycleSupersededError(_SUPERSEDED_MESSAGE)

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


class _SystemSessionOwnerV2(Protocol):
    def connect(
        self,
        profile: SystemOpenSshAliasProfile,
        *,
        connection_generation: int,
        prompt_observer: Callable[[AskpassPromptObservation], None] | None = None,
        output_observer: LifecycleRawOutputObserverV2 | None = None,
    ) -> object: ...

    def active_session(self) -> SystemOpenSshSession: ...

    def disconnect(self) -> None: ...

    def close(self) -> None: ...


class _SystemHostTrustV2(Protocol):
    def replace_changed_key(
        self,
        review: PendingSystemHostKeyReview,
        *,
        profile: SystemOpenSshAliasProfile,
        connection_generation: int,
        review_id: str,
        review_sha256: str,
        timeout_seconds: float = 5.0,
    ) -> None: ...

    def close(self) -> None: ...


SystemTransportFactoryV2 = Callable[[RemoteProfileConfig, object, str], _RemoteTransport]
SystemPromptObserverV2 = Callable[[str, AskpassPromptObservation], None]
_SYSTEM_REMOTE_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]{0,127}\Z", re.ASCII)


@dataclass(slots=True)
class _ActiveSystemRemoteV2:
    profile_id: str
    connection_generation: int
    transport: _RemoteTransport


def _system_transport_factory_v2(
    config: RemoteProfileConfig,
    session: object,
    remote_user: str,
) -> _RemoteTransport:
    if type(session) is not SystemOpenSshSession:
        raise TypeError("system OpenSSH lifecycle requires an exact owned session")
    authority = SystemOpenSshFollowerTransportAuthority(
        session,
        remote_user=remote_user,
    )
    return SshRemoteExecutorTransport(
        config,
        system_openssh_authority=authority,
    )


class SystemOpenSshRemoteLifecycleV2:
    """Own one generation-bound rich transport behind a literal SSH alias."""

    def __init__(
        self,
        session_owner: SystemOpenSshSessionOwner,
        host_trust: SystemOpenSshHostTrust,
        *,
        transport_factory: SystemTransportFactoryV2 = _system_transport_factory_v2,
        discovery_timeout_seconds: float = 30.0,
        owned_askpass_helper: AskpassHelperAuthority | None = None,
        output_observer: LifecycleRawOutputObserverV2 | None = None,
    ) -> None:
        if any(
            not callable(getattr(session_owner, method, None))
            for method in ("connect", "active_session", "disconnect", "close")
        ):
            raise TypeError("system OpenSSH session owner is invalid")
        if any(
            not callable(getattr(host_trust, method, None))
            for method in ("replace_changed_key", "close")
        ):
            raise TypeError("system OpenSSH host trust authority is invalid")
        if not callable(transport_factory):
            raise TypeError("system OpenSSH transport factory is invalid")
        if output_observer is not None and not callable(output_observer):
            raise TypeError("system OpenSSH output observer is invalid")
        if (
            owned_askpass_helper is not None
            and type(owned_askpass_helper) is not AskpassHelperAuthority
        ):
            raise TypeError("owned system OpenSSH askpass helper is invalid")
        if (
            isinstance(discovery_timeout_seconds, bool)
            or not isinstance(discovery_timeout_seconds, (int, float))
            or not 0 < discovery_timeout_seconds <= 30.0
        ):
            raise ValueError("system OpenSSH user discovery timeout is invalid")
        self._session_owner: _SystemSessionOwnerV2 = session_owner
        self._host_trust: _SystemHostTrustV2 = host_trust
        self._owned_askpass_helper = owned_askpass_helper
        self._transport_factory = transport_factory
        self._discovery_timeout_seconds = float(discovery_timeout_seconds)
        self._transition = Lock()
        self._state = Lock()
        self._active: _ActiveSystemRemoteV2 | None = None
        self._pending_reviews: dict[str, PendingSystemHostKeyReview] = {}
        self._prompt_observer: SystemPromptObserverV2 | None = None
        self._output_observer = output_observer
        self._closed = False

    def set_prompt_observer(self, observer: SystemPromptObserverV2) -> None:
        if not callable(observer):
            raise TypeError("system OpenSSH prompt observer is invalid")
        with self._state:
            if self._closed or self._active is not None or self._prompt_observer is not None:
                raise RemoteConnectionFailedError(
                    "The system SSH prompt observer cannot be changed."
                )
            self._prompt_observer = observer

    def set_output_observer(self, observer: LifecycleRawOutputObserverV2) -> None:
        if not callable(observer):
            raise TypeError("system OpenSSH output observer is invalid")
        with self._state:
            if self._closed or self._active is not None or self._output_observer is not None:
                raise RemoteConnectionFailedError(
                    "The system SSH output observer cannot be changed."
                )
            self._output_observer = observer

    def connect(self, profile: local_v2.RemoteWorkspaceProfileV2) -> None:
        self._validate_connect_profile(profile)
        with self._transition:
            self._require_open()
            self._close_active()
            self._connect_locked(profile)

    def active_transport(
        self,
        profile_id: str,
        profile_connection_generation: int,
    ) -> _RemoteTransport:
        with self._state:
            active = self._active
            if (
                self._closed
                or active is None
                or active.profile_id != profile_id
                or active.connection_generation != profile_connection_generation
            ):
                raise RemoteConnectionFailedError("The requested remote profile is not connected.")
            return active.transport

    def disconnect(self, profile_id: str, connection_generation: int) -> None:
        with self._transition:
            self._require_open()
            with self._state:
                active = self._active
                if (
                    active is None
                    or active.profile_id != profile_id
                    or active.connection_generation + 1 != connection_generation
                ):
                    raise RemoteConnectionFailedError(
                        "The requested remote profile is not connected."
                    )
            self._close_active()
            self._pending_reviews.pop(profile_id, None)

    def review_host_key(
        self,
        profile: local_v2.RemoteWorkspaceProfileV2,
        request: local_v2.HostKeyReviewRequestV2,
    ) -> Literal["connected", "rejected"]:
        self._validate_connect_profile(profile)
        if type(request) is not local_v2.HostKeyReviewRequestV2:
            raise TypeError("host-key review request has the wrong type")
        with self._transition:
            self._require_open()
            pending = self._pending_reviews.get(profile.profile_id)
            if (
                pending is None
                or pending.profile_id != profile.profile_id
                or pending.connection_generation != request.expected_connection_generation
                or profile.connection_generation != pending.connection_generation + 1
                or pending.review_id != request.review_id
                or pending.review_sha256 != request.review_sha256
            ):
                raise RemoteConnectionFailedError("The pending SSH host key is no longer current.")
            if request.action == "reject":
                self._pending_reviews.pop(profile.profile_id, None)
                self._close_active()
                return "rejected"
            if request.action != "replace_changed_key":
                raise RemoteConnectionFailedError(
                    "First-use trust is handled by the native system SSH prompt."
                )
            alias = self._alias_profile(profile)
            try:
                self._host_trust.replace_changed_key(
                    pending,
                    profile=alias,
                    connection_generation=pending.connection_generation,
                    review_id=request.review_id,
                    review_sha256=request.review_sha256,
                )
            except SystemOpenSshSessionError:
                raise
            except Exception:
                raise RemoteConnectionFailedError(
                    "The SSH host key could not be confirmed."
                ) from None
            self._pending_reviews.pop(profile.profile_id, None)
            self._connect_locked(profile)
            return "connected"

    def close(self) -> None:
        with self._transition:
            with self._state:
                if self._closed:
                    return
                self._closed = True
            failure: BaseException | None = None
            try:
                self._close_active()
            except BaseException as exc:
                failure = exc
            self._pending_reviews.clear()
            cleanups = [self._session_owner.close, self._host_trust.close]
            if self._owned_askpass_helper is not None:
                cleanups.append(self._owned_askpass_helper.close)
            for close in cleanups:
                try:
                    close()
                except BaseException as exc:
                    if failure is None:
                        failure = exc
            if failure is not None:
                raise failure

    def _connect_locked(self, profile: local_v2.RemoteWorkspaceProfileV2) -> None:
        alias = self._alias_profile(profile)
        transport: _RemoteTransport | None = None
        try:
            with self._state:
                observer = self._prompt_observer

            def observe_prompt(observation: AskpassPromptObservation) -> None:
                if (
                    observer is not None
                    and observation.connection_generation == profile.connection_generation
                ):
                    observer(profile.profile_id, observation)

            if self._output_observer is not None:
                self._session_owner.connect(
                    alias,
                    connection_generation=profile.connection_generation,
                    prompt_observer=observe_prompt if observer is not None else None,
                    output_observer=self._output_observer,
                )
            else:
                self._session_owner.connect(
                    alias,
                    connection_generation=profile.connection_generation,
                    prompt_observer=observe_prompt if observer is not None else None,
                )
            session = self._session_owner.active_session()
            result = session.run(
                "id -un",
                timeout_seconds=self._discovery_timeout_seconds,
            )
            remote_user = self._remote_user(result)
            config = RemoteProfileConfig(
                id=profile.profile_id,
                name=profile.display_name,
                host=profile.ssh_host_alias,
                port=22,
                user=remote_user,
                auth=SSHAuthConfig(method="ssh_agent"),
            )
            transport = self._transport_factory(config, session, remote_user)
            if not callable(getattr(transport, "run", None)) or not callable(
                getattr(transport, "close", None)
            ):
                raise TypeError("system OpenSSH rich transport is invalid")
            with self._state:
                if self._closed:
                    raise RemoteConnectionFailedError(
                        "The requested remote profile is not connected."
                    )
                self._active = _ActiveSystemRemoteV2(
                    profile_id=profile.profile_id,
                    connection_generation=profile.connection_generation,
                    transport=transport,
                )
        except SystemOpenSshSessionError as exc:
            review = exc.host_key_review
            if review is not None:
                if (
                    review.profile_id != profile.profile_id
                    or review.connection_generation != profile.connection_generation
                ):
                    raise RemoteConnectionFailedError(
                        "The pending SSH host key is no longer current."
                    ) from None
                self._pending_reviews[profile.profile_id] = review
            self._cleanup_failed_connect(transport)
            raise
        except RemoteLifecycleError:
            self._cleanup_failed_connect(transport)
            raise
        except Exception:
            self._cleanup_failed_connect(transport)
            raise RemoteConnectionFailedError(
                "The SSH connection could not be established."
            ) from None

    def _cleanup_failed_connect(self, transport: _RemoteTransport | None) -> None:
        if transport is not None:
            self._close_transport(transport)
        try:
            self._session_owner.disconnect()
        except Exception:
            pass
        with self._state:
            self._active = None

    def _close_active(self) -> None:
        with self._state:
            active, self._active = self._active, None
        failure: BaseException | None = None
        if active is not None:
            try:
                self._close_transport(active.transport)
            except BaseException as exc:
                failure = exc
        if active is not None:
            try:
                self._session_owner.disconnect()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure

    @staticmethod
    def _close_transport(transport: _RemoteTransport) -> None:
        transport.close()

    @staticmethod
    def _remote_user(result: RemoteCommandResult) -> str:
        if not result.ok or type(result.stdout) is not str:
            raise RemoteConnectionFailedError("The SSH remote user could not be discovered.")
        lines = result.stdout.splitlines()
        if len(lines) != 1 or _SYSTEM_REMOTE_USER.fullmatch(lines[0]) is None:
            raise RemoteConnectionFailedError("The SSH remote user could not be discovered.")
        return lines[0]

    @staticmethod
    def _alias_profile(
        profile: local_v2.RemoteWorkspaceProfileV2,
    ) -> SystemOpenSshAliasProfile:
        return SystemOpenSshAliasProfile(
            profile_id=profile.profile_id,
            ssh_host_alias=profile.ssh_host_alias,
        )

    @staticmethod
    def _validate_connect_profile(profile: local_v2.RemoteWorkspaceProfileV2) -> None:
        if type(
            profile
        ) is not local_v2.RemoteWorkspaceProfileV2 or profile.connection_state not in {
            "connecting",
            "bootstrapping",
            "negotiating",
        }:
            raise TypeError("system OpenSSH lifecycle profile is invalid")

    def _require_open(self) -> None:
        with self._state:
            if self._closed:
                raise RemoteConnectionFailedError("The requested remote profile is not connected.")


__all__ = (
    "DesktopRemoteLifecycle",
    "RemoteConnectionFailedError",
    "RemoteConnectionResult",
    "RemoteCredentialUnavailableError",
    "RemoteLifecycleSnapshot",
    "RemoteLifecycleSupersededError",
    "SystemOpenSshRemoteLifecycleV2",
    "remote_profile_config",
    "ssh_agent_auth_for_release",
)
