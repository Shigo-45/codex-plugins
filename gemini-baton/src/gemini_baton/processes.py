from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CANCEL_KILL_GRACE_SECONDS, CANCEL_TERM_GRACE_SECONDS
from .paths import ensure_private_dir
from .util import json_dumps, json_loads, utc_now_iso

_PS = "/bin/ps" if os.path.exists("/bin/ps") else "ps"


@dataclass(frozen=True)
class GroupTermination:
    requested: bool
    confirmed: bool
    group_alive_before: bool
    group_alive_after: bool
    worker_lock_released: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "confirmed": self.confirmed,
            "group_alive_before": self.group_alive_before,
            "group_alive_after": self.group_alive_after,
            "worker_lock_released": self.worker_lock_released,
            "detail": self.detail,
        }


# -- worker ownership lock -------------------------------------------------
#
# Liveness deliberately does not depend on `ps`. A worker holds an exclusive
# advisory lock for its lifetime. The supervisor probes that lock. This works
# across a restarted MCP process and makes stale PID reuse harmless: no signal
# is ever sent based on a PID alone; the lock must still be held.


def worker_lock_path(run_dir: Path) -> Path:
    return run_dir / "worker.lock"


def acquire_worker_lock(
    lock_path: Path, metadata: dict[str, Any]
) -> Any:
    """Acquire and return the held lock file object. Keep it open until exit."""
    import fcntl

    ensure_private_dir(lock_path.parent)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    handle = os.fdopen(fd, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("another worker already holds the run lock") from None
    handle.seek(0)
    handle.truncate()
    handle.write(json_dumps(metadata, pretty=True))
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
    return handle


def worker_lock_held(lock_path: Path) -> bool:
    """Probe, without disturbing the worker, whether its lock is held."""
    import fcntl

    if not lock_path.exists():
        return False
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except FileNotFoundError:
        return False
    except OSError:
        # Never treat an unreadable ownership file as proof of death.
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return True
        return True
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        os.close(fd)


def read_worker_lock(lock_path: Path) -> dict[str, Any] | None:
    try:
        path = lock_path.resolve()
        if path.parent != lock_path.parent.resolve():
            return None
        value = json_loads(path.read_text(encoding="utf-8"), default=None)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# -- process groups --------------------------------------------------------


def group_alive(pgid: int | None) -> bool:
    if not pgid or pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Conservatively treat EPERM as a live group: never claim cleanup
        # succeeded when the OS refuses to tell us.
        return True
    except OSError:
        return True
    return True


def _signal_group(pgid: int, sig: signal.Signals) -> bool:
    if not pgid or pgid <= 1:
        return False
    if hasattr(os, "getpgrp") and pgid == os.getpgrp():
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return False


def reap_child(pid: int | None) -> None:
    if not pid or pid <= 1 or not hasattr(os, "waitpid"):
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def terminate_process_group(
    pgid: int,
    *,
    worker_pid: int | None = None,
    lock_path: Path | None = None,
    term_grace: float = CANCEL_TERM_GRACE_SECONDS,
    kill_grace: float = CANCEL_KILL_GRACE_SECONDS,
) -> GroupTermination:
    """Signal a process group that has *already been authenticated*.

    Callers must first verify that the run's worker lock is held. This avoids
    targeting a stale/reused PID. Because this process group contains the worker
    and any commands/checks it launched (they inherit the group), terminating
    the group addresses the whole in-flight tree.
    """
    before = group_alive(pgid)
    if not before:
        reap_child(worker_pid)
        lock_released = not worker_lock_held(lock_path) if lock_path else True
        return GroupTermination(
            requested=False,
            confirmed=lock_released,
            group_alive_before=False,
            group_alive_after=False,
            worker_lock_released=lock_released,
            detail="process group already had no live members",
        )
    if not _signal_group(pgid, signal.SIGTERM):
        return GroupTermination(
            requested=False,
            confirmed=False,
            group_alive_before=before,
            group_alive_after=group_alive(pgid),
            worker_lock_released=not worker_lock_held(lock_path)
            if lock_path
            else False,
            detail="SIGTERM could not be delivered",
        )
    deadline = time.monotonic() + term_grace
    while time.monotonic() < deadline:
        reap_child(worker_pid)
        if not group_alive(pgid):
            break
        time.sleep(0.05)
    else:
        _signal_group(pgid, signal.SIGKILL)
        deadline = time.monotonic() + kill_grace
        while time.monotonic() < deadline:
            reap_child(worker_pid)
            if not group_alive(pgid):
                break
            time.sleep(0.05)
    reap_child(worker_pid)
    after = group_alive(pgid)
    lock_released = not worker_lock_held(lock_path) if lock_path else None
    confirmed = not after and (lock_released is not False)
    if after:
        detail = "process group still has live members after SIGKILL"
    elif lock_released is False:
        detail = "process group is gone but the worker lock is still held"
    else:
        detail = "terminated with SIGTERM/SIGKILL and confirmed gone"
    return GroupTermination(
        requested=True,
        confirmed=confirmed,
        group_alive_before=before,
        group_alive_after=after,
        worker_lock_released=bool(lock_released),
        detail=detail,
    )


# -- command timeouts ------------------------------------------------------
# Commands intentionally share the worker's process group; cancellation of the
# group catches them. For a single command timeout, try to find descendants via
# ps when available. Some sandboxed hosts forbid process enumeration; in that
# case fall back to signalling the direct child. The README states this limit.


def _run_ps(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            [_PS, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def terminate_process_tree(pid: int, *, grace: float = 1.5) -> None:
    """Terminate a command process (and descendants where ps is permitted)."""
    if not pid or pid <= 1:
        return
    parent_map: dict[int, list[int]] = {}
    output = _run_ps(["-axo", "pid=,ppid="])
    if output is not None:
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                child = int(parts[0])
                parent = int(parts[1])
            except ValueError:
                continue
            parent_map.setdefault(parent, []).append(child)
    descendants: list[int] = []
    stack = list(parent_map.get(pid, []))
    while stack:
        current = stack.pop()
        descendants.append(current)
        stack.extend(parent_map.get(current, []))
    targets = list(dict.fromkeys(descendants + [pid]))
    for target in reversed(targets):
        try:
            os.kill(target, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not any(_pid_alive(target) for target in targets):
            return
        time.sleep(0.05)
    for target in reversed(targets):
        try:
            os.kill(target, signal.SIGKILL)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def current_process_coordinates() -> tuple[int, int]:
    pid = os.getpid()
    pgid = os.getpgid(0) if hasattr(os, "getpgid") else pid
    return pid, pgid


def new_worker_identity() -> str:
    return f"{os.getpid()}:{utc_now_iso()}:{os.urandom(8).hex()}"
