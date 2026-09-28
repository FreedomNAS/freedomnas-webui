"""
/api/storage/pools/import* — look at a pool first, then bring it in with the
shares and users it carries; never mount another system's root over this one.
"""

import pytest

from freedomnas import pool_import
from freedomnas.engine import CommandExecutionError

LISTING = """   pool: tank
     id: 42
  state: ONLINE
 action: The pool can be imported using its name or numeric identifier.
 config:

\ttank        ONLINE

   pool: naspool
     id: 77
  state: ONLINE
 status: The pool was last accessed by another system.
 action: The pool can be imported using its name or numeric identifier and
\tthe '-f' flag.
"""

DATA = [("", "/tank", "default", "on"), ("media", "/tank/media", "inherited from tank", "on")]
FREEDOMNAS = DATA + [(".freedomnas", "/etc/freedomnas/state", "local", "noauto")]
ROOT = [("", "none", "local", "on"), ("ROOT/debian", "/", "local", "noauto")]
FOREIGN_CONFIG = {
    "users": [{"username": "carla", "uid": 1005, "groups": ["wheel", "users"], "shell": "/bin/bash"}],
    "shares": {
        "smb": [{"name": "media", "path": "/tank/media", "acl": "Everyone:R,carla:F",
                 "comment": "Films", "valid_users": ["carla"]}],
        "nfs": [{"name": "media-nfs", "path": "/tank/media", "allowed_hosts": "10.0.0.0/24",
                 "options": "ro,sync,no_subtree_check"}],
    },
}


def tree_of(rows):
    def answer(pool_name, **_):
        lines = []
        for rel, mp, source, canmount in rows:
            name = f"{pool_name}/{rel}" if rel else pool_name
            lines += [f"{name}\tmountpoint\t{mp}\t{source}", f"{name}\tcanmount\t{canmount}\tdefault",
                      f"{name}\tused\t4096\t-", f"{name}\tencryption\toff\tdefault",
                      f"{name}\tencryptionroot\t-\t-", f"{name}\tkeyformat\tnone\tdefault",
                      f"{name}\tkeystatus\t-\t-"]
        return "\n".join(lines)
    return answer


@pytest.fixture
def pools(engine, monkeypatch):
    engine.outputs[("zfs", "pool_importable")] = LISTING
    engine.outputs[("zfs", "dataset_tree")] = tree_of(DATA)
    monkeypatch.setattr(pool_import, "_occupied", lambda path: None)
    return engine


def freedomnas_pool(engine, monkeypatch, config_data=FOREIGN_CONFIG):
    engine.outputs[("zfs", "dataset_tree")] = tree_of(FREEDOMNAS)
    monkeypatch.setattr(pool_import, "_read_config", lambda engine, pool: (config_data, None))
    monkeypatch.setattr(pool_import, "_keep_foreign_config", lambda *a: "tank/.freedomnas:config.yaml.imported")


def test_the_importable_pools_are_listed(client, pools):
    r = client.get("/api/storage/pools/importable")
    assert r.status == 200
    assert [(p["name"], p["foreign"]) for p in r.body["pools"]] == [("tank", False), ("naspool", True)]


def test_inspecting_changes_nothing_and_reports_the_mountpoints(client, pools, config):
    r = client.post("/api/storage/pools/import/inspect", {"pool_id": "42"})
    assert r.status == 200
    assert [d["mountpoint"] for d in r.body["datasets"]] == ["/tank", "/tank/media"]
    assert not r.body["freedomnas"] and not r.body["name_taken"]
    actions = pools.actions()
    assert actions.index(("zfs", "pool_import_inspect")) < actions.index(("zfs", "pool_export"))
    assert ("zfs", "mount_pool_auto") not in actions
    [imp] = pools.called("zfs", "pool_import_inspect")
    assert "readonly=on" in imp.command and "-N" in imp.argv
    assert pools.called("zfs", "pool_import_id") == []
    assert config.get_pools() == []


