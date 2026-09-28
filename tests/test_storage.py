"""/api/storage/* — pools, datasets, snapshots, properties and pool devices."""

import json
from pathlib import Path

import pytest

from freedomnas.engine import CommandExecutionError


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------

def test_pools_merge_what_zfs_says_with_what_the_config_recorded(client, engine, config):
    engine.outputs[("zfs", "pool_list")] = (
        "tank\t3958241161216\t1979120580608\t1979120580608\t50\tONLINE\n"
        "backup\t1979120580608\t98956029030\t1880164551578\t5\tDEGRADED\n"
    )
    config.add_pool({"name": "tank", "vdev_type": "mirror", "cache_devices": ["/dev/nvme0n1"]})
    r = client.get("/api/storage/pools")
    assert r.status == 200
    assert r.body == {"pools": [
        {"name": "tank", "size": 3958241161216, "used": 1979120580608,
         "free": 1979120580608, "used_pct": 50, "health": "ONLINE",
         "cache_devices": ["/dev/nvme0n1"], "vdev_type": "mirror"},
        {"name": "backup", "size": 1979120580608, "used": 98956029030,
         "free": 1880164551578, "used_pct": 5, "health": "DEGRADED"},
    ]}


def test_a_profile_that_prints_only_name_and_size_still_works(client, engine):
    """The page shows what it was given: '3.6T' is already readable."""
    engine.outputs[("zfs", "pool_list")] = "tank\t3.6T\n"
    r = client.get("/api/storage/pools")
    assert r.body == {"pools": [{"name": "tank", "size": "3.6T"}]}


def test_datasets_are_parsed_and_short_lines_skipped(client, engine):
    engine.outputs[("zfs", "dataset_list")] = (
        "tank\t1G\t3T\t96K\t/tank\n"
        "tank/home\t500M\t3T\t500M\t/tank/home\n"
        "broken line\n"
    )
    r = client.get("/api/storage/datasets")
    assert [d["name"] for d in r.body["datasets"]] == ["tank", "tank/home"]
    assert r.body["datasets"][1] == {
        "name": "tank/home", "used": "500M", "avail": "3T", "refer": "500M",
        "mountpoint": "/tank/home", "protected": None,
    }


def test_snapshots_are_listed(client, engine):
    engine.outputs[("zfs", "snapshot_list")] = "tank@a\tMon Sep 22 2026\t0B\n"
    r = client.get("/api/storage/snapshots")
    assert r.body == {"snapshots": [{"name": "tank@a", "creation": "Mon Sep 22 2026", "used": "0B"}]}


def test_a_failing_listing_is_a_500(client, engine):
    engine.outputs[("zfs", "pool_list")] = CommandExecutionError("zfs module not loaded")
    r = client.get("/api/storage/pools")
    assert r.status == 500
    assert "zfs module" in r.body["detail"]


# ---------------------------------------------------------------------------
# Snapshots and their policies
# ---------------------------------------------------------------------------

def test_a_snapshot_is_taken_and_recorded(client, engine, config):
    r = client.post("/api/storage/snapshots", {"dataset": "tank/home", "snap_name": "before"})
    assert r.status == 200
    assert r.body == {"snapshot": "tank/home@before"}
    [call] = engine.called("zfs", "snapshot_create")
    assert call.argv == ["zfs", "snapshot", "tank/home@before"]
    assert any(s.get("name") == "tank/home@before" for s in config.get_root_snapshots())


def test_a_hostile_snapshot_name_stays_part_of_one_argument(client, engine):
    client.post("/api/storage/snapshots", {"dataset": "tank", "snap_name": "x -r other"})
    [call] = engine.called("zfs", "snapshot_create")
    assert call.argv == ["zfs", "snapshot", "tank@x -r other"]


@pytest.mark.parametrize("body", [{"dataset": "tank"}, {"snap_name": "a"}, {}])
def test_a_snapshot_needs_dataset_and_name(client, engine, body):
    assert client.post("/api/storage/snapshots", body).status == 422
    assert engine.actions() == []


def test_a_snapshot_policy_is_set_listed_and_removed(client, engine, config):
    r = client.put("/api/storage/snapshot-policies/tank/home", {"hourly": 24, "daily": "7"})
    assert r.status == 200
    assert r.body["dataset"] == "tank/home"
    assert r.body["hourly"] == 24 and r.body["daily"] == 7
    listed = client.get("/api/storage/snapshot-policies").body["policies"]
    assert [p["dataset"] for p in listed] == ["tank/home"]
    assert client.delete("/api/storage/snapshot-policies/tank/home").body == {"deleted": "tank/home"}
    assert config.get_snapshot_policies() == []
    assert engine.actions() == []


