from __future__ import annotations

from dataclasses import dataclass
import errno
from threading import Event, Thread
import traceback
from typing import Callable, cast

import pytest

from desktop.sidecar.askpass_broker import AskpassPromptObservation
from desktop.sidecar.contracts.v1.models import (
    HostKeyAcceptV1,
    RemoteProfileV1,
)
from desktop.sidecar.contracts.v2 import models as local_v2
from desktop.sidecar.lifecycle_logs_v2 import (
    LifecycleLogSourceV2,
    LifecycleRawOutputObserverV2,
)
from desktop.sidecar.remote_lifecycle import (
    DesktopRemoteLifecycle,
    RemoteConnectionFailedError,
    RemoteCredentialUnavailableError,
    RemoteLifecycleSupersededError,
    SystemOpenSshRemoteLifecycleV2,
    remote_profile_config,
)
from desktop.sidecar.system_ssh_session import SystemOpenSshSessionError
from openevo.deployment.host_keys import (
    HostKeyCandidate,
    PendingHostKeyProbe,
    PendingSystemHostKeyReview,
    SystemKnownHostsPolicy,
    TrustedKnownHostsBinding,
)
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.profile import SSHAuthConfig, SystemOpenSshAliasProfile
from openevo.deployment.remote_home import (
    RemoteHomeAuthority,
    parse_remote_home_probe,
)


TIMESTAMP = "2026-07-14T12:00:00.000000Z"


def _system_profile(*, generation: int = 2) -> local_v2.RemoteWorkspaceProfileV2:
    return local_v2.RemoteWorkspaceProfileV2(
        profile_id="profile-system-1",
        display_name="Configured GPU",
        ssh_host_alias="gpu-via-config",
        catalog_generation=3,
        connection_generation=generation,
        connection_state="connecting",
        prompt=None,
        trust=local_v2.SshTrustStateV2(
            connection_generation=generation,
            state="unverified",
            review_id=None,
            review_sha256=None,
            key_fingerprints=[],
            repair_support="not_needed",
        ),
        failure=None,
        active_project_id=None,
        core_api_major=None,
        core_openapi_sha256=None,
        core_event_schema_sha256=None,
        core_registry_sha256=None,
        created_at=TIMESTAMP,
        updated_at=TIMESTAMP,
        etag=f'"{"2" * 64}"',
    )


def _profile(
    *,
    profile_id: str = "profile-1",
    fingerprint: str | None = None,
    authentication_kind: str = "ssh_agent",
) -> RemoteProfileV1:
    return RemoteProfileV1.model_validate(
        {
            "profile_id": profile_id,
            "name": "Research server",
            "host": "compute.example.org",
            "port": 2222,
            "user": "researcher",
            "authentication_kind": authentication_kind,
            "credential_slots": (),
            "proxy": {
                "http_url": "http://127.0.0.1:7890",
                "https_url": None,
                "no_proxy": ("127.0.0.1", "localhost"),
            },
            "connection_state": "disconnected",
            "host_key_fingerprint": fingerprint,
            "etag": f'"{"1" * 64}"',
            "created_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
        }
    )


def _candidate(
    algorithm: str = "ssh-ed25519",
    fingerprint: str = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
) -> HostKeyCandidate:
    return HostKeyCandidate(
        algorithm=algorithm,  # type: ignore[arg-type]
        public_key="AAAAC3NzaC1lZDI1NTE5AAAAITest",
        fingerprint=fingerprint,
    )


@dataclass
class FakeHostKeys:
    candidates: tuple[HostKeyCandidate, ...]
    loaded: object | None = None

    def __post_init__(self) -> None:
        self.probes: list[object] = []
        self.confirmations: list[tuple[str, str]] = []

    def probe(self, profile, *, timeout_seconds: float = 10.0):
        self.probes.append((profile, timeout_seconds))
        return PendingHostKeyProbe(
            profile_id=profile.id,
            host=profile.host,
            port=profile.port,
            candidates=self.candidates,
            _store_token=object(),
            _digest="digest",
        )

    def confirm(
        self,
        pending,
        *,
        profile,
        algorithm,
        fingerprint,
        timeout_seconds: float = 10.0,
    ):
        assert pending.profile_id == profile.id
        self.confirmations.append((algorithm, fingerprint))
        return cast(TrustedKnownHostsBinding, self.loaded or object())

    def load(self, profile, *, expected_fingerprint: str):
        assert profile.id
        assert expected_fingerprint
        return cast(TrustedKnownHostsBinding | None, self.loaded)


