"""
/ws/updates/terminal — the one door that starts a system update.

The process is never topgrade: create_subprocess_exec is replaced by one
that records the argv it was given and runs a harmless script instead.
"""

import asyncio
import base64
import json

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

import websocket.terminal as terminal
from conftest import ACCOUNTS

SCRIPT = "printf 'upgrading\\ndone\\n'; echo 'a warning' >&2; exit 3"


@pytest.fixture
def launched(monkeypatch):
    argv = []
    real = asyncio.create_subprocess_exec

    async def fake(*args, **kwargs):
        argv.append(list(args))
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        return await real("sh", "-c", SCRIPT, **kwargs)

    monkeypatch.setattr(terminal.asyncio, "create_subprocess_exec", fake)
    return argv


def session(server, first_frame):
    """Send the opening frame, collect every message, return them with the close code."""
    messages = []
    with connect(f"ws://{server}/ws/updates/terminal", open_timeout=5) as ws:
        if first_frame is not None:
            ws.send(first_frame)
        try:
            while True:
                messages.append(json.loads(ws.recv(timeout=10)))
        except ConnectionClosed as closed:
            code = closed.rcvd.code if closed.rcvd else None
    return messages, code


def auth_frame(username, password):
    blob = base64.b64encode(f"{username}:{password}".encode()).decode()
    return json.dumps({"auth": blob})


def test_a_frame_that_is_not_the_auth_blob_closes_4401(server, engine, config, launched):
    messages, code = session(server, "hello")
    assert code == 4401
    assert messages == [{"stream": "stderr", "line": "Authentication required"}]
    assert launched == []


def test_silence_closes_4401(server, engine, config, launched, monkeypatch):
    monkeypatch.setattr(terminal, "AUTH_TIMEOUT_SECONDS", 0.2)
    messages, code = session(server, None)
    assert code == 4401
    assert launched == []


def test_wrong_credentials_close_4401(server, engine, config, launched):
    messages, code = session(server, auth_frame("admin", "wrong"))
    assert code == 4401
    assert messages[-1]["line"] == "Invalid credentials"
    assert launched == []


def test_a_non_administrator_closes_4403(server, engine, config, launched):
    messages, code = session(server, auth_frame("mario", ACCOUNTS["mario"][0]))
    assert code == 4403
    assert "not an administrator" in messages[-1]["line"]
    assert launched == []
    assert engine.called("zfs", "snapshot_create_recursive") == []


def test_an_update_snapshots_every_pool_then_streams_topgrade(server, engine, config, launched):
    engine.outputs[("zfs", "dataset_at_path")] = "rpool/ROOT/debian\n"
    config.add_pool({"name": "tank"})
    config.add_pool({"name": "backup"})
    messages, code = session(server, auth_frame("admin", ACCOUNTS["admin"][0]))
    assert code == 1000

    snapshots = engine.called("zfs", "snapshot_create_recursive")
    assert [c.kwargs["dataset"] for c in snapshots] == ["rpool", "tank", "backup"]
    assert all(c.argv[:3] == ["zfs", "snapshot", "-r"] for c in snapshots)
    assert all(c.kwargs["snap_name"].startswith("pre-update-") for c in snapshots)

    assert launched == [["su", "-", "admin", "-c",
                         "topgrade --yes --config /etc/freedomnas/topgrade.toml"]]
    lines = [(m["stream"], m["line"]) for m in messages if "line" in m]
    # Nobody went through /configure here, so the defaults are written first.
    assert lines[0][1].startswith("Wrote default configuration to ")
    snap = snapshots[0].kwargs["snap_name"]
    assert lines[1:4] == [("stdout", f"Snapshot created: rpool@{snap} (recursive)"),
                          ("stdout", f"Snapshot created: tank@{snap} (recursive)"),
                          ("stdout", f"Snapshot created: backup@{snap} (recursive)")]
    assert ("stdout", "upgrading") in lines
    assert ("stdout", "done") in lines
    assert ("stderr", "a warning") in lines
    assert messages[-1] == {"exit_code": 3}


def test_the_default_configuration_is_written_once(server, engine, config, files, launched):
    """
    Without topgrade.toml, topgrade runs every step it knows. The first
    update writes the defaults; a configuration already there is left alone.
    """
    path = files[("updates", "topgrade_config")]
    session(server, auth_frame("admin", ACCOUNTS["admin"][0]))
    written = path.read_text()
    assert "assume_yes = true" in written
    assert '"system"' in written and '"containers"' in written
    assert config.is_topgrade_configured()

    path.write_text("[misc]\nonly = [\"system\"]\n")
    messages, _ = session(server, auth_frame("admin", ACCOUNTS["admin"][0]))
    assert path.read_text() == "[misc]\nonly = [\"system\"]\n"
    assert not any("Wrote default configuration" in m.get("line", "") for m in messages)



def test_a_missing_file_is_rewritten_from_the_saved_steps(server, engine, config, files, launched):
    """
    The file lives on the system disk and the steps in the configuration: a
    restored system has the second without the first. They come back as saved.
    """
    config.configure_topgrade(["system"])
    path = files[("updates", "topgrade_config")]
    messages, _ = session(server, auth_frame("admin", ACCOUNTS["admin"][0]))
    assert 'only = ["system"]' in path.read_text()
    assert config.get("updates.topgrade.enabled_steps") == ["system"]
    assert any("saved configuration" in m.get("line", "") for m in messages)

def test_no_snapshot_when_auto_snapshot_is_off(server, engine, config, launched):
    config.add_pool({"name": "tank"})
    config.set("updates.topgrade.auto_snapshot", False)
    messages, _ = session(server, auth_frame("admin", ACCOUNTS["admin"][0]))
    assert engine.called("zfs", "snapshot_create_recursive") == []
    assert messages[-1] == {"exit_code": 3}


def test_a_failed_snapshot_warns_but_the_update_runs(server, engine, config, launched):
    from freedomnas.engine import CommandExecutionError
    config.add_pool({"name": "tank"})
    config.add_pool({"name": "backup"})
    engine.outputs[("zfs", "snapshot_create_recursive")] = (
        lambda dataset, **_: CommandExecutionError("out of space") if dataset == "tank" else "")
    messages, _ = session(server, auth_frame("admin", ACCOUNTS["admin"][0]))
    lines = [m.get("line", "") for m in messages]
    assert any("snapshot of tank failed" in line for line in lines)
    assert any(line.startswith("Snapshot created: backup@") for line in lines)
    assert len(launched) == 1
