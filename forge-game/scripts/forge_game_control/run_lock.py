from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

from .errors import RunLockError


class RunFileLock:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._stream: BinaryIO | None = None

    def __enter__(self) -> "RunFileLock":
        if self._path.is_symlink():
            raise RunLockError(f"Run lock path must not be a symlink: {self._path}")
        self._stream = self._path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._stream.seek(0)
                if self._stream.read(1) == b"":
                    self._stream.write(b"\0")
                    self._stream.flush()
                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    self._stream.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        except (BlockingIOError, OSError) as exc:
            self._stream.close()
            self._stream = None
            raise RunLockError(f"Run is already locked: {self._path.parent.name}") from exc
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None
