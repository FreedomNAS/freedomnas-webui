"""api/restart.py — the fingerprint of the installed trees and the marker for supervisor.sh."""

import asyncio
import os
import signal

import api.restart as restart


def test_the_fingerprint_follows_the_files_but_not_the_bytecode(tmp_path):
    tree = tmp_path / "webui"
    (tree / "api" / "__pycache__").mkdir(parents=True)
    source = tree / "api" / "main.py"
    source.write_text("a")
    before = restart.fingerprint([tree])
    (tree / "api" / "__pycache__" / "main.cpython-313.pyc").write_bytes(b"x")
    (tree / "api" / "other.pyc").write_bytes(b"x")
    assert restart.fingerprint([tree]) == before
    os.utime(source, ns=(1, 1))
    assert restart.fingerprint([tree]) != before
    changed = restart.fingerprint([tree])
    source.unlink()
    assert restart.fingerprint([tree]) != changed


def test_a_restart_leaves_the_marker_then_stops_the_process(tmp_path, monkeypatch):
    marker = tmp_path / "etc" / "webui-restart"
    signals = []
    monkeypatch.setattr(restart, "marker_path", lambda engine: marker)
    monkeypatch.setattr(restart.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    async def ask():
        restart.request_restart(object(), delay=0)
        assert signals == []
        await asyncio.sleep(0.05)

    asyncio.run(ask())
    assert marker.read_text() == f"{restart.STARTED}\n"
    assert signals == [(os.getpid(), signal.SIGTERM)]
