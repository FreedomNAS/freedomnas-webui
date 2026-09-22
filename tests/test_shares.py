"""/api/shares/* — Samba usershares and NFS exports."""

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
    assert engine.actions() == [("samba", "create_share"), ("samba", "reload_config")]
    [create] = engine.called("samba", "create_share")
    assert create.argv == ["net", "usershare", "add", "foto", "/tank/foto", "foto",
                           "Everyone:R,admin:F"]
    saved = config.get_smb_share("foto")
    assert saved["path"] == "/tank/foto"
    assert saved["valid_users"] == ["admin"]
    assert saved["guest_ok"] is False


def test_a_comment_with_spaces_stays_the_comment(client, engine, smb_ready):
    client.post("/api/shares/smb", {"name": "foto", "path": "/tank/foto",
                                    "comment": "Foto & Video", "acl": "Everyone:F"})
    [create] = engine.called("samba", "create_share")
    assert create.argv[-2:] == ["Foto & Video", "Everyone:F"]


def test_an_empty_comment_does_not_shift_the_acl(client, engine, smb_ready):
    client.post("/api/shares/smb", {"name": "foto", "path": "/tank/foto", "comment": ""})
    [create] = engine.called("samba", "create_share")
    assert create.argv[-2:] == ["", "Everyone:R,admin:F"]


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

def test_an_nfs_share_needs_name_path_and_hosts(client, engine):
    r = client.post("/api/shares/nfs", {"name": "backup", "path": "/tank/backup"})
    assert r.status == 422
    assert engine.actions() == []


def test_an_nfs_share_is_exported_reloaded_and_recorded(client, engine, config, files):
    exports = files[("nfs", "config_file")]
    exports.write_text("/tank/other *(ro)\n")
    r = client.post("/api/shares/nfs", {"name": "backup", "path": "/tank/backup",
                                        "allowed_hosts": "192.168.1.0/24"})
    assert r.status == 200
    assert exports.read_text() == (
        "/tank/other *(ro)\n/tank/backup 192.168.1.0/24(rw,sync,no_subtree_check)\n"
    )
    assert engine.called("nfs", "exportfs_reload")[0].argv == ["exportfs", "-ra"]
    assert config.get_nfs_shares()[0]["allowed_hosts"] == "192.168.1.0/24"


def test_an_nfs_share_is_removed_line_and_record(client, engine, config, files):
    exports = files[("nfs", "config_file")]
    exports.write_text("/tank/backup 192.168.1.0/24(rw)\n/tank/backup2 *(ro)\n")
    config.add_nfs_share({"name": "backup", "path": "/tank/backup",
                          "allowed_hosts": "192.168.1.0/24", "options": "rw"})
    r = client.delete("/api/shares/nfs/backup")
    assert r.status == 200
    assert exports.read_text() == "/tank/backup2 *(ro)\n"
    assert config.get_nfs_shares() == []
    assert ("nfs", "exportfs_reload") in engine.actions()


def test_removing_an_unknown_nfs_share_is_404(client, engine):
    assert client.delete("/api/shares/nfs/nope").status == 404
    assert engine.actions() == []
