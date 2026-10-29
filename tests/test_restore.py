"""
While the restore lock exists the WebUI serves only the restore page and its
two open calls; a finished restore with errors is left with Continue.
"""

import pytest

from freedomnas import restoring
from api import restart
from api.routes import restore


@pytest.fixture
def restarts(monkeypatch):
    asked = []
    monkeypatch.setattr(restart, "request_restart", lambda engine, delay=0.5: asked.append(delay))
    monkeypatch.setattr(restore, "_started_restoring", None)
    monkeypatch.setattr(restore, "_restarting", False)
    return asked


@pytest.fixture
def lock(monkeypatch, tmp_path, restarts):
    path = tmp_path / "state" / restoring.LOCK_NAME
    monkeypatch.setattr(restoring, "lock_file", lambda engine=None: path)
    return path


def test_without_a_lock_nothing_changes(anonymous, client, location, lock):
    assert anonymous.get("/api/restore/progress").body == {"active": False}
    assert location("/") == (307, "/dashboard/")
    assert client.get("/api/system/status").status == 200


def test_while_restoring_every_page_goes_to_the_restore_page(anonymous, client, location, lock):
    restoring.begin(lock, "restored")
    assert location("/dashboard/") == (303, "/restoring/")
    assert location("/auth/") == (303, "/restoring/")
    page = anonymous.get("/restoring/")
    assert page.status == 200 and "Restore in progress" in page.body
    assert anonymous.get("/static/js/restoring.js").status == 200
    busy = client.get("/api/system/status")
    assert busy.status == 503 and busy.body["restoring"] is True


def test_the_progress_is_open_without_signing_in(anonymous, lock):
    restoring.begin(lock, "reinitialized")
    progress = restoring.Progress(lock)
    progress.plan(["Hostname", "Module containers"])
    progress.done(0, [])
    progress.start(1)
    body = anonymous.get("/api/restore/progress").body
    assert body["active"] and body["reason"] == "reinitialized"
    assert [s["state"] for s in body["steps"]] == ["done", "running"]


def test_continue_waits_for_the_restore_to_finish(anonymous, lock):
    restoring.begin(lock, "restored")
    assert anonymous.post("/api/restore/continue").status == 409
    assert lock.exists()


def test_continue_after_a_failed_step_restarts_the_webui_into_the_normal_one(anonymous, location, lock,
                                                                             config, restarts):
    restoring.begin(lock, "restored")
    restoring.Progress(lock).finish(["containers.runtime: Docker could not be installed"])
    body = anonymous.get("/api/restore/progress").body
    assert body["finished"] and body["failed"] and body["errors"] == ["containers.runtime: Docker could not be installed"]
    assert anonymous.post("/api/restore/continue").body == {"active": False}
    assert not lock.exists()
    assert anonymous.get("/api/restore/progress").body == {"active": True, "restarting": True}
    assert location("/") == (303, "/restoring/")
    assert restarts == [2.0]


def test_a_webui_started_without_the_lock_never_restarts(anonymous, location, lock, config, restarts):
    assert anonymous.get("/api/restore/progress").body == {"active": False}
    assert location("/") == (307, "/dashboard/")
    assert restarts == []
