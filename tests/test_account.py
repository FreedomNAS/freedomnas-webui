"""/api/account/* — what every account sees and does for itself, administrator or not."""

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


class LinkModule:
    def __init__(self, name, links):
        self.name, self._links = name, links

    def start(self):
        pass

    def links(self):
        return self._links


@pytest.fixture
def modules(monkeypatch):
    import api.routes.account as account
    found = []

    class Loader:
        def __init__(self, engine, config):
            pass

        def instances(self):
            return found

    monkeypatch.setattr(account, "ModuleLoader", Loader)
    return found


def test_an_account_that_does_not_administer_signs_in_to_its_own_page(client, config, modules):
    config.add_smb_share({"name": "mario", "path": "/tank/mario", "owner": "mario", "valid_users": ["mario"]})
    config.add_smb_share({"name": "foto", "path": "/tank/foto", "owner": "admin", "valid_users": ["admin"]})
    config.add_smb_share({"name": "comune", "path": "/tank/comune", "group": "users", "read_only": True})
    config.add_nfs_share({"name": "backup", "path": "/tank/backup", "owner": "mario"})
    config.add_nfs_share({"name": "altro", "path": "/tank/altro", "owner": "admin"})
    r = client.get("/api/account/me", auth=("mario", "mario-password"))
    assert r.status == 200
    assert (r.body["username"], r.body["admin"], r.body["groups"]) == ("mario", False, ["mario", "users"])
    assert [(s["name"], s["read_only"]) for s in r.body["shares"]["smb"]] == [("mario", False), ("comune", True)]
    assert [s["name"] for s in r.body["shares"]["nfs"]] == ["backup"]


def test_the_administration_stays_closed_to_it(client):
    assert client.get("/api/users/", auth=("mario", "mario-password")).status == 403


def test_an_administrator_is_told_it_administers(client, modules):
    assert client.get("/api/account/me").body["admin"] is True


def test_a_wrong_password_opens_no_account_page(client):
    assert client.get("/api/account/me", auth=("mario", "sbagliata")).status == 401


def test_the_links_of_the_modules_are_joined_by_name(client, modules):
    modules.append(({"id": "containers"}, LinkModule("containers", [
        {"name": "jellyfin", "title": "Jellyfin", "path": "/"},
        {"name": "pihole", "title": "Pi-hole", "path": "/admin/", "scheme": "https", "port": 8053},
        {"name": "stopped"}])))
    modules.append(({"id": "proxy"}, LinkModule("proxy", [
        {"name": "jellyfin", "url": "https://jellyfin.nas.home.arpa/"}])))
    links = client.get("/api/account/me", auth=("mario", "mario-password")).body["links"]
    assert links == [
        {"name": "jellyfin", "title": "Jellyfin", "path": "/", "url": "https://jellyfin.nas.home.arpa/"},
        {"name": "pihole", "title": "Pi-hole", "path": "/admin/", "scheme": "https", "port": 8053}]


def test_a_module_that_fails_leaves_the_others_links(client, modules):
    class Broken(LinkModule):
        def links(self):
            raise RuntimeError("no")
    modules.append(({"id": "broken"}, Broken("broken", [])))
    modules.append(({"id": "proxy"}, LinkModule("proxy", [{"name": "site", "scheme": "https", "port": 8443}])))
    assert [l["name"] for l in client.get("/api/account/me").body["links"]] == ["site"]


def test_a_temporary_password_opens_nothing_but_its_change(client, config, mario, modules):
    config.add_user({"username": "mario", "temporary_password": True})
    EXPIRED.add("mario")
    r = client.get("/api/account/me", auth=("mario", "Temporanea-2026"))
    assert (r.status, r.body["detail"]) == (401, "Password expired")
    assert change(client).status == 200
    EXPIRED.discard("mario")
    ACCOUNTS["mario"] = ("una-nuova-password", ["mario"])
    assert client.get("/api/account/me", auth=("mario", "una-nuova-password")).status == 200


def test_a_password_still_recorded_as_temporary_is_refused_even_when_pam_takes_it(client, config):
    config.add_user({"username": "admin", "temporary_password": True})
    r = client.get("/api/users/")
    assert (r.status, r.body["detail"]) == (401, "Password expired")


def test_a_wrong_password_is_not_told_apart_from_a_temporary_one(client, config, mario):
    config.add_user({"username": "mario", "temporary_password": True})
    r = client.get("/api/account/me", auth=("mario", "sbagliata"))
    assert (r.status, r.body["detail"]) == (401, "Invalid credentials")
