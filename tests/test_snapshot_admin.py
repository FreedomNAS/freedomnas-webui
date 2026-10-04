"""/api/storage — volumes, dataset administration, snapshots, holds, diffs, bookmarks."""

import pytest

from freedomnas.engine import CommandExecutionError


def commands(engine, action):
    return [c.command for c in engine.called("zfs", action)]


@pytest.fixture
def tank(engine):
    existing = {"tank", "tank/a", "tank/vms"}

    def at_path(path, **_):
        if path == "/":
            return "rpool/ROOT/default"
        return path if path in existing else ""
    engine.outputs[("zfs", "dataset_at_path")] = at_path
    engine.outputs[("zfs", "dataset_list")] = (
        "tank\t1\t1\t1\t/tank\tfilesystem\tyes\t-\n"
        "tank/a\t1\t1\t1\t/tank/a\tfilesystem\tyes\t-\n"
        "tank/copy\t1\t1\t1\t/tank/copy\tfilesystem\tno\ttank/a@s1\n"
        "rpool\t1\t1\t1\tnone\tfilesystem\tno\t-\n"
        "rpool/ROOT/default\t1\t1\t1\t/\tfilesystem\tyes\t-\n")
    return existing


def test_dataset_listing_has_type_mounted_origin(client, engine, tank):
    rows = {d["name"]: d for d in client.get("/api/storage/datasets").body["datasets"]}
    assert rows["tank/copy"]["origin"] == "tank/a@s1" and rows["tank/copy"]["mounted"] is False
    assert rows["tank/a"]["type"] == "filesystem" and rows["tank/a"]["origin"] == ""


def test_create_volume(client, engine, tank):
    r = client.post("/api/storage/volumes", {"parent": "tank/vms", "name": "win", "size": "40G",
                                             "sparse": True, "blocksize": "16K",
                                             "properties": {"compression": "zstd"}})
    assert r.status == 200, r.body
    assert commands(engine, "volume_create_sparse") == [
        "zfs create -s -V 40G -o compression=zstd -o volblocksize=16K tank/vms/win"]


@pytest.mark.parametrize("body", [
    {"parent": "tank", "name": "v", "size": "big"},
    {"parent": "tank", "name": "v", "size": "1G", "blocksize": "3K"},
    {"parent": "rpool", "name": "v", "size": "1G"},
    {"parent": "tank", "name": "v", "size": "1G", "properties": {"compression": "brotli"}},
])
def test_create_volume_refused(client, engine, tank, body):
    assert client.post("/api/storage/volumes", body).status == 422
    assert not commands(engine, "volume_create") and not commands(engine, "volume_create_sparse")


def test_mount_unmount_promote(client, engine, tank):
    assert client.post("/api/storage/datasets/tank/a/unmount", {}).status == 200
    assert client.post("/api/storage/datasets/tank/a/mount", {}).status == 200
    assert client.post("/api/storage/datasets/tank/copy/promote", {}).status == 200
    assert commands(engine, "unmount_dataset") == ["zfs unmount tank/a"]
    assert commands(engine, "mount_dataset") == ["zfs mount tank/a"]
    assert commands(engine, "dataset_promote") == ["zfs promote tank/copy"]


def test_system_datasets_are_not_unmounted_or_promoted(client, engine, tank):
    assert client.post("/api/storage/datasets/rpool/ROOT/default/unmount", {}).status == 422
    assert client.post("/api/storage/datasets/rpool/ROOT/default/promote", {}).status == 422
    assert not commands(engine, "unmount_dataset") and not commands(engine, "dataset_promote")


def test_inherit(client, engine, tank):
    assert client.post("/api/storage/datasets/tank/a/inherit", {"property": "compression"}).status == 200
    assert client.post("/api/storage/datasets/tank/a/inherit", {"property": "com.example:note"}).status == 200
    assert client.post("/api/storage/datasets/tank/a/inherit", {"property": "casesensitivity"}).status == 422
    assert client.post("/api/storage/datasets/tank/a/inherit", {"property": "-r"}).status == 422
    assert commands(engine, "dataset_inherit") == ["zfs inherit compression tank/a",
                                                   "zfs inherit com.example:note tank/a"]


