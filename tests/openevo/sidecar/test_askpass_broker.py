from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import stat
import tempfile

import pytest

from desktop.sidecar.askpass_broker import (
    AskpassAuthorizationBroker,
    AskpassBrokerError,
    ProcessIdentity,
    UnixPeerAuthority,
)


@pytest.fixture
def runtime_dir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="oe-ab-", dir="/tmp"))
    path.chmod(0o700)
    try:
        yield path
    finally:
        for child in path.iterdir():
            child.unlink()
        path.rmdir()


class _Inspector:
    def __init__(self, identities: dict[int, ProcessIdentity]) -> None:
        self.identities = identities

    def inspect(self, process_id: int) -> ProcessIdentity | None:
        return self.identities.get(process_id)


def _identity(
    process_id: int,
    *,
    parent_id: int,
    executable: str,
    group: int = 70,
    session: int = 60,
    birth: str | None = None,
) -> ProcessIdentity:
    return ProcessIdentity(
        process_id=process_id,
        parent_process_id=parent_id,
        process_group_id=group,
        session_id=session,
        user_id=os.geteuid(),
        birth_identity=birth or f"birth-{process_id}",
        executable_path=executable,
    )


def test_process_identity_accepts_posix_session_one_and_rejects_zero() -> None:
    identity = _identity(
        71,
        parent_id=1,
        executable="/usr/bin/ssh",
        group=71,
        session=1,
    )

    assert identity.session_id == 1
    with pytest.raises(ValueError, match="invalid process identity"):
        _identity(
            71,
            parent_id=1,
            executable="/usr/bin/ssh",
            group=71,
            session=0,
        )


def _request(
    *,
    capability: str,
    generation: int,
    helper_pid: int,
    ssh_parent_pid: int,
    owner_pid: int,
    kind: str = "password",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event": "authorize",
        "capability": capability,
        "connection_generation": generation,
        "helper_pid": helper_pid,
        "ssh_parent_pid": ssh_parent_pid,
        "owner_pid": owner_pid,
        "prompt_kind": kind,
        "prompt_sha256": "a" * 64,
        "prompt_bytes": 17,
    }


def _completion(
    authorization: dict[str, object],
    *,
    outcome: str,
) -> dict[str, object]:
    return {
        **authorization,
        "event": "complete",
        "outcome": outcome,
    }


