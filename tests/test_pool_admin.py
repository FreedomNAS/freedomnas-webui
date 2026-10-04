"""/api/storage/pools/{pool}/… — status, maintenance, devices, vdevs, properties."""

import json

import pytest

from freedomnas.engine import CommandExecutionError

STATUS = {"pools": {"tank": {
    "name": "tank", "state": "DEGRADED", "error_count": 0,
    "status": "One or more devices has been taken offline.\n\tSufficient replicas exist.\n",
    "action": "Online the device using 'zpool online'.\n",
    "scan_stats": {"function": "SCRUB", "state": "SCANNING", "to_examine": 400, "issued": 100},
    "vdevs": {"tank": {"name": "tank", "vdevs": {
        "mirror-0": {"name": "mirror-0", "vdev_type": "mirror", "state": "DEGRADED", "vdevs": {
            "/dev/sda1": {"name": "/dev/sda1", "path": "/dev/sda1", "vdev_type": "disk", "state": "ONLINE"},
            "/dev/sdb1": {"name": "/dev/sdb1", "path": "/dev/sdb1", "vdev_type": "disk", "state": "OFFLINE"},
        }}}}},
    "spares": {"/dev/sdc": {"name": "/dev/sdc", "path": "/dev/sdc", "vdev_type": "disk", "state": "AVAIL"}},
}}}


def commands(engine, action):
    return [c.command for c in engine.called("zfs", action)]


def test_status_is_the_parsed_tree(client, engine):
    engine.outputs[("zfs", "pool_status_json")] = json.dumps(STATUS)
    r = client.get("/api/storage/pools/tank/status")
    assert r.status == 200
    assert r.body["state"] == "DEGRADED" and r.body["scan"]["percent"] == 25.0
    assert r.body["status"] == "One or more devices has been taken offline. Sufficient replicas exist."
    assert [g["role"] for g in r.body["groups"]] == ["data", "spare"]
    assert r.body["system"] is False
    assert commands(engine, "pool_status_json") == ["zpool status -j --json-int -P tank"]


def test_status_of_an_unknown_pool_is_a_404(client, engine):
    engine.outputs[("zfs", "pool_status_json")] = json.dumps(STATUS)
    assert client.get("/api/storage/pools/other/status").status == 404


@pytest.mark.parametrize("task,action,command", [
    ("scrub", "start", "zpool scrub tank"),
    ("scrub", "pause", "zpool scrub -p tank"),
    ("scrub", "stop", "zpool scrub -s tank"),
    ("trim", "start", "zpool trim tank"),
    ("trim", "suspend", "zpool trim -s tank"),
    ("trim", "cancel", "zpool trim -c tank"),
    ("initialize", "start", "zpool initialize tank"),
    ("initialize", "cancel", "zpool initialize -c tank"),
])
def test_maintenance(client, engine, task, action, command):
    r = client.post(f"/api/storage/pools/tank/{task}", {"action": action})
    assert r.status == 200
    assert [c.command for c in engine.calls if c.category == "zfs"] == [command]


def test_maintenance_refuses_an_unknown_action(client, engine):
    assert client.post("/api/storage/pools/tank/scrub", {"action": "explode"}).status == 422
    assert client.post("/api/storage/pools/tank/frobnicate", {}).status == 404


def test_clear_pool_and_device(client, engine):
    client.post("/api/storage/pools/tank/clear", {})
    client.post("/api/storage/pools/tank/clear", {"device": "/dev/sda1"})
    assert commands(engine, "pool_clear") == ["zpool clear tank"]
    assert commands(engine, "pool_clear_device") == ["zpool clear tank /dev/sda1"]


