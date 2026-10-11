"""Community modules: which ones get mounted, and which are skipped without breaking the rest."""

import json

import pytest

from api.module_discovery import discover_community_modules

ROUTES = "from fastapi import APIRouter\nrouter = APIRouter()\nother = APIRouter()\n"


def module(base, name, manifest, routes=ROUTES, dirs=()):
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(
        manifest if isinstance(manifest, str) else json.dumps(manifest))
    if routes is not None:
        (folder / "api_routes.py").write_text(routes)
    for d in dirs:
        (folder / d).mkdir()
    return folder


@pytest.fixture
def base(tmp_path):
    return tmp_path / "modules"


def test_a_module_with_a_webui_block_is_found_with_its_defaults(base):
    module(base, "docker", {"id": "docker", "webui": {"api_routes": "api_routes.py"}})
    [found] = discover_community_modules(str(base))
    assert found.id == "docker"
    assert found.route_prefix == "/api/docker"
    assert found.templates_dir is None and found.static_dir is None
    assert found.page_route is None


def test_everything_a_manifest_declares_is_carried_over(base):
    folder = module(base, "nextcloud", {"id": "nextcloud", "webui": {
        "api_routes": "api_routes.py", "router_attr": "other", "route_prefix": "/api/nc",
        "templates_dir": "templates", "static_dir": "static",
        "page_route": "/nextcloud/", "page_template": "nextcloud.html",
    }}, dirs=("templates", "static"))
    [found] = discover_community_modules(str(base))
    assert found.route_prefix == "/api/nc"
    assert found.templates_dir == folder / "templates"
    assert found.static_dir == folder / "static"
    assert (found.page_route, found.page_template) == ("/nextcloud/", "nextcloud.html")


def test_directories_that_do_not_exist_are_left_out(base):
    module(base, "docker", {"id": "docker", "webui": {
        "api_routes": "api_routes.py", "templates_dir": "templates", "static_dir": "static"}})
    [found] = discover_community_modules(str(base))
    assert found.templates_dir is None and found.static_dir is None


@pytest.mark.parametrize("name, manifest, routes", [
    ("storage", {"id": "storage"}, ROUTES),
    ("broken", "{not json", ROUTES),
    ("noroutes", {"id": "noroutes", "webui": {"api_routes": "api_routes.py"}}, None),
    ("norouter", {"id": "norouter", "webui": {"api_routes": "api_routes.py"}}, "x = 1\n"),
    ("_template", {"id": "_template", "webui": {"api_routes": "api_routes.py"}}, ROUTES),
])
def test_what_cannot_be_mounted_is_skipped_and_the_rest_still_is(base, name, manifest, routes):
    module(base, name, manifest, routes)
    module(base, "zz_good", {"id": "good", "webui": {"api_routes": "api_routes.py"}})
    assert [m.id for m in discover_community_modules(str(base))] == ["good"]


def test_no_modules_directory_means_no_modules(tmp_path):
    assert discover_community_modules(str(tmp_path / "missing")) == []


def page(**nav):
    return {"api_routes": "api_routes.py", "static_dir": "static",
            "page_route": "/containers/", "page_template": "containers.html", "nav": nav}


def test_a_page_with_a_nav_block_gets_a_sidebar_entry(base):
    module(base, "containers", {"id": "containers", "webui": page(
        label="Containers", icon="icons.svg#containers", level="advanced")}, dirs=("static",))
    [found] = discover_community_modules(str(base))
    assert found.nav == ("/containers/", "Containers", "/static/modules/containers/icons.svg#containers",
                         "advanced-only")


def test_the_entry_defaults_to_every_level_and_the_generic_icon(base):
    module(base, "containers", {"id": "containers", "webui": page()})
    [found] = discover_community_modules(str(base))
    assert found.nav == ("/containers/", "containers", "#icon-module", "")


def test_an_unknown_level_is_shown_from_advanced(base):
    module(base, "containers", {"id": "containers", "webui": page(level="wizard")})
    [found] = discover_community_modules(str(base))
    assert found.nav.level_class == "advanced-only"


def test_no_page_means_no_sidebar_entry(base):
    module(base, "containers", {"id": "containers", "webui": {
        "api_routes": "api_routes.py", "nav": {"label": "Containers"}}})
    [found] = discover_community_modules(str(base))
    assert found.nav is None
