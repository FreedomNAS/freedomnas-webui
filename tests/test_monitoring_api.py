"""/api/storage — delegations, events, iostat, ARC."""

import pytest


def commands(engine, action):
    return [c.command for c in engine.called("zfs", action)]


def test_delegations(client, engine):
    engine.outputs[("zfs", "allow_show")] = (
        "---- Permissions on tank/data ----\nLocal+Descendent permissions:\n\tuser sarah create,mount\n")
    r = client.get("/api/storage/delegations/tank/data/list")
    assert r.body["delegations"][0]["name"] == "sarah"
    r = client.post("/api/storage/delegations/tank/data/grant",
                    {"who": "user", "names": "sarah", "perms": ["snapshot", "send"], "scope": "local"})
    assert r.status == 200, r.body
    r = client.post("/api/storage/delegations/tank/data/revoke",
                    {"who": "user", "names": "sarah", "scope": "both"})
    assert r.status == 200, r.body
    assert [c.argv for c in engine.called("zfs", "allow")] == [
        ["zfs", "allow", "-l", "-u", "sarah", "snapshot,send", "tank/data"]]
    assert [c.argv for c in engine.called("zfs", "unallow")] == [
        ["zfs", "unallow", "-u", "sarah", "tank/data"]]


@pytest.mark.parametrize("body", [
    {"who": "user", "names": "-r", "perms": "mount"},
    {"who": "user", "names": "sarah", "perms": "-o"},
    {"who": "user", "names": "sarah"},
])
def test_delegation_refused(client, engine, body):
    assert client.post("/api/storage/delegations/tank/data/grant", body).status == 422
    assert not engine.called("zfs", "allow")
    assert client.post("/api/storage/delegations/tank/data/explode", {}).status == 404


def test_events(client, engine):
    engine.outputs[("zfs", "events")] = (
        "Oct  4 2026 17:47:44.1\tsysevent.fs.zfs.pool_create\n        pool = \"tank\"\n")
    r = client.get("/api/storage/events")
    assert r.body["events"][0]["short"] == "pool_create" and r.body["events"][0]["pool"] == "tank"
    assert client.post("/api/storage/events/clear", {}).status == 200
    assert commands(engine, "events_clear") == ["zpool events -c"]


def test_iostat_and_arc(client, engine):
    engine.outputs[("zfs", "pool_iostat")] = "tank\t1\t2\t3\t4\t5\t6\n/dev/sda\t-\t-\t1\t1\t1\t1\n"
    r = client.get("/api/storage/pools/tank/iostat")
    assert [d["name"] for d in r.body["devices"]] == ["tank", "/dev/sda"]
    assert commands(engine, "pool_iostat") == ["zpool iostat -Hpvy tank 1 1"]
    engine.outputs[("zfs", "arc_stats")] = "name type data\nsize 4 100\nhits 4 3\nmisses 4 1\n"
    assert client.get("/api/storage/arc").body["hit_ratio"] == 75.0
    engine.outputs[("zfs", "arc_stats")] = ""
    assert client.get("/api/storage/arc").status == 404
