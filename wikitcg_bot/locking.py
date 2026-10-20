"""Verrou de run : empêche deux exécutions simultanées (elles se disputeraient les mêmes packs)."""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Iterator

STALE_AFTER = 30 * 60  # un run dure quelques minutes ; au-delà, le verrou est considéré périmé


class AlreadyRunning(Exception):
    pass


@contextlib.contextmanager
def run_lock(path: Path, stale_after: float = STALE_AFTER) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if time.time() - path.stat().st_mtime > stale_after:
            path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise AlreadyRunning(f"un autre run est en cours ({path})") from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)