def test_user_properties(client, engine, tank):
    engine.outputs[("zfs", "dataset_get")] = (
        "compression\tlz4\tlocal\ncom.example:note\thello\tlocal\nused\t1\t-\n")
    r = client.get("/api/storage/datasets/tank/a/user-properties")
    assert r.body["properties"] == {"com.example:note": {"value": "hello", "source": "local"}}
    r = client.put("/api/storage/datasets/tank/a/user-properties", {"com.example:note": "two words"})
    assert r.status == 200
    assert commands(engine, "dataset_set") == ["zfs set com.example:note='two words' tank/a"]
    for bad in ({"compression": "off"}, {"Com:x": "y"}, {"a:b": "x\x01"}, {}):
        assert client.put("/api/storage/datasets/tank/a/user-properties", bad).status == 422


def test_quotas(client, engine, tank):
    engine.outputs[("zfs", "groupspace")] = "POSIX Group\twheel\t1024\t1073741824\t2\tnone\n"
    r = client.get("/api/storage/datasets/tank/a/quotas?kind=group")
    assert r.body["entries"][0]["quota"] == 1073741824
    r = client.put("/api/storage/datasets/tank/a/quotas",
                   {"kind": "user", "name": "sarah", "quota": "50G", "objquota": "100000"})
    assert r.status == 200, r.body
    assert commands(engine, "dataset_set") == ["zfs set userquota@sarah=50G tank/a",
                                               "zfs set userobjquota@sarah=100000 tank/a"]
    for bad in ({"kind": "user", "name": "-x", "quota": "1G"}, {"kind": "project", "name": "a", "quota": "1G"},
                {"kind": "user", "name": "sarah", "quota": "lots"}, {"kind": "user", "name": "sarah"}):
        assert client.put("/api/storage/datasets/tank/a/quotas", bad).status == 422


def test_snapshot_listing_and_create_recursive(client, engine, tank, config):
    engine.outputs[("zfs", "snapshot_list")] = "tank/a@s1\t1700000000\t0\t4096\t1\ttank/copy\n"
    r = client.get("/api/storage/snapshots")
    assert r.body["snapshots"][0]["holds"] == 1 and r.body["snapshots"][0]["clones"] == ["tank/copy"]
    assert client.post("/api/storage/snapshots", {"dataset": "tank/a", "snap_name": "now",
                                                  "recursive": True}).status == 200
    assert commands(engine, "snapshot_create_recursive") == ["zfs snapshot -r tank/a@now"]


def test_destroy_snapshot_forgets_the_record(client, engine, tank, config):
    config.add_root_snapshot("tank/a@s1")
    assert client.delete("/api/storage/snapshots/tank/a@s1").status == 200
    assert client.delete("/api/storage/snapshots/tank/a@s2?recursive=true").status == 200
    assert commands(engine, "snapshot_destroy") == ["zfs destroy tank/a@s1"]
    assert commands(engine, "snapshot_destroy_recursive") == ["zfs destroy -r tank/a@s2"]
    assert config.get_root_snapshots() == []


@pytest.mark.parametrize("name", ["tank/a", "tank/a@-r", "tank@a@b", "tank/a@x%20y"])
def test_bad_snapshot_names(client, engine, tank, name):
    assert client.delete(f"/api/storage/snapshots/{name}").status == 422


def test_rollback(client, engine, tank):
    assert client.post("/api/storage/snapshots/tank/a@s1/rollback", {}).status == 200
    assert client.post("/api/storage/snapshots/tank/a@s1/rollback", {"destroy_newer": True}).status == 200
    assert commands(engine, "snapshot_rollback") == ["zfs rollback tank/a@s1"]
    assert commands(engine, "snapshot_rollback_recursive") == ["zfs rollback -r tank/a@s1"]
    r = client.post("/api/storage/snapshots/rpool/ROOT/default@s1/rollback", {})
    assert r.status == 422 and "runs from" in r.body["detail"]


