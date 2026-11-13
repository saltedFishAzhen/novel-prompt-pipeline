from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def fingerprint(value) -> str:
    return hashlib.sha256(json_text(value).encode("utf-8")).hexdigest()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temp.write_text(json_text(data) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class RunLock:
    """Prevent concurrent writers with an operating-system-backed lock.

    The lock file itself may remain on disk after release or a process crash.
    Its presence is not an indication that the lock is currently held.
    """

    def __init__(self, root: Path):
        self.path = root / ".run.lock"
        self._lock = FileLock(str(self.path), timeout=0)

    def __enter__(self):
        if self.path.is_symlink():
            raise RuntimeError(f"Refusing a symbolic-link run lock: {self.path}")
        # Previous releases used an exclusive-created file containing a PID,
        # without an OS lock. Do not overlap a still-running legacy process.
        try:
            with self.path.open("rb") as marker:
                content = marker.read(32)
        except (FileNotFoundError, PermissionError):
            content = b""
        if re.fullmatch(rb"[1-9][0-9]{0,9}", content):
            pid = int(content)
            if _process_is_alive(pid):
                raise RuntimeError(f"Run is held by a legacy process (PID {pid}): {self.path}")
        try:
            self._lock.acquire()
        except Timeout:
            raise RuntimeError(f"Run is locked: {self.path}") from None
        return self

    def __exit__(self, *args):
        self._lock.release()


def _process_is_alive(pid: int) -> bool:
    """Fail closed if an existing legacy PID cannot be queried reliably."""
    if pid <= 0 or pid > 0xFFFFFFFF:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # ERROR_INVALID_PARAMETER means the PID does not exist; access
            # denied or an unrecognized error must remain conservatively live.
            return ctypes.get_last_error() != 87
        try:
            status = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(status)):
                return True
            return status.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except (PermissionError, OSError):
        return True
    return True
