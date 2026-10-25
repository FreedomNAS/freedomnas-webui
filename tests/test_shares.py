"""/api/shares/* — Samba usershares and NFS exports."""

import subprocess

import pytest


@pytest.fixture
def smb_ready(files):
    files[("samba", "config_file")].write_text(
        "[global]\n   usershare path = /var/lib/samba/usershares\n"
        "   usershare max shares = 100\n"
    )


# ---------------------------------------------------------------------------
# SMB
# ---------------------------------------------------------------------------

def test_smb_shares_are_listed_from_the_config(client, config):
    config.add_smb_share({"name": "foto", "path": "/tank/foto"})
    assert client.get("/api/shares/smb").body["shares"][0]["name"] == "foto"


@pytest.mark.parametrize("body", [{"name": "foto"}, {"path": "/tank/foto"}])
def test_an_smb_share_needs_name_and_path(client, engine, smb_ready, body):
    assert client.post("/api/shares/smb", body).status == 422
    assert engine.actions() == []


def test_smb_waits_for_the_usershare_setup(client, engine, files):
    files[("samba", "config_file")].write_text("[global]\n   workgroup = HOME\n")
    r = client.post("/api/shares/smb", {"name": "foto", "path": "/tank/foto"})
    assert r.status == 409
    assert "setup wizard" in r.body["detail"]
    assert engine.actions() == []


def test_smb_without_any_smb_conf_is_a_409(client, engine):
    assert client.post("/api/shares/smb", {"name": "foto", "path": "/tank/foto"}).status == 409


def test_an_smb_share_is_created_reloaded_and_recorded(client, engine, config, smb_ready):
    r = client.post("/api/shares/smb", {"name": "foto", "path": "/tank/foto"})
    assert r.status == 200
    assert engine.actions() == [("zfs", "dataset_at_path"),
                                ("samba", "create_share"), ("samba", "reload_config")]
    [create] = engine.called("samba", "create_share")
    assert create.argv == ["net", "usershare", "add", "foto", "/tank/foto", "foto", "Unix User\\admin:F"]
    saved = config.get_smb_share("foto")
    assert saved["path"] == "/tank/foto"
    assert saved["valid_users"] == ["admin"]
    assert saved["owner"] == "admin"
    assert saved["guest_ok"] is False
    assert saved["acl"] == "Unix User\\admin:F"
    assert saved["comment"] == "foto"
    assert "left as they are" in r.body["warning"]


def test_a_read_only_share_gives_nobody_write_access(client, engine, config, smb_ready):
    r = client.post("/api/shares/smb", {"name": "foto", "path": "/tank/foto", "read_only": True})
    assert r.status == 200
    [create] = engine.called("samba", "create_share")
    assert create.argv[-1] == "Unix User\\admin:R"
    assert config.get_smb_share("foto")["acl"] == "Unix User\\admin:R"


def test_a_comment_with_spaces_stays_the_comment(client, engine, smb_ready):
    client.post("/api/shares/smb", {"name": "foto", "path": "/tank/foto",
                                    "comment": "Foto & Video", "acl": "Everyone:F"})
    [create] = engine.called("samba", "create_share")
    assert create.argv[-2:] == ["Foto & Video", "Everyone:F"]


def test_an_empty_comment_does_not_shift_the_acl(client, engine, smb_ready):
    client.post("/api/shares/smb", {"name": "foto", "path": "/tank/foto", "comment": ""})
    [create] = engine.called("samba", "create_share")
    assert create.argv[-2:] == ["", "Unix User\\admin:F"]


@pytest.fixture
def dataset_dir(engine):
    """/tank/foto is where tank/foto mounts, and root still owns it."""
    owner = {"name": "root"}
    engine.outputs[("zfs", "dataset_at_path")] = lambda path, **_: "rpool/ROOT" if path == "/" else "tank/foto"
    engine.outputs[("zfs", "dataset_get_property")] = lambda dataset, **_: (
        "/\tlocal" if dataset == "rpool/ROOT" else "/tank/foto\tdefault")
    engine.outputs[("directories", "owner_get")] = lambda **_: owner["name"]
    return owner


