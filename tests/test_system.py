"""/api/system/* — identity, power, and the configuration export/import."""

import pytest
import yaml

from freedomnas.engine import CommandExecutionError


def test_status_needs_no_credentials(anonymous, config):
    r = anonymous.get("/api/system/status")
    assert r.status == 200
    assert r.body == {"status": "ok"}


def test_info_reports_what_the_system_says(client, engine, config):
    config.set_hostname("nas")
    engine.outputs[("system", "uptime")] = "2026-09-22 08:00:00"
    engine.outputs[("system", "timezone_get")] = "Europe/Rome"
    engine.outputs[("bootloader", "firmware_mode")] = "UEFI\n"
    r = client.get("/api/system/info")
    assert r.status == 200
    assert r.body == {
        "hostname": "nas",
        "edition": "debian",
        "timezone": "Europe/Rome",
        "uptime_since": "2026-09-22 08:00:00",
        "initialized": True,
        "version": None,
        "init_system": "systemd",
        "firmware": "UEFI",
    }


def test_info_reports_the_installed_version(client, engine, monkeypatch, tmp_path):
    version = tmp_path / "VERSION"
    version.write_text("1.0+20260927.abc1234\n")
    import api.routes.system as system_routes
    monkeypatch.setattr(system_routes, "_VERSION_FILE", version)
    assert client.get("/api/system/info").body["version"] == "1.0+20260927.abc1234"


def test_info_reports_a_failing_command(client, engine):
    engine.outputs[("system", "uptime")] = CommandExecutionError("uptime: broken")
    r = client.get("/api/system/info")
    assert r.status == 500
    assert "broken" in r.body["detail"]


def test_hostname_is_set_on_the_system_then_in_the_config(client, engine, config):
    r = client.post("/api/system/hostname", {"hostname": "  nas  "})
    assert r.status == 200
    assert r.body == {"hostname": "nas"}
    [call] = engine.called("system", "hostname_set")
    assert call.argv == ["hostnamectl", "set-hostname", "nas"]
    assert config.get_hostname() == "nas"


def test_hostname_is_required(client, engine):
    assert client.post("/api/system/hostname", {"hostname": "  "}).status == 422
    assert engine.actions() == []


def test_timezone_is_set_on_the_system_then_in_the_config(client, engine, config):
    r = client.post("/api/system/timezone", {"timezone": " Asia/Tokyo "})
    assert r.status == 200
    assert r.body == {"timezone": "Asia/Tokyo"}
    [call] = engine.called("system", "timezone_set")
    assert call.argv == ["timedatectl", "set-timezone", "Asia/Tokyo"]
    assert config.get("system.timezone") == "Asia/Tokyo"


@pytest.mark.parametrize("timezone, message", [
    ("", "required"),
    ("../../../etc/shadow", "not a timezone name"),
    ("Europe/../../etc/passwd", "not a timezone name"),
    ("-u", "not a timezone name"),
    ("Europe/Rome; reboot", "not a timezone name"),
    ("Mars/Base", "Unknown timezone"),
])
def test_a_timezone_that_is_not_one_is_refused_before_anything_runs(client, engine, config,
                                                                    timezone, message):
    r = client.post("/api/system/timezone", {"timezone": timezone})
    assert r.status == 422
    assert message in r.body["detail"]
    assert engine.actions() == []
    assert config.get("system.timezone") != timezone


def test_a_timezone_the_system_refuses_is_a_500_and_not_recorded(client, engine, config):
    engine.outputs[("system", "timezone_set")] = CommandExecutionError("timedatectl failed")
    r = client.post("/api/system/timezone", {"timezone": "Asia/Tokyo"})
    assert r.status == 500
    assert config.get("system.timezone") != "Asia/Tokyo"


def test_a_hostile_hostname_stays_one_argument(client, engine):
    client.post("/api/system/hostname", {"hostname": "nas; reboot"})
    [call] = engine.called("system", "hostname_set")
    assert call.argv == ["hostnamectl", "set-hostname", "nas; reboot"]


def test_a_hostname_that_looks_like_an_option_is_refused(client, engine, config):
    r = client.post("/api/system/hostname", {"hostname": "--help"})
    assert r.status == 500
    assert "may not start with" in r.body["detail"]
    assert config.get_hostname() != "--help"


def test_reboot_and_shutdown(client, engine):
    assert client.post("/api/system/reboot").body == {"detail": "Rebooting"}
    assert client.post("/api/system/shutdown").body == {"detail": "Shutting down"}
    assert [c.argv for c in engine.calls if c.category == "system"] == [
        ["systemctl", "reboot"], ["systemctl", "poweroff"],
    ]


def test_power_needs_an_administrator(anonymous, engine, config):
    assert anonymous.post("/api/system/reboot").status == 401
    assert engine.actions() == []