def _exchange(path: Path, request: dict[str, object]) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2.0)
        client.connect(str(path))
        client.sendall(
            json.dumps(request, separators=(",", ":"), sort_keys=True).encode("ascii") + b"\n"
        )
        payload = bytearray()
        while not payload.endswith(b"\n"):
            chunk = client.recv(512 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
    return json.loads(bytes(payload).decode("ascii"))


def test_broker_creates_one_private_bounded_socket_and_redacts_capability(
    runtime_dir: Path,
) -> None:
    runtime = runtime_dir
    helper = "/Applications/OpenEvo Desktop.app/Contents/MacOS/openevo-ssh-askpass"
    broker = AskpassAuthorizationBroker(
        runtime / "a",
        helper_path=helper,
        inspector=_Inspector({}),
        hmac_key=b"k" * 32,
    )
    try:
        broker.start()
        capability = broker.issue_capability(connection_generation=4)
        metadata = os.lstat(broker.socket_path)

        assert len(os.fsencode(broker.socket_path)) <= 103
        assert stat.S_ISSOCK(metadata.st_mode)
        assert stat.S_IMODE(metadata.st_mode) == 0o600
        assert metadata.st_uid == os.geteuid()
        assert metadata.st_nlink == 1
        assert len(capability.value) == 64
        assert capability.value not in repr(capability)
        assert capability.value not in repr(broker)
    finally:
        broker.close()


def test_direct_ssh_askpass_authorization_is_single_use(runtime_dir: Path) -> None:
    runtime = runtime_dir
    helper_pid = os.getpid()
    owner = _identity(100, parent_id=50, executable="/usr/bin/ssh")
    helper = _identity(
        helper_pid,
        parent_id=100,
        executable="/Applications/OpenEvo Desktop.app/Contents/MacOS/openevo-ssh-askpass",
    )
    inspector = _Inspector({100: owner, helper_pid: helper})
    broker = AskpassAuthorizationBroker(
        runtime / "a",
        helper_path=helper.executable_path,
        inspector=inspector,
        hmac_key=b"h" * 32,
    )
    try:
        broker.start()
        capability = broker.issue_capability(connection_generation=8)
        broker.bind_owner(capability, owner)
        payload = _request(
            capability=capability.value,
            generation=8,
            helper_pid=helper_pid,
            ssh_parent_pid=100,
            owner_pid=100,
        )

        first = broker.authorize_payload(
            payload,
            peer=UnixPeerAuthority(process_id=helper_pid, user_id=os.geteuid()),
        )
        second = broker.authorize_payload(
            payload,
            peer=UnixPeerAuthority(process_id=helper_pid, user_id=os.geteuid()),
        )

        assert first is True
        assert second is False
        assert broker.authorized_prompt_count == 1
        assert broker.prompt_observation is not None
        assert broker.prompt_observation.state == "pending"
    finally:
        broker.close()


@pytest.mark.parametrize(
    ("outcome", "state"),
    [
        ("accepted", "completed"),
        ("rejected", "rejected"),
        ("cancelled", "cancelled"),
    ],
)
def test_prompt_completion_is_bound_to_the_authorized_helper_and_has_no_secret(
    runtime_dir: Path,
    outcome: str,
    state: str,
) -> None:
    helper_pid = os.getpid()
    owner = _identity(150, parent_id=50, executable="/usr/bin/ssh")
    helper = _identity(helper_pid, parent_id=150, executable="/tmp/helper")
    broker = AskpassAuthorizationBroker(
        runtime_dir / "a",
        helper_path=helper.executable_path,
        inspector=_Inspector({150: owner, helper_pid: helper}),
        hmac_key=b"o" * 32,
    )
    try:
        broker.start()
        capability = broker.issue_capability(connection_generation=10)
        broker.bind_owner(capability, owner)
        authorization = _request(
            capability=capability.value,
            generation=10,
            helper_pid=helper_pid,
            ssh_parent_pid=150,
            owner_pid=150,
            kind="host_confirmation",
        )
        peer = UnixPeerAuthority(process_id=helper_pid, user_id=os.geteuid())

        assert broker.authorize_payload(authorization, peer=peer)
        assert broker.complete_payload(
            _completion(authorization, outcome=outcome),
            peer=peer,
        )
        assert broker.prompt_observation is not None
        assert broker.prompt_observation.kind == "host_confirmation"
        assert broker.prompt_observation.state == state
        assert not broker.complete_payload(
            _completion(authorization, outcome=outcome),
            peer=peer,
        )
    finally:
        broker.close()


def test_prompt_observer_receives_only_generation_kind_and_state(
    runtime_dir: Path,
) -> None:
    helper_pid = os.getpid()
    owner = _identity(175, parent_id=50, executable="/usr/bin/ssh")
    helper = _identity(helper_pid, parent_id=175, executable="/tmp/helper")
    observations: list[object] = []
    broker = AskpassAuthorizationBroker(
        runtime_dir / "a",
        helper_path=helper.executable_path,
        inspector=_Inspector({175: owner, helper_pid: helper}),
        hmac_key=b"p" * 32,
        observation_callback=observations.append,
    )
    try:
        broker.start()
        capability = broker.issue_capability(connection_generation=11)
        broker.bind_owner(capability, owner)
        authorization = _request(
            capability=capability.value,
            generation=11,
            helper_pid=helper_pid,
            ssh_parent_pid=175,
            owner_pid=175,
            kind="passphrase",
        )
        peer = UnixPeerAuthority(process_id=helper_pid, user_id=os.geteuid())

        assert broker.authorize_payload(authorization, peer=peer)
        assert broker.complete_payload(
            _completion(authorization, outcome="accepted"),
            peer=peer,
        )

        assert [
            (item.connection_generation, item.kind, item.state)
            for item in observations
        ] == [
            (11, "passphrase", "pending"),
            (11, "passphrase", "completed"),
        ]
        assert all(not hasattr(item, "prompt") for item in observations)
    finally:
        broker.close()


def test_proxyjump_descendant_shape_is_bound_to_the_outer_owned_ssh(
    runtime_dir: Path,
) -> None:
    runtime = runtime_dir
    helper_pid = os.getpid()
    owner = _identity(200, parent_id=40, executable="/usr/bin/ssh")
    jump_ssh = _identity(210, parent_id=200, executable="/usr/bin/ssh")
    helper = _identity(
        helper_pid,
        parent_id=210,
        executable="/Applications/OpenEvo Desktop.app/Contents/MacOS/openevo-ssh-askpass",
    )
    broker = AskpassAuthorizationBroker(
        runtime / "a",
        helper_path=helper.executable_path,
        inspector=_Inspector({200: owner, 210: jump_ssh, helper_pid: helper}),
        hmac_key=b"p" * 32,
    )
    try:
        broker.start()
        capability = broker.issue_capability(connection_generation=11)
        broker.bind_owner(capability, owner)

        assert broker.authorize_payload(
            _request(
                capability=capability.value,
                generation=11,
                helper_pid=helper_pid,
                ssh_parent_pid=210,
                owner_pid=200,
                kind="passphrase",
            ),
            peer=UnixPeerAuthority(process_id=helper_pid, user_id=os.geteuid()),
        )
    finally:
        broker.close()


def test_proxycommand_wrapper_may_be_in_the_bound_ancestry(
    runtime_dir: Path,
) -> None:
    runtime = runtime_dir
    helper_pid = os.getpid()
    owner = _identity(300, parent_id=30, executable="/usr/bin/ssh")
    wrapper = _identity(310, parent_id=300, executable="/usr/local/bin/proxy-wrapper")
    nested_ssh = _identity(320, parent_id=310, executable="/usr/bin/ssh")
    helper = _identity(
        helper_pid,
        parent_id=320,
        executable="/Applications/OpenEvo Desktop.app/Contents/MacOS/openevo-ssh-askpass",
    )
    broker = AskpassAuthorizationBroker(
        runtime / "a",
        helper_path=helper.executable_path,
        inspector=_Inspector({300: owner, 310: wrapper, 320: nested_ssh, helper_pid: helper}),
        hmac_key=b"q" * 32,
    )
    try:
        broker.start()
        capability = broker.issue_capability(connection_generation=12)
        broker.bind_owner(capability, owner)

        assert broker.authorize_payload(
            _request(
                capability=capability.value,
                generation=12,
                helper_pid=helper_pid,
                ssh_parent_pid=320,
                owner_pid=300,
            ),
            peer=UnixPeerAuthority(process_id=helper_pid, user_id=os.geteuid()),
        )
    finally:
        broker.close()


@pytest.mark.parametrize(
    "mutation",
    [
        {"prompt": "Password: secret"},
        {"response": "secret"},
        {"unknown": True},
        {"prompt_kind": "keyboard_interactive"},
        {"prompt_bytes": 2_049},
        {"prompt_sha256": "not-a-digest"},
        {"schema_version": 2},
    ],
)
def test_broker_rejects_open_or_secret_bearing_payloads(
    runtime_dir: Path,
    mutation: dict[str, object],
) -> None:
    runtime = runtime_dir
    helper_pid = os.getpid()
    owner = _identity(400, parent_id=20, executable="/usr/bin/ssh")
    helper = _identity(helper_pid, parent_id=400, executable="/tmp/helper")
    broker = AskpassAuthorizationBroker(
        runtime / "a",
        helper_path=helper.executable_path,
        inspector=_Inspector({400: owner, helper_pid: helper}),
        hmac_key=b"x" * 32,
    )
    try:
        broker.start()
        capability = broker.issue_capability(connection_generation=3)
        broker.bind_owner(capability, owner)
        payload = _request(
            capability=capability.value,
            generation=3,
            helper_pid=helper_pid,
            ssh_parent_pid=400,
            owner_pid=400,
        )
        payload.update(mutation)

        assert not broker.authorize_payload(
            payload,
            peer=UnixPeerAuthority(process_id=helper_pid, user_id=os.geteuid()),
        )
    finally:
        broker.close()


def test_cancelled_generation_denies_and_zeroes_outstanding_capability(
    runtime_dir: Path,
) -> None:
    runtime = runtime_dir
    helper_pid = os.getpid()
    owner = _identity(500, parent_id=10, executable="/usr/bin/ssh")
    helper = _identity(helper_pid, parent_id=500, executable="/tmp/helper")
    broker = AskpassAuthorizationBroker(
        runtime / "a",
        helper_path=helper.executable_path,
        inspector=_Inspector({500: owner, helper_pid: helper}),
        hmac_key=b"c" * 32,
    )
    try:
        broker.start()
        capability = broker.issue_capability(connection_generation=21)
        broker.bind_owner(capability, owner)
        broker.cancel_generation(21)

        assert not broker.authorize_payload(
            _request(
                capability=capability.value,
                generation=21,
                helper_pid=helper_pid,
                ssh_parent_pid=500,
                owner_pid=500,
            ),
            peer=UnixPeerAuthority(process_id=helper_pid, user_id=os.geteuid()),
        )
        assert broker.cancelled
    finally:
        broker.close()


def test_concurrent_replay_authorizes_exactly_one_request(runtime_dir: Path) -> None:
    runtime = runtime_dir
    helper_pid = os.getpid()
    owner = _identity(600, parent_id=10, executable="/usr/bin/ssh")
    helper = _identity(helper_pid, parent_id=600, executable="/tmp/helper")
    broker = AskpassAuthorizationBroker(
        runtime / "a",
        helper_path=helper.executable_path,
        inspector=_Inspector({600: owner, helper_pid: helper}),
        hmac_key=b"r" * 32,
    )
    try:
        broker.start()
        capability = broker.issue_capability(connection_generation=5)
        broker.bind_owner(capability, owner)
        payload = _request(
            capability=capability.value,
            generation=5,
            helper_pid=helper_pid,
            ssh_parent_pid=600,
            owner_pid=600,
        )
        peer = UnixPeerAuthority(process_id=helper_pid, user_id=os.geteuid())
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(lambda _index: broker.authorize_payload(payload, peer=peer), range(8))
            )

        assert results.count(True) == 1
        assert results.count(False) == 7
    finally:
        broker.close()


