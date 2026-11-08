"""/api/setup/* — the first-run wizard, reachable before anything else is."""

import pytest

from conftest import ACCOUNTS, Client
from freedomnas.engine import CommandExecutionError
import api.routes.setup as setup_routes

LOGIN_PASSWORD = ACCOUNTS["admin"][0]
TEMPORARY = "Temporanea-2026"
NEW_PASSWORD = "a-new-secret"


@pytest.fixture
def admin(server, engine, fresh_config, monkeypatch):
    engine.outputs[("zfs", "pool_list")] = "rpool\t10G\t1G\t9G\t10\tONLINE"
    activated = []

    def activate(engine_, pool, state=None):
        activated.append((pool, state))
        return f"{pool}/.freedomnas"

    monkeypatch.setattr(setup_routes.shadow, "activate", activate)
    mirrored = []
    monkeypatch.setattr(setup_routes.shadow, "sync", lambda engine_, path: mirrored.append(path) or [])
    client = Client(server, ("admin", LOGIN_PASSWORD))
    client.activated = activated
    client.mirrored = mirrored
    return client


def body(**changes):
    base = {"hostname": "nas", "timezone": "Europe/Rome",
            "admin": {"password": NEW_PASSWORD}, "state_pool": "rpool"}
    base.update(changes)
    return base


def test_status_before_setup(anonymous, fresh_config):
    r = anonymous.get("/api/setup/status")
    assert r.status == 200
    assert r.body == {"initialized": False, "restored": False, "password_chosen": False,
                      "edition": None, "detected_edition": "debian"}


def test_the_wizard_needs_the_administrators_credentials(anonymous, engine, fresh_config):
    assert anonymous.post("/api/setup/initialize", body()).status == 401
    assert anonymous.get("/api/setup/whoami").status == 401
    assert engine.actions() == []
    assert not fresh_config.is_initialized()


def test_whoami_answers_before_setup(admin):
    r = admin.get("/api/setup/whoami")
    assert r.status == 200 and r.body == {"username": "admin"}


def test_initialize_sets_up_the_system_and_marks_it(admin, engine, fresh_config, files):
    r = admin.post("/api/setup/initialize", body())
    assert r.status == 200
    assert r.body == {"detail": "System initialized", "hostname": "nas", "edition": "debian",
                      "admin": "admin", "state_dataset": "rpool/.freedomnas"}
    assert engine.actions() == [
        ("zfs", "pool_list"),
        ("system", "hostname_set"),
        ("system", "timezone_set"),
        ("samba", "usershare_prepare"),
        ("services", "restart"),
        ("users", "set_password_stdin"),
        ("samba", "check_installed"),
        ("samba", "set_user_password"),
        ("users", "group_exists"),
        ("users", "group_exists"),
        ("users", "get_user_info"),
    ]
    [password] = engine.called("users", "set_password_stdin")
    assert password.stdin == f"admin:{NEW_PASSWORD}\n"
    assert "usershare max shares = 100" in files[("samba", "config_file")].read_text()
    assert fresh_config.is_initialized()
    assert admin.mirrored == [str(fresh_config.config_path)]
    assert fresh_config.get_hostname() == "nas"
    assert fresh_config.get("system.ssh_enabled") is False
    assert [u["username"] for u in fresh_config.get_users()] == ["admin"]
    assert fresh_config.get_users()[0]["uid"] == 1000
    assert admin.activated == [("rpool", fresh_config.config_path.parent)]


def test_a_new_name_keeps_the_old_account_with_the_new_password(admin, engine, fresh_config):
    r = admin.post("/api/setup/initialize", body(admin={"username": "sarah", "password": NEW_PASSWORD}))
    assert r.status == 200 and r.body["admin"] == "sarah"
    [create] = engine.called("users", "create")
    assert create.argv[-1] == "sarah" and "sudo" in create.argv
    assert [c.stdin for c in engine.called("users", "set_password_stdin")] == [
        f"sarah:{NEW_PASSWORD}\n", f"admin:{NEW_PASSWORD}\n"]
    assert engine.called("users", "delete") == []
    assert sorted(u["username"] for u in fresh_config.get_users()) == ["admin", "sarah"]


def test_the_old_account_can_be_removed(admin, engine, fresh_config):
    r = admin.post("/api/setup/initialize",
                   body(admin={"username": "sarah", "password": NEW_PASSWORD, "remove_default": True}))
    assert r.status == 200
    [delete] = engine.called("users", "delete")
    assert delete.argv[-1] == "admin"
    [smb] = engine.called("samba", "delete_user")
    assert smb.argv[-1] == "admin"
    assert [u["username"] for u in fresh_config.get_users()] == ["sarah"]


