"""Descriptor-only extended attribute operations for supported sidecar hosts."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from typing import Protocol


MAX_XATTR_VALUE_BYTES = 4096
_DARWIN_XATTR_POSITION = 0
_DARWIN_XATTR_OPTIONS = 0


class _OsXattrApi(Protocol):
    def getxattr(self, path: int, attribute: str | bytes) -> bytes: ...

    def setxattr(self, path: int, attribute: str | bytes, value: bytes) -> None: ...

    def removexattr(self, path: int, attribute: str | bytes) -> None: ...


def _require_descriptor(descriptor: int) -> int:
    if isinstance(descriptor, bool) or not isinstance(descriptor, int) or descriptor < 0:
        raise TypeError("xattr target must be a non-negative file descriptor")
    return descriptor


def _require_name(name: str | bytes) -> bytes:
    if not isinstance(name, (str, bytes)):
        raise TypeError("xattr name must be str or bytes")
    encoded = os.fsencode(name)
    if not encoded or b"\0" in encoded:
        raise ValueError("xattr name must be non-empty and contain no NUL bytes")
    return encoded


def _require_max_bytes(max_bytes: int) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("xattr max_bytes must be a non-negative integer")
    return max_bytes


def _require_value(value: bytes, max_bytes: int) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError("xattr value must be bytes")
    if len(value) > max_bytes:
        raise OSError(errno.E2BIG, "extended attribute value exceeds the configured limit")
    return value


class _LinuxXattrs:
    def __init__(self, os_module: _OsXattrApi) -> None:
        self._os = os_module

    def getxattr(
        self,
        descriptor: int,
        name: str | bytes,
        *,
        max_bytes: int = MAX_XATTR_VALUE_BYTES,
    ) -> bytes:
        descriptor = _require_descriptor(descriptor)
        _require_name(name)
        max_bytes = _require_max_bytes(max_bytes)
        value = self._os.getxattr(descriptor, name)
        if len(value) > max_bytes:
            raise OSError(errno.E2BIG, "extended attribute value exceeds the configured limit")
        return value

    def setxattr(
        self,
        descriptor: int,
        name: str | bytes,
        value: bytes,
        *,
        max_bytes: int = MAX_XATTR_VALUE_BYTES,
    ) -> None:
        descriptor = _require_descriptor(descriptor)
        _require_name(name)
        value = _require_value(value, _require_max_bytes(max_bytes))
        self._os.setxattr(descriptor, name, value)

    def removexattr(self, descriptor: int, name: str | bytes) -> None:
        descriptor = _require_descriptor(descriptor)
        _require_name(name)
        self._os.removexattr(descriptor, name)


class _DarwinXattrs:
    def __init__(self, libc: object) -> None:
        self._fgetxattr = libc.fgetxattr
        self._fgetxattr.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        self._fgetxattr.restype = ctypes.c_ssize_t

        self._fsetxattr = libc.fsetxattr
        self._fsetxattr.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        self._fsetxattr.restype = ctypes.c_int

        self._fremovexattr = libc.fremovexattr
        self._fremovexattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        self._fremovexattr.restype = ctypes.c_int

    @staticmethod
    def _errno_or_eio() -> int:
        return ctypes.get_errno() or errno.EIO

    @staticmethod
    def _raise_errno(operation: str, error_number: int) -> None:
        raise OSError(error_number, f"{operation} failed: {os.strerror(error_number)}")

    def getxattr(
        self,
        descriptor: int,
        name: str | bytes,
        *,
        max_bytes: int = MAX_XATTR_VALUE_BYTES,
    ) -> bytes:
        descriptor = _require_descriptor(descriptor)
        encoded_name = _require_name(name)
        remaining_bytes = _require_max_bytes(max_bytes)

        while True:
            ctypes.set_errno(0)
            size = self._fgetxattr(
                descriptor,
                encoded_name,
                None,
                0,
                _DARWIN_XATTR_POSITION,
                _DARWIN_XATTR_OPTIONS,
            )
            if size < 0:
                self._raise_errno("fgetxattr", self._errno_or_eio())
            if size > remaining_bytes:
                raise OSError(
                    errno.E2BIG,
                    "extended attribute reads exceed the configured cumulative limit",
                )
            if size == 0:
                return b""

            remaining_bytes -= size
            buffer = ctypes.create_string_buffer(size)
            ctypes.set_errno(0)
            read_size = self._fgetxattr(
                descriptor,
                encoded_name,
                buffer,
                size,
                _DARWIN_XATTR_POSITION,
                _DARWIN_XATTR_OPTIONS,
            )
            if read_size >= 0:
                if read_size > size:
                    raise OSError(errno.EOVERFLOW, "fgetxattr returned an invalid value size")
                return buffer.raw[:read_size]

            error_number = self._errno_or_eio()
            if error_number != errno.ERANGE:
                self._raise_errno("fgetxattr", error_number)

    def setxattr(
        self,
        descriptor: int,
        name: str | bytes,
        value: bytes,
        *,
        max_bytes: int = MAX_XATTR_VALUE_BYTES,
    ) -> None:
        descriptor = _require_descriptor(descriptor)
        encoded_name = _require_name(name)
        value = _require_value(value, _require_max_bytes(max_bytes))
        buffer = ctypes.create_string_buffer(value, len(value)) if value else None
        ctypes.set_errno(0)
        result = self._fsetxattr(
            descriptor,
            encoded_name,
            buffer,
            len(value),
            _DARWIN_XATTR_POSITION,
            _DARWIN_XATTR_OPTIONS,
        )
        if result < 0:
            self._raise_errno("fsetxattr", self._errno_or_eio())

    def removexattr(self, descriptor: int, name: str | bytes) -> None:
        descriptor = _require_descriptor(descriptor)
        encoded_name = _require_name(name)
        ctypes.set_errno(0)
        result = self._fremovexattr(descriptor, encoded_name, _DARWIN_XATTR_OPTIONS)
        if result < 0:
            self._raise_errno("fremovexattr", self._errno_or_eio())


class _UnsupportedXattrs:
    @staticmethod
    def _raise() -> None:
        raise OSError(errno.ENOTSUP, f"descriptor xattrs are unsupported on {sys.platform}")

    def getxattr(
        self,
        descriptor: int,
        name: str | bytes,
        *,
        max_bytes: int = MAX_XATTR_VALUE_BYTES,
    ) -> bytes:
        _require_descriptor(descriptor)
        _require_name(name)
        _require_max_bytes(max_bytes)
        self._raise()

    def setxattr(
        self,
        descriptor: int,
        name: str | bytes,
        value: bytes,
        *,
        max_bytes: int = MAX_XATTR_VALUE_BYTES,
    ) -> None:
        _require_descriptor(descriptor)
        _require_name(name)
        _require_value(value, _require_max_bytes(max_bytes))
        self._raise()

    def removexattr(self, descriptor: int, name: str | bytes) -> None:
        _require_descriptor(descriptor)
        _require_name(name)
        self._raise()


if sys.platform.startswith("linux"):
    _BACKEND = _LinuxXattrs(os)
elif sys.platform == "darwin":
    _BACKEND = _DarwinXattrs(ctypes.CDLL(None, use_errno=True))
else:
    _BACKEND = _UnsupportedXattrs()


def getxattr(
    descriptor: int,
    name: str | bytes,
    *,
    max_bytes: int = MAX_XATTR_VALUE_BYTES,
) -> bytes:
    return _BACKEND.getxattr(descriptor, name, max_bytes=max_bytes)


def setxattr(
    descriptor: int,
    name: str | bytes,
    value: bytes,
    *,
    max_bytes: int = MAX_XATTR_VALUE_BYTES,
) -> None:
    _BACKEND.setxattr(descriptor, name, value, max_bytes=max_bytes)


def removexattr(descriptor: int, name: str | bytes) -> None:
    _BACKEND.removexattr(descriptor, name)