def test_a_pool_of_another_system_needs_the_confirmation(client, pools):
    r = client.post("/api/storage/pools/import/inspect", {"pool_id": "77"})
    assert r.status == 422 and "another system" in r.body["detail"]
    r = client.post("/api/storage/pools/import/inspect", {"pool_id": "77", "force": True})
    assert r.status == 200
    [imp] = pools.called("zfs", "pool_import_inspect")
    assert "-f" in imp.argv


def test_another_systems_root_cannot_be_imported(client, pools, config):
    pools.outputs[("zfs", "dataset_tree")] = tree_of(ROOT)
    r = client.post("/api/storage/pools/import", {"pool_id": "42"})
    assert r.status == 422 and "another system's root" in r.body["detail"]
    assert pools.called("zfs", "pool_import_id") == []
    assert config.get_pools() == []


def test_a_system_mountpoint_asks_for_another_one(client, pools):
    pools.outputs[("zfs", "dataset_tree")] = tree_of(
        [("", "/", "local", "off"), ("var", "/var", "inherited from tank", "on")])
    r = client.post("/api/storage/pools/import", {"pool_id": "42"})
    assert r.status == 422 and "Choose another mountpoint" in r.body["detail"]
    r = client.post("/api/storage/pools/import", {"pool_id": "42", "mountpoint": "/mnt/tank"})
    assert r.status == 200
    [setting] = pools.called("zfs", "dataset_set")
    assert setting.argv == ["zfs", "set", "mountpoint=/mnt/tank", "tank"]


def test_a_system_directory_is_refused_as_mountpoint(client, pools):
    r = client.post("/api/storage/pools/import/inspect", {"pool_id": "42", "mountpoint": "/usr/tank"})
    assert r.status == 422 and "system directory" in r.body["detail"]


def test_a_taken_name_asks_for_another(client, pools, config):
    pools.outputs[("zfs", "pool_list")] = "tank\t100\t1\t99\t1\tONLINE"
    r = client.post("/api/storage/pools/import", {"pool_id": "42"})
    assert r.status == 422 and "already exists" in r.body["detail"]
    r = client.post("/api/storage/pools/import", {"pool_id": "42", "name": "vault"})
    assert r.status == 200 and r.body["pool"] == "vault"
    [imp] = pools.called("zfs", "pool_import_id")
    assert imp.argv[-2:] == ["42", "vault"]


def test_a_plain_pool_is_imported_mounted_and_recorded(client, pools, config):
    r = client.post("/api/storage/pools/import", {"pool_id": "42"})
    assert r.status == 200
    assert r.body["pool"] == "tank" and r.body["warnings"] == []
    actions = pools.actions()
    assert actions.index(("zfs", "pool_import_id")) < actions.index(("zfs", "mount_pool_auto"))
    assert pools.called("zfs", "create_state_dataset")
    [pool] = config.get_pools()
    assert pool["name"] == "tank" and pool["imported"]


def test_a_wrong_passphrase_stops_the_import(client, pools, config):
    def encrypted(pool_name, **_):
        return "\n".join([
            f"{pool_name}\tmountpoint\t/tank\tdefault", f"{pool_name}\tcanmount\ton\tdefault",
            f"{pool_name}\tencryption\taes-256-gcm\t-", f"{pool_name}\tencryptionroot\t{pool_name}\t-",
            f"{pool_name}\tkeyformat\tpassphrase\t-", f"{pool_name}\tkeystatus\tunavailable\t-",
        ])
    pools.outputs[("zfs", "dataset_tree")] = encrypted
    pools.outputs[("zfs", "load_key_prompt")] = CommandExecutionError("Incorrect key provided")
    r = client.post("/api/storage/pools/import", {"pool_id": "42", "passphrases": {"": "not it at all"}})
    assert r.status == 422 and "wrong passphrase" in r.body["detail"]
    assert pools.called("zfs", "pool_import_id") == []
    [load] = pools.called("zfs", "load_key_prompt")
    assert load.stdin == "not it at all\n" and "not it at all" not in load.command