def test_root_is_never_removed(server, engine, fresh_config, monkeypatch):
    monkeypatch.setitem(ACCOUNTS, "root", ("root-password", ["root"]))
    monkeypatch.setattr(setup_routes.shadow, "activate", lambda *a, **k: "rpool/.freedomnas")
    engine.outputs[("zfs", "pool_list")] = "rpool\t10G\t1G\t9G\t10\tONLINE"
    root = Client(server, ("root", "root-password"))
    r = root.post("/api/setup/initialize",
                  body(admin={"username": "sarah", "password": NEW_PASSWORD, "remove_default": True}))
    assert r.status == 200 and r.body["admin"] == "sarah"
    assert engine.called("users", "delete") == []
    assert engine.called("samba", "delete_user") == []
    assert [c.kwargs["username"] for c in engine.called("users", "set_password_stdin")] == ["sarah"]
    assert [u["username"] for u in fresh_config.get_users()] == ["sarah"]


@pytest.mark.parametrize("admin_body, message", [
    ({"password": LOGIN_PASSWORD}, "Choose a new password"),
    ({"password": "short"}, "at least 8"),
    ({}, "at least 8"),
    ({"username": "Sarah", "password": NEW_PASSWORD}, "lowercase"),
    ({"username": "root:0", "password": NEW_PASSWORD}, "lowercase"),
])
def test_the_administrator_is_checked_before_anything_runs(admin, engine, fresh_config,
                                                          admin_body, message):
    r = admin.post("/api/setup/initialize", body(admin=admin_body))
    assert r.status == 422
    assert message in r.body["detail"]
    assert engine.actions() == []
    assert not fresh_config.is_initialized()


@pytest.mark.parametrize("state_pool, message", [
    ("", "Choose the pool"),
    ("tank", "No pool named tank"),
])
def test_the_pool_for_the_configuration_must_exist(admin, engine, fresh_config, state_pool, message):
    r = admin.post("/api/setup/initialize", body(state_pool=state_pool))
    assert r.status == 422
    assert message in r.body["detail"]
    assert ("system", "hostname_set") not in engine.actions()
    assert not fresh_config.is_initialized()


def test_the_configuration_can_go_to_the_data_pool_created_in_the_wizard(admin, engine, fresh_config):
    pool = {"name": "tank", "vdev_type": "mirror", "devices": ["/dev/sda", "/dev/sdb"]}
    r = admin.post("/api/setup/initialize", body(pool=pool, state_pool="tank"))
    assert r.status == 200
    [create] = engine.called("zfs", "pool_create")
    assert create.argv[-3:] == ["mirror", "/dev/sda", "/dev/sdb"]
    assert fresh_config.get_pool("tank")["vdev_type"] == "mirror"
    assert admin.activated == [("tank", fresh_config.config_path.parent)]


def test_an_invalid_pool_stops_the_wizard_before_anything_changes(admin, engine, fresh_config):
    pool = {"name": "1tank", "vdev_type": "mirror", "devices": ["/dev/sda", "/dev/sdb"]}
    r = admin.post("/api/setup/initialize", body(pool=pool, state_pool="1tank"))
    assert r.status == 422
    assert ("system", "hostname_set") not in engine.actions()
    assert not fresh_config.is_initialized()


def test_initialize_runs_once(client, engine):
    r = client.post("/api/setup/initialize", body())
    assert r.status == 409
    assert engine.actions() == []


def test_a_failing_step_leaves_the_system_uninitialized(admin, engine, fresh_config):
    engine.outputs[("system", "timezone_set")] = CommandExecutionError("timedatectl failed")
    r = admin.post("/api/setup/initialize", body())
    assert r.status == 500
    assert "timedatectl failed" in r.body["detail"]
    assert not fresh_config.is_initialized()
    # The state already moved — harmless: activating the same pool again on
    # the next attempt does nothing.
    assert admin.activated == [("rpool", fresh_config.config_path.parent)]


def test_a_state_that_cannot_move_leaves_the_system_untouched(admin, engine, fresh_config,
                                                              monkeypatch):
    def activate(engine_, pool, state=None):
        raise CommandExecutionError("cannot mount tank/.freedomnas")

    monkeypatch.setattr(setup_routes.shadow, "activate", activate)
    r = admin.post("/api/setup/initialize", body())
    assert r.status == 500
    assert "cannot mount" in r.body["detail"]
    assert not fresh_config.is_initialized()
    assert not fresh_config.setup_pending()
    assert engine.actions() == [("zfs", "pool_list")]