class FakeTransport:
    def __init__(
        self,
        *,
        return_code: int = 0,
        started: Event | None = None,
        release: Event | None = None,
        advance: Callable[[], None] | None = None,
    ) -> None:
        self.return_code = return_code
        self.started = started
        self.release = release
        self.advance = advance
        self.commands: list[tuple[str, float]] = []
        self.closed = False
        self.close_calls = 0
        self.close_errors: list[BaseException] = []

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        assert cwd is None and env is None
        self.commands.append((command, timeout_seconds))
        if self.advance is not None:
            self.advance()
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            assert self.release.wait(2)
        return RemoteCommandResult(command=command, return_code=self.return_code)

    def close(self) -> None:
        self.close_calls += 1
        if self.close_errors:
            raise self.close_errors.pop(0)
        self.closed = True


def test_new_host_requires_review_and_prefers_ed25519() -> None:
    rsa = _candidate("rsa-sha2-512", "SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=")
    ed25519 = _candidate()
    host_keys = FakeHostKeys((rsa, ed25519))
    transports: list[FakeTransport] = []
    lifecycle = DesktopRemoteLifecycle(
        cast(object, host_keys),
        transport_factory=lambda *_: transports.append(FakeTransport()) or transports[-1],
    )

    result = lifecycle.connect(_profile())

    assert result.state == "host_key_required"
    assert result.host_key_candidate == ed25519
    assert lifecycle.snapshot().host_key_candidate == ed25519
    assert transports == []


def test_host_key_acceptance_connects_and_preserves_proxy_config() -> None:
    candidate = _candidate()
    binding = object()
    host_keys = FakeHostKeys((candidate,), loaded=binding)
    seen: list[object] = []
    transport = FakeTransport()

    def transport_factory(profile, trusted):
        seen.extend((profile, trusted))
        return transport

    lifecycle = DesktopRemoteLifecycle(
        cast(object, host_keys), transport_factory=transport_factory
    )
    profile = _profile()
    lifecycle.connect(profile)

    result = lifecycle.accept_host_key(
        profile,
        HostKeyAcceptV1(algorithm=candidate.algorithm, fingerprint=candidate.fingerprint),
    )

    assert result.state == "connected"
    assert lifecycle.snapshot().state == "connected"
    assert lifecycle.active_transport(profile.profile_id) is transport
    assert transport.commands[0][0] == "true"
    assert 0 < transport.commands[0][1] <= 12.0
    config = seen[0]
    assert config.proxy.http_proxy == "http://127.0.0.1:7890"
    assert config.proxy.no_proxy == "127.0.0.1,localhost"
    assert seen[1] is binding


def test_host_key_acceptance_shares_one_deadline_with_the_ssh_check() -> None:
    candidate = _candidate()
    now = [100.0]

    class DeadlineHostKeys(FakeHostKeys):
        def confirm(self, *args, timeout_seconds: float = 10.0, **kwargs):
            assert timeout_seconds == pytest.approx(12.0)
            now[0] += 7.0
            return super().confirm(*args, timeout_seconds=timeout_seconds, **kwargs)

    host_keys = DeadlineHostKeys((candidate,), loaded=object())
    transport = FakeTransport(advance=lambda: now.__setitem__(0, now[0] + 4.0))
    lifecycle = DesktopRemoteLifecycle(
        cast(object, host_keys),
        transport_factory=lambda *_: transport,
        connection_timeout_seconds=12.0,
        monotonic=lambda: now[0],
    )
    profile = _profile()
    lifecycle.connect(profile)

    lifecycle.accept_host_key(
        profile,
        HostKeyAcceptV1(algorithm=candidate.algorithm, fingerprint=candidate.fingerprint),
    )

    assert transport.commands == [("true", pytest.approx(5.0))]
    assert now[0] - 100.0 == 11.0


def test_connection_deadline_cannot_consume_the_desktop_request_margin() -> None:
    with pytest.raises(ValueError, match="between zero and 12 seconds"):
        DesktopRemoteLifecycle(
            cast(object, FakeHostKeys((_candidate(),))),
            connection_timeout_seconds=12.001,
        )


def test_transport_validation_cannot_publish_after_the_shared_deadline() -> None:
    now = [100.0]
    transport = FakeTransport(advance=lambda: now.__setitem__(0, now[0] + 13.0))
    lifecycle = DesktopRemoteLifecycle(
        cast(object, FakeHostKeys((_candidate(),), loaded=object())),
        transport_factory=lambda *_: transport,
        connection_timeout_seconds=12.0,
        monotonic=lambda: now[0],
    )

    with pytest.raises(RemoteConnectionFailedError, match="deadline expired"):
        lifecycle.connect(_profile(fingerprint=_candidate().fingerprint))

    assert lifecycle.snapshot().state == "failed"
    assert transport.closed