def test_the_export_is_the_configuration_as_yaml(client, config):
    config.set_hostname("nas")
    r = client.get("/api/system/config")
    assert r.status == 200
    assert r.headers["content-type"].startswith("application/x-yaml")
    assert 'filename="nas-config.yaml"' in r.headers["content-disposition"]
    assert yaml.safe_load(r.body) == config.config


def test_an_import_that_is_not_yaml_is_refused(client, config):
    before = config.config_path.read_text()
    r = client.post("/api/system/config", raw=b"system: [unclosed")
    assert r.status == 422
    assert "Not valid YAML" in r.body["detail"]
    assert config.config_path.read_text() == before


def test_an_import_that_is_not_a_mapping_is_refused(client, config):
    r = client.post("/api/system/config", raw=b"- just\n- a list\n")
    assert r.status == 422
    assert "mapping" in r.body["detail"]


def test_an_import_is_saved_then_applied(client, engine, config):
    exported = yaml.safe_load(client.get("/api/system/config").body)
    exported["system"]["hostname"] = "restored"
    r = client.post("/api/system/config", raw=yaml.safe_dump(exported).encode())
    assert r.status == 200
    assert set(r.body) == {"mirrored_to", "applied", "skipped", "failed"}
    assert r.body["failed"] == []
    assert config.get_hostname() == "restored"
    assert yaml.safe_load(config.config_path.read_text())["system"]["hostname"] == "restored"
    assert ("zfs", "pool_list") in engine.actions()


def test_ssh_is_off_until_turned_on(client, engine, config):
    assert client.get("/api/system/ssh").body == {"enabled": False, "service": "ssh"}


def test_turning_ssh_on_enables_and_starts_it_then_records_it(client, engine, config):
    r = client.post("/api/system/ssh", {"enabled": True})
    assert r.status == 200 and r.body == {"enabled": True, "service": "ssh"}
    assert engine.actions() == [("services", "enable"), ("services", "start")]
    assert all(c.argv[-1] == "ssh" for c in engine.calls if c.category == "services")
    assert config.get("system.ssh_enabled") is True
    assert client.get("/api/system/ssh").body["enabled"] is True


def test_turning_ssh_off_stops_and_disables_it(client, engine, config):
    config.set("system.ssh_enabled", True)
    r = client.post("/api/system/ssh", {"enabled": False})
    assert r.status == 200
    assert engine.actions() == [("services", "stop"), ("services", "disable")]
    assert config.get("system.ssh_enabled") is False


def test_a_failed_enable_is_not_recorded(client, engine, config):
    engine.outputs[("services", "enable")] = CommandExecutionError("no such unit")
    r = client.post("/api/system/ssh", {"enabled": True})
    assert r.status == 500
    assert not config.get("system.ssh_enabled")


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_ssh_wants_a_boolean(client, engine, value):
    assert client.post("/api/system/ssh", {"enabled": value}).status == 422
    assert engine.actions() == []


@pytest.fixture
def core_modules(monkeypatch):
    from conftest import CORE
    from freedomnas.module_loader import ModuleLoader
    monkeypatch.setattr(ModuleLoader, "_get_default_modules_dirs",
                        staticmethod(lambda: [CORE / "modules"]))


def test_health_asks_every_module(client, engine, config, core_modules):
    engine.outputs[("zfs", "pool_list")] = "tank\t1\t1\t1\t1\tDEGRADED\n"
    engine.outputs[("network", "list_interfaces")] = (
        '[{"ifname": "lo", "operstate": "UNKNOWN"}, {"ifname": "eth0", "operstate": "UP"}]')
    r = client.get("/api/system/health")
    assert r.status == 200
    report = {m["id"]: (m["state"], m["detail"]) for m in r.body["modules"]}
    assert report["storage"] == ("unhealthy", "tank is DEGRADED")
    assert report["network"] == ("healthy", "eth0 up")
    assert report["shares"] == ("healthy", "no shares, no share services enabled")
    assert report["updates"][0] == "healthy"
    assert set(report) == {"network", "shares", "storage", "updates", "users"}


def test_a_module_that_breaks_is_reported_not_raised(client, engine, config, core_modules):
    engine.outputs[("zfs", "pool_list")] = RuntimeError("zpool exploded")
    r = client.get("/api/system/health")
    assert r.status == 200
    [storage] = [m for m in r.body["modules"] if m["id"] == "storage"]
    assert storage == {"id": "storage", "name": "Storage", "state": "error", "detail": "zpool exploded"}


ROLLED_BACK = """autorollback: yes
state: ok
failed-kernel: 6.18.60-1-freedomnas
restored-kernel: 6.18.54-3-freedomnas
reason: the initramfs reached the emergency shell
date: 20261009-101500
snapshot: rpool/ROOT/debian@pre-update-20261009-100000
fs-rollback: yes
failed-dataset: rpool/ROOT/debian-failed-20261009-101500
"""