@pytest.mark.parametrize("body,action,command", [
    ({"action": "online", "device": "/dev/sdb1"}, "pool_online", "zpool online tank /dev/sdb1"),
    ({"action": "offline", "device": "/dev/sdb1"}, "pool_offline", "zpool offline tank /dev/sdb1"),
    ({"action": "offline_temporary", "device": "/dev/sdb1"}, "pool_offline_temporary",
     "zpool offline -t tank /dev/sdb1"),
    ({"action": "expand", "device": "/dev/sdb1"}, "pool_expand", "zpool online -e tank /dev/sdb1"),
    ({"action": "replace", "device": "/dev/sdb1", "new_device": "/dev/sdd"}, "pool_replace",
     "zpool replace tank /dev/sdb1 /dev/sdd"),
    ({"action": "replace", "device": "/dev/sdb1"}, "pool_replace_in_place",
     "zpool replace tank /dev/sdb1"),
    ({"action": "attach", "device": "/dev/sda1", "new_device": "/dev/sdd"}, "pool_attach",
     "zpool attach tank /dev/sda1 /dev/sdd"),
    ({"action": "detach", "device": "/dev/sdb1"}, "pool_detach", "zpool detach tank /dev/sdb1"),
    ({"action": "remove", "device": "mirror-1"}, "pool_remove_device", "zpool remove tank mirror-1"),
    ({"action": "cancel_remove"}, "pool_remove_cancel", "zpool remove -s tank"),
])
def test_device_actions(client, engine, body, action, command):
    r = client.post("/api/storage/pools/tank/device", body)
    assert r.status == 200, r.body
    assert commands(engine, action) == [command]


@pytest.mark.parametrize("body", [
    {"action": "obliterate", "device": "/dev/sda1"},
    {"action": "offline", "device": ""},
    {"action": "offline", "device": "-f"},
    {"action": "offline", "device": "/dev/sda1 /dev/sdb1"},
    {"action": "attach", "device": "/dev/sda1", "new_device": "-f"},
    {"action": "attach", "device": "/dev/sda1", "new_device": "sdd"},
    {"action": "replace", "device": "/dev/sda1", "new_device": "/dev/../etc/passwd"},
])
def test_device_actions_refuse_bad_input(client, engine, body):
    assert client.post("/api/storage/pools/tank/device", body).status == 422
    assert not [c for c in engine.calls if c.category == "zfs"]


def test_removing_a_cache_device_updates_the_config(client, engine, config):
    config.add_pool({"name": "tank", "cache_devices": ["/dev/nvme0n1"], "log_devices": []})
    client.post("/api/storage/pools/tank/device", {"action": "remove", "device": "/dev/nvme0n1"})
    assert config.get_pools()[0]["cache_devices"] == []


def test_zfs_refusal_is_reported(client, engine):
    engine.outputs[("zfs", "pool_detach")] = CommandExecutionError("only one copy left")
    r = client.post("/api/storage/pools/tank/device", {"action": "detach", "device": "/dev/sda1"})
    assert r.status == 500 and "only one copy left" in r.body["detail"]


def test_add_vdevs_and_record_them(client, engine, config):
    config.add_pool({"name": "tank", "vdev_type": "mirror"})
    r = client.post("/api/storage/pools/tank/vdevs", {"vdevs": [
        {"class": "data", "type": "mirror", "devices": ["/dev/sdd", "/dev/sde"]},
        {"class": "special", "type": "mirror", "devices": ["/dev/nvme0n1", "/dev/nvme1n1"]},
        {"class": "spare", "devices": ["/dev/sdf"]},
    ]})
    assert r.status == 200, r.body
    assert commands(engine, "pool_add_vdevs") == [
        "zpool add tank mirror /dev/sdd /dev/sde special mirror /dev/nvme0n1 /dev/nvme1n1 spare /dev/sdf"]
    assert [g["class"] for g in config.get_pools()[0]["vdevs"]] == ["data", "special", "spare"]


