"""Single-writer lock for indexing.

Three things can write to the derived collections: a manual `indexer.cli
index`, the scheduled sync, and Claude calling the MCP `sync_index` tool.
Racing two of them means concurrent delete-then-write of the same points --
Qdrant tolerates it, but the interleaving can transiently drop chunks. All
three paths therefore go through this one lock.

O_EXCL creation is atomic on every platform we care about. A lock older than
STALE_SECONDS belongs to a crashed run and is stolen.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

# The HOLDER refreshes the lock's mtime as it works (see Pipeline's per-file
# heartbeat), so staleness only means "holder died". 5 minutes keeps the
# post-crash BUSY window short; without the heartbeat this had to be 30
# minutes to survive a long first index, which made crashes expensive.
STALE_SECONDS = 300


def heartbeat() -> None:
    """Refresh the lock's mtime; called periodically by the work loop."""
    import contextlib

    with contextlib.suppress(OSError):
        os.utime(LOCK_PATH)

LOCK_PATH = Path(__file__).resolve().parent.parent / "data" / ".index.lock"


@contextmanager
def index_lock():
    """Yield True if the lock was acquired (caller must do the work), False if
    another indexer holds it (caller should report busy and do nothing)."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    try:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            acquired = True
        except FileExistsError:
            try:
                age = time.time() - LOCK_PATH.stat().st_mtime
            except OSError:  # vanished between exists-check and stat: retry once
                age = STALE_SECONDS + 1
            if age >= STALE_SECONDS:
                LOCK_PATH.unlink(missing_ok=True)
                try:
                    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, str(os.getpid()).encode())
                    os.close(fd)
                    acquired = True
                except FileExistsError:
                    # Two processes can both see the stale lock and both try
                    # to steal it; the loser must report busy, not crash out
                    # of the context manager with an uncaught exception.
                    pass
        yield acquired
    finally:
        if acquired:
            LOCK_PATH.unlink(missing_ok=True)


def holder_age_seconds() -> float | None:
    """Age of the current lock, or None if unlocked. For busy messages."""
    try:
        return time.time() - LOCK_PATH.stat().st_mtime
    except OSError:
        return None