def test_clone(client, engine, tank):
    assert client.post("/api/storage/snapshots/tank/a@s1/clone", {"target": "tank/new"}).status == 200
    assert commands(engine, "snapshot_clone") == ["zfs clone tank/a@s1 tank/new"]
    for target in ("other/new", "tank/a", "tank/missing/x", "rpool/x"):
        assert client.post("/api/storage/snapshots/tank/a@s1/clone", {"target": target}).status == 422


def test_rename_follows_the_record(client, engine, tank, config):
    config.add_root_snapshot("tank/a@s1")
    r = client.post("/api/storage/snapshots/tank/a@s1/rename", {"new_name": "kept"})
    assert r.status == 200 and r.body["snapshot"] == "tank/a@kept"
    assert commands(engine, "snapshot_rename") == ["zfs rename tank/a@s1 tank/a@kept"]
    assert config.get_root_snapshots()[0]["name"] == "tank/a@kept"
    assert client.post("/api/storage/snapshots/tank/a@s1/rename", {"new_name": "a b"}).status == 422


def test_holds(client, engine, tank):
    engine.outputs[("zfs", "snapshot_holds")] = "tank/a@s1\tkeep\t1700000000\n"
    assert client.get("/api/storage/snapshots/tank/a@s1/holds").body["holds"] == [
        {"tag": "keep", "time": 1700000000}]
    client.post("/api/storage/snapshots/tank/a@s1/hold", {"tag": "backup"})
    client.post("/api/storage/snapshots/tank/a@s1/release", {"tag": "keep"})
    assert commands(engine, "snapshot_hold") == ["zfs hold backup tank/a@s1"]
    assert commands(engine, "snapshot_release") == ["zfs release keep tank/a@s1"]
    assert client.post("/api/storage/snapshots/tank/a@s1/hold", {"tag": "-r"}).status == 422


def test_diff(client, engine, tank):
    engine.outputs[("zfs", "snapshot_diff")] = "1.0\tM\t/\t/tank/a\n1.1\t+\tF\t/tank/a/new\n"
    r = client.get("/api/storage/snapshots/tank/a@s1/diff")
    assert r.body["total"] == 2 and r.body["changes"][1]["change"] == "created"
    client.get("/api/storage/snapshots/tank/a@s1/diff?to=tank/a@s2")
    assert commands(engine, "snapshot_diff_to") == ["zfs diff -FHt tank/a@s1 tank/a@s2"]
    assert client.get("/api/storage/snapshots/tank/a@s1/diff?to=tank/b@s2").status == 422


def test_bookmarks(client, engine, tank):
    engine.outputs[("zfs", "bookmark_list")] = "tank/a#b1\t1700000000\n"
    assert client.get("/api/storage/bookmarks").body["bookmarks"][0]["bookmark"] == "b1"
    client.post("/api/storage/snapshots/tank/a@s1/bookmark", {"name": "b2"})
    assert [c.argv for c in engine.called("zfs", "bookmark_create")] == [
        ["zfs", "bookmark", "tank/a@s1", "tank/a#b2"]]
    assert client.delete("/api/storage/bookmarks/tank/a%23b1").status == 200
    assert [c.argv for c in engine.called("zfs", "bookmark_destroy")] == [["zfs", "destroy", "tank/a#b1"]]
    assert client.delete("/api/storage/bookmarks/tank/a").status == 422


def test_zfs_refusal_is_reported(client, engine, tank):
    engine.outputs[("zfs", "snapshot_destroy")] = CommandExecutionError("dataset is busy: hold keep")
    r = client.delete("/api/storage/snapshots/tank/a@s1")
    assert r.status == 500 and "hold keep" in r.body["detail"]