@pytest.mark.parametrize("vdevs", [
    [],
    [{"type": "raidz2", "devices": ["/dev/sdd", "/dev/sde"]}],
    [{"devices": ["-f"]}],
    [{"devices": ["sdd"]}],
    [{"class": "dedup", "type": "raidz1", "devices": ["/dev/sdd", "/dev/sde"]}],
])
def test_add_vdevs_refuses_bad_layouts(client, engine, vdevs):
    assert client.post("/api/storage/pools/tank/vdevs", {"vdevs": vdevs}).status == 422
    assert not commands(engine, "pool_add_vdevs")


def test_create_pool_with_several_vdevs(client, engine, config):
    r = client.post("/api/storage/pools", {"name": "big", "vdevs": [
        {"class": "data", "type": "raidz1", "devices": ["/dev/sda", "/dev/sdb"]},
        {"class": "data", "type": "raidz1", "devices": ["/dev/sdc", "/dev/sdd"]},
        {"class": "dedup", "type": "", "devices": ["/dev/sde"]},
    ]})
    assert r.status == 200, r.body
    words = commands(engine, "pool_create")[0].split()
    assert words[words.index("big"):] == [
        "big", "raidz1", "/dev/sda", "/dev/sdb", "raidz1", "/dev/sdc", "/dev/sdd", "dedup", "/dev/sde"]
    assert len(config.get_pools()[0]["vdevs"]) == 3


def test_history(client, engine):
    engine.outputs[("zfs", "pool_history")] = (
        "History for 'tank':\n2026-10-04.10:00:00 zpool create tank /dev/sda [user 0 (root) on nas:linux]\n")
    r = client.get("/api/storage/pools/tank/history")
    assert r.body["lines"] == ["2026-10-04.10:00:00 zpool create tank /dev/sda [user 0 (root) on nas:linux]"]
    client.get("/api/storage/pools/tank/history?internal=true")
    assert commands(engine, "pool_history_internal") == ["zpool history -il tank"]


def test_pool_properties(client, engine):
    engine.outputs[("zfs", "pool_get_all")] = (
        "size\t100\t-\nautotrim\toff\tdefault\ncomment\tmy pool\tlocal\n")
    r = client.get("/api/storage/pools/tank/properties")
    assert r.body["properties"] == {"autotrim": {"value": "off", "source": "default"},
                                    "comment": {"value": "my pool", "source": "local"}}
    r = client.put("/api/storage/pools/tank/properties", {"autotrim": "on", "comment": "a b"})
    assert r.status == 200 and r.body["applied"] == ["autotrim", "comment"]
    assert commands(engine, "pool_set") == ["zpool set autotrim=on tank", "zpool set comment='a b' tank"]
    assert "autotrim" in client.get("/api/storage/pool-properties").body["properties"]


@pytest.mark.parametrize("body", [{"autotrim": "maybe"}, {"cachefile": "/x"}, {"comment": "a\x01b"}, {}])
def test_pool_properties_are_validated(client, engine, body):
    assert client.put("/api/storage/pools/tank/properties", body).status == 422
    assert not commands(engine, "pool_set")


def test_checkpoint_upgrade_reguid(client, engine):
    client.post("/api/storage/pools/tank/checkpoint", {})
    client.post("/api/storage/pools/tank/checkpoint", {"discard": True})
    client.post("/api/storage/pools/tank/upgrade", {})
    client.post("/api/storage/pools/tank/reguid", {})
    assert [c.command for c in engine.calls if c.category == "zfs" and c.action.startswith("pool_")
            and c.action != "pool_list"] == [
        "zpool checkpoint tank", "zpool checkpoint -d tank", "zpool upgrade tank", "zpool reguid tank"]


@pytest.mark.parametrize("task", ["upgrade", "reguid"])
def test_upgrade_and_reguid_refused_on_the_system_pool(client, engine, monkeypatch, task):
    from freedomnas import datasets as zfs_datasets
    monkeypatch.setattr(zfs_datasets, "root_pool", lambda engine: "rpool")
    r = client.post(f"/api/storage/pools/rpool/{task}", {})
    assert r.status == 422 and "boots from" in r.body["detail"]
    assert not commands(engine, f"pool_{task}")
