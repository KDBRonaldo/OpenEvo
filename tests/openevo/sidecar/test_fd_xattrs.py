from __future__ import annotations

import ctypes
import errno
from types import SimpleNamespace
from typing import Callable

import pytest

from desktop.sidecar import fd_xattrs


class _FakeFunction:
    def __init__(self, implementation: Callable[..., int]) -> None:
        self._implementation = implementation
        self.calls: list[tuple[object, ...]] = []
        self.argtypes: list[object] | None = None
        self.restype: object | None = None

    def __call__(self, *args: object) -> int:
        self.calls.append(args)
        return self._implementation(*args)


class _FakeLibc:
    def __init__(
        self,
        *,
        get: Callable[..., int] | None = None,
        set_: Callable[..., int] | None = None,
        remove: Callable[..., int] | None = None,
    ) -> None:
        self.fgetxattr = _FakeFunction(get or (lambda *_args: 0))
        self.fsetxattr = _FakeFunction(set_ or (lambda *_args: 0))
        self.fremovexattr = _FakeFunction(remove or (lambda *_args: 0))


def test_linux_backend_uses_only_descriptor_os_xattr_api() -> None:
    calls: list[tuple[object, ...]] = []
    os_module = SimpleNamespace(
        getxattr=lambda *args: calls.append(("get", *args)) or b"value",
        setxattr=lambda *args: calls.append(("set", *args)),
        removexattr=lambda *args: calls.append(("remove", *args)),
    )
    backend = fd_xattrs._LinuxXattrs(os_module)

    assert backend.getxattr(17, "user.test") == b"value"
    backend.setxattr(17, "user.test", b"updated")
    backend.removexattr(17, "user.test")

    assert calls == [
        ("get", 17, "user.test"),
        ("set", 17, "user.test", b"updated"),
        ("remove", 17, "user.test"),
    ]


@pytest.mark.parametrize("operation", ["getxattr", "setxattr", "removexattr"])
def test_linux_backend_rejects_paths(operation: str) -> None:
    os_module = SimpleNamespace(
        getxattr=lambda *_args: pytest.fail("getxattr must not be called"),
        setxattr=lambda *_args: pytest.fail("setxattr must not be called"),
        removexattr=lambda *_args: pytest.fail("removexattr must not be called"),
    )
    backend = fd_xattrs._LinuxXattrs(os_module)

    arguments = ("/tmp/file", "user.test")
    if operation == "setxattr":
        arguments += (b"value",)
    with pytest.raises(TypeError, match="file descriptor"):
        getattr(backend, operation)(*arguments)


def test_linux_backend_preserves_errno_and_enforces_value_limit() -> None:
    failure = OSError(errno.EIO, "injected failure")
    get_calls = 0
    set_calls = 0

    def fail_get(*_args: object) -> bytes:
        nonlocal get_calls
        get_calls += 1
        if get_calls == 1:
            raise failure
        return b"12345"

    def record_set(*_args: object) -> None:
        nonlocal set_calls
        set_calls += 1

    backend = fd_xattrs._LinuxXattrs(
        SimpleNamespace(
            getxattr=fail_get,
            setxattr=record_set,
            removexattr=lambda *_args: None,
        )
    )

    with pytest.raises(OSError) as caught:
        backend.getxattr(9, "user.test", max_bytes=4)
    assert caught.value is failure

    with pytest.raises(OSError) as caught:
        backend.getxattr(9, "user.test", max_bytes=4)
    assert caught.value.errno == errno.E2BIG

    with pytest.raises(OSError) as caught:
        backend.setxattr(9, "user.test", b"12345", max_bytes=4)
    assert caught.value.errno == errno.E2BIG
    assert set_calls == 0


