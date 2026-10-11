"""Page routes, static files and the placeholders for later phases."""

import re
from pathlib import Path

import pytest

PAGES = ["/auth/", "/setup/", "/dashboard/", "/storage/", "/shares/", "/network/",
         "/users/", "/updates/", "/services/", "/plugins/", "/system/"]


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


@pytest.mark.parametrize("path", ["/static/css/main.css", "/static/js/main.js"])
def test_static_files_are_revalidated_after_an_update(anonymous, config, path):
    assert anonymous.get(path).headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("method, path", [
    ("GET", "/api/packages/"),
    ("POST", "/api/packages/install"),
    ("GET", "/api/backup/"),
    ("DELETE", "/api/backup/job/1"),
])
def test_later_phases_answer_501(anonymous, config, method, path):
    assert anonymous.request(method, path).status == 501


# ---------------------------------------------------------------------------
# What the pages put into HTML
#
# The credentials sit in sessionStorage, so script injected into a page can
# read the administrator's password. A name inside an inline handler is
# decoded by the HTML parser before JavaScript reads it, so no escaping keeps
# it inside its string: values travel in data-* attributes instead.
# ---------------------------------------------------------------------------

JS = sorted(Path("frontend/static/js").glob("*.js"))


@pytest.mark.parametrize("script", JS, ids=lambda p: p.name)
def test_no_inline_handler_carries_a_value(script):
    assert not re.search(r"""\bon\w+=("[^"]*|'[^']*)\$\{""", script.read_text())


@pytest.mark.parametrize("script", [p for p in JS if "function esc(" in p.read_text()],
                         ids=lambda p: p.name)
def test_esc_also_escapes_quotes(script):
    body = script.read_text().split("function esc(", 1)[1].split("\n}", 1)[0]
    assert "&quot;" in body and "&#39;" in body


def test_a_toast_shows_its_message_as_text():
    toast = Path("frontend/static/js/main.js").read_text().split("function toast(", 1)[1]
    toast = toast.split("\n}", 1)[0]
    assert "${msg}" not in toast
    assert "textContent = msg" in toast


# A dialog nested in another inherits its opacity: opening it catches every
# click on the page while showing nothing. An unclosed <div> is enough.

TEMPLATES = sorted(Path("frontend/templates").glob("*.html"))


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_divs_balance_and_no_dialog_sits_inside_another(template):
    html = template.read_text()
    open_divs = []
    for tag in re.finditer(r"<div\b[^>]*>|</div>", html):
        if tag.group().startswith("</"):
            assert open_divs, f"extra </div> at offset {tag.start()}"
            open_divs.pop()
            continue
        backdrop = "modal-backdrop" in tag.group()
        assert not (backdrop and any(open_divs)), f"nested dialog: {tag.group()}"
        open_divs.append(backdrop)
    assert not open_divs, "unclosed <div>"


def test_the_hidden_attribute_wins_over_any_display_rule():
    css = Path("frontend/static/css/main.css").read_text()
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important;?\s*\}", css)


def test_the_setup_wizard_shows_only_the_current_steps_footer():
    wizard = Path("frontend/static/js/setup.js").read_text()
    show_step = wizard.split("function showStep(", 1)[1].split("\n  }", 1)[0]
    assert ".wizard-footer" in show_step


def test_community_pages_appear_in_the_sidebar(anonymous, config, monkeypatch):
    from api.main import templates
    from api.module_discovery import NavEntry
    monkeypatch.setitem(templates.env.globals, "community_nav", [
        NavEntry("/containers/", "Containers", "#icon-module", "advanced-only")])
    body = anonymous.get("/dashboard/").body
    assert re.search(r'href="/containers/" class="nav-item advanced-only"', body)
    assert "Containers" in body
