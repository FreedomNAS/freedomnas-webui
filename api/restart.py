"""
Restarting the WebUI after FreedomNAS itself was updated.

Python loads core, profiles, webui and the community modules once, so an
update of their packages takes effect only when the WebUI starts again. At
start-up the WebUI takes a fingerprint of those trees; while it differs from
the files now installed, a restart is needed, and the pages say so.

The WebUI never restarts on its own: jobs it runs (replication, community
module jobs) would be cut off. The administrator chooses when, and the
restart is a clean exit: a marker in the configuration directory tells
supervisor.sh that this exit is wanted, so it neither counts it as a failure
nor waits before starting the WebUI again.
"""

import asyncio
import hashlib
import os
import signal
import time
from pathlib import Path

from freedomnas import paths

TREES = ("core", "profiles", "webui", "modules")
MARKER = "webui-restart"
SKIPPED_DIRS = {"__pycache__", ".git", ".pytest_cache", "tests"}

STARTED = f"{os.getpid()}-{time.time_ns()}"


def _roots():
    return [paths.INSTALL_ROOT / tree for tree in TREES]


def fingerprint(roots=None) -> str:
    digest = hashlib.sha256()
    for root in roots if roots is not None else _roots():
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_DIRS)
            for name in sorted(filenames):
                if name.endswith(".pyc"):
                    continue
                path = Path(dirpath) / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                digest.update(f"{path}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
    return digest.hexdigest()


_at_start = fingerprint()


def restart_needed() -> bool:
    return fingerprint() != _at_start


def marker_path(engine) -> Path:
    return paths.config_dir(engine) / MARKER


def request_restart(engine, delay: float = 0.5) -> None:
    """Leave the marker, then stop once the answer to this request is out."""
    marker = marker_path(engine)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{STARTED}\n")
    asyncio.get_running_loop().call_later(delay, os.kill, os.getpid(), signal.SIGTERM)
