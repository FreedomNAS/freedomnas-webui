"""
/api/plugins/* — community modules from the package repository.

The package manager never runs: the listings come from the recording
engine, and the job's process is a harmless script recording its argv.
"""

import asyncio

import pytest

from api.routes import plugins
from conftest import wait_for
from freedomnas.engine import CommandTranslationError

SEARCH = ("freedomnas-module-containers\t0.1.0-1\tContainers for FreedomNAS\n"
          "freedomnas-module-photos\t0.2.0-1\tPhotos: albums, faces\tand tabs\n"
          "freedomnas-core\t0.9.12-1\tnot a module\n"
          "garbage line\n")
INSTALLED = "freedomnas-module-containers\t0.1.0-1\tContainers for FreedomNAS\n"


@pytest.fixture(autouse=True)
def idle(monkeypatch):
    monkeypatch.setattr(plugins, "_job", dict(plugins._job, state="idle", kind=None, module=None))
    monkeypatch.setattr(plugins, "LOADED", {"containers"})
    plugins._output.clear()
    monkeypatch.setattr(plugins, "_dropped", 0)


@pytest.fixture
def listings(engine):
    engine.outputs[("packages", "module_search")] = SEARCH
    engine.outputs[("packages", "module_installed")] = INSTALLED
    return engine


@pytest.fixture
def launched(monkeypatch):
    argv = []
    real = asyncio.create_subprocess_exec

    async def fake(*args, **kwargs):
        argv.append(list(args))
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        return await real("sh", "-c", "echo step one; echo step two", **kwargs)

    monkeypatch.setattr(plugins.asyncio, "create_subprocess_exec", fake)
    return argv


def test_the_listing_lines_become_modules():
    found = plugins.parse(SEARCH + "app-nas/freedomnas-module-media\t9999\tMedia\n")
    assert set(found) == {"containers", "photos", "media"}
    assert found["photos"]["description"] == "Photos: albums, faces\tand tabs"
    assert found["media"]["package"] == "app-nas/freedomnas-module-media"


def test_the_page_lists_what_is_offered_and_installed(client, listings):
    body = client.get("/api/plugins/").body
    assert body["supported"] is True
    assert body["modules"] == [
        {"id": "containers", "package": "freedomnas-module-containers",
         "description": "Containers for FreedomNAS", "available": "0.1.0-1",
         "installed": "0.1.0-1", "loaded": True},
        {"id": "photos", "package": "freedomnas-module-photos",
         "description": "Photos: albums, faces\tand tabs", "available": "0.2.0-1",
         "installed": None, "loaded": False},
    ]
    assert body["job"]["state"] == "idle"
    assert [c.action for c in listings.calls if c.category == "packages"] == ["module_search", "module_installed"]


def test_a_module_loaded_but_not_from_a_package_is_listed(client, engine, monkeypatch):
    monkeypatch.setattr(plugins, "LOADED", {"example"})
    modules = client.get("/api/plugins/").body["modules"]
    assert modules == [{"id": "example", "package": None, "description": "", "available": None,
                        "installed": None, "loaded": True}]


def test_a_profile_without_the_commands_is_not_supported(client, engine):
    engine.outputs[("packages", "module_search")] = CommandTranslationError("no such command")
    body = client.get("/api/plugins/").body
    assert body["supported"] is False and body["modules"] == []


def test_installing_runs_the_package_manager_and_keeps_its_output(client, listings, launched):
    r = client.post("/api/plugins/photos/install")
    assert r.status == 200 and r.body["job"]["state"] == "running"
    wait_for(lambda: client.get("/api/plugins/job").body["state"] == "done")
    assert launched == [["apt", "install", "-y", "freedomnas-module-photos"]]
    job = client.get("/api/plugins/job").body
    assert job["kind"] == "install" and job["module"] == "photos" and job["exit_code"] == 0
    assert job["lines"] == ["$ apt install -y freedomnas-module-photos", "step one", "step two"]
    assert client.get(f"/api/plugins/job?since={job['next'] - 1}").body["lines"] == ["step two"]


def test_a_failed_command_fails_the_job(client, listings, monkeypatch):
    real = asyncio.create_subprocess_exec

    async def failing(*args, **kwargs):
        return await real("sh", "-c", "echo 'E: no space'; exit 100", **kwargs)

    monkeypatch.setattr(plugins.asyncio, "create_subprocess_exec", failing)
    client.post("/api/plugins/photos/install")
    wait_for(lambda: client.get("/api/plugins/job").body["state"] == "failed")
    job = client.get("/api/plugins/job").body
    assert job["exit_code"] == 100 and "100" in job["error"]
    assert job["lines"][-1] == "E: no space"


def test_only_modules_the_repository_offers_are_installed(client, listings, launched):
    r = client.post("/api/plugins/nextcloud/install")
    assert r.status == 404 and "no module 'nextcloud'" in r.body["detail"]
    assert client.post("/api/plugins/Bad;name/install").status in (404, 422)
    assert client.post("/api/plugins/-x/install").status == 422
    assert launched == []


def test_removing_takes_the_installed_package(client, listings, launched):
    assert client.delete("/api/plugins/photos").status == 404
    r = client.delete("/api/plugins/containers")
    assert r.status == 200
    wait_for(lambda: client.get("/api/plugins/job").body["state"] == "done")
    assert launched == [["apt", "remove", "-y", "freedomnas-module-containers"]]


def test_one_job_at_a_time_and_never_during_an_update(client, listings, launched, monkeypatch):
    monkeypatch.setitem(plugins._job, "state", "running")
    r = client.post("/api/plugins/photos/install")
    assert r.status == 409
    monkeypatch.setitem(plugins._job, "state", "idle")
    import websocket.terminal as terminal
    monkeypatch.setattr(terminal, "running", 1)
    r = client.post("/api/plugins/refresh")
    assert r.status == 409 and "update is running" in r.body["detail"]
    assert launched == []


def test_refreshing_the_index_is_a_job(client, listings, launched):
    assert client.post("/api/plugins/refresh").status == 200
    wait_for(lambda: client.get("/api/plugins/job").body["state"] == "done")
    assert launched == [["apt", "update"]]


def test_plugins_need_an_administrator(anonymous, engine, config, launched):
    assert anonymous.get("/api/plugins/").status == 401
    assert anonymous.post("/api/plugins/photos/install").status == 401
    assert launched == []
