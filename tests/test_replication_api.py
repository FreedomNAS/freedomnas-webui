"""/api/storage/replication/* — local send/receive jobs."""

import time

import pytest

from freedomnas import replication


@pytest.fixture
def setup(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(replication, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(replication.pool_import, "is_system_path",
                        lambda p: p == "/etc" or p.startswith("/etc/"))

    def at_path(path, **_):
        return "rpool/ROOT/default" if path == "/" else (path if path in ("tank", "backup") else "")
    engine.outputs[("zfs", "dataset_at_path")] = at_path
    data = tmp_path / "data"
    data.mkdir()
    return data


def finished(client, job_id):
    for _ in range(100):
        job = next(j for j in client.get("/api/storage/replication/jobs").body["jobs"] if j["id"] == job_id)
        if job["state"] != "running":
            return job
        time.sleep(0.02)
    return job


def test_replicate_builds_the_pipeline(client, engine, setup):
    r = client.post("/api/storage/replication/replicate", {
        "snapshot": "tank/a@s2", "from": "tank/a@s1", "intermediates": True,
        "replicate": True, "raw": True, "target": "backup/a", "force": True})
    assert r.status == 200, r.body
    assert finished(client, r.body["id"])["state"] == "done"
    [call] = engine.called("zfs", "replicate")
    assert "zfs send -vP -R -w -I tank/a@s1 tank/a@s2 2>" in call.command
    assert call.command.endswith("| zfs receive -F -u -s -x mountpoint backup/a")
    assert call.command.startswith("echo $$ > ")


@pytest.mark.parametrize("body", [
    {"snapshot": "tank/a", "target": "backup/a"},
    {"snapshot": "tank/a@s1", "target": "rpool/x"},
    {"snapshot": "tank/a@s1", "target": "-F"},
    {"snapshot": "tank/a@s1", "target": "backup/a", "from": "other/b@s0"},
    {"snapshot": "tank/a@s1", "target": "backup/a", "raw": "yes"},
])
def test_replicate_refuses(client, engine, setup, body):
    assert client.post("/api/storage/replication/replicate", body).status == 422
    assert not engine.called("zfs", "replicate")


def test_send_and_receive_files(client, engine, setup):
    out = setup / "a.zfs"
    r = client.post("/api/storage/replication/send-to-file", {"snapshot": "tank/a@s1", "file": str(out)})
    assert r.status == 200, r.body
    finished(client, r.body["id"])
    [call] = engine.called("zfs", "send_to_file")
    assert "set -C;" in call.command and call.command.endswith(f"> {out}")
    out.write_text("x")
    assert client.post("/api/storage/replication/send-to-file",
                       {"snapshot": "tank/a@s1", "file": str(out)}).status == 422
    assert client.post("/api/storage/replication/send-to-file",
                       {"snapshot": "tank/a@s1", "file": "/etc/a.zfs"}).status == 422
    r = client.post("/api/storage/replication/receive-from-file", {"file": str(out), "target": "backup/b"})
    assert r.status == 200, r.body
    finished(client, r.body["id"])
    [call] = engine.called("zfs", "receive_from_file")
    assert call.command.endswith(f"zfs receive -u -x mountpoint backup/b < {out}")


def test_estimate(client, engine, setup):
    engine.outputs[("zfs", "send_estimate")] = "full\ttank/a@s1\t4096\nsize\t4096\n"
    assert client.post("/api/storage/replication/estimate", {"snapshot": "tank/a@s1"}).body == {"size": 4096}


def test_resume_takes_the_token_from_zfs(client, engine, setup):
    engine.outputs[("zfs", "resume_tokens")] = "tank/a\t-\nbackup/a\t1-abc-123\n"
    assert client.get("/api/storage/replication/resumable").body == {"datasets": ["backup/a"]}
    r = client.post("/api/storage/replication/resume", {"target": "backup/a"})
    assert r.status == 200, r.body
    finished(client, r.body["id"])
    [call] = engine.called("zfs", "replicate_resume")
    assert "zfs send -vP -t 1-abc-123" in call.command
    assert client.post("/api/storage/replication/resume", {"target": "tank/a"}).status == 404
    assert client.post("/api/storage/replication/abort", {"target": "backup/a"}).status == 200
    assert [c.command for c in engine.called("zfs", "receive_abort")] == ["zfs receive -A backup/a"]


def test_cancel_unknown_or_finished(client, engine, setup):
    assert client.post("/api/storage/replication/jobs/999/cancel", {}).status == 404
    r = client.post("/api/storage/replication/replicate", {"snapshot": "tank/a@s1", "target": "backup/c"})
    finished(client, r.body["id"])
    assert client.post(f"/api/storage/replication/jobs/{r.body['id']}/cancel", {}).status == 409
    assert client.post("/api/storage/replication/frobnicate", {}).status == 404
