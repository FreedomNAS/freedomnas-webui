"""/api/setup/* — the first-run wizard, reachable before anything else is."""

import pytest

from freedomnas.engine import CommandExecutionError

POOL = {"name": "tank", "devices": ["/dev/sda", "/dev/sdb"], "vdev_type": "mirror",
        "master_passphrase": "correct horse battery"}


def test_status_before_setup(anonymous, fresh_config):
    r = anonymous.get("/api/setup/status")
    assert r.status == 200
    assert r.body == {"initialized": False, "edition": None, "detected_edition": "debian"}


def test_initialize_sets_up_the_system_and_marks_it(anonymous, engine, fresh_config, files):
    r = anonymous.post("/api/setup/initialize",
                       {"hostname": "nas", "timezone": "Europe/Rome", "edition": "debian"})
    assert r.status == 200
    assert r.body == {"detail": "System initialized", "hostname": "nas", "edition": "debian"}
    assert [c.argv for c in engine.calls] == [
        ["hostnamectl", "set-hostname", "nas"],
        ["timedatectl", "set-timezone", "Europe/Rome"],
        ["install", "-d", "-m", "1770", str(files[("samba", "usershare_path")])],
    ]
    smb = files[("samba", "config_file")].read_text()
    assert f"usershare path = {files[('samba', 'usershare_path')]}" in smb
    assert "usershare max shares = 100" in smb
    assert fresh_config.is_initialized()
    assert fresh_config.get_edition() == "debian"
    assert fresh_config.get_hostname() == "nas"


def test_an_existing_usershare_stanza_is_not_written_twice(anonymous, fresh_config, files):
    smb = files[("samba", "config_file")]
    smb.write_text("[global]\n   usershare path = /elsewhere\n")
    anonymous.post("/api/setup/initialize", {"edition": "debian"})
    assert smb.read_text() == "[global]\n   usershare path = /elsewhere\n"


@pytest.mark.parametrize("edition, message", [
    ("", "edition is required"),
    ("plan9", "Unknown edition"),
    ("freebsd", "runs the debian edition"),
])
def test_the_edition_must_be_the_one_running(anonymous, engine, fresh_config, edition, message):
    r = anonymous.post("/api/setup/initialize", {"edition": edition})
    assert r.status == 422
    assert message in r.body["detail"]
    assert engine.calls == []
    assert not fresh_config.is_initialized()


def test_initialize_runs_once(anonymous, engine, config):
    r = anonymous.post("/api/setup/initialize", {"edition": "debian"})
    assert r.status == 409
    assert engine.calls == []


def test_a_failing_step_leaves_the_system_uninitialized(anonymous, engine, fresh_config):
    engine.outputs[("system", "timezone_set")] = CommandExecutionError("timedatectl failed")
    r = anonymous.post("/api/setup/initialize", {"edition": "debian", "timezone": "Europe/Rome"})
    assert r.status == 500
    assert "timedatectl failed" in r.body["detail"]
    assert not fresh_config.is_initialized()


@pytest.mark.parametrize("timezone", ["../../../etc/shadow", "-u", "Europe/Rome x", "Mars/Base"])
def test_a_timezone_that_is_not_one_is_refused_before_anything_runs(anonymous, engine,
                                                                    fresh_config, timezone):
    r = anonymous.post("/api/setup/initialize", {"edition": "debian", "timezone": timezone})
    assert r.status == 422
    assert engine.calls == []
    assert not fresh_config.is_initialized()


@pytest.mark.parametrize("change, message", [
    ({"name": ""}, "pool.name is required"),
    ({"devices": []}, "pool.devices"),
    ({"master_passphrase": "short"}, "at least 12"),
    ({"cache_devices": ["/dev/sda"]}, "cannot reuse a data device"),
    ({"devices": ["-f", "/dev/sda"]}, "not a device path"),
    ({"log_devices": ["-f"]}, "not a device path"),
    ({"vdev_type": "mirror -f"}, "vdev_type must be"),
])
def test_an_invalid_pool_is_refused_before_anything_runs(anonymous, engine, fresh_config,
                                                         change, message):
    r = anonymous.post("/api/setup/initialize",
                       {"edition": "debian", "pool": {**POOL, **change}})
    assert r.status == 422
    assert message in r.body["detail"]
    assert engine.calls == []


def test_initialize_can_create_the_encrypted_pool(anonymous, engine, fresh_config, tmp_path):
    r = anonymous.post("/api/setup/initialize",
                       {"edition": "debian", "pool": {**POOL, "log_devices": ["/dev/nvme0n1"]}})
    assert r.status == 200
    [create] = engine.called("zfs", "pool_create_encrypted")
    assert create.argv[-4:] == ["tank", "mirror", "/dev/sda", "/dev/sdb"]
    assert engine.called("zfs", "pool_add_log")[0].argv[-1] == "/dev/nvme0n1"
    assert (tmp_path / "keyslots" / "tank.json").exists()
    pool = fresh_config.get_pool("tank")
    assert pool["encrypted"] is True
    assert pool["log_devices"] == ["/dev/nvme0n1"]
    assert POOL["master_passphrase"] not in fresh_config.config_path.read_text()
