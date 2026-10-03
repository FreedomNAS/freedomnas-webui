"""/api/updates/* — the pending-update count and the topgrade configuration."""

import json
import tomllib

from conftest import wait_for
from freedomnas.engine import CommandExecutionError


def test_status_answers_at_once_and_counts_in_the_background(client, engine):
    engine.outputs[("packages", "list_updatable")] = "vim/stable 2:9.1\nzfsutils/stable 2.3\n\n"
    first = client.get("/api/updates/status")
    assert first.status == 200
    assert first.body["pending_updates"] is None
    wait_for(lambda: client.get("/api/updates/status").body["pending_updates"] == 2)
    assert engine.called("packages", "list_updatable")[0].argv[:2] == ["apt", "list"]


def test_a_fresh_count_is_not_asked_for_again(client, engine):
    engine.outputs[("packages", "list_updatable")] = "a\n"
    client.get("/api/updates/status")
    wait_for(lambda: client.get("/api/updates/status").body["pending_updates"] == 1)
    client.get("/api/updates/status")
    assert len(engine.called("packages", "list_updatable")) == 1


def test_a_failing_count_is_reported(client, engine):
    engine.outputs[("packages", "list_updatable")] = CommandExecutionError("apt is locked")
    client.get("/api/updates/status")
    wait_for(lambda: client.get("/api/updates/status").body["error"])
    assert "locked" in client.get("/api/updates/status").body["error"]


def test_status_reports_the_topgrade_settings(client, config):
    body = client.get("/api/updates/status").body
    assert body["topgrade_configured"] is False
    assert body["auto_snapshot"] is True
    assert body["enabled_steps"] == ["system", "containers", "firmware"]


def test_status_reports_the_saved_steps(client, config):
    config.configure_topgrade(["system", "firmware"])
    assert client.get("/api/updates/status").body["enabled_steps"] == ["system", "firmware"]


def test_configure_writes_a_topgrade_file_it_accepts(client, engine, config, files):
    r = client.post("/api/updates/configure",
                    {"enabled_steps": ["system", "firmware"], "auto_snapshot": False})
    assert r.status == 200
    written = tomllib.loads(files[("updates", "topgrade_config")].read_text())
    assert written["misc"] == {
        "assume_yes": True, "no_retry": True, "run_in_tmux": False,
        "skip_notify": True, "only": ["system", "firmware"],
    }
    assert config.is_topgrade_configured() is True
    assert config.get("updates.topgrade.auto_snapshot") is False


def test_configure_defaults_to_the_usual_steps(client, files):
    r = client.post("/api/updates/configure", {})
    assert r.body["enabled_steps"] == ["system", "containers", "firmware"]
    assert json.dumps(["system", "containers", "firmware"]) in files[
        ("updates", "topgrade_config")].read_text()