def test_existing_trust_connects_without_a_new_probe() -> None:
    binding = object()
    host_keys = FakeHostKeys((_candidate(),), loaded=binding)
    transport = FakeTransport()
    lifecycle = DesktopRemoteLifecycle(
        cast(object, host_keys), transport_factory=lambda *_: transport
    )

    result = lifecycle.connect(_profile(fingerprint=_candidate().fingerprint))

    assert result.state == "connected"
    assert host_keys.probes == []


def test_native_authentication_modes_fail_closed_in_release() -> None:
    host_keys = FakeHostKeys((_candidate(),))
    lifecycle = DesktopRemoteLifecycle(cast(object, host_keys))

    with pytest.raises(RemoteCredentialUnavailableError):
        lifecycle.connect(_profile(authentication_kind="native_password"))

    assert lifecycle.snapshot().profile_id == "profile-1"
    assert lifecycle.snapshot().state == "failed"
    assert host_keys.probes == []


def test_replacement_closes_active_transport_before_credential_resolution() -> None:
    host_keys = FakeHostKeys((_candidate(),), loaded=object())
    first_transport = FakeTransport()

    def resolve_auth(profile: RemoteProfileV1) -> SSHAuthConfig:
        if profile.profile_id == "profile-2":
            raise RemoteCredentialUnavailableError("credential unavailable")
        return SSHAuthConfig(method="ssh_agent")

    lifecycle = DesktopRemoteLifecycle(
        cast(object, host_keys),
        auth_resolver=resolve_auth,
        transport_factory=lambda *_: first_transport,
    )
    lifecycle.connect(_profile(fingerprint=_candidate().fingerprint))

    with pytest.raises(RemoteCredentialUnavailableError):
        lifecycle.connect(
            _profile(
                profile_id="profile-2",
                fingerprint=_candidate().fingerprint,
            )
        )

    assert first_transport.closed
    assert lifecycle.snapshot().profile_id == "profile-2"
    assert lifecycle.snapshot().state == "failed"
    with pytest.raises(RemoteConnectionFailedError):
        lifecycle.active_transport("profile-1")


def test_host_key_persistence_and_transport_construction_share_deadline() -> None:
    candidate = _candidate()
    now = [100.0]
    transport = FakeTransport()

    class SlowConfirmHostKeys(FakeHostKeys):
        def confirm(self, *args, timeout_seconds: float = 10.0, **kwargs):
            now[0] += 7.0
            return super().confirm(*args, timeout_seconds=timeout_seconds, **kwargs)

    def slow_transport_factory(*_args: object) -> FakeTransport:
        now[0] += 6.0
        return transport

    host_keys = SlowConfirmHostKeys((candidate,), loaded=object())
    lifecycle = DesktopRemoteLifecycle(
        cast(object, host_keys),
        transport_factory=slow_transport_factory,
        connection_timeout_seconds=12.0,
        monotonic=lambda: now[0],
    )
    profile = _profile()
    lifecycle.connect(profile)

    with pytest.raises(RemoteConnectionFailedError, match="deadline expired"):
        lifecycle.accept_host_key(
            profile,
            HostKeyAcceptV1(algorithm=candidate.algorithm, fingerprint=candidate.fingerprint),
        )

    assert now[0] - 100.0 == 13.0
    assert transport.commands == []
    assert transport.closed


def test_expired_host_key_persistence_budget_prevents_transport_construction() -> None:
    candidate = _candidate()
    now = [100.0]
    factory_calls = 0

    class ExpiredConfirmHostKeys(FakeHostKeys):
        def confirm(self, *args, timeout_seconds: float = 10.0, **kwargs):
            now[0] += 13.0
            return super().confirm(*args, timeout_seconds=timeout_seconds, **kwargs)

    def transport_factory(*_args: object) -> FakeTransport:
        nonlocal factory_calls
        factory_calls += 1
        return FakeTransport()

    host_keys = ExpiredConfirmHostKeys((candidate,), loaded=object())
    lifecycle = DesktopRemoteLifecycle(
        cast(object, host_keys),
        transport_factory=transport_factory,
        connection_timeout_seconds=12.0,
        monotonic=lambda: now[0],
    )
    profile = _profile()
    lifecycle.connect(profile)

    with pytest.raises(RemoteConnectionFailedError, match="deadline expired"):
        lifecycle.accept_host_key(
            profile,
            HostKeyAcceptV1(algorithm=candidate.algorithm, fingerprint=candidate.fingerprint),
        )

    assert factory_calls == 0


