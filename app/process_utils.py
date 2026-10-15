"""Safe subprocess helpers: argument arrays only, never a shell."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from contextlib import suppress
from typing import Any

_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class _ThreadedProcess:
    """Popen wrapper exposing the asyncio.subprocess.Process interface used by this app.

    Needed because uvicorn may run a SelectorEventLoop on Windows (e.g. with --reload),
    which does not implement asyncio subprocess support.
    """

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self._popen = popen

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def returncode(self) -> int | None:
        return self._popen.poll()

    async def communicate(self) -> tuple[bytes, bytes]:
        stdout, stderr = await asyncio.to_thread(self._popen.communicate)
        return stdout or b"", stderr or b""

    async def wait(self) -> int:
        return await asyncio.to_thread(self._popen.wait)

    def terminate(self) -> None:
        self._popen.terminate()

    def kill(self) -> None:
        self._popen.kill()


def _validate_args(args: list[str]) -> list[str]:
    if not isinstance(args, list) or not args:
        raise ValueError("Command must be a non-empty argument list.")
    if not all(isinstance(item, str) for item in args):
        raise ValueError("All command arguments must be strings.")
    if any("\x00" in item for item in args):
        raise ValueError("Command arguments must not contain NUL characters.")
    return list(args)


async def spawn(args: list[str], *, capture_output: bool = True) -> Any:
    """Start a process from an argument array (no shell). Returns an asyncio-like process object."""
    args = _validate_args(args)
    stream = asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
    try:
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            creationflags=_CREATION_FLAGS,
        )
    except NotImplementedError:
        popen_stream = subprocess.PIPE if capture_output else subprocess.DEVNULL
        popen = subprocess.Popen(  # noqa: S603 - argument list, shell=False
            args,
            stdin=subprocess.DEVNULL,
            stdout=popen_stream,
            stderr=popen_stream,
            shell=False,
            creationflags=_CREATION_FLAGS,
        )
        return _ThreadedProcess(popen)


async def stop_process(process: Any, grace_seconds: float = 3.0) -> None:
    """Terminate a process, escalating to kill if it does not exit in time."""
    if process is None or process.returncode is not None:
        return
    with suppress(ProcessLookupError, OSError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except asyncio.TimeoutError:
        pass
    with suppress(ProcessLookupError, OSError):
        process.kill()
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)


async def run_capture(args: list[str], timeout_seconds: float) -> tuple[int | None, bytes, bytes]:
    """Run a short command and capture output. Returns (returncode, stdout, stderr); kills on timeout."""
    process = await spawn(args)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        await stop_process(process)
        raise
    return process.returncode, stdout or b"", stderr or b""
