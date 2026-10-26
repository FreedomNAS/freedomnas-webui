"""/api/account/password — any account changes its own password."""

import pytest

from conftest import ACCOUNTS, EXPIRED


@pytest.fixture
def mario(monkeypatch):
    monkeypatch.setitem(ACCOUNTS, "mario", ("Temporanea-2026", ["mario"]))
    yield
    EXPIRED.discard("mario")


def change(client, **body):
    return client.post("/api/account/password", {"username": "mario", "password": "Temporanea-2026",
                                                 "new_password": "una-nuova-password", **body})


def test_an_account_with_an_expired_temporary_password_sets_its_own(client, engine, config, mario):
    config.add_user({"username": "mario", "temporary_password": True})
    EXPIRED.add("mario")
    r = change(client)
    assert r.status == 200
    assert [c.stdin for c in engine.called("users", "set_password_stdin")] == ["mario:una-nuova-password\n"]
    assert [c.argv for c in engine.called("users", "password_fresh")] == [["chage", "-d", "-1", "mario"]]
    assert "temporary_password" not in config.get_user("mario")


def test_a_wrong_password_changes_nothing(client, engine, mario):
    r = change(client, password="sbagliata")
    assert r.status == 401
    assert engine.called("users", "set_password_stdin") == []


@pytest.mark.parametrize("new", ["corta", "Temporanea-2026"])
def test_the_new_password_must_be_a_good_one(client, engine, mario, new):
    assert change(client, new_password=new).status == 422
    assert engine.called("users", "set_password_stdin") == []


def test_root_cannot_change_its_password_here(client, engine):
    r = client.post("/api/account/password", {"username": "root", "password": "x", "new_password": "una-nuova-password"})
    assert r.status == 422