def test_the_boot_status_reports_the_last_rollback(client, engine, config):
    engine.outputs[("bootloader", "boot_state")] = ROLLED_BACK
    body = client.get("/api/system/boot").body
    assert body["supported"] and body["autorollback"] and body["state"] == "ok"
    assert body["rollback"]["failed_kernel"] == "6.18.60-1-freedomnas"
    assert body["rollback"]["failed_dataset"] == "rpool/ROOT/debian-failed-20261009-101500"
    assert body["rollback"]["system_restored"] is True


def test_turning_the_rollback_off_writes_the_esp_and_the_configuration(client, engine, config):
    r = client.post("/api/system/boot/autorollback", {"enabled": False})
    assert r.status == 200 and r.body["autorollback"] is False
    sets = engine.called("bootloader", "autorollback_set")
    assert [c.command for c in sets] == ["/usr/lib/freedomnas/profiles/assets/zz-dracut-uki --autorollback no"]
    assert config.get("system.autorollback") is False


@pytest.mark.parametrize("value", [None, "yes", 1])
def test_the_rollback_setting_wants_a_boolean(client, engine, value):
    assert client.post("/api/system/boot/autorollback", {"enabled": value}).status == 422


def test_dismissing_keeps_the_failed_system(client, engine, config):
    engine.outputs[("bootloader", "boot_state")] = ROLLED_BACK
    r = client.post("/api/system/boot/forget", {})
    assert r.status == 200 and r.body["destroyed"] is None
    assert engine.called("zfs", "dataset_destroy_recursive") == []
    assert len(engine.called("bootloader", "rollback_forget")) == 1


def test_the_failed_system_can_be_deleted(client, engine, config):
    engine.outputs[("bootloader", "boot_state")] = ROLLED_BACK
    r = client.post("/api/system/boot/forget", {"destroy_failed": True})
    assert r.status == 200 and r.body["destroyed"] == "rpool/ROOT/debian-failed-20261009-101500"
    assert [c.argv for c in engine.called("zfs", "dataset_destroy_recursive")] == [
        ["zfs", "destroy", "-r", "rpool/ROOT/debian-failed-20261009-101500"]]


def test_a_new_kernel_in_the_active_uki_asks_for_a_reboot(client, engine, config):
    import os
    engine.outputs[("bootloader", "boot_state")] = "state: trial\nactive-kernel: 9.9.9-new\n"
    body = client.get("/api/system/boot").body
    assert body["reboot_needed"] is True
    assert body["kernel"] == {"running": os.uname().release, "installed": "9.9.9-new"}
    engine.outputs[("bootloader", "boot_state")] = f"state: ok\nactive-kernel: {os.uname().release}\n"
    assert client.get("/api/system/boot").body["reboot_needed"] is False


@pytest.fixture
def restarts(monkeypatch):
    import api.restart as restart
    calls = []
    monkeypatch.setattr(restart, "request_restart", lambda engine: calls.append(engine))
    return calls


def test_the_webui_state_says_when_the_files_changed(client, engine, monkeypatch):
    import api.restart as restart
    body = client.get("/api/system/webui").body
    assert body == {"started": restart.STARTED, "restart_needed": False,
                    "busy": {"updates": 0, "plugins": 0, "replication": 0}}
    monkeypatch.setattr(restart, "fingerprint", lambda roots=None: "changed")
    assert client.get("/api/system/webui").body["restart_needed"] is True


def test_the_webui_restarts_on_request(client, engine, restarts):
    r = client.post("/api/system/webui/restart", {})
    assert r.status == 200 and r.body["detail"] == "Restarting"
    assert len(restarts) == 1


def test_no_restart_while_an_update_runs(client, engine, restarts, monkeypatch):
    import websocket.terminal as terminal
    monkeypatch.setattr(terminal, "running", 1)
    r = client.post("/api/system/webui/restart", {"force": True})
    assert r.status == 409 and "update is running" in r.body["detail"]
    assert restarts == []


def test_running_replication_needs_force(client, engine, restarts, monkeypatch):
    from freedomnas import replication
    monkeypatch.setattr(replication, "jobs", lambda: [{"state": "running"}, {"state": "done"}])
    r = client.post("/api/system/webui/restart", {})
    assert r.status == 409 and r.body["busy"] == {"updates": 0, "plugins": 0, "replication": 1}
    assert client.post("/api/system/webui/restart", {"force": True}).status == 200
    assert len(restarts) == 1


def test_restarting_needs_an_administrator(anonymous, engine, config, restarts):
    assert anonymous.post("/api/system/webui/restart", {}).status == 401
    assert restarts == []


def test_no_restart_while_a_module_is_installed(client, engine, restarts, monkeypatch):
    from api.routes import plugins
    monkeypatch.setitem(plugins._job, "state", "running")
    r = client.post("/api/system/webui/restart", {"force": True})
    assert r.status == 409 and r.body["busy"]["plugins"] == 1
    assert restarts == []