@pytest.mark.parametrize("timezone", ["../../../etc/shadow", "-u", "Europe/Rome x", "Mars/Base"])
def test_a_timezone_that_is_not_one_is_refused_before_anything_runs(admin, engine,
                                                                    fresh_config, timezone):
    r = admin.post("/api/setup/initialize", body(timezone=timezone))
    assert r.status == 422
    assert engine.actions() == []


def test_the_edition_must_be_the_one_running(admin, engine, fresh_config):
    r = admin.post("/api/setup/initialize", body(edition="freebsd"))
    assert r.status == 422
    assert "runs the debian edition" in r.body["detail"]
    assert engine.actions() == []


def test_the_wizard_lists_disks_and_pools(admin, engine):
    engine.outputs[("zfs", "dataset_at_path")] = "rpool/ROOT/debian\n"
    pools = admin.get("/api/setup/pools").body
    assert pools["pools"][0]["name"] == "rpool" and pools["system_pool"] == "rpool"
    assert admin.get("/api/setup/devices").status == 200


@pytest.fixture
def restored(admin, engine, fresh_config, setup_pending_file):
    """An image whose first boot restored another system's configuration."""
    fresh_config.set_hostname("oldnas")
    fresh_config.add_user({"username": "sarah", "uid": 1500, "groups": ["sudo"], "admin": True})
    fresh_config.add_user({"username": "mario", "uid": 1501, "groups": ["users"]})
    fresh_config.set("system.ssh_enabled", True)
    fresh_config.mark_initialized("freebsd")  # made on another edition
    setup_pending_file.touch()
    engine.outputs[("zfs", "dataset_at_path")] = "tank/.freedomnas\n"
    return admin


def test_a_restored_configuration_keeps_the_wizard_in_front(restored, anonymous):
    assert anonymous.get("/api/setup/status").body["initialized"] is False
    assert anonymous.get("/api/setup/status").body["restored"] is True
    assert restored.get("/api/system/info").status == 503


def test_the_wizard_shows_what_was_restored(restored):
    r = restored.get("/api/setup/restored")
    assert r.status == 200
    assert r.body == {"hostname": "oldnas", "timezone": "UTC", "state_dataset": "tank/.freedomnas",
                      "admins": ["sarah"], "users": ["mario"], "ssh_enabled": True}


def test_nothing_is_restored_on_a_system_set_up_here(admin, fresh_config):
    fresh_config.mark_initialized("debian")
    assert admin.get("/api/setup/restored").status == 409
    assert admin.post("/api/setup/initialize", body()).status == 409


def test_finishing_a_restore_takes_over_the_restored_administrator(
        restored, engine, fresh_config, setup_pending_file, monkeypatch):
    monkeypatch.setitem(ACCOUNTS, "sarah", ("unknown", ["sarah", "sudo"]))
    r = restored.post("/api/setup/initialize",
                      {"admin": {"username": "sarah", "password": NEW_PASSWORD, "remove_default": True},
                       "users": {"mario": TEMPORARY}})
    assert r.status == 200
    assert r.body == {"detail": "System restored", "hostname": "oldnas", "admin": "sarah",
                      "temporary_passwords": ["mario"]}
    assert engine.called("users", "create") == []
    assert [c.stdin for c in engine.called("users", "set_password_stdin")] == [
        f"sarah:{NEW_PASSWORD}\n", f"mario:{TEMPORARY}\n"]
    assert [c.argv for c in engine.called("users", "password_expire")] == [["chage", "-d", "0", "mario"]]
    [delete] = engine.called("users", "delete")
    assert delete.argv[-1] == "admin"
    assert engine.called("services", "enable") == []
    assert engine.called("system", "hostname_set") == []
    assert engine.called("samba", "usershare_prepare") != []
    assert restored.activated == []
    users = {u["username"]: u for u in fresh_config.get_users()}
    assert sorted(users) == ["mario", "sarah"]
    assert users["sarah"]["uid"] == 1500 and users["sarah"]["admin"] is True
    assert fresh_config.get("system.ssh_enabled") is False
    assert not setup_pending_file.exists()
    assert restored.get("/api/setup/status").body["initialized"] is True
    assert fresh_config.get_edition() == "debian"