def test_a_freedomnas_pool_offers_its_shares_and_users(client, pools, config, monkeypatch):
    freedomnas_pool(pools, monkeypatch)
    r = client.post("/api/storage/pools/import/inspect", {"pool_id": "42", "mountpoint": "/mnt/tank"})
    assert r.status == 200 and r.body["freedomnas"]
    [smb] = r.body["shares"]["smb"]
    assert smb["path"] == "/mnt/tank/media" and smb["users"] == ["carla"] and not smb["exists_here"]
    [carla] = r.body["users"]
    assert carla["uid"] == 1005 and carla["admin"] and not carla["exists_here"]


def test_a_share_that_needs_a_user_needs_its_password(client, pools, config, monkeypatch):
    freedomnas_pool(pools, monkeypatch)
    r = client.post("/api/storage/pools/import", {"pool_id": "42", "smb": ["media"]})
    assert r.status == 422 and "carla" in r.body["detail"]
    assert pools.called("zfs", "pool_import_id") == []
    r = client.post("/api/storage/pools/import", {
        "pool_id": "42", "smb": ["media"], "users": [{"username": "carla", "password": "short"}]})
    assert r.status == 422 and "at least 8" in r.body["detail"]


def test_shares_and_users_come_along(client, pools, config, monkeypatch):
    freedomnas_pool(pools, monkeypatch)
    r = client.post("/api/storage/pools/import", {
        "pool_id": "42", "smb": ["media"], "nfs": ["media-nfs"],
        "users": [{"username": "carla", "password": "a long password"}],
    })
    assert r.status == 200, r.body
    assert r.body["users"] == ["carla"]
    assert r.body["shares"] == ["smb:media", "nfs:media-nfs"]
    assert r.body["kept_config"] == "tank/.freedomnas:config.yaml.imported"
    [create] = pools.called("users", "create_with_uid")
    assert create.argv == ["useradd", "-m", "-u", "1005", "-s", "/bin/bash", "-G", "", "carla"]
    [share] = config.get_smb_shares()
    assert share["path"] == "/tank/media" and share["acl"] == "Everyone:R,carla:F"
    assert [s["options"] for s in config.get_nfs_shares()] == ["ro,sync,no_subtree_check"]
    assert [u["username"] for u in config.get_users()] == ["carla"]
    actions = pools.actions()
    assert actions.index(("zfs", "mount_pool_auto")) < actions.index(("users", "create_with_uid"))


def test_admin_rights_only_when_ticked(client, pools, config, monkeypatch):
    freedomnas_pool(pools, monkeypatch)
    r = client.post("/api/storage/pools/import", {
        "pool_id": "42", "smb": ["media"],
        "users": [{"username": "carla", "password": "a long password", "admin": True}],
    })
    assert r.status == 200
    [create] = pools.called("users", "create_with_uid")
    assert create.argv[create.argv.index("-G") + 1] == "sudo"


def test_a_taken_uid_is_not_reused(client, pools, config, monkeypatch):
    freedomnas_pool(pools, monkeypatch)
    pools.outputs[("users", "get_user_info")] = lambda username, **_: (
        "bruno:x:1005:1005::/home/bruno:/bin/bash" if username == "1005" else "")
    r = client.post("/api/storage/pools/import", {
        "pool_id": "42", "smb": ["media"],
        "users": [{"username": "carla", "password": "a long password"}],
    })
    assert r.status == 200
    assert pools.called("users", "create_with_uid") == []
    assert pools.called("users", "create")
    assert any("uid 1005 is taken" in w for w in r.body["warnings"])


def test_a_share_with_a_name_already_used_is_left_alone(client, pools, config, monkeypatch):
    freedomnas_pool(pools, monkeypatch)
    config.add_smb_share({"name": "media", "path": "/srv/mine", "acl": "Everyone:R"})
    r = client.post("/api/storage/pools/import", {
        "pool_id": "42", "smb": ["media"],
        "users": [{"username": "carla", "password": "a long password"}],
    })
    assert r.status == 200
    assert r.body["shares"] == []
    assert [s["path"] for s in config.get_smb_shares()] == ["/srv/mine"]
    assert any("already exists here" in w for w in r.body["warnings"])
