"""/api/network/* — interfaces and static addresses."""

import json

import pytest


def test_interfaces_come_from_the_profile_command(client, engine):
    links = [{"ifname": "lo"}, {"ifname": "fnas_lan", "address": "aa:bb:cc:dd:ee:ff"}]
    engine.outputs[("network", "list_interfaces")] = json.dumps(links)
    r = client.get("/api/network/interfaces")
    assert r.status == 200
    assert r.body == {"interfaces": links}
    [call] = engine.called("network", "list_interfaces")
    assert call.argv == ["ip", "-j", "addr", "show"]


@pytest.mark.parametrize("missing", ["ip", "cidr", "gateway", "dns"])
def test_a_static_address_needs_every_field(client, engine, missing):
    body = {"ip": "192.168.1.5", "cidr": 24, "gateway": "192.168.1.1", "dns": "1.1.1.1"}
    del body[missing]
    assert client.post("/api/network/interfaces/fnas_lan/ip", body).status == 422
    assert engine.actions() == []


def test_a_static_address_gets_its_own_networkmanager_connection(client, engine, config):
    body = {"ip": "192.168.1.5", "cidr": 24, "gateway": "192.168.1.1",
            "dns": ["1.1.1.1", "9.9.9.9"]}
    r = client.post("/api/network/interfaces/fnas_lan/ip", body)
    assert r.status == 200
    delete, add, up = engine.called("network", "set_static_ip")
    assert delete.command == "nmcli connection delete fnas-fnas_lan || true"
    assert add.argv == [
        "nmcli", "connection", "add", "type", "ethernet", "ifname", "fnas_lan",
        "con-name", "fnas-fnas_lan", "connection.autoconnect-priority", "100",
        "ipv4.method", "manual", "ipv4.addresses", "192.168.1.5/24",
        "ipv4.gateway", "192.168.1.1", "ipv4.dns", "1.1.1.1 9.9.9.9",
    ]
    assert up.argv == ["nmcli", "connection", "up", "fnas-fnas_lan"]
    iface = config.get_interface("fnas_lan")
    assert iface["config"]["method"] == "static"
    assert iface["config"]["dns"] == ["1.1.1.1", "9.9.9.9"]


def test_a_hostile_interface_name_stays_one_word_in_the_shell_step(client, engine):
    body = {"ip": "192.168.1.5", "cidr": 24, "gateway": "192.168.1.1", "dns": []}
    client.post("/api/network/interfaces/x;reboot/ip", body)
    delete = engine.called("network", "set_static_ip")[0]
    assert delete.command == "nmcli connection delete fnas-'x;reboot' || true"


import time

from api.routes import network as network_routes
from conftest import wait_for
from freedomnas.engine import CommandExecutionError

CHECKPOINT = "/org/freedesktop/NetworkManager/Checkpoint/7"
STATIC = {"method": "static", "ip": "192.168.1.5", "cidr": 24, "gateway": "192.168.1.1",
          "dns": "1.1.1.1", "dns_search": "lan", "ipv6_method": "auto", "mtu": 9000}


@pytest.fixture(autouse=True)
def no_pending():
    network_routes._pending.clear()
    yield
    network_routes._pending.clear()


@pytest.fixture
def checkpoint(engine):
    engine.outputs[("network", "checkpoint_create")] = CHECKPOINT + "\n"
    return engine


def _applied(client):
    wait_for(lambda: client.get("/api/network/pending").body.get("state") != "applying")
    return client.get("/api/network/pending").body


def test_a_change_runs_under_a_checkpoint_and_waits(client, checkpoint, config):
    r = client.put("/api/network/interfaces/fnas_lan", STATIC)
    assert r.status == 202
    assert r.body["pending"] is True and r.body["interface"] == "fnas_lan"
    view = _applied(client)
    assert view["state"] == "waiting" and 0 < view["seconds_left"] <= 90
    [create] = checkpoint.called("network", "checkpoint_create")
    assert "CheckpointCreate '[]' 90 3" in create.command
    actions = [a for _, a in checkpoint.actions()]
    assert actions.index("checkpoint_create") < actions.index("ipv4_static") < actions.index("connection_activate")
    [mtu] = checkpoint.called("network", "mtu_set")
    assert mtu.argv[-2:] == ["802-3-ethernet.mtu", "9000"]
    assert config.get_interface("fnas_lan") is None


