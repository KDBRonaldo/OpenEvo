from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread
from typing import cast

import pytest

from desktop.sidecar.contracts.v1.models import (
    HostKeyAcceptV1,
    RemoteProfileV1,
)
from desktop.sidecar.remote_lifecycle import (
    DesktopRemoteLifecycle,
    RemoteConnectionFailedError,
    RemoteCredentialUnavailableError,
    RemoteLifecycleSupersededError,
    remote_profile_config,
)
from openevo.deployment.host_keys import (
    HostKeyCandidate,
    PendingHostKeyProbe,
    TrustedKnownHostsBinding,
)
from openevo.deployment.preflight import RemoteCommandResult


TIMESTAMP = "2026-07-14T12:00:00.000000Z"


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
    ) -> None:
        self.return_code = return_code
        self.started = started
        self.release = release
        self.commands: list[tuple[str, float]] = []
        self.closed = False

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
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            assert self.release.wait(2)
        return RemoteCommandResult(command=command, return_code=self.return_code)

    def close(self) -> None:
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
    assert transport.commands == [("true", 10.0)]
    config = seen[0]
    assert config.proxy.http_proxy == "http://127.0.0.1:7890"
    assert config.proxy.no_proxy == "127.0.0.1,localhost"
    assert seen[1] is binding


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


def test_native_credentials_fail_closed_until_the_broker_supplies_auth() -> None:
    host_keys = FakeHostKeys((_candidate(),))
    lifecycle = DesktopRemoteLifecycle(cast(object, host_keys))

    with pytest.raises(RemoteCredentialUnavailableError):
        lifecycle.connect(_profile(authentication_kind="native_password"))

    assert lifecycle.snapshot().state == "disconnected"
    assert host_keys.probes == []


def test_disconnect_supersedes_a_late_connect_and_closes_its_transport() -> None:
    started = Event()
    release = Event()
    transport = FakeTransport(started=started, release=release)
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
    release.set()
    thread.join(2)

    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], RemoteLifecycleSupersededError)
    assert lifecycle.snapshot().state == "disconnected"
    assert transport.closed


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


def test_remote_profile_projection_has_no_credential_or_local_path() -> None:
    config = remote_profile_config(_profile())

    assert config.auth.method == "ssh_agent"
    assert config.auth.private_key_path is None
    assert config.auth.password_ref is None
    assert config.path is None
    assert config.workspace_root is None
