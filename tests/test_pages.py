"""Page routes, static files and the placeholders for later phases."""

import pytest

PAGES = ["/auth/", "/setup/", "/dashboard/", "/storage/", "/shares/", "/network/",
         "/users/", "/updates/", "/system/"]


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders(anonymous, config, path):
    r = anonymous.get(path)
    assert r.status == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<html" in r.body.lower()


def test_the_user_form_offers_the_groups_the_profile_names(anonymous, config):
    body = anonymous.get("/users/").body
    assert "sudo" in body
    assert "sambashare" in body


def test_root_goes_to_the_dashboard_once_set_up(location, engine, config):
    assert location("/") == (307, "/dashboard/")


def test_root_goes_to_setup_before_that(location, engine, fresh_config):
    assert location("/") == (307, "/setup/")


@pytest.mark.parametrize("path", ["/static/css/main.css", "/static/js/main.js"])
def test_static_files_are_served(anonymous, config, path):
    assert anonymous.get(path).status == 200


@pytest.mark.parametrize("method, path", [
    ("GET", "/api/packages/"),
    ("POST", "/api/packages/install"),
    ("GET", "/api/backup/"),
    ("DELETE", "/api/backup/job/1"),
])
def test_later_phases_answer_501(anonymous, config, method, path):
    assert anonymous.request(method, path).status == 501