def test_confirming_keeps_and_saves_it(client, checkpoint, config):
    client.put("/api/network/interfaces/fnas_lan", STATIC)
    _applied(client)
    r = client.post("/api/network/confirm")
    assert r.status == 200
    [destroy] = checkpoint.called("network", "checkpoint_destroy")
    assert destroy.argv[-1] == CHECKPOINT
    saved = config.get_interface("fnas_lan")["config"]
    assert saved["ip"] == "192.168.1.5" and saved["dns_search"] == ["lan"] and saved["mtu"] == 9000
    assert client.get("/api/network/pending").body == {"pending": False}
    assert client.get("/api/network/config").body["interfaces"]["fnas_lan"] == saved


def test_reverting_rolls_back_now(client, checkpoint, config):
    client.put("/api/network/interfaces/fnas_lan", STATIC)
    _applied(client)
    assert client.post("/api/network/revert").status == 200
    [rollback] = checkpoint.called("network", "checkpoint_rollback")
    assert rollback.argv[-1] == CHECKPOINT
    assert config.get_interface("fnas_lan") is None
    assert client.post("/api/network/confirm").status == 409


def test_a_failed_step_rolls_back_and_reports(client, checkpoint, config):
    checkpoint.outputs[("network", "connection_activate")] = CommandExecutionError("no carrier")
    client.put("/api/network/interfaces/fnas_lan", STATIC)
    view = _applied(client)
    assert view["state"] == "failed" and "no carrier" in view["error"]
    assert checkpoint.called("network", "checkpoint_rollback")
    assert client.post("/api/network/confirm").status == 409
    assert client.put("/api/network/interfaces/fnas_lan", STATIC).status == 202


def test_only_one_change_at_a_time(client, checkpoint):
    client.put("/api/network/interfaces/fnas_lan", STATIC)
    _applied(client)
    assert client.put("/api/network/interfaces/fnas_lan", {}).status == 409


def test_an_expired_change_cannot_be_confirmed(client, checkpoint, config):
    client.put("/api/network/interfaces/fnas_lan", STATIC)
    _applied(client)
    network_routes._pending["deadline"] = 0
    r = client.post("/api/network/confirm")
    assert r.status == 409 and "undone" in r.body["detail"]
    assert config.get_interface("fnas_lan") is None


def test_nothing_is_applied_without_a_checkpoint(client, engine):
    r = client.put("/api/network/interfaces/fnas_lan", STATIC)
    assert r.status == 500 and "checkpoint" in r.body["detail"]
    assert [a for _, a in engine.actions()] == ["checkpoint_create"]


@pytest.mark.parametrize("name, body", [
    ("x;reboot", STATIC),
    ("fnas_lan", dict(STATIC, ip="300.1.1.1")),
    ("fnas_lan", dict(STATIC, dns="")),
])
def test_bad_requests_change_nothing(client, checkpoint, name, body):
    assert client.put(f"/api/network/interfaces/{name}", body).status == 422
    assert checkpoint.actions() == []


def test_the_new_address_confirms_with_the_token_alone(client, anonymous, checkpoint, config):
    token = client.put("/api/network/interfaces/fnas_lan", STATIC).body["token"]
    _applied(client)
    assert anonymous.post("/api/network/confirm-token", {"token": "wrong"}).status == 409
    r = anonymous.post("/api/network/confirm-token", {"token": token})
    assert r.status == 200
    assert checkpoint.called("network", "checkpoint_destroy")
    assert config.get_interface("fnas_lan")["config"]["ip"] == "192.168.1.5"
    assert anonymous.post("/api/network/confirm-token", {"token": token}).status == 409


def test_a_token_does_not_confirm_while_applying(anonymous, client, checkpoint):
    network_routes._pending.update(token="t", state="applying", checkpoint=CHECKPOINT,
                                   interface="fnas_lan", config={}, deadline=time.monotonic() + 60)
    r = anonymous.post("/api/network/confirm-token", {"token": "t"})
    assert r.status == 409 and "still being applied" in r.body["detail"]
    assert not checkpoint.called("network", "checkpoint_destroy")