def test_a_share_of_a_new_dataset_goes_to_whoever_shares_it(client, engine, config, smb_ready, dataset_dir):
    r = client.post("/api/shares/smb", {"name": "foto", "path": "/tank/foto"})
    assert r.status == 200 and r.body["owner"] == "admin" and "warning" not in r.body
    assert engine.called("directories", "owner_set")[0].argv == ["chown", "admin", "/tank/foto"]
    assert engine.called("directories", "mode_set")[0].argv == ["chmod", "00700", "/tank/foto"]


def test_a_share_keeps_the_owner_its_dataset_was_given(client, engine, config, smb_ready, dataset_dir):
    dataset_dir["name"] = "mario"
    r = client.post("/api/shares/smb", {"name": "foto", "path": "/tank/foto"})
    assert r.body["owner"] == "mario"
    assert engine.called("samba", "create_share")[0].argv[-1] == "Unix User\\mario:F"
    assert config.get_smb_share("foto")["valid_users"] == ["mario"]
    assert engine.called("directories", "owner_set")[0].argv == ["chown", "mario", "/tank/foto"]


def test_a_share_can_be_made_for_another_account(client, engine, config, smb_ready, dataset_dir):
    r = client.post("/api/shares/nfs", {"name": "foto", "path": "/tank/foto",
                                        "allowed_hosts": "192.168.1.0/24", "owner": "mario"})
    assert r.status == 200 and r.body["owner"] == "mario"
    assert engine.called("directories", "owner_set")[0].argv == ["chown", "mario", "/tank/foto"]
    assert config.get_nfs_shares()[0]["owner"] == "mario"


def test_an_nfs_share_of_a_given_dataset_maps_its_clients_onto_the_owner(
        client, engine, config, dataset_dir):
    r = client.post("/api/shares/nfs", {"name": "foto", "path": "/tank/foto",
                                        "allowed_hosts": "192.168.1.20", "owner": "mario"})
    assert r.status == 200
    [add] = engine.called("nfs", "export_add")
    assert add.kwargs["options"] == "rw,sync,no_subtree_check,all_squash,anonuid=1000,anongid=1000"
    saved = config.get_nfs_shares()[0]
    assert saved["options"] == "rw,sync,no_subtree_check" and saved["map_to_owner"] is True


def test_an_nfs_share_of_another_directory_maps_nobody(client, engine, config):
    r = client.post("/api/shares/nfs", {"name": "x", "path": "/srv/x", "allowed_hosts": "*"})
    assert r.status == 200 and "warning" in r.body
    assert engine.called("nfs", "export_add")[0].kwargs["options"] == "rw,sync,no_subtree_check"
    assert config.get_nfs_shares()[0]["map_to_owner"] is False


def test_a_share_for_nobody_is_refused(client, engine, config, smb_ready, dataset_dir):
    r = client.post("/api/shares/smb", {"name": "foto", "path": "/tank/foto", "owner": "ghost"})
    assert r.status == 422 and "no account named ghost" in r.body["detail"]
    assert engine.called("samba", "create_share") == []


def test_a_directory_inside_a_dataset_keeps_its_permissions(client, engine, config, smb_ready, dataset_dir):
    r = client.post("/api/shares/smb", {"name": "estate", "path": "/tank/foto/estate"})
    assert r.status == 200 and "left as they are" in r.body["warning"]
    assert engine.called("directories", "owner_set") == []


def test_an_smb_share_is_deleted(client, engine, config):
    config.add_smb_share({"name": "foto", "path": "/tank/foto"})
    config.add_smb_share({"name": "musica", "path": "/tank/musica"})
    r = client.delete("/api/shares/smb/foto")
    assert r.status == 200
    assert engine.called("samba", "delete_share")[0].argv == ["net", "usershare", "delete", "foto"]
    assert [s["name"] for s in config.get_smb_shares()] == ["musica"]


# ---------------------------------------------------------------------------
# NFS
# ---------------------------------------------------------------------------

def apply_to(exports, call):
    """Run a recorded profile command for real, against a copy of /etc/exports."""
    subprocess.run(["sh", "-c", call.command.replace("/etc/exports", str(exports))], check=True)


