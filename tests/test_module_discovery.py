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


@pytest.fixture(autouse=True)
def no_failures_left():
    from api import module_discovery
    yield
    module_discovery.failed_modules.clear()


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


@pytest.mark.parametrize("name, manifest, routes, reason", [
    ("syntax", {"id": "syntax", "webui": {"api_routes": "api_routes.py"}}, "def broken(:\n",
     "SyntaxError in"),
    ("imports", {"id": "imports", "webui": {"api_routes": "api_routes.py"}}, "import freedomnas_missing\n",
     "ModuleNotFoundError"),
    ("raises", {"id": "raises", "webui": {"api_routes": "api_routes.py"}}, "raise RuntimeError('boom')\n",
     "RuntimeError: boom"),
    ("noapi", {"id": "noapi", "webui": {"page_route": "/noapi/"}}, ROUTES, "names no api_routes"),
    ("badjson", '{"id": "badjson",', ROUTES, "line 1 column"),
    ("noroutes", {"id": "noroutes", "webui": {"api_routes": "api_routes.py"}}, None, "which is missing"),
    ("norouter", {"id": "norouter", "webui": {"api_routes": "api_routes.py"}}, "router = 1\n", "no APIRouter"),
    ("prefix", {"id": "prefix", "webui": {"api_routes": "api_routes.py", "route_prefix": "api/x"}}, ROUTES,
     "route_prefix"),
])
def test_a_module_that_cannot_be_loaded_is_reported_and_the_rest_still_loads(base, name, manifest, routes, reason):
    from api import module_discovery
    module(base, name, manifest, routes)
    module(base, "zz_good", {"id": "good", "webui": {"api_routes": "api_routes.py"}})
    assert [m.id for m in discover_community_modules(str(base))] == ["good"]
    [failed] = module_discovery.failed_modules
    assert failed.id == name
    assert reason in failed.reason


def test_a_new_discovery_forgets_the_old_failures(base, tmp_path):
    from api import module_discovery
    module(base, "syntax", {"id": "syntax", "webui": {"api_routes": "api_routes.py"}}, "def broken(:\n")
    discover_community_modules(str(base))
    discover_community_modules(str(tmp_path / "empty"))
    assert module_discovery.failed_modules == []


def test_the_webui_starts_with_a_broken_module_and_serves_the_others(base, monkeypatch, engine, config):
    import importlib.util
    from starlette.testclient import TestClient
    from api import module_discovery
    from conftest import ACCOUNTS, REPO, basic
    module(base, "broken", {"id": "broken", "webui": {"api_routes": "api_routes.py"}}, "def broken(:\n")
    module(base, "good", {"id": "good", "webui": {"api_routes": "api_routes.py"}},
           "from fastapi import APIRouter\nrouter = APIRouter()\n\n"
           "@router.get('/ping')\nasync def ping():\n    return {'pong': True}\n")
    monkeypatch.setattr(module_discovery, "_default_modules_dir", lambda: base)
    spec = importlib.util.spec_from_file_location("webui_main_with_broken_module", REPO / "api" / "main.py")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    client = TestClient(fresh.app)
    auth = {"Authorization": f"Basic {basic('admin', ACCOUNTS['admin'][0])}"}
    assert client.get("/api/good/ping", headers=auth).json() == {"pong": True}
    failed = client.get("/api/plugins/failed", headers=auth).json()["failed"]
    assert [m["id"] for m in failed] == ["broken"]
    assert "SyntaxError" in failed[0]["reason"]
    assert client.get("/", headers=auth).status_code == 200