def test_a_policy_count_must_be_a_number(client):
    r = client.put("/api/storage/snapshot-policies/tank", {"hourly": "lots"})
    assert r.status == 422
    assert "hourly" in r.body["detail"]


def test_a_policy_that_keeps_nothing_is_refused(client):
    r = client.put("/api/storage/snapshot-policies/tank", {"hourly": 0})
    assert r.status == 422
    assert "remove the policy" in r.body["detail"]


def test_removing_a_policy_that_does_not_exist_is_404(client):
    assert client.delete("/api/storage/snapshot-policies/tank").status == 404


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

def test_the_property_catalogue_comes_from_the_profile(client, engine):
    r = client.get("/api/storage/properties")
    assert r.body["properties"] == engine.get_profile_section("zfs_properties")
    assert "compression" in r.body["properties"]


def test_dataset_properties_are_filtered_to_the_catalogue(client, engine):
    engine.outputs[("zfs", "dataset_get")] = (
        "compression\tlz4\tlocal\n"
        "guid\t123456\t-\n"
        "atime\toff\tinherited from tank\n"
    )
    r = client.get("/api/storage/datasets/tank/home/properties")
    assert r.status == 200
    assert r.body == {"dataset": "tank/home", "properties": {
        "compression": {"value": "lz4", "source": "local"},
        "atime": {"value": "off", "source": "inherited from tank"},
    }}
    [call] = engine.called("zfs", "dataset_get")
    assert call.argv[-1] == "tank/home"


def test_properties_of_a_missing_dataset_are_404(client, engine):
    engine.outputs[("zfs", "dataset_get")] = CommandExecutionError("dataset does not exist")
    assert client.get("/api/storage/datasets/tank/nope/properties").status == 404


def test_properties_are_set_one_command_each(client, engine):
    r = client.put("/api/storage/datasets/tank/home/properties", {"atime": "off", "quota": "100G"})
    assert r.status == 200
    assert r.body == {"dataset": "tank/home", "applied": ["atime", "quota"], "failed": []}
    assert [c.argv for c in engine.called("zfs", "dataset_set")] == [
        ["zfs", "set", "atime=off", "tank/home"],
        ["zfs", "set", "quota=100G", "tank/home"],
    ]


@pytest.mark.parametrize("body, message", [
    ({}, "Nothing to set"),
    ({"nonsense": "on"}, "Unknown property"),
    ({"atime": "sometimes"}, "atime accepts"),
    ({"quota": "lots"}, "wants a size"),
    ({"casesensitivity": "mixed"}, "only be set when the dataset is created"),
])
def test_invalid_properties_are_refused_before_zfs_is_asked(client, engine, body, message):
    r = client.put("/api/storage/datasets/tank/properties", body)
    assert r.status == 422
    assert message in r.body["detail"]
    assert engine.actions() == []


def test_a_partly_failed_update_is_a_207(client, engine):
    def refuse_quota(property, **_):
        if property == "quota":
            raise CommandExecutionError("quota below used space")
        return ""
    engine.outputs[("zfs", "dataset_set")] = refuse_quota
    r = client.put("/api/storage/datasets/tank/properties", {"atime": "off", "quota": "1K"})
    assert r.status == 207
    assert r.body["applied"] == ["atime"]
    assert r.body["failed"][0]["property"] == "quota"


# ---------------------------------------------------------------------------
# Block devices
# ---------------------------------------------------------------------------

def test_devices_are_parsed_from_lsblk_and_marked_in_use(client, engine):
    engine.outputs[("media", "list_devices")] = (
        'NAME="/dev/sda" SIZE="4000787030016" TYPE="disk" FSTYPE="" MOUNTPOINT="" MODEL="WDC WD40"\n'
        'NAME="/dev/sdb1" SIZE="536870912" TYPE="part" FSTYPE="vfat" MOUNTPOINT="/boot/efi" MODEL=""\n'
    )
    r = client.get("/api/storage/devices")
    assert r.body == {"devices": [
        {"name": "/dev/sda", "parent": "", "size": 4000787030016, "type": "disk", "fstype": "",
         "mountpoint": "", "model": "WDC WD40", "in_use": False},
        {"name": "/dev/sdb1", "parent": "", "size": 536870912, "type": "part", "fstype": "vfat",
         "mountpoint": "/boot/efi", "model": "", "in_use": True},
    ]}


