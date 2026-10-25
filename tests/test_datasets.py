"""
/api/storage/datasets* — create, move, rename and destroy datasets, and keep
the shares and snapshot policies that depend on them in step.
"""

import posixpath

import pytest

from freedomnas import pool_import


class Pool:
    """Just enough of ZFS for the routes: datasets with explicit or inherited mountpoints."""

    def __init__(self):
        self.explicit = {"rpool": "none", "rpool/ROOT": "/", "tank": None, "tank/media": None,
                         "tank/.freedomnas": "/etc/freedomnas/state"}
        self.snapshots = []

    def effective(self, name):
        mp = self.explicit[name]
        if mp is not None:
            return mp, "local"
        if "/" not in name:
            return "/" + name, "default"
        parent, _ = self.effective(name.rsplit("/", 1)[0])
        return posixpath.join(parent, name.rsplit("/", 1)[1]), f"inherited from {name.rsplit('/', 1)[0]}"

    def wire(self, engine):
        o = engine.outputs
        o[("zfs", "dataset_at_path")] = self._at_path
        o[("zfs", "dataset_get_property")] = lambda dataset, **_: "\t".join(self.effective(dataset))
        o[("zfs", "dataset_list")] = lambda **_: "\n".join(
            f"{n}\t1024\t2048\t512\t{self.effective(n)[0]}" for n in self.explicit)
        o[("zfs", "snapshot_list")] = lambda **_: "\n".join(f"{s}\t0\t0" for s in self.snapshots)
        o[("zfs", "dataset_create_with")] = self._create
        o[("zfs", "dataset_set")] = lambda dataset, value, **_: self.explicit.__setitem__(dataset, value) or ""
        o[("zfs", "dataset_inherit")] = lambda dataset, **_: self.explicit.__setitem__(dataset, None) or ""
        o[("zfs", "dataset_rename")] = self._rename
        return engine

    def _at_path(self, path, **_):
        """A dataset's name answers itself; a directory, the dataset it lies in."""
        if path in self.explicit:
            return path
        found = ""
        for name in self.explicit:
            mp = self.effective(name)[0]
            if mp.startswith("/") and (path == mp or path.startswith(mp.rstrip("/") + "/")):
                if not found or len(mp) > len(self.effective(found)[0]):
                    found = name
        return found

    def _create(self, dataset, options, **_):
        opts = dict(w.split("=", 1) for w in options.split() if "=" in w)
        self.explicit[dataset] = opts.get("mountpoint")
        return ""

    def _rename(self, dataset, new_name, **_):
        for name in sorted(self.explicit):
            if name == dataset or name.startswith(dataset + "/"):
                self.explicit[new_name + name[len(dataset):]] = self.explicit.pop(name)
        return ""


@pytest.fixture
def pool(engine, monkeypatch):
    monkeypatch.setattr(pool_import, "_occupied", lambda path: None)
    p = Pool()
    p.wire(engine)
    return p


def test_the_list_marks_what_cannot_be_changed(client, engine, pool):
    rows = {d["name"]: d for d in client.get("/api/storage/datasets").body["datasets"]}
    assert rows["rpool/ROOT"]["protected"] and rows["tank/.freedomnas"]["protected"]
    assert rows["tank/media"]["protected"] is None
    assert rows["tank/media"]["used"] == 1024


def test_a_dataset_is_created_with_its_options(client, engine, pool):
    r = client.post("/api/storage/datasets", {
        "parent": "tank", "name": "films", "mountpoint_mode": "path", "mountpoint": "/srv/films",
        "properties": {"compression": "zstd", "casesensitivity": "insensitive", "quota": ""}})
    assert r.status == 200 and r.body == {"dataset": "tank/films", "owner": "admin"}
    [create] = engine.called("zfs", "dataset_create_with")
    assert create.argv == ["zfs", "create", "-o", "mountpoint=/srv/films", "-o", "compression=zstd",
                           "-o", "casesensitivity=insensitive", "tank/films"]