PHYSICAL = "fnas_lan1 52:54:00:00:00:0a fnas_lan1\nfnas_lan2 52:54:00:00:00:0b fnas_lan2\n"


@pytest.fixture
def cards(checkpoint):
    checkpoint.outputs[("network", "list_physical")] = PHYSICAL
    checkpoint.outputs[("network", "list_interfaces")] = json.dumps([{"ifname": "lo"}, {"ifname": "fnas_lan1"},
                                                                     {"ifname": "fnas_lan2"}])
    return checkpoint


def test_a_bridge_is_created_and_saved_with_its_ports_on_confirm(client, cards, config):
    config.add_interface({"logical_name": "fnas_lan1", "mac_address": "52:54:00:00:00:0a",
                          "config": {"method": "dhcp"}})
    r = client.post("/api/network/virtual", {"type": "bridge", "ports": ["fnas_lan1"],
                                             "config": {"method": "dhcp"}})
    assert r.status == 202 and r.body["entry"]["logical_name"] == "fnas_bridge1"
    _applied(client)
    [create] = cards.called("network", "bridge_create")
    assert create.argv[:4] == ["nmcli", "connection", "add", "type"] and "fnas_bridge1" in create.argv
    [mac] = cards.called("network", "mac_set")
    assert mac.argv[-1] == "52:54:00:00:00:0a"
    assert config.get_interface("fnas_bridge1") is None
    assert client.post("/api/network/confirm").status == 200
    assert config.get_interface("fnas_bridge1")["ports"] == ["fnas_lan1"]
    port = config.get_interface("fnas_lan1")
    assert port["master"] == "fnas_bridge1" and "config" not in port
    assert client.put("/api/network/interfaces/fnas_lan1", STATIC).status == 409


def test_a_bad_bridge_is_refused(client, cards):
    r = client.post("/api/network/virtual", {"type": "bridge", "ports": ["fnas_lan7"]})
    assert r.status == 422 and "No interface" in r.body["detail"]
    assert not cards.called("network", "checkpoint_create")


def test_removing_a_bridge_frees_its_ports(client, cards, config):
    config.add_interface({"logical_name": "fnas_bridge1", "type": "bridge", "ports": ["fnas_lan1"],
                          "config": {"method": "dhcp"}})
    config.add_interface({"logical_name": "fnas_lan1", "master": "fnas_bridge1"})
    assert client.delete("/api/network/virtual/fnas_bridge1").status == 202
    _applied(client)
    assert [a for _, a in cards.actions()] == ["checkpoint_create", "virtual_delete", "port_release"]
    assert client.post("/api/network/confirm").status == 200
    assert config.get_interface("fnas_bridge1") is None
    assert "master" not in config.get_interface("fnas_lan1")


def test_a_bridge_in_use_cannot_be_removed(client, cards, config):
    config.add_interface({"logical_name": "fnas_bond1", "type": "bond", "ports": ["fnas_lan1", "fnas_lan2"]})
    config.add_interface({"logical_name": "fnas_vlan5", "type": "vlan", "parent": "fnas_bond1", "vlan_id": 5})
    r = client.delete("/api/network/virtual/fnas_bond1")
    assert r.status == 409 and "fnas_vlan5" in r.body["detail"]
    assert client.delete("/api/network/virtual/fnas_lan1").status == 404


def test_configuring_a_bridge_keeps_its_ports_and_does_not_recreate_it(client, cards, config):
    config.add_interface({"logical_name": "fnas_bridge1", "type": "bridge", "ports": ["fnas_lan1"],
                          "mac_address": "52:54:00:00:00:0a", "config": {"method": "dhcp"}})
    assert client.put("/api/network/interfaces/fnas_bridge1", STATIC).status == 202
    _applied(client)
    actions = [a for _, a in cards.actions()]
    assert "bridge_create" not in actions and "connection_activate" not in actions
    assert actions[-3:] == ["mac_set", "port_activate", "virtual_activate"]
    assert "bridge_port_add" in actions
    client.post("/api/network/confirm")
    assert config.get_interface("fnas_bridge1")["ports"] == ["fnas_lan1"]
    assert config.get_interface("fnas_bridge1")["config"]["ip"] == "192.168.1.5"