def test_a_disk_whose_partitions_are_in_use_is_in_use(client, engine):
    engine.outputs[("media", "list_devices")] = (
        'NAME="/dev/sda" PKNAME="" SIZE="21474836480" TYPE="disk" FSTYPE="" MOUNTPOINT="" MODEL="QEMU"\n'
        'NAME="/dev/sda1" PKNAME="/dev/sda" SIZE="536870912" TYPE="part" FSTYPE="vfat" MOUNTPOINT="/boot/efi" MODEL=""\n'
        'NAME="/dev/sda2" PKNAME="/dev/sda" SIZE="20000000000" TYPE="part" FSTYPE="zfs_member" MOUNTPOINT="" MODEL=""\n'
        'NAME="/dev/sdb" PKNAME="" SIZE="4294967296" TYPE="disk" FSTYPE="" MOUNTPOINT="" MODEL="QEMU"\n'
    )
    devices = {d["name"]: d["in_use"] for d in client.get("/api/storage/devices").body["devices"]}
    assert devices == {"/dev/sda": True, "/dev/sda1": True, "/dev/sda2": True, "/dev/sdb": False}


# ---------------------------------------------------------------------------
# Cache and log devices
# ---------------------------------------------------------------------------

def test_cache_devices_are_attached_in_one_command_and_recorded(client, engine, config):
    config.add_pool({"name": "tank", "cache_devices": []})
    r = client.post("/api/storage/pools/tank/devices",
                    {"role": "cache", "devices": ["/dev/nvme0n1", "/dev/nvme1n1"]})
    assert r.status == 200
    assert r.body == {"pool": "tank", "role": "cache", "attached": ["/dev/nvme0n1", "/dev/nvme1n1"]}
    [call] = engine.called("zfs", "pool_add_cache")
    assert call.argv == ["zpool", "add", "tank", "cache", "/dev/nvme0n1", "/dev/nvme1n1"]
    assert config.get_pool("tank")["cache_devices"] == ["/dev/nvme0n1", "/dev/nvme1n1"]


@pytest.mark.parametrize("body, message", [
    ({"role": "spare", "devices": ["/dev/sdc"]}, "role must be one of"),
    ({"role": "log", "devices": []}, "must not be empty"),
    ({"role": "log", "devices": ["-f", "/dev/sdc"]}, "not a device path"),
    ({"role": "cache", "devices": ["/dev/sdc -f"]}, "not a device path"),
])
def test_attaching_needs_a_known_role_and_devices(client, engine, body, message):
    r = client.post("/api/storage/pools/tank/devices", body)
    assert r.status == 422
    assert message in r.body["detail"]
    assert engine.actions() == []


def test_a_device_zfs_refuses_is_a_500(client, engine, config):
    engine.outputs[("zfs", "pool_add_log")] = CommandExecutionError("device is busy")
    r = client.post("/api/storage/pools/tank/devices", {"role": "log", "devices": ["/dev/sdc"]})
    assert r.status == 500
    assert "busy" in r.body["detail"]


def test_a_device_is_detached_by_its_full_path(client, engine, config):
    config.add_pool({"name": "tank", "cache_devices": ["/dev/disk/by-id/nvme-A", "/dev/sdz"]})
    r = client.delete("/api/storage/pools/tank/devices?device=/dev/disk/by-id/nvme-A")
    assert r.status == 200
    [call] = engine.called("zfs", "pool_remove_device")
    assert call.argv == ["zpool", "remove", "tank", "/dev/disk/by-id/nvme-A"]
    assert config.get_pool("tank")["cache_devices"] == ["/dev/sdz"]


# ---------------------------------------------------------------------------
# Creating a pool
# ---------------------------------------------------------------------------

def test_a_pool_is_created_with_each_device_its_own_argument(client, engine, config):
    r = client.post("/api/storage/pools", {
        "name": "tank", "vdev_type": "mirror",
        "devices": ["/dev/sda", "/dev/sdb"], "cache_devices": ["/dev/nvme0n1"],
    })
    assert r.status == 200
    assert r.body["pool"] == "tank"
    [create] = engine.called("zfs", "pool_create")
    assert create.argv == [
        "zpool", "create", "-o", "ashift=12", "-O", "compression=lz4",
        "-O", "acltype=posixacl", "tank", "mirror", "/dev/sda", "/dev/sdb",
    ]
    assert "-f" not in create.argv
    assert engine.called("zfs", "pool_add_cache")[0].argv[-1] == "/dev/nvme0n1"
    assert ("zfs", "create_state_dataset") in engine.actions()
    recorded = config.get_pool("tank")
    assert recorded["vdev_type"] == "mirror"
    assert recorded["encrypted"] is False
    assert recorded["cache_devices"] == ["/dev/nvme0n1"]