def test_a_new_dataset_belongs_to_whoever_creates_it(client, engine, pool):
    r = client.post("/api/storage/datasets", {"parent": "tank", "name": "films"})
    assert r.body == {"dataset": "tank/films", "owner": "admin"}
    assert [c.argv for c in engine.called("directories", "owner_set")] == [["chown", "admin", "/tank/films"]]
    assert [c.argv for c in engine.called("directories", "group_set")] == [["chgrp", "admin", "/tank/films"]]
    assert [c.argv for c in engine.called("directories", "mode_set")] == [["chmod", "00700", "/tank/films"]]


def test_a_dataset_can_be_made_for_another_account(client, engine, pool):
    r = client.post("/api/storage/datasets", {"parent": "tank", "name": "mario", "owner": "mario"})
    assert r.body["owner"] == "mario"
    assert engine.called("directories", "owner_set")[0].argv == ["chown", "mario", "/tank/mario"]


def test_a_dataset_for_nobody_is_not_created(client, engine, pool):
    r = client.post("/api/storage/datasets", {"parent": "tank", "name": "x", "owner": "ghost"})
    assert r.status == 422 and "no account named ghost" in r.body["detail"]
    assert engine.called("zfs", "dataset_create_with") == []


def test_an_unmounted_dataset_has_no_directory_to_give(client, engine, pool):
    r = client.post("/api/storage/datasets", {"parent": "tank", "name": "x", "mountpoint_mode": "none"})
    assert r.status == 200 and r.body == {"dataset": "tank/x", "owner": None}
    assert engine.called("directories", "owner_set") == []


@pytest.mark.parametrize("body, detail", [
    ({"parent": "rpool", "name": "x"}, "runs from"),
    ({"parent": "tank", "name": "x", "properties": {"compression": "fast"}}, "compression accepts"),
    ({"parent": "tank", "name": "x", "properties": {"mountpoint": "/x"}}, "Unknown property"),
    ({"parent": "tank", "name": "x", "mountpoint_mode": "path", "mountpoint": "/etc/x"}, "system directory"),
])
def test_bad_creations_are_refused(client, engine, pool, body, detail):
    r = client.post("/api/storage/datasets", body)
    assert r.status == 422 and detail in r.body["detail"]
    assert engine.called("zfs", "dataset_create_with") == []


def test_create_only_properties_stay_out_of_later_changes(client, engine, pool):
    r = client.put("/api/storage/datasets/tank/media/properties", {"casesensitivity": "mixed"})
    assert r.status == 422 and "only be set when the dataset is created" in r.body["detail"]


def test_moving_a_dataset_can_take_its_shares_along(client, engine, pool, config, monkeypatch):
    monkeypatch.setattr(posixpath, "isdir", lambda p: True)
    config.add_smb_share({"name": "films", "path": "/tank/media/films", "comment": "f",
                          "acl": "Everyone:R", "read_only": False, "valid_users": []})
    config.add_nfs_share({"name": "films-nfs", "path": "/tank/media", "allowed_hosts": "10.0.0.0/24",
                          "options": "ro"})
    deps = client.get("/api/storage/datasets/tank/media/dependents").body
    assert [s["name"] for s in deps["shares"]["smb"]] == ["films"]
    r = client.put("/api/storage/datasets/tank/media/mountpoint",
                   {"mode": "path", "mountpoint": "/srv/media", "update_shares": True})
    assert r.status == 200
    assert (r.body["old"], r.body["new"]) == ("/tank/media", "/srv/media")
    assert sorted(r.body["shares"]["moved"]) == ["nfs:films-nfs", "smb:films"]
    assert config.get_smb_shares()[0]["path"] == "/srv/media/films"
    assert config.get_nfs_shares()[0]["path"] == "/srv/media"
    [removed] = engine.called("nfs", "export_remove")
    assert removed.kwargs["path"] == "/tank/media"


def test_without_the_tick_shares_are_reported_left_behind(client, engine, pool, config):
    config.add_smb_share({"name": "films", "path": "/tank/media", "comment": "f", "acl": "Everyone:R"})
    r = client.put("/api/storage/datasets/tank/media/mountpoint", {"mode": "path", "mountpoint": "/srv/media"})
    assert r.status == 200 and r.body["shares"]["left"] == ["smb:films"]
    assert config.get_smb_shares()[0]["path"] == "/tank/media"
    assert engine.called("samba", "delete_share") == []


