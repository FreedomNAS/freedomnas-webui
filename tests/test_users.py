"""
/api/users/* — what reaches the engine, and what never does.

chpasswd reads one "user:password" per line: a line break in the password
would start a second line, for another account. These tests hold the
refusals to happening before the engine is asked for anything.
"""

import pytest


def stdin_payloads(engine):
    return [c.args[2] for c in engine.execute_with_stdin.call_args_list]


def engine_actions(engine):
    """Everything asked of the engine, minus the group lookup authentication makes."""
    return [
        c.args[:2] for c in engine.execute.call_args_list
        if c.args[:2] != ("users", "list_groups")
    ] + [c.args[:2] for c in engine.execute_with_stdin.call_args_list]


@pytest.mark.parametrize("password", [
    "12345678\nroot:taken",
    "12345678\rroot:taken",
    "12345678\0root",
])
def test_a_password_with_a_control_character_never_reaches_chpasswd(client, engine, password):
    status, body = client.post("/api/users/bob/password", {"password": password})
    assert status == 422
    assert "line breaks" in body["detail"]
    assert stdin_payloads(engine) == []


def test_a_new_user_with_a_bad_password_is_not_created_at_all(client, engine):
    status, _ = client.post("/api/users/", {"username": "carla", "password": "12345678\nroot:x"})
    assert status == 422
    assert engine_actions(engine) == []


@pytest.mark.parametrize("path, body", [
    ("/api/users/", {"username": "a:b", "password": "a long password"}),
    ("/api/users/a:b/password", {"password": "a long password"}),
])
def test_a_colon_in_the_username_is_refused(client, engine, path, body):
    status, response = client.post(path, body)
    assert status == 422
    assert "':'" in response["detail"]
    assert engine_actions(engine) == []


def test_a_short_password_is_refused(client, engine):
    status, body = client.post("/api/users/bob/password", {"password": "short"})
    assert status == 422
    assert "at least 8" in body["detail"]
    assert stdin_payloads(engine) == []


def test_a_password_travels_on_stdin_as_one_line(client, engine):
    status, _ = client.post("/api/users/bob/password", {"password": "a long: password"})
    assert status == 200
    assert stdin_payloads(engine) == ["bob:a long: password\n"]
    call = engine.execute_with_stdin.call_args
    assert call.args[:2] == ("users", "set_password_stdin")
    assert "a long: password" not in repr(call.kwargs)


def test_creating_a_user_creates_it_then_sets_its_password(client, engine, config):
    status, body = client.post(
        "/api/users/",
        {"username": "carla", "password": "a long password", "groups": ["wheel", "users"]},
    )
    assert status == 200
    assert body == {"username": "carla"}
    create = [c for c in engine.execute.call_args_list if c.args[:2] == ("users", "create")]
    assert len(create) == 1
    assert create[0].kwargs["username"] == "carla"
    assert create[0].kwargs["groups"] == "wheel,users"
    assert "password" not in create[0].kwargs
    assert stdin_payloads(engine) == ["carla:a long password\n"]
    config.add_user.assert_called_once()
    assert "password" not in config.add_user.call_args.args[0]


@pytest.mark.parametrize("body, missing", [
    ({"password": "a long password"}, "username"),
    ({"username": "carla"}, "password"),
])
def test_create_needs_username_and_password(client, engine, body, missing):
    status, response = client.post("/api/users/", body)
    assert status == 422
    assert missing in response["detail"]


def test_deleting_a_user_goes_through_the_engine_and_the_config(client, engine, config):
    status, _ = client.delete("/api/users/carla")
    assert status == 200
    assert ("users", "delete") in [c.args[:2] for c in engine.execute.call_args_list]
    config.remove_user.assert_called_once_with("carla")