def test_disconnect_supersedes_a_late_connect_and_closes_its_transport() -> None:
    started = Event()
    release = Event()

    class InterruptibleTransport(FakeTransport):
        def close(self) -> None:
            super().close()
            release.set()

    transport = InterruptibleTransport(started=started, release=release)
    host_keys = FakeHostKeys((_candidate(),), loaded=object())
    lifecycle = DesktopRemoteLifecycle(
        cast(object, host_keys), transport_factory=lambda *_: transport
    )
    errors: list[BaseException] = []

    def connect() -> None:
        try:
            lifecycle.connect(_profile(fingerprint=_candidate().fingerprint))
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=connect)
    thread.start()
    assert started.wait(2)
    lifecycle.disconnect("profile-1")
    thread.join(2)

    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], RemoteLifecycleSupersededError)
    assert lifecycle.snapshot().state == "disconnected"
    assert transport.closed


def test_disconnect_rejects_non_owner_without_closing_active_transport() -> None:
    transport = FakeTransport()
    lifecycle = DesktopRemoteLifecycle(
        cast(object, FakeHostKeys((_candidate(),), loaded=object())),
        transport_factory=lambda *_: transport,
    )
    lifecycle.connect(_profile(fingerprint=_candidate().fingerprint))

    with pytest.raises(RemoteConnectionFailedError, match="Another remote profile"):
        lifecycle.disconnect("profile-2")

    assert lifecycle.snapshot().profile_id == "profile-1"
    assert lifecycle.snapshot().state == "connected"
    assert lifecycle.active_transport("profile-1") is transport
    assert not transport.closed


def test_failed_connect_does_not_publish_an_active_transport() -> None:
    transport = FakeTransport(return_code=255)
    host_keys = FakeHostKeys((_candidate(),), loaded=object())
    lifecycle = DesktopRemoteLifecycle(
        cast(object, host_keys), transport_factory=lambda *_: transport
    )

    with pytest.raises(RemoteConnectionFailedError):
        lifecycle.connect(_profile(fingerprint=_candidate().fingerprint))

    assert lifecycle.snapshot().state == "failed"
    assert lifecycle.snapshot().failure_code == "ssh_connection_failed"
    assert transport.closed


def test_remote_lifecycle_drops_untrusted_transport_exception_chain() -> None:
    canary = "raw-agent-lifecycle-canary.sock"

    def fail_transport(*_args: object) -> FakeTransport:
        raise OSError(errno.ENOENT, canary)

    lifecycle = DesktopRemoteLifecycle(
        cast(object, FakeHostKeys((_candidate(),), loaded=object())),
        transport_factory=fail_transport,
    )

    with pytest.raises(RemoteConnectionFailedError) as exc_info:
        lifecycle.connect(_profile(fingerprint=_candidate().fingerprint))

    error = exc_info.value
    assert error.__cause__ is None
    assert error.__context__ is None
    formatted = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert canary not in formatted
    assert lifecycle.snapshot().state == "failed"


def test_remote_lifecycle_rebuilds_nested_lifecycle_exception_chain() -> None:
    canary = "raw-agent-wrapped-lifecycle-canary.sock"

    def fail_transport(*_args: object) -> FakeTransport:
        try:
            raise OSError(errno.ENOENT, canary)
        except OSError as error:
            raise RemoteConnectionFailedError(canary) from error

    lifecycle = DesktopRemoteLifecycle(
        cast(object, FakeHostKeys((_candidate(),), loaded=object())),
        transport_factory=fail_transport,
    )

    with pytest.raises(RemoteConnectionFailedError) as exc_info:
        lifecycle.connect(_profile(fingerprint=_candidate().fingerprint))

    error = exc_info.value
    assert str(error) == "The SSH connection could not be established."
    assert error.__cause__ is None
    assert error.__context__ is None
    assert canary not in "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


def test_remote_profile_projection_has_no_credential_or_local_path() -> None:
    config = remote_profile_config(_profile())

    assert config.auth.method == "ssh_agent"
    assert config.auth.private_key_path is None
    assert config.auth.password_ref is None
    assert config.path is None
    assert config.workspace_root is None


