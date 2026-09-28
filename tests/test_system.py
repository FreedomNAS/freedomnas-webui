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
    monkeypatch.setattr(ModuleLoader, "_get_default_modules_dir",
                        staticmethod(lambda: str(CORE / "modules")))


def test_health_asks_every_module(client, engine, config, core_modules):
    engine.outputs[("zfs", "pool_list")] = "tank\t1\t1\t1\t1\tDEGRADED\n"
    engine.outputs[("network", "list_interfaces")] = (
        '[{"ifname": "lo", "operstate": "UNKNOWN"}, {"ifname": "eth0", "operstate": "UP"}]')
    r = client.get("/api/system/health")
    assert r.status == 200
    report = {m["id"]: (m["state"], m["detail"]) for m in r.body["modules"]}
    assert report["storage"] == ("unhealthy", "tank is DEGRADED")
    assert report["network"] == ("healthy", "eth0 up")
    assert report["shares"] == ("healthy", "no share services enabled")
    assert report["updates"][0] == "healthy"
    assert set(report) == {"network", "shares", "storage", "updates", "users"}


def test_a_module_that_breaks_is_reported_not_raised(client, engine, config, core_modules):
    engine.outputs[("zfs", "pool_list")] = RuntimeError("zpool exploded")
    r = client.get("/api/system/health")
    assert r.status == 200
    [storage] = [m for m in r.body["modules"] if m["id"] == "storage"]
    assert storage == {"id": "storage", "name": "Storage", "state": "error", "detail": "zpool exploded"}