def test_an_nfs_share_needs_name_path_and_hosts(client, engine):
    r = client.post("/api/shares/nfs", {"name": "backup", "path": "/tank/backup"})
    assert r.status == 422
    assert engine.actions() == []


def test_an_nfs_share_is_exported_through_the_profile(client, engine, config, tmp_path):
    exports = tmp_path / "exports"
    exports.write_text("/tank/other *(ro)\n")
    r = client.post("/api/shares/nfs", {"name": "backup", "path": "/tank/backup",
                                        "allowed_hosts": "192.168.1.0/24"})
    assert r.status == 200
    assert engine.actions() == [("zfs", "dataset_at_path"),
                                ("nfs", "export_add"), ("nfs", "exportfs_reload")]
    [add] = engine.called("nfs", "export_add")
    apply_to(exports, add)
    apply_to(exports, add)
    assert exports.read_text() == (
        "/tank/other *(ro)\n/tank/backup 192.168.1.0/24(rw,sync,no_subtree_check)\n"
    )
    assert engine.called("nfs", "exportfs_reload")[0].argv == ["exportfs", "-ra"]
    assert config.get_nfs_shares()[0]["allowed_hosts"] == "192.168.1.0/24"


@pytest.mark.parametrize("field, value", [
    ("allowed_hosts", "192.168.1.0/24\n/ *(rw,no_root_squash)"),
    ("allowed_hosts", "10.0.0.1 *"),
    ("options", "rw)\n/ *(rw"),
    ("options", "rw no_root_squash"),
    ("path", "/tank/my backup"),
    ("path", "/tank/back\rup"),
])
def test_what_would_not_stay_one_export_is_refused(client, engine, config, field, value):
    body = {"name": "backup", "path": "/tank/backup", "allowed_hosts": "192.168.1.0/24"}
    body[field] = value
    r = client.post("/api/shares/nfs", body)
    assert r.status == 422
    assert field in r.body["detail"]
    assert engine.actions() == []
    assert config.get_nfs_shares() == []


def test_an_nfs_share_is_removed_line_and_record(client, engine, config, tmp_path):
    exports = tmp_path / "exports"
    exports.write_text("/tank/backup 192.168.1.0/24(rw)\n/tank/backup2 *(ro)\n")
    config.add_nfs_share({"name": "backup", "path": "/tank/backup",
                          "allowed_hosts": "192.168.1.0/24", "options": "rw"})
    r = client.delete("/api/shares/nfs/backup")
    assert r.status == 200
    assert engine.actions() == [("nfs", "export_remove"), ("nfs", "exportfs_reload")]
    apply_to(exports, engine.called("nfs", "export_remove")[0])
    assert exports.read_text() == "/tank/backup2 *(ro)\n"
    assert config.get_nfs_shares() == []


def test_removing_an_unknown_nfs_share_is_404(client, engine):
    assert client.delete("/api/shares/nfs/nope").status == 404
    assert engine.actions() == []


# ---------------------------------------------------------------------------
# Group shares
# ---------------------------------------------------------------------------

@pytest.fixture
def family(config):
    for name in ("luca", "anna", "mario"):
        config.add_user({"username": name, "groups": []})


def test_a_group_share_gives_the_dataset_to_a_new_group(client, engine, config, smb_ready, dataset_dir, family):
    r = client.post("/api/shares/smb", {"name": "Famiglia", "path": "/tank/foto",
                                        "group": "famiglia", "members": ["luca", "anna"]})
    assert r.status == 200 and r.body["members"] == ["anna", "luca"]
    argv = lambda action: [c.argv for c in engine.called("directories", action)]
    assert engine.called("users", "group_create")[0].argv == ["groupadd", "famiglia"]
    assert engine.called("zfs", "dataset_set")[0].argv == ["zfs", "set", "acltype=posixacl", "tank/foto"]
    assert argv("owner_set") == [["chown", "root", "/tank/foto"]]
    assert argv("group_set_recursive") == [["chgrp", "-R", "famiglia", "/tank/foto"]]
    assert argv("mode_set") == [["chmod", "2770", "/tank/foto"]]
    assert argv("acl_group_set") == [["setfacl", "-R", "-m",
                                      "g:famiglia:rwX,d:g:famiglia:rwX,m::rwX,d:m::rwX", "/tank/foto"]]
    assert sorted(c.kwargs["username"] for c in engine.called("users", "add_to_group")) == ["anna", "luca"]
    assert engine.called("samba", "create_share")[0].argv[-1] == "Unix Group\\famiglia:F"
    saved = config.get_smb_share("Famiglia")
    assert saved["group"] == "famiglia" and saved["everyone"] is False and "owner" not in saved
    assert config.get_user("luca")["groups"] == ["famiglia"]
    assert config.get_user("mario")["groups"] == []
    assert "create mask = 0770" in (engine.paths[("samba", "config_file")]).read_text()