class _SystemSession:
    def __init__(
        self,
        alias: str,
        *,
        profile_id: str,
        connection_generation: int,
        remote_user: str = "researcher",
        remote_home: str = "/srv/research/alice",
        discovery_error: SystemOpenSshSessionError | None = None,
    ) -> None:
        self.ssh_host_alias = alias
        self.profile_id = profile_id
        self.connection_generation = connection_generation
        self.remote_user = remote_user
        self.remote_home = remote_home
        self.discovery_error = discovery_error
        self.commands: list[str] = []
        self.discoveries: list[float] = []
        self.discovery_cancel_events: list[Event | None] = []
        self.authorities: list[RemoteHomeAuthority] = []

    def snapshot(self) -> object:
        return object()

    def run(self, command: str, *, timeout_seconds: float = 30.0) -> RemoteCommandResult:
        assert 0 < timeout_seconds <= 30.0
        self.commands.append(command)
        return RemoteCommandResult(
            command=command,
            return_code=0,
            stdout=f"{self.remote_user}\n",
        )

    def discover_remote_home_authority(
        self,
        *,
        timeout_seconds: float = 30.0,
        cancel_event: Event | None = None,
    ) -> RemoteHomeAuthority:
        self.discoveries.append(timeout_seconds)
        self.discovery_cancel_events.append(cancel_event)
        if self.discovery_error is not None:
            raise self.discovery_error
        uid = 1001
        record = (
            "openevo-remote-home-v1\n"
            f"{self.remote_user}\n{uid}\n{self.remote_user}\n{uid}\n"
            f"{self.remote_home}\n{self.remote_home}\n{uid}\n1\n"
        ).encode("utf-8")
        authority = parse_remote_home_probe(
            profile_id=self.profile_id,
            connection_generation=self.connection_generation,
            return_code=0,
            stdout=record,
            stderr=b"",
        )
        self.authorities.append(authority)
        return authority


class _SystemSessionOwner:
    def __init__(self) -> None:
        self.active: _SystemSession | None = None
        self.failures: list[SystemOpenSshSessionError] = []
        self.discovery_failures: list[SystemOpenSshSessionError] = []
        self.remote_homes: dict[int, str] = {}
        self.connections: list[tuple[SystemOpenSshAliasProfile, int]] = []
        self.cancel_events: list[Event | None] = []
        self.sessions: list[_SystemSession] = []
        self.disconnects = 0
        self.disconnect_errors: list[BaseException] = []
        self.prompt_observer: Callable[[AskpassPromptObservation], None] | None = None
        self.output_observer: LifecycleRawOutputObserverV2 | None = None

    def connect(
        self,
        profile: SystemOpenSshAliasProfile,
        *,
        connection_generation: int,
        prompt_observer: Callable[[AskpassPromptObservation], None] | None = None,
        output_observer: LifecycleRawOutputObserverV2 | None = None,
        cancel_event: Event | None = None,
    ) -> object:
        self.connections.append((profile, connection_generation))
        self.cancel_events.append(cancel_event)
        self.prompt_observer = prompt_observer
        self.output_observer = output_observer
        if self.failures:
            raise self.failures.pop(0)
        self.active = _SystemSession(
            profile.ssh_host_alias,
            profile_id=profile.profile_id,
            connection_generation=connection_generation,
            remote_home=self.remote_homes.get(
                connection_generation,
                "/srv/research/alice",
            ),
            discovery_error=(
                self.discovery_failures.pop(0) if self.discovery_failures else None
            ),
        )
        self.sessions.append(self.active)
        return object()

    def active_session(self) -> _SystemSession:
        assert self.active is not None
        return self.active

    def disconnect(self) -> None:
        self.disconnects += 1
        if self.disconnect_errors:
            raise self.disconnect_errors.pop(0)
        self.active = None

    close = disconnect


class _SystemTransport(FakeTransport):
    pass


class _SystemHostTrust:
    def __init__(self) -> None:
        self.replacements: list[tuple[object, ...]] = []
        self.reissues: list[tuple[object, object]] = []
        self.reissued_review: PendingSystemHostKeyReview | None = None

    def reissue_changed_key_review(
        self,
        review: object,
        **arguments: object,
    ) -> PendingSystemHostKeyReview:
        self.reissues.append((review, arguments))
        assert self.reissued_review is not None
        return self.reissued_review

    def replace_changed_key(self, review: object, **arguments: object) -> None:
        self.replacements.append((review, arguments))

    def close(self) -> None:
        return None