def test_darwin_getxattr_uses_probe_then_fd_read() -> None:
    payload = b"darwin-value"

    def get(
        descriptor: int,
        name: bytes,
        value: object,
        size: int,
        position: int,
        options: int,
    ) -> int:
        assert descriptor == 23
        assert name == b"user.test"
        assert position == 0
        assert options == 0
        if value is None:
            assert size == 0
            return len(payload)
        assert size == len(payload)
        ctypes.memmove(value, payload, len(payload))
        return len(payload)

    libc = _FakeLibc(get=get)
    backend = fd_xattrs._DarwinXattrs(libc)

    assert backend.getxattr(23, "user.test") == payload
    assert len(libc.fgetxattr.calls) == 2
    assert libc.fgetxattr.argtypes == [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    assert libc.fsetxattr.argtypes == [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    assert libc.fremovexattr.argtypes == [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    assert libc.fgetxattr.restype is ctypes.c_ssize_t
    assert libc.fsetxattr.restype is ctypes.c_int
    assert libc.fremovexattr.restype is ctypes.c_int


def test_darwin_getxattr_preserves_errno() -> None:
    def get(*_args: object) -> int:
        ctypes.set_errno(errno.ENODATA)
        return -1

    backend = fd_xattrs._DarwinXattrs(_FakeLibc(get=get))

    with pytest.raises(OSError) as caught:
        backend.getxattr(31, "user.missing")
    assert caught.value.errno == errno.ENODATA


def test_darwin_getxattr_bounds_cumulative_erange_allocations() -> None:
    probes = iter((3, 4))

    def get(
        _descriptor: int,
        _name: bytes,
        value: object,
        _size: int,
        _position: int,
        _options: int,
    ) -> int:
        if value is None:
            return next(probes)
        ctypes.set_errno(errno.ERANGE)
        return -1

    libc = _FakeLibc(get=get)
    backend = fd_xattrs._DarwinXattrs(libc)

    with pytest.raises(OSError) as caught:
        backend.getxattr(11, "user.growing", max_bytes=6)
    assert caught.value.errno == errno.E2BIG
    assert len(libc.fgetxattr.calls) == 3


def test_darwin_getxattr_accepts_exact_limit_and_rejects_larger_probe() -> None:
    payload = b"1234"

    def get(
        _descriptor: int,
        _name: bytes,
        value: object,
        _size: int,
        _position: int,
        _options: int,
    ) -> int:
        if value is None:
            return len(payload)
        ctypes.memmove(value, payload, len(payload))
        return len(payload)

    backend = fd_xattrs._DarwinXattrs(_FakeLibc(get=get))
    assert backend.getxattr(4, "user.test", max_bytes=4) == payload

    with pytest.raises(OSError) as caught:
        backend.getxattr(4, "user.test", max_bytes=3)
    assert caught.value.errno == errno.E2BIG


def test_darwin_set_and_remove_use_fd_contract_and_preserve_errno() -> None:
    payload = b"a\0b"

    def set_(
        descriptor: int,
        name: bytes,
        value: object,
        size: int,
        position: int,
        options: int,
    ) -> int:
        assert (descriptor, name, size, position, options) == (
            41,
            b"user.test",
            len(payload),
            0,
            0,
        )
        assert ctypes.string_at(value, size) == payload
        return 0

    def remove(descriptor: int, name: bytes, options: int) -> int:
        assert (descriptor, name, options) == (41, b"user.test", 0)
        ctypes.set_errno(errno.EPERM)
        return -1

    libc = _FakeLibc(set_=set_, remove=remove)
    backend = fd_xattrs._DarwinXattrs(libc)
    backend.setxattr(41, "user.test", payload)

    with pytest.raises(OSError) as caught:
        backend.removexattr(41, "user.test")
    assert caught.value.errno == errno.EPERM


def test_darwin_setxattr_preserves_errno() -> None:
    def set_(*_args: object) -> int:
        ctypes.set_errno(errno.ENOSPC)
        return -1

    backend = fd_xattrs._DarwinXattrs(_FakeLibc(set_=set_))

    with pytest.raises(OSError) as caught:
        backend.setxattr(5, "user.test", b"")
    assert caught.value.errno == errno.ENOSPC


def test_darwin_set_rejects_value_over_limit_without_calling_libc() -> None:
    libc = _FakeLibc()
    backend = fd_xattrs._DarwinXattrs(libc)

    with pytest.raises(OSError) as caught:
        backend.setxattr(7, "user.test", b"12345", max_bytes=4)
    assert caught.value.errno == errno.E2BIG
    assert libc.fsetxattr.calls == []
