"""Authentication (PAM), authorisation (the admin group) and the first-run guard."""

import base64
import urllib.error
import urllib.request

import pytest

from conftest import ACCOUNTS


def test_no_credentials_is_401(anonymous, config):
    assert anonymous.get("/api/users/").status == 401


def test_a_wrong_password_is_401(client):
    assert client.get("/api/users/", auth=("admin", "wrong")).status == 401


def test_an_unknown_account_is_401(client):
    assert client.get("/api/users/", auth=("nobody", "whatever")).status == 401


@pytest.mark.parametrize("authorization", [
    None, "Basic !!!not-base64", "Basic " + base64.b64encode(b"no-colon").decode(),
    "Bearer abc", "Basic " + base64.b64encode(b"admin:wrong").decode(),
])
def test_no_401_invites_the_browser_login_dialog(client, authorization):
    """
    A Basic challenge makes the browser open its own dialog in front of the
    page and remember what is typed into it, sending it cross-site too.
    """
    headers = {"Authorization": authorization} if authorization else {}
    req = urllib.request.Request(f"http://{client.host}/api/users/", headers=headers)
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=10)
    assert err.value.code == 401
    assert err.value.headers["WWW-Authenticate"] == "FreedomNAS"


def test_a_valid_account_outside_the_admin_group_is_403(client, engine):
    r = client.delete("/api/users/admin", auth=("mario", ACCOUNTS["mario"][0]))
    assert r.status == 403
    assert "administrator" in r.body["detail"]
    assert ("users", "delete") not in engine.actions()


def test_the_admin_group_comes_from_the_profile(client, engine):
    assert client.get("/api/users/").status == 200
    lookup = engine.called("users", "list_groups")
    assert lookup and lookup[-1].argv == ["id", "-nG", "admin"]


def test_root_is_an_administrator_without_a_group_lookup(client, engine, monkeypatch):
    monkeypatch.setitem(ACCOUNTS, "root", ("root-password", []))
    assert client.get("/api/users/", auth=("root", "root-password")).status == 200
    assert engine.called("users", "list_groups") == []


def test_an_administrator_gets_through(client):
    r = client.get("/api/users/")
    assert r.status == 200
    assert r.body == {"users": []}


def test_five_failures_lock_the_account_even_for_the_right_password(client):
    for _ in range(5):
        assert client.get("/api/users/", auth=("admin", "wrong")).status == 401
    assert client.get("/api/users/").status == 401


def test_a_lockout_is_per_account(client):
    for _ in range(5):
        client.get("/api/users/", auth=("mario", "wrong"))
    assert client.get("/api/users/").status == 200


def test_a_success_resets_the_failure_count(client):
    for _ in range(4):
        client.get("/api/users/", auth=("admin", "wrong"))
    assert client.get("/api/users/").status == 200
    for _ in range(4):
        client.get("/api/users/", auth=("admin", "wrong"))
    assert client.get("/api/users/").status == 200


def test_the_api_waits_for_setup(client, fresh_config):
    r = client.get("/api/users/")
    assert r.status == 503
    assert "not initialized" in r.body["detail"]


def test_status_and_setup_are_reachable_before_setup(anonymous, fresh_config):
    assert anonymous.get("/api/system/status").status == 200
    assert anonymous.get("/api/setup/status").status == 200