def test_v2_lifecycle_uses_literal_alias_and_verified_remote_home_authority() -> None:
    owner = _SystemSessionOwner()
    seen: list[tuple[object, object, RemoteHomeAuthority]] = []
    transport = _SystemTransport()

    def transport_factory(
        config: object,
        session: object,
        authority: RemoteHomeAuthority,
    ) -> object:
        seen.append((config, session, authority))
        return transport

    lifecycle = SystemOpenSshRemoteLifecycleV2(
        cast(object, owner),
        cast(object, _SystemHostTrust()),
        transport_factory=transport_factory,
    )
    profile = _system_profile()

    lifecycle.connect(profile)

    alias_profile, generation = owner.connections[0]
    assert alias_profile == SystemOpenSshAliasProfile(
        profile_id=profile.profile_id,
        ssh_host_alias="gpu-via-config",
    )
    assert generation == profile.connection_generation
    config, session, authority = seen[0]
    assert config.host == "gpu-via-config"
    assert config.user == authority.remote_user == "researcher"
    assert config.port == 22
    assert config.path is None
    assert config.workspace_root == "/srv/research/alice/.openevo/workspaces"
    assert config.effective_workspace_root == config.workspace_root
    assert "/srv/research/alice" not in repr(config)
    assert session is owner.active
    assert owner.active is not None
    assert owner.active.commands == []
    assert owner.active.discoveries == [30.0]
    assert owner.active.authorities == [authority]
    assert (
        lifecycle.active_transport(profile.profile_id, profile.connection_generation) is transport
    )
    with pytest.raises(RemoteConnectionFailedError):
        lifecycle.active_transport(profile.profile_id, profile.connection_generation - 1)

    lifecycle.disconnect(profile.profile_id, profile.connection_generation + 1)
    assert transport.closed
    assert owner.disconnects == 1


def test_v2_lifecycle_retries_the_same_cleanup_authority_after_failure() -> None:
    owner = _SystemSessionOwner()
    transport = _SystemTransport()
    lifecycle = SystemOpenSshRemoteLifecycleV2(
        cast(object, owner),
        cast(object, _SystemHostTrust()),
        transport_factory=lambda *_: transport,
    )
    profile = _system_profile()
    lifecycle.connect(profile)
    owner.disconnect_errors.append(
        SystemOpenSshSessionError(
            "ssh_cleanup_failed",
            "SSH master did not stop before its deadline.",
        )
    )

    with pytest.raises(SystemOpenSshSessionError, match="did not stop"):
        lifecycle.disconnect(profile.profile_id, profile.connection_generation + 1)

    assert lifecycle.cleanup_authority() == (
        profile.profile_id,
        profile.connection_generation,
    )
    assert transport.close_calls == 1
    assert owner.active is not None
    with pytest.raises(RemoteConnectionFailedError, match="not connected"):
        lifecycle.active_transport(profile.profile_id, profile.connection_generation)

    lifecycle.disconnect(profile.profile_id, profile.connection_generation + 2)

    assert transport.close_calls == 1
    assert owner.disconnects == 2
    assert owner.active is None
    assert lifecycle.cleanup_authority() is None


def test_v2_lifecycle_forwards_exact_connection_cancellation_authority() -> None:
    owner = _SystemSessionOwner()
    lifecycle = SystemOpenSshRemoteLifecycleV2(
        cast(object, owner),
        cast(object, _SystemHostTrust()),
        transport_factory=lambda *_: _SystemTransport(),
    )
    cancel_event = Event()

    lifecycle.connect(_system_profile(), cancel_event=cancel_event)

    assert owner.cancel_events == [cancel_event]
    assert owner.sessions[0].discovery_cancel_events == [cancel_event]


def test_v2_lifecycle_rediscovers_home_for_every_connection_generation() -> None:
    owner = _SystemSessionOwner()
    owner.remote_homes = {
        2: "/srv/research/alice",
        3: "/EvoLab/accounts/alice",
    }
    seen: list[tuple[object, RemoteHomeAuthority, _SystemTransport]] = []

    def transport_factory(
        config: object,
        _session: object,
        authority: RemoteHomeAuthority,
    ) -> _SystemTransport:
        transport = _SystemTransport()
        seen.append((config, authority, transport))
        return transport

    lifecycle = SystemOpenSshRemoteLifecycleV2(
        cast(object, owner),
        cast(object, _SystemHostTrust()),
        transport_factory=transport_factory,
    )

    lifecycle.connect(_system_profile(generation=2))
    lifecycle.connect(_system_profile(generation=3))

    assert len(seen) == 2
    assert seen[0][1] is not seen[1][1]
    assert seen[0][1].connection_generation == 2
    assert seen[1][1].connection_generation == 3
    assert seen[0][0].workspace_root == "/srv/research/alice/.openevo/workspaces"
    assert seen[1][0].workspace_root == "/EvoLab/accounts/alice/.openevo/workspaces"
    assert owner.sessions[0].discoveries == [30.0]
    assert owner.sessions[1].discoveries == [30.0]
    assert seen[0][2].closed


