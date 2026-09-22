"""Authentication (PAM) and authorisation (the admin group), through HTTP."""

from conftest import ACCOUNTS


def test_no_credentials_is_401(client, engine):
    status, _ = client.get("/api/users/", auth=None)
    assert status == 401


def test_a_wrong_password_is_401(client):
    status, _ = client.get("/api/users/", auth=("admin", "wrong"))
    assert status == 401


def test_an_unknown_account_is_401(client):
    status, _ = client.get("/api/users/", auth=("nobody", "whatever"))
    assert status == 401


def test_a_valid_account_outside_the_admin_group_is_403(client, engine):
    status, body = client.delete("/api/users/admin", auth=("mario", ACCOUNTS["mario"][0]))
    assert status == 403
    assert "administrator" in body["detail"]
    actions = [c.args[:2] for c in engine.execute.call_args_list]
    assert ("users", "delete") not in actions


def test_an_administrator_gets_through(client):
    status, body = client.get("/api/users/")
    assert status == 200
    assert body == {"users": []}


def test_five_failures_lock_the_account_even_for_the_right_password(client):
    for _ in range(5):
        assert client.get("/api/users/", auth=("admin", "wrong"))[0] == 401
    status, _ = client.get("/api/users/")
    assert status == 401


def test_a_lockout_is_per_account(client):
    for _ in range(5):
        client.get("/api/users/", auth=("mario", "wrong"))
    status, _ = client.get("/api/users/")
    assert status == 200


def test_the_api_waits_for_setup_before_anything_else(client, config):
    config.is_initialized.return_value = False
    status, body = client.get("/api/users/")
    assert status == 503
    assert "not initialized" in body["detail"]