def test_a_group_share_for_everyone_takes_every_account_and_the_new_ones(
        client, engine, config, smb_ready, dataset_dir, family):
    r = client.post("/api/shares/smb", {"name": "media", "path": "/tank/foto",
                                        "group": "media", "everyone": True})
    assert r.status == 200
    assert {u["username"] for u in config.get_users() if "media" in u.get("groups", [])} == {
        "luca", "anna", "mario"}
    assert client.post("/api/users/", {"username": "nuova", "password": "una-password"}).status == 200
    [create] = engine.called("users", "create")
    assert "media" in create.argv
    assert "media" in config.get_user("nuova")["groups"]


@pytest.mark.parametrize("group", ["Famiglia", "1abc", "a b", "x" * 32, ""])
def test_a_group_share_needs_a_valid_group_name(client, engine, smb_ready, dataset_dir, family, group):
    r = client.post("/api/shares/smb", {"name": "f", "path": "/tank/foto", "group": group or " ",
                                        "members": []})
    assert r.status == 422
    assert engine.called("users", "group_create") == []


def test_a_group_share_never_takes_over_a_group_of_the_system(client, engine, smb_ready, dataset_dir, family):
    engine.outputs[("users", "group_exists")] = "video:x:44:"
    r = client.post("/api/shares/smb", {"name": "video", "path": "/tank/foto", "group": "video", "members": []})
    assert r.status == 422 and "already a group" in r.body["detail"]
    assert engine.called("directories", "owner_set") == []


def test_unknown_members_change_nothing(client, engine, smb_ready, dataset_dir, family):
    r = client.post("/api/shares/smb", {"name": "f", "path": "/tank/foto", "group": "f", "members": ["nessuno"]})
    assert r.status == 422 and "nessuno" in r.body["detail"]
    assert engine.called("users", "group_create") == []
    assert engine.called("directories", "owner_set") == []


def test_the_members_of_a_group_share_can_change(client, engine, config, smb_ready, dataset_dir, family):
    client.post("/api/shares/smb", {"name": "Famiglia", "path": "/tank/foto",
                                    "group": "famiglia", "members": ["luca", "anna"]})
    r = client.put("/api/shares/smb/Famiglia/members", {"members": ["luca", "mario"], "everyone": True})
    assert r.status == 200 and r.body["members"] == ["luca", "mario"]
    [removed] = engine.called("users", "remove_from_group")
    assert removed.kwargs == {"group": "famiglia", "username": "anna"}
    assert config.get_user("anna")["groups"] == []
    assert config.get_smb_share("Famiglia")["everyone"] is True
    listed = client.get("/api/shares/smb").body["shares"][0]
    assert listed["members"] == ["luca", "mario"]


def test_removing_a_group_share_gives_the_directory_back_to_root(
        client, engine, config, smb_ready, dataset_dir, family):
    client.post("/api/shares/smb", {"name": "Famiglia", "path": "/tank/foto",
                                    "group": "famiglia", "members": ["luca"]})
    assert client.delete("/api/shares/smb/Famiglia").status == 200
    assert engine.called("directories", "acl_group_remove")[0].argv == [
        "setfacl", "-R", "-x", "g:famiglia,d:g:famiglia", "/tank/foto"]
    assert engine.called("directories", "group_set_recursive")[-1].argv == ["chgrp", "-R", "root", "/tank/foto"]
    assert engine.called("users", "group_delete")[0].argv == ["groupdel", "famiglia"]
    assert config.get_user("luca")["groups"] == []