def test_v2_lifecycle_discovery_failure_prevents_transport_and_disconnects() -> None:
    owner = _SystemSessionOwner()
    owner.discovery_failures.append(
        SystemOpenSshSessionError(
            "ssh_remote_account_unavailable",
            "The remote SSH account could not be verified.",
        )
    )
    factory_calls: list[object] = []
    lifecycle = SystemOpenSshRemoteLifecycleV2(
        cast(object, owner),
        cast(object, _SystemHostTrust()),
        transport_factory=lambda *arguments: factory_calls.append(arguments)
        or _SystemTransport(),
    )

    with pytest.raises(SystemOpenSshSessionError) as captured:
        lifecycle.connect(_system_profile())

    assert captured.value.code == "ssh_remote_account_unavailable"
    assert factory_calls == []
    assert owner.sessions[0].commands == []
    assert owner.sessions[0].discoveries == [30.0]
    assert owner.disconnects == 1


def test_v2_lifecycle_discovery_failure_retains_typed_cleanup_authority() -> None:
    owner = _SystemSessionOwner()
    owner.discovery_failures.append(
        SystemOpenSshSessionError(
            "ssh_remote_account_unavailable",
            "The remote SSH account could not be verified.",
        )
    )
    owner.disconnect_errors.append(RuntimeError("owned master is still running"))
    lifecycle = SystemOpenSshRemoteLifecycleV2(
        cast(object, owner),
        cast(object, _SystemHostTrust()),
        transport_factory=lambda *_: _SystemTransport(),
    )
    profile = _system_profile()

    with pytest.raises(SystemOpenSshSessionError) as captured:
        lifecycle.connect(profile)

    assert captured.value.code == "ssh_cleanup_failed"
    assert owner.active is not None
    with pytest.raises(RemoteConnectionFailedError, match="not connected"):
        lifecycle.active_transport(profile.profile_id, profile.connection_generation)

    lifecycle.disconnect(profile.profile_id, profile.connection_generation + 1)
    assert owner.active is None


def test_v2_lifecycle_disconnect_is_restart_idempotent_without_an_owned_session() -> None:
    owner = _SystemSessionOwner()
    lifecycle = SystemOpenSshRemoteLifecycleV2(
        cast(object, owner),
        cast(object, _SystemHostTrust()),
        transport_factory=lambda *_: _SystemTransport(),
    )

    lifecycle.disconnect("profile-system-1", 3)

    assert owner.disconnects == 0


def test_v2_lifecycle_passes_only_the_closed_process_output_observer() -> None:
    owner = _SystemSessionOwner()
    observed: list[tuple[str, bytes]] = []

    def output_observer(source: LifecycleLogSourceV2, chunk: bytes) -> None:
        observed.append((source, chunk))

    typed_output_observer: LifecycleRawOutputObserverV2 = output_observer
    lifecycle = SystemOpenSshRemoteLifecycleV2(
        cast(object, owner),
        cast(object, _SystemHostTrust()),
        transport_factory=lambda *_: _SystemTransport(),
        output_observer=typed_output_observer,
    )

    lifecycle.connect(_system_profile())

    assert owner.output_observer is typed_output_observer
    assert observed == []


def test_v2_lifecycle_replaces_only_the_exact_changed_key_review() -> None:
    owner = _SystemSessionOwner()
    policy = SystemKnownHostsPolicy(
        repair_support="automatic_replacement_available",
        reason="test",
        known_hosts_file=None,
        lookup_token=None,
        _file_identity=None,
    )
    review = PendingSystemHostKeyReview(
        review_id="host-review-1",
        review_sha256="e" * 64,
        profile_id="profile-system-1",
        connection_generation=2,
        key_fingerprints=(("ssh-ed25519", "SHA256:" + ("A" * 43)),),
        repair_support="automatic_replacement_available",
        _policy=policy,
        _authority_token=object(),
    )
    owner.failures.append(
        SystemOpenSshSessionError(
            "ssh_host_key_changed",
            "The configured server identity changed and requires review.",
            host_key_review=review,
        )
    )
    trust = _SystemHostTrust()
    transports: list[_SystemTransport] = []
    lifecycle = SystemOpenSshRemoteLifecycleV2(
        cast(object, owner),
        cast(object, trust),
        transport_factory=lambda *_: transports.append(_SystemTransport()) or transports[-1],
    )

    with pytest.raises(SystemOpenSshSessionError) as failure:
        lifecycle.connect(_system_profile(generation=2))
    assert failure.value.host_key_review is review

    request = local_v2.HostKeyReviewRequestV2(
        expected_connection_generation=2,
        review_id=review.review_id,
        review_sha256=review.review_sha256,
        action="replace_changed_key",
    )
    lifecycle.review_host_key(_system_profile(generation=3), request)

    assert len(trust.replacements) == 1
    replaced, arguments = trust.replacements[0]
    assert replaced is review
    assert arguments["connection_generation"] == 2
    assert arguments["review_id"] == review.review_id
    assert arguments["review_sha256"] == review.review_sha256
    assert owner.connections[-1][1] == 3
    assert lifecycle.active_transport("profile-system-1", 3) is transports[0]