def test_ssh_comes_back_only_when_asked(restored, engine, fresh_config):
    r = restored.post("/api/setup/initialize", {"admin": {"password": NEW_PASSWORD}, "ssh": True,
                                                "users": {"mario": TEMPORARY, "sarah": TEMPORARY}})
    assert r.status == 200 and r.body["admin"] == "admin"
    assert [c.kwargs["service_name"] for c in engine.called("services", "enable")] == ["ssh"]
    assert fresh_config.get("system.ssh_enabled") is True
    assert sorted(u["username"] for u in fresh_config.get_users()) == ["admin", "mario", "sarah"]


def test_a_failed_restore_keeps_the_password_it_was_opened_with(
        restored, engine, fresh_config, setup_pending_file):
    from freedomnas.engine import CommandExecutionError
    engine.outputs[("services", "start")] = CommandExecutionError(
        "warning: ssh: unable to open supervise/control: file does not exist")
    r = restored.post("/api/setup/initialize", {"admin": {"password": NEW_PASSWORD}, "ssh": True,
                                                "users": {"mario": TEMPORARY, "sarah": TEMPORARY}})
    assert r.status == 500
    assert engine.called("users", "set_password_stdin") == []
    assert setup_pending_file.exists()
    assert restored.get("/api/setup/status").body["restored"] is True


def test_a_restore_still_needs_a_new_password(restored, engine, setup_pending_file):
    r = restored.post("/api/setup/initialize", {"admin": {"password": LOGIN_PASSWORD}})
    assert r.status == 422
    assert engine.called("users", "set_password_stdin") == []
    assert setup_pending_file.exists()


def test_a_fresh_setup_clears_the_pending_file(admin, setup_pending_file):
    setup_pending_file.touch()
    assert admin.post("/api/setup/initialize", body()).status == 200
    assert not setup_pending_file.exists()


def test_an_image_needs_a_new_password(admin, setup_pending_file):
    setup_pending_file.touch()
    r = admin.post("/api/setup/initialize", body(admin={"password": LOGIN_PASSWORD}))
    assert r.status == 422
    assert setup_pending_file.exists()


def test_an_installation_keeps_the_password_chosen_in_the_installer(admin, setup_pending_file):
    setup_pending_file.write_text("install\n")
    assert admin.get("/api/setup/status").body["password_chosen"] is True
    r = admin.post("/api/setup/initialize", body(admin={"password": LOGIN_PASSWORD}))
    assert r.status == 200
    assert not setup_pending_file.exists()


def test_a_restore_after_an_installation_keeps_the_chosen_password(
        restored, engine, setup_pending_file):
    setup_pending_file.write_text("install\n")
    r = restored.post("/api/setup/initialize", {"admin": {"password": LOGIN_PASSWORD},
                                                "users": {"mario": TEMPORARY, "sarah": TEMPORARY}})
    assert r.status == 200
    assert not setup_pending_file.exists()


def test_the_administrator_joins_the_admin_groups_this_system_has(admin, engine, fresh_config):
    engine.outputs[("users", "group_exists")] = lambda group, **_: "docker:x:970:\n" if group == "docker" else ""
    assert admin.post("/api/setup/initialize", body()).status == 200
    [joined] = engine.called("users", "add_to_group")
    assert joined.kwargs == {"username": "admin", "group": "docker"}
    entry = next(u for u in fresh_config.get_users() if u["username"] == "admin")
    assert (entry["groups"], entry["admin"]) == (["docker"], True)


def test_a_restore_needs_a_temporary_password_for_every_restored_account(restored, engine, setup_pending_file):
    r = restored.post("/api/setup/initialize", {"admin": {"password": NEW_PASSWORD}, "users": {"mario": TEMPORARY}})
    assert r.status == 422 and "sarah" in r.body["detail"]
    r = restored.post("/api/setup/initialize", {"admin": {"password": NEW_PASSWORD},
                                                "users": {"mario": "short", "sarah": TEMPORARY}})
    assert r.status == 422 and r.body["detail"].startswith("mario:")
    assert engine.called("users", "set_password_stdin") == []
    assert setup_pending_file.exists()


def test_restored_accounts_are_recorded_with_a_temporary_password(restored, engine, fresh_config):
    restored.post("/api/setup/initialize", {"admin": {"password": NEW_PASSWORD},
                                            "users": {"mario": TEMPORARY, "sarah": TEMPORARY}})
    users = {u["username"]: u for u in fresh_config.get_users()}
    assert users["mario"]["temporary_password"] is True and users["sarah"]["temporary_password"] is True
    assert "temporary_password" not in users["admin"]
