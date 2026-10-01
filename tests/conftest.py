"""
The WebUI, served for real by uvicorn, in front of a recording engine and a fake PAM.

The engine is the real one, loaded with the real Debian profile from the
sibling freedomnas-profiles checkout: every action a route asks for is
translated into its actual command — template, placeholders, quoting — and
recorded instead of run. A route asking for an action the profile lacks, or
forgetting a placeholder, fails here the way it would on an appliance.
Paths the routes open themselves (smb.conf, /etc/exports, topgrade.toml,
the keyslots) are redirected into the test's temporary directory.

PAM is replaced unconditionally — even where python-pam is installed — so no
test ever authenticates against the host's real accounts. The configuration
is a real ConfigurationManager writing into the temporary directory.

freedomnas-core is taken from the sibling checkout, never from an installed
copy, the same way freedomnas-core's own tests do it.
"""

import asyncio
import base64
import importlib.util
import json
import os
import shlex
import socket
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO.parent / "freedomnas-core"
PROFILES = REPO.parent / "freedomnas-profiles"

for name in [m for m in sys.modules if m == "freedomnas" or m.startswith("freedomnas.")]:
    del sys.modules[name]
_spec = importlib.util.spec_from_file_location(
    "freedomnas", CORE / "__init__.py", submodule_search_locations=[str(CORE)]
)
_freedomnas = importlib.util.module_from_spec(_spec)
sys.modules["freedomnas"] = _freedomnas
_spec.loader.exec_module(_freedomnas)

ACCOUNTS = {
    "admin": ("admin-password", ["admin", "sudo"]),
    "mario": ("mario-password", ["mario", "users"]),
}


class FakePam:
    def authenticate(self, username, password):
        return username in ACCOUNTS and ACCOUNTS[username][0] == password


sys.modules["pam"] = types.SimpleNamespace(pam=FakePam)
sys.path.insert(0, str(REPO))

# api/main.py mounts frontend/templates and frontend/static by relative path.
os.chdir(REPO)

import uvicorn  # noqa: E402

import api.deps as deps  # noqa: E402
import api.main as main  # noqa: E402
import api.routes.updates as updates_routes  # noqa: E402
from freedomnas import keymgmt  # noqa: E402
from freedomnas.config import ConfigurationManager  # noqa: E402
from freedomnas.engine import CommandExecutionError, FreedomNASEngine  # noqa: E402


@dataclass
class Call:
    category: str
    action: str
    kwargs: dict
    command: str
    stdin: str = None

    @property
    def argv(self):
        return shlex.split(self.command)


class RecordingEngine:
    """
    The real Debian engine, with execution replaced by a recording.

    `outputs[(category, action)]` is what the command "prints": a string, a
    callable taking the command's kwargs, or an exception to raise.
    """

    def __init__(self, paths):
        self.real = FreedomNASEngine.for_target("debian", profile_dir=str(PROFILES))
        self.paths = paths
        self.calls = []
        self.outputs = {("users", "list_groups"): self._groups_of,
                        ("users", "get_user_info"): self._passwd_entry}

    @staticmethod
    def _groups_of(username, **_):
        return " ".join(ACCOUNTS.get(username, ("", []))[1])

    @staticmethod
    def _passwd_entry(username, **_):
        if username not in ACCOUNTS:
            return CommandExecutionError(f"no user {username}")
        return f"{username}:x:1000:1000::/home/{username}:/bin/bash"

    def __getattr__(self, name):
        return getattr(self.real, name)

    def get_profile_value(self, category, key):
        if (category, key) in self.paths:
            return str(self.paths[(category, key)])
        return self.real.get_profile_value(category, key)

    def _answer(self, category, action, kwargs):
        out = self.outputs.get((category, action), "")
        if isinstance(out, Exception):
            raise out
        if callable(out):
            out = out(**kwargs)
            if isinstance(out, Exception):
                raise out
        return out, ""

    def execute(self, category, action, timeout=30, check=True, **kwargs):
        command = self.real.get_command(category, action, **kwargs)
        for one in command if isinstance(command, list) else [command]:
            self.calls.append(Call(category, action, kwargs, one))
        return self._answer(category, action, kwargs)

    def execute_with_stdin(self, category, action, stdin_data, timeout=30, **kwargs):
        command = self.real.get_command(category, action, **kwargs)
        self.calls.append(Call(category, action, kwargs, command, stdin=stdin_data))
        return self._answer(category, action, kwargs)

    def actions(self):
        """What was asked of the engine, minus the group lookup authentication makes."""
        return [(c.category, c.action) for c in self.calls
                if (c.category, c.action) != ("users", "list_groups")]

    def called(self, category, action):
        return [c for c in self.calls if (c.category, c.action) == (category, action)]