def test_v2_lifecycle_reissues_an_exact_changed_key_review_after_restart() -> None:
    owner = _SystemSessionOwner()
    policy = SystemKnownHostsPolicy(
        repair_support="automatic_replacement_available",
        reason="test",
        known_hosts_file=None,
        lookup_token=None,
        _file_identity=None,
    )
    rediscovered = PendingSystemHostKeyReview(
        review_id="host-review-new",
        review_sha256="a" * 64,
        profile_id="profile-system-1",
        connection_generation=3,
        key_fingerprints=(("ssh-ed25519", "SHA256:" + ("A" * 43)),),
        repair_support="automatic_replacement_available",
        _policy=policy,
        _authority_token=object(),
    )
    restored = PendingSystemHostKeyReview(
        review_id="host-review-old",
        review_sha256="e" * 64,
        profile_id="profile-system-1",
        connection_generation=2,
        key_fingerprints=rediscovered.key_fingerprints,
        repair_support="automatic_replacement_available",
        _policy=policy,
        _authority_token=object(),
    )
    owner.failures.append(
        SystemOpenSshSessionError(
            "ssh_host_key_changed",
            "The configured server identity changed and requires review.",
            host_key_review=rediscovered,
        )
    )
    trust = _SystemHostTrust()
    trust.reissued_review = restored
    lifecycle = SystemOpenSshRemoteLifecycleV2(
        cast(object, owner),
        cast(object, trust),
        transport_factory=lambda *_: _SystemTransport(),
    )
    request = local_v2.HostKeyReviewRequestV2(
        expected_connection_generation=2,
        review_id=restored.review_id,
        review_sha256=restored.review_sha256,
        action="replace_changed_key",
    )

    assert lifecycle.review_host_key(_system_profile(generation=3), request) == "connected"

    assert len(trust.reissues) == 1
    current, reissue_arguments = trust.reissues[0]
    assert current is rediscovered
    assert reissue_arguments["connection_generation"] == 2
    assert reissue_arguments["review_id"] == restored.review_id
    assert reissue_arguments["review_sha256"] == restored.review_sha256
    assert trust.replacements[0][0] is restored
    assert [generation for _, generation in owner.connections] == [3, 3]


def test_v2_lifecycle_rejects_a_persisted_changed_key_review_after_restart() -> None:
    owner = _SystemSessionOwner()
    lifecycle = SystemOpenSshRemoteLifecycleV2(
        cast(object, owner),
        cast(object, _SystemHostTrust()),
        transport_factory=lambda *_: _SystemTransport(),
    )
    request = local_v2.HostKeyReviewRequestV2(
        expected_connection_generation=2,
        review_id="host-review-old",
        review_sha256="e" * 64,
        action="reject",
    )

    assert lifecycle.review_host_key(_system_profile(generation=3), request) == "rejected"
    assert owner.connections == []


def test_v2_lifecycle_binds_text_free_native_prompt_observations_to_profile() -> None:
    owner = _SystemSessionOwner()
    observed: list[tuple[str, AskpassPromptObservation]] = []
    lifecycle = SystemOpenSshRemoteLifecycleV2(
        cast(object, owner),
        cast(object, _SystemHostTrust()),
        transport_factory=lambda *_: _SystemTransport(),
    )
    lifecycle.set_prompt_observer(
        lambda profile_id, observation: observed.append((profile_id, observation))
    )

    lifecycle.connect(_system_profile())
    assert owner.prompt_observer is not None
    owner.prompt_observer(
        AskpassPromptObservation(
            connection_generation=2,
            kind="password",
            state="pending",
        )
    )

    assert observed == [
        (
            "profile-system-1",
            AskpassPromptObservation(
                connection_generation=2,
                kind="password",
                state="pending",
            ),
        )
    ]
