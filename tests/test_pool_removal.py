"""
/api/storage/pools/{pool} export and destroy — never the system's pool or
the one serving the configuration; shares go only when asked.
"""

import pytest

from freedomnas import keymgmt, shadow

LIST = "\n".join(f"{n}\t1\t2\t3\t{mp}" for n, mp in [
    ("rpool", "none"), ("rpool/ROOT", "/"),
    ("mainpool", "none"), ("mainpool/.freedomnas", "/etc/freedomnas/state"),
    ("tank", "/tank"), ("tank/.freedomnas", "/etc/freedomnas/state"), ("tank/media", "/tank/media"),
])


@pytest.fixture
def pools(engine, monkeypatch):
    engine.outputs[("zfs", "dataset_list")] = LIST
    engine.outputs[("zfs", "dataset_at_path")] = lambda path, **_: "rpool/ROOT" if path == "/" else ""
    engine.outputs[("zfs", "pool_list")] = "rpool\t1\t1\t1\t1\tONLINE\nmainpool\t1\t1\t1\t1\tONLINE\ntank\t1\t1\t1\t1\tONLINE"
    monkeypatch.setattr(shadow, "active_dataset", lambda engine: "mainpool/.freedomnas")
    return engine


def test_the_pool_list_says_which_pools_must_stay(client, pools):
    rows = {p["name"]: p["protected"] for p in client.get("/api/storage/pools").body["pools"]}
    assert "runs from" in rows["rpool"]
    assert "configuration this system is using" in rows["mainpool"]
    assert rows["tank"] is None


@pytest.mark.parametrize("pool", ["rpool", "mainpool"])
def test_they_are_neither_exported_nor_destroyed(client, pools, config, pool):
    assert client.post(f"/api/storage/pools/{pool}/export", {}).status == 422
    assert client.delete(f"/api/storage/pools/{pool}?confirm={pool}").status == 422
    assert pools.called("zfs", "pool_export") == [] and pools.called("zfs", "pool_destroy") == []


def test_the_report_shows_what_goes_along(client, pools, config):
    config.add_smb_share({"name": "media", "path": "/tank/media", "comment": "m", "acl": "Everyone:R"})
    config.set_snapshot_policy("tank/media", {"daily": 7})
    r = client.get("/api/storage/pools/tank/removal").body
    assert r["protected"] is None and r["datasets"] == ["tank", "tank/.freedomnas", "tank/media"]
    assert [s["name"] for s in r["shares"]["smb"]] == ["media"] and r["policies"] == ["tank/media"]


def test_export_takes_the_pool_out_of_the_config_and_its_shares_only_if_asked(client, pools, config):
    config.add_pool({"name": "tank"})
    config.add_smb_share({"name": "media", "path": "/tank/media", "comment": "m", "acl": "Everyone:R"})
    r = client.post("/api/storage/pools/tank/export", {})
    assert r.status == 422 and "Shares use it" in r.body["detail"]
    assert pools.called("zfs", "pool_export") == []
    r = client.post("/api/storage/pools/tank/export", {"remove_shares": True})
    assert r.status == 200 and r.body["removed_shares"] == ["smb:media"]
    [export] = pools.called("zfs", "pool_export")
    assert export.argv == ["zpool", "export", "tank"]
    assert config.get_pools() == [] and config.get_smb_shares() == []


def test_destroy_needs_the_name_and_forgets_the_keyslots(client, pools, config, tmp_path):
    keymgmt.KeyslotStore("tank").set_slot("usb", {"salt": "00"})
    config.add_pool({"name": "tank", "encrypted": True})
    config.set_snapshot_policy("tank/media", {"daily": 7})
    assert client.delete("/api/storage/pools/tank?confirm=tan").status == 422
    r = client.delete("/api/storage/pools/tank?confirm=tank")
    assert r.status == 200 and r.body["forgot_keyslots"]
    [destroy] = pools.called("zfs", "pool_destroy")
    assert destroy.argv == ["zpool", "destroy", "tank"]
    assert not keymgmt.KeyslotStore("tank").exists()
    assert config.get_pools() == [] and config.get_snapshot_policies() == []


def test_a_busy_pool_is_reported_and_stays_recorded(client, pools, config):
    from freedomnas.engine import CommandExecutionError
    config.add_pool({"name": "tank"})
    pools.outputs[("zfs", "pool_export")] = CommandExecutionError("cannot unmount '/tank/media': pool or dataset is busy")
    r = client.post("/api/storage/pools/tank/export", {})
    assert r.status == 500 and "busy" in r.body["detail"]
    assert [p["name"] for p in config.get_pools()] == ["tank"]