def test_a_striped_pool_has_no_vdev_type_argument(client, engine):
    client.post("/api/storage/pools", {"name": "tank", "devices": ["/dev/sda"]})
    [create] = engine.called("zfs", "pool_create")
    assert create.argv[-2:] == ["tank", "/dev/sda"]
    assert "" not in create.argv


@pytest.mark.parametrize("body, message", [
    ({"name": "1tank", "devices": ["/dev/sda"]}, "starts with a letter"),
    ({"name": "tank x", "devices": ["/dev/sda"]}, "starts with a letter"),
    ({"name": "tank", "devices": []}, "at least one device"),
    ({"name": "tank", "devices": ["/dev/sda"], "log_devices": ["/dev/sda"]}, "listed twice"),
    ({"name": "tank", "devices": ["-f", "/dev/sda"]}, "not a device path"),
    ({"name": "tank", "devices": ["/dev/sda -f"]}, "not a device path"),
    ({"name": "tank", "devices": ["/dev/../tmp/disk.img"]}, "not a device path"),
    ({"name": "tank", "devices": ["sda"]}, "not a device path"),
    ({"name": "tank", "devices": ["/dev/sda"], "cache_devices": ["-f"]}, "not a device path"),
    ({"name": "tank", "devices": ["/dev/sda"], "vdev_type": "-O mountpoint=/etc mirror"},
     "vdev_type must be"),
    ({"name": "tank", "devices": ["/dev/sda"], "encrypted": True,
      "master_passphrase": "short"}, "at least 12"),
])
def test_invalid_pools_are_refused_before_zfs_is_asked(client, engine, body, message):
    r = client.post("/api/storage/pools", body)
    assert r.status == 422
    assert message in r.body["detail"]
    assert engine.actions() == []


def test_an_encrypted_pool_gets_its_keyslots_and_no_secret_in_the_config(client, engine, config, tmp_path):
    passphrase = "correct horse battery"
    r = client.post("/api/storage/pools", {
        "name": "vault", "devices": ["/dev/sdc"], "encrypted": True,
        "master_passphrase": passphrase,
    })
    assert r.status == 200
    [create] = engine.called("zfs", "pool_create_encrypted")
    assert "-O" in create.argv and "encryption=aes-256-gcm" in create.argv
    location = next(a for a in create.argv if a.startswith("keylocation=file://"))
    keyfile = Path(location.removeprefix("keylocation=file://"))
    assert keyfile.parent == tmp_path / "run"
    assert not keyfile.exists()
    slots = json.loads((tmp_path / "keyslots" / "vault.json").read_text())
    assert set(slots["slots"]) == {"passphrase", "usb"}
    assert config.get_pool("vault")["encrypted"] is True
    assert passphrase not in config.config_path.read_text()
    assert passphrase not in json.dumps(slots)
    assert all(passphrase not in c.command for c in engine.calls)


def test_the_usb_devices_are_the_removable_partitions(client, engine):
    engine.outputs[("media", "list_removable")] = "sda 0 disk\nsda1 0 part\nsdh 1 disk\nsdh1 1 part"
    r = client.get("/api/storage/usb-devices")
    assert r.status == 200 and r.body == {"devices": ["/dev/sdh1"]}


@pytest.mark.parametrize("usb, encrypted, detail", [
    (["/dev/sda1"], True, "not a partition of a removable"),
    (["/dev/sdh1"], False, "only unlocks an encrypted pool"),
])
def test_a_usb_key_goes_only_on_a_removable_stick_of_an_encrypted_pool(client, engine, usb, encrypted, detail):
    engine.outputs[("media", "list_removable")] = "sda1 0 part\nsdh1 1 part"
    r = client.post("/api/storage/pools", {
        "name": "vault", "devices": ["/dev/sdc"], "encrypted": encrypted,
        "master_passphrase": "correct horse battery", "usb_devices": usb,
    })
    assert r.status == 422 and detail in r.body["detail"]
    assert engine.called("media", "mount_rw") == []
    assert engine.called("zfs", "pool_create_encrypted") == []


def test_the_usb_key_is_written_and_the_keyslots_go_on_the_pool(client, engine, config, tmp_path):
    engine.outputs[("media", "list_removable")] = "sdh1 1 part"
    r = client.post("/api/storage/pools", {
        "name": "vault", "devices": ["/dev/sdc"], "encrypted": True,
        "master_passphrase": "correct horse battery", "usb_devices": ["/dev/sdh1"],
    })
    assert r.status == 200
    [mount] = engine.called("media", "mount_rw")
    assert mount.kwargs["device"] == "/dev/sdh1"
    [prop] = [c for c in engine.called("zfs", "dataset_set") if "org.freedomnas:keyslots" in c.command]
    assert prop.argv[-1] == "vault"
