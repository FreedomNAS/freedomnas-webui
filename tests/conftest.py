"""
The WebUI, served for real by uvicorn, in front of a fake engine and a fake PAM.

Nothing here reaches the machine running the tests: every command goes to a
MagicMock engine, and PAM is replaced unconditionally — even where
python-pam is installed — so no test ever authenticates against the host's
real accounts. The HTTP layer, the routing, the dependencies and the
authentication logic in api/deps.py are the real ones.

freedomnas-core is taken from the sibling checkout, never from an installed
copy, the same way freedomnas-core's own tests do it.
"""

import base64
import importlib.util
import json
import socket
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO.parent / "freedomnas-core"

for name in [m for m in sys.modules if m == "freedomnas" or m.startswith("freedomnas.")]:
    del sys.modules[name]
_spec = importlib.util.spec_from_file_location(
    "freedomnas", CORE / "__init__.py", submodule_search_locations=[str(CORE)]
)
_freedomnas = importlib.util.module_from_spec(_spec)
sys.modules["freedomnas"] = _freedomnas
_spec.loader.exec_module(_freedomnas)

ACCOUNTS = {
    "admin": ("admin-password", ["admin", "wheel"]),
    "mario": ("mario-password", ["mario", "users"]),
}


class FakePam:
    def authenticate(self, username, password):
        return username in ACCOUNTS and ACCOUNTS[username][0] == password


sys.modules["pam"] = types.SimpleNamespace(pam=FakePam)
sys.path.insert(0, str(REPO))

import uvicorn  # noqa: E402

import api.deps as deps  # noqa: E402
import api.main as main  # noqa: E402


def _profile_value(category, key):
    return {
        ("users", "sudo_group"): "wheel",
        ("users", "password_stdin_format"): "{username}:{password}\n",
    }[(category, key)]


def _execute(category, action, **kwargs):
    if (category, action) == ("users", "list_groups"):
        return " ".join(ACCOUNTS.get(kwargs["username"], ("", []))[1]), ""
    return "", ""


@pytest.fixture
def engine(monkeypatch):
    fake = MagicMock()
    fake.get_profile_value.side_effect = _profile_value
    fake.execute.side_effect = _execute
    fake.execute_with_stdin.return_value = ("", "")
    monkeypatch.setattr(deps, "_get_engine", lambda: fake)
    return fake


@pytest.fixture
def config(monkeypatch):
    fake = MagicMock()
    fake.is_initialized.return_value = True
    fake.get_users.return_value = []
    monkeypatch.setattr(deps, "_config", fake)
    return fake


@pytest.fixture(autouse=True)
def no_lockouts():
    deps._failures.clear()
    deps._locked_until.clear()
    yield
    deps._failures.clear()
    deps._locked_until.clear()


@pytest.fixture(scope="session")
def server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    served = uvicorn.Server(uvicorn.Config(main.app, lifespan="off", log_level="warning"))
    thread = threading.Thread(target=served.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not served.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    served.should_exit = True
    thread.join(timeout=5)


class Client:
    def __init__(self, base, auth):
        self.base = base
        self.auth = auth

    def request(self, method, path, body=None, auth=...):
        auth = self.auth if auth is ... else auth
        headers = {"Content-Type": "application/json"}
        if auth is not None:
            token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"null")

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body, **kw):
        return self.request("POST", path, body, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


@pytest.fixture
def client(server, engine, config):
    return Client(server, ("admin", ACCOUNTS["admin"][0]))