def test_the_system_pool_cannot_be_moved(client, engine, pool):
    r = client.put("/api/storage/datasets/rpool/ROOT/mountpoint", {"mode": "path", "mountpoint": "/mnt/x"})
    assert r.status == 422
    assert engine.called("zfs", "dataset_set") == []


def test_a_rename_carries_the_snapshot_policies(client, engine, pool, config):
    pool.explicit["tank/media/films"] = None
    config.set_snapshot_policy("tank/media/films", {"daily": 7})
    r = client.post("/api/storage/datasets/tank/media/rename", {"new_name": "tank/video"})
    assert r.status == 200 and (r.body["old"], r.body["new"]) == ("/tank/media", "/tank/video")
    assert [(p["dataset"], p["daily"]) for p in config.get_snapshot_policies()] == [("tank/video/films", 7)]


def test_destroying_asks_for_the_name_and_for_what_goes_along(client, engine, pool, config):
    config.add_smb_share({"name": "films", "path": "/tank/media", "comment": "f", "acl": "Everyone:R"})
    config.set_snapshot_policy("tank/media", {"daily": 7})
    r = client.delete("/api/storage/datasets/tank/media?confirm=tank/medi")
    assert r.status == 422 and "full name" in r.body["detail"]
    r = client.delete("/api/storage/datasets/tank/media?confirm=tank/media")
    assert r.status == 422 and "Shares use it" in r.body["detail"]
    assert engine.called("zfs", "dataset_destroy") == []
    r = client.delete("/api/storage/datasets/tank/media?confirm=tank/media&remove_shares=true")
    assert r.status == 200 and r.body["removed_shares"] == ["smb:films"]
    assert engine.called("zfs", "dataset_destroy")
    assert config.get_smb_shares() == [] and config.get_snapshot_policies() == []


def test_children_and_snapshots_need_the_recursive_tick(client, engine, pool):
    pool.explicit["tank/media/films"] = None
    pool.snapshots = ["tank/media@daily-1"]
    r = client.delete("/api/storage/datasets/tank/media?confirm=tank/media")
    assert r.status == 422 and "tick that they go too" in r.body["detail"]
    r = client.delete("/api/storage/datasets/tank/media?confirm=tank/media&recursive=true")
    assert r.status == 200
    [destroy] = engine.called("zfs", "dataset_destroy_recursive")
    assert destroy.argv == ["zfs", "destroy", "-r", "tank/media"]


def test_a_share_samba_already_dropped_still_moves(client, engine, pool, config, monkeypatch):
    from freedomnas.engine import CommandExecutionError
    monkeypatch.setattr(posixpath, "isdir", lambda p: True)
    config.add_smb_share({"name": "films", "path": "/tank/media", "comment": "f", "acl": "Everyone:R"})

    real_execute = engine.execute

    def samba_dropped_it(category, action, timeout=30, check=True, **kw):
        if (category, action) == ("samba", "delete_share") and check:
            raise CommandExecutionError("unable to remove usershare: No such file or directory")
        return real_execute(category, action, timeout=timeout, check=check, **kw)
    monkeypatch.setattr(engine, "execute", samba_dropped_it)
    r = client.put("/api/storage/datasets/tank/media/mountpoint",
                   {"mode": "path", "mountpoint": "/srv/media", "update_shares": True})
    assert r.body["shares"]["moved"] == ["smb:films"]
    assert config.get_smb_shares()[0]["path"] == "/srv/media"


def test_home_and_what_holds_it_carry_no_buttons(client, engine, pool):
    pool.explicit["tank/home"] = "/home"
    rows = {d["name"]: d for d in client.get("/api/storage/datasets").body["datasets"]}
    assert "system directory" in rows["tank/home"]["protected"]
    assert "tank/home inside it" in rows["tank"]["protected"]
    assert rows["tank/media"]["protected"] is None
    r = client.delete("/api/storage/datasets/tank/home?confirm=tank/home&recursive=true")
    assert r.status == 422 and engine.called("zfs", "dataset_destroy_recursive") == []
