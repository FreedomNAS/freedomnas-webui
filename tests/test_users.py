"""
/api/users/* — what reaches the engine, and what never does.

chpasswd reads one "user:password" per line: a line break in the password
would start a second line, for another account. These tests hold the
refusals to happening before the engine is asked for anything.
"""

import pytest


def stdin_payloads(engine):
    return [c.stdin for c in engine.calls if c.stdin is not None]


@pytest.mark.parametrize("password", [
    "12345678\nroot:taken",
    "12345678\rroot:taken",
    "12345678\0root",
])
def test_a_password_with_a_control_character_never_reaches_chpasswd(client, engine, password):
    r = client.post("/api/users/bob/password", {"password": password})
    assert r.status == 422
    assert "line breaks" in r.body["detail"]
    assert stdin_payloads(engine) == []


def test_a_new_user_with_a_bad_password_is_not_created_at_all(client, engine):
    r = client.post("/api/users/", {"username": "carla", "password": "12345678\nroot:x"})
    assert r.status == 422
    assert engine.actions() == []


@pytest.mark.parametrize("path, body", [
    ("/api/users/", {"username": "a:b", "password": "a long password"}),
    ("/api/users/a:b/password", {"password": "a long password"}),
])
def test_a_colon_in_the_username_is_refused(client, engine, path, body):
    r = client.post(path, body)
    assert r.status == 422
    assert "':'" in r.body["detail"]
    assert engine.actions() == []


def test_a_short_password_is_refused(client, engine):
    r = client.post("/api/users/bob/password", {"password": "short"})
    assert r.status == 422
    assert "at least 8" in r.body["detail"]
    assert stdin_payloads(engine) == []


def test_a_password_travels_on_stdin_as_one_line(client, engine):
    r = client.post("/api/users/bob/password", {"password": "a long: password"})
    assert r.status == 200
    [call] = engine.called("users", "set_password_stdin")
    assert call.argv == ["chpasswd"]
    assert call.stdin == "bob:a long: password\n"
    assert "a long: password" not in call.command


def test_creating_a_user_creates_it_then_sets_its_password(client, engine, config):
    r = client.post(
        "/api/users/",
        {"username": "carla", "password": "a long password", "groups": ["sudo", "users"]},
    )
    assert r.status == 200
    assert r.body == {"username": "carla"}
    assert engine.actions() == [
        ("users", "create"),
        ("users", "set_password_stdin"),
        ("samba", "check_installed"),
        ("samba", "set_user_password"),
    ]
    [create] = engine.called("users", "create")
    assert create.argv == ["useradd", "-m", "-s", "/bin/bash", "-G", "sudo,users", "carla"]
    assert stdin_payloads(engine) == ["carla:a long password\n",
                                      "a long password\na long password\n"]
    [saved] = config.get_users()
    assert saved["username"] == "carla"
    assert "password" not in saved
    assert "a long password" not in config.config_path.read_text()


def test_a_hostile_username_stays_one_argument(client, engine):
    r = client.post("/api/users/", {"username": "x; reboot", "password": "a long password"})
    assert r.status == 200
    [create] = engine.called("users", "create")
    assert create.argv[-1] == "x; reboot"


@pytest.mark.parametrize("body, missing", [
    ({"password": "a long password"}, "username"),
    ({"username": "carla"}, "password"),
])
def test_create_needs_username_and_password(client, engine, body, missing):
    r = client.post("/api/users/", body)
    assert r.status == 422
    assert missing in r.body["detail"]


def test_a_failing_useradd_is_reported_and_not_recorded(client, engine, config):
    from freedomnas.engine import CommandExecutionError
    engine.outputs[("users", "create")] = CommandExecutionError("useradd: user exists")
    r = client.post("/api/users/", {"username": "carla", "password": "a long password"})
    assert r.status == 500
    assert "user exists" in r.body["detail"]
    assert config.get_users() == []
    assert stdin_payloads(engine) == []


def test_deleting_a_user_goes_through_the_engine_and_the_config(client, engine, config):
    config.add_user({"username": "carla", "groups": []})
    r = client.delete("/api/users/carla")
    assert r.status == 200
    [delete] = engine.called("users", "delete")
    assert delete.argv == ["userdel", "-r", "carla"]
    assert config.get_users() == []


def test_the_samba_password_travels_on_stdin_too(client, engine):
    r = client.post("/api/users/bob/password", {"password": "a long: password"})
    assert r.status == 200
    [call] = engine.called("samba", "set_user_password")
    assert call.argv == ["smbpasswd", "-a", "-s", "bob"]
    assert call.stdin == "a long: password\na long: password\n"
    assert "a long: password" not in call.command


def test_without_samba_only_the_system_password_is_set(client, engine):
    from freedomnas.engine import CommandExecutionError
    engine.outputs[("samba", "check_installed")] = CommandExecutionError("testparm: not found")
    r = client.post("/api/users/", {"username": "carla", "password": "a long password"})
    assert r.status == 200
    assert engine.called("samba", "set_user_password") == []
    assert stdin_payloads(engine) == ["carla:a long password\n"]


def test_a_deleted_user_leaves_the_samba_database_first(client, engine, config):
    config.add_user({"username": "carla", "groups": []})
    assert client.delete("/api/users/carla").status == 200
    [smb] = engine.called("samba", "delete_user")
    assert smb.argv == ["smbpasswd", "-x", "carla"]
    actions = engine.actions()
    assert actions.index(("samba", "delete_user")) < actions.index(("users", "delete"))


def test_a_user_whose_samba_password_fails_is_taken_back(client, engine, config):
    from freedomnas.engine import CommandExecutionError
    engine.outputs[("samba", "set_user_password")] = CommandExecutionError("smbpasswd: can't load smb.conf")
    r = client.post("/api/users/", {"username": "carla", "password": "a long password"})
    assert r.status == 500
    assert "smb.conf" in r.body["detail"]
    [delete] = engine.called("users", "delete")
    assert delete.argv == ["userdel", "-r", "carla"]
    assert engine.called("samba", "delete_user")
    assert config.get_users() == []


def test_an_account_that_already_existed_is_never_deleted(client, engine, config):
    from freedomnas.engine import CommandExecutionError
    engine.outputs[("users", "create")] = CommandExecutionError("useradd: user 'carla' already exists")
    assert client.post("/api/users/", {"username": "carla", "password": "a long password"}).status == 500
    assert engine.called("users", "delete") == []
    assert engine.called("samba", "delete_user") == []