@pytest.fixture
def files(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    return {
        ("samba", "config_file"): root / "smb.conf",
        ("samba", "usershare_path"): root / "usershares",
        ("nfs", "config_file"): root / "exports",
        ("updates", "topgrade_config"): root / "freedomnas" / "topgrade.toml",
    }


@pytest.fixture
def engine(monkeypatch, files, tmp_path):
    recording = RecordingEngine(files)
    monkeypatch.setattr(deps, "_get_engine", lambda: recording)
    monkeypatch.setattr(keymgmt, "KEYSLOT_DIR", tmp_path / "keyslots")
    monkeypatch.setattr(keymgmt, "RUNTIME_KEY_DIR", tmp_path / "run")
    monkeypatch.setattr(keymgmt, "USB_MOUNTPOINT", tmp_path / "run" / "usbkey")
    return recording


@pytest.fixture
def config(monkeypatch, tmp_path):
    manager = ConfigurationManager(str(tmp_path / "config.yaml"))
    manager.mark_initialized(edition="debian", initialized_by="tests")
    monkeypatch.setattr(deps, "_config", manager)
    return manager


@pytest.fixture
def fresh_config(monkeypatch, tmp_path):
    """A configuration the setup wizard has not been through yet."""
    (tmp_path / "state").mkdir()
    manager = ConfigurationManager(str(tmp_path / "state" / "config.yaml"))
    monkeypatch.setattr(deps, "_config", manager)
    return manager


@pytest.fixture(autouse=True)
def setup_pending_file(monkeypatch, tmp_path):
    """Where an image's setup-pending file would be; absent unless a test makes it."""
    path = tmp_path / "setup-pending"
    monkeypatch.setattr(ConfigurationManager, "SETUP_PENDING_PATH", path)
    return path


@pytest.fixture(autouse=True)
def clean_module_state():
    deps._failures.clear()
    deps._locked_until.clear()
    updates_routes._pending.update(count=None, checked_at=None, error=None)
    updates_routes._refresh = None
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
    yield f"127.0.0.1:{port}"
    served.should_exit = True
    thread.join(timeout=5)


def basic(username, password):
    return base64.b64encode(f"{username}:{password}".encode()).decode()


@dataclass
class Response:
    status: int
    body: object
    headers: dict


class Client:
    def __init__(self, host, auth):
        self.host = host
        self.auth = auth

    def request(self, method, path, body=None, auth=..., raw=None):
        auth = self.auth if auth is ... else auth
        headers = {}
        if auth is not None:
            headers["Authorization"] = f"Basic {basic(*auth)}"
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"http://{self.host}{path}", data=data,
                                     headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as err:
            resp = err
        with resp:
            content = resp.read()
            kind = resp.headers.get("Content-Type", "")
            parsed = json.loads(content) if "json" in kind and content else content.decode()
            return Response(resp.status, parsed, {k.lower(): v for k, v in resp.headers.items()})

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body, **kw)

    def put(self, path, body=None, **kw):
        return self.request("PUT", path, body, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@pytest.fixture
def client(server, engine, config):
    return Client(server, ("admin", ACCOUNTS["admin"][0]))


@pytest.fixture
def anonymous(server, engine):
    return Client(server, None)


@pytest.fixture
def location(server):
    """Where a page redirects to, without following it."""
    opener = urllib.request.build_opener(_NoRedirect)

    def fetch(path):
        try:
            resp = opener.open(f"http://{server}{path}", timeout=10)
        except urllib.error.HTTPError as err:
            return err.code, err.headers.get("Location")
        return resp.status, resp.headers.get("Location")
    return fetch


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.02)


def run(coroutine):
    return asyncio.run(coroutine)