def test_socket_protocol_is_bounded_closed_and_never_returns_response_bytes(
    runtime_dir: Path,
) -> None:
    runtime = runtime_dir
    helper_pid = os.getpid()
    owner = _identity(700, parent_id=10, executable="/usr/bin/ssh")
    helper = _identity(helper_pid, parent_id=700, executable="/tmp/helper")
    broker = AskpassAuthorizationBroker(
        runtime / "a",
        helper_path=helper.executable_path,
        inspector=_Inspector({700: owner, helper_pid: helper}),
        hmac_key=b"s" * 32,
    )
    try:
        broker.start()
        capability = broker.issue_capability(connection_generation=6)
        broker.bind_owner(capability, owner)
        request = _request(
            capability=capability.value,
            generation=6,
            helper_pid=helper_pid,
            ssh_parent_pid=700,
            owner_pid=700,
        )
        response = _exchange(broker.socket_path, request)
        completion = _exchange(
            broker.socket_path,
            _completion(request, outcome="cancelled"),
        )

        assert response == {"authorized": True, "schema_version": 1}
        assert completion == {"authorized": True, "schema_version": 1}
        assert set(response) == {"authorized", "schema_version"}
    finally:
        broker.close()


def test_owner_pid_reuse_and_broker_socket_replacement_fail_closed(
    runtime_dir: Path,
) -> None:
    runtime = runtime_dir
    helper_pid = os.getpid()
    owner = _identity(800, parent_id=10, executable="/usr/bin/ssh")
    helper = _identity(helper_pid, parent_id=800, executable="/tmp/helper")
    inspector = _Inspector({800: owner, helper_pid: helper})
    broker = AskpassAuthorizationBroker(
        runtime / "a",
        helper_path=helper.executable_path,
        inspector=inspector,
        hmac_key=b"z" * 32,
    )
    broker.start()
    capability = broker.issue_capability(connection_generation=7)
    broker.bind_owner(capability, owner)
    inspector.identities[800] = _identity(
        800,
        parent_id=10,
        executable="/usr/bin/ssh",
        birth="reused-pid",
    )
    assert not broker.authorize_payload(
        _request(
            capability=capability.value,
            generation=7,
            helper_pid=helper_pid,
            ssh_parent_pid=800,
            owner_pid=800,
        ),
        peer=UnixPeerAuthority(process_id=helper_pid, user_id=os.geteuid()),
    )
    broker.socket_path.unlink()
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement.bind(str(runtime / "a"))
    try:
        with pytest.raises(AskpassBrokerError, match="socket identity"):
            broker.close()
    finally:
        replacement.close()
        (runtime / "a").unlink()
