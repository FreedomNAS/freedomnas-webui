"""/api/services/* — every service of the init, its state and the boot choice."""

from freedomnas.engine import CommandExecutionError

STOPPED = {"cron"}
DISABLED = {"cron", "smbd"}


def system(engine, names=("cron", "nfs-server", "smbd", "ssh")):
    engine.outputs[("services", "list_all")] = "\n".join(names) + "\n"
    engine.outputs[("services", "is_running")] = (
        lambda service_name: "" if service_name in STOPPED else "yes\n")
    engine.outputs[("services", "is_enabled")] = (
        lambda service_name: "" if service_name in DISABLED else "yes\n")


def test_every_service_is_listed_with_its_state_and_role(client, engine):
    system(engine)
    r = client.get("/api/services/")
    assert r.status == 200
    assert r.body["services"] == [
        {"name": "cron", "running": False, "enabled": False, "role": None},
        {"name": "nfs-server", "running": True, "enabled": True, "role": "NFS file sharing"},
        {"name": "smbd", "running": True, "enabled": False, "role": "SMB file sharing"},
        {"name": "ssh", "running": True, "enabled": True, "role": "SSH"},
    ]
    assert engine.called("services", "list_all")[0].argv[:2] == ["systemctl", "list-unit-files"]


def test_stopping_lasts_until_the_next_boot(client, engine, config):
    system(engine)
    r = client.post("/api/services/nfs-server/stop")
    assert r.status == 200 and r.body["name"] == "nfs-server"
    assert engine.called("services", "stop")[0].argv[-1] == "nfs-server"
    assert config.get_service_boot() == {}


def test_disabling_at_boot_is_recorded(client, engine, config):
    system(engine)
    assert client.post("/api/services/nfs-server/disable").status == 200
    assert engine.called("services", "disable")[0].argv[-1] == "nfs-server"
    assert config.get_service_boot() == {"nfs-server": False}
    assert client.post("/api/services/cron/enable").status == 200
    assert config.get_service_boot() == {"nfs-server": False, "cron": True}


def test_the_ssh_service_keeps_its_own_setting(client, engine, config):
    system(engine)
    config.set("system.ssh_enabled", True)
    assert client.post("/api/services/ssh/disable").status == 200
    assert config.get("system.ssh_enabled") is False
    assert config.get_service_boot() == {}


def test_an_unknown_service_or_action_is_refused(client, engine):
    system(engine)
    assert client.post("/api/services/evil;reboot/start").status == 404
    assert client.post("/api/services/ssh/mask").status == 404
    assert engine.called("services", "start") == []


def test_a_failing_action_is_reported(client, engine, config):
    system(engine)
    engine.outputs[("services", "disable")] = CommandExecutionError("no such unit")
    r = client.post("/api/services/smbd/disable")
    assert r.status == 500 and "no such unit" in r.body["detail"]
    assert config.get_service_boot() == {}
