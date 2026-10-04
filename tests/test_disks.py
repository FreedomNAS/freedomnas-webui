"""/api/storage/disks/* — disk health from SMART, and self-tests."""

import json

from freedomnas.engine import CommandExecutionError


def ata(serial, attributes=(), passed=True, errors=0, tests=()):
    return {
        "smartctl": {"exit_status": 0},
        "device": {"name": "/dev/x", "type": "sat", "protocol": "ATA"},
        "model_name": "ST9500420AS",
        "serial_number": serial,
        "user_capacity": {"bytes": 500107862016},
        "rotation_rate": 5400,
        "smart_status": {"passed": passed},
        "temperature": {"current": 31},
        "power_on_time": {"hours": 8771},
        "ata_smart_attributes": {"table": [
            {"id": i, "name": n, "value": 100, "worst": 100, "thresh": 36,
             "when_failed": f, "raw": {"value": raw, "string": str(raw)}}
            for i, n, raw, f in attributes]},
        "ata_smart_error_log": {"summary": {"count": errors}},
        "ata_smart_self_test_log": {"standard": {"table": list(tests)}},
        "ata_smart_data": {"self_test": {"status": {"string": "completed without error"}}},
    }


HEALTHY = ata("GOOD", [(5, "Reallocated_Sector_Ct", 0, ""), (199, "UDMA_CRC_Error_Count", 0, "")],
              tests=[{"type": {"string": "Short offline"},
                      "status": {"string": "Completed without error", "passed": True},
                      "lifetime_hours": 8700}])
WORN = ata("WORN", [(5, "Reallocated_Sector_Ct", 25, ""), (187, "Reported_Uncorrect", 5269, ""),
                    (199, "UDMA_CRC_Error_Count", 24, "")], errors=4885)
FAILING = ata("BAD", [(5, "Reallocated_Sector_Ct", 3000, "now")], passed=False)
STANDBY = {"smartctl": {"exit_status": 2}, "device": {"name": "/dev/sdd", "protocol": "ATA"},
           "power_mode": {"name": "STANDBY"}}
NVME = {
    "smartctl": {"exit_status": 0},
    "device": {"name": "/dev/nvme0", "type": "nvme", "protocol": "NVMe"},
    "model_name": "WD_BLACK SN850P", "serial_number": "N1", "user_capacity": {"bytes": 1000204886016},
    "smart_status": {"passed": True}, "temperature": {"current": 40},
    "nvme_smart_health_information_log": {
        "critical_warning": 0, "percentage_used": 3, "media_errors": 0,
        "available_spare": 100, "available_spare_threshold": 10, "power_on_hours": 1200},
}

SMART = {"/dev/sda": HEALTHY, "/dev/sdb": WORN, "/dev/sdc": FAILING, "/dev/sdd": STANDBY,
         "/dev/nvme0": NVME}


def disks(engine, smart=SMART):
    engine.outputs[("media", "smart_scan")] = json.dumps(
        {"devices": [{"name": p, "type": "scsi"} for p in smart]})
    engine.outputs[("media", "smart_info")] = lambda device: json.dumps(smart[device])
    engine.outputs[("media", "list_devices")] = (
        'NAME="/dev/sda" PKNAME="" TYPE="disk"\n'
        'NAME="/dev/sda1" PKNAME="/dev/sda" TYPE="part"\n'
        'NAME="/dev/sda2" PKNAME="/dev/sda" TYPE="part"\n'
        'NAME="/dev/sdb" PKNAME="" TYPE="disk"\n')
    engine.outputs[("zfs", "pool_member_devices")] = (
        "  pool: rpool\n state: ONLINE\nconfig:\n\n\tNAME STATE\n\trpool ONLINE\n"
        "\t  /dev/sda2  ONLINE  0 0 0\n\n"
        "  pool: tank\n state: ONLINE\nconfig:\n\n\ttank ONLINE\n\t  /dev/sdb  ONLINE  0 0 0\n")


def by_name(body):
    return {d["name"]: d for d in body["disks"]}


def test_each_disk_gets_one_verdict_with_its_reasons(client, engine):
    disks(engine)
    r = client.get("/api/storage/disks/")
    assert r.status == 200 and r.body["available"] is True
    found = by_name(r.body)
    assert found["sda"]["health"] == "ok" and found["sda"]["reasons"] == []
    assert found["sdb"]["health"] == "warning"
    assert found["sdb"]["reasons"] == [
        "25 reallocated sectors",
        "5269 errors the disk could not correct",
        "24 transfer (CRC) errors: check the cable",
        "4885 errors in the disk's error log",
    ]
    assert found["sdc"]["health"] == "failing"
    assert found["sdc"]["reasons"][:2] == [
        "The disk's own SMART check reports it is failing",
        "Reallocated_Sector_Ct is below its failure threshold",
    ]
    assert found["sdd"]["health"] == "standby"
    assert found["nvme0"]["health"] == "ok" and found["nvme0"]["kind"] == "NVMe"
    assert found["nvme0"]["power_on_hours"] == 1200


def test_the_details_come_along(client, engine):
    disks(engine)
    sda = by_name(client.get("/api/storage/disks/").body)["sda"]
    assert sda["model"] == "ST9500420AS" and sda["serial"] == "GOOD"
    assert sda["kind"] == "HDD" and sda["size"] == 500107862016
    assert sda["temperature"] == 31 and sda["power_on_hours"] == 8771
    assert sda["counters"] == {"reallocated": 0, "crc_errors": 0}
    assert sda["last_test"] == {"type": "Short offline", "status": "Completed without error",
                                "passed": True, "hours": 8700}
    assert [a["name"] for a in sda["attributes"]] == ["Reallocated_Sector_Ct", "UDMA_CRC_Error_Count"]


def test_disks_are_matched_to_their_pools(client, engine):
    disks(engine)
    found = by_name(client.get("/api/storage/disks/").body)
    assert found["sda"]["pools"] == ["rpool"]
    assert found["sdb"]["pools"] == ["tank"]
    assert found["sdc"]["pools"] == []


def test_a_sleeping_disk_is_not_woken(client, engine):
    disks(engine)
    client.get("/api/storage/disks/")
    assert all(c.argv[:4] == ["smartctl", "-a", "-j", "-n"] and c.argv[4] == "standby"
               for c in engine.called("media", "smart_info"))


def test_without_smartctl_the_tab_says_so(client, engine):
    engine.outputs[("media", "smart_scan")] = CommandExecutionError("smartctl: not found")
    r = client.get("/api/storage/disks/")
    assert r.status == 200 and r.body == {"available": False, "disks": []}


def test_a_self_test_starts_on_a_known_disk(client, engine):
    disks(engine)
    r = client.post("/api/storage/disks/sdb/test", {"type": "long"})
    assert r.status == 200
    assert engine.called("media", "smart_test")[0].argv == ["smartctl", "-t", "long", "/dev/sdb"]


def test_only_known_disks_and_test_types_are_accepted(client, engine):
    disks(engine)
    assert client.post("/api/storage/disks/sdz/test", {"type": "short"}).status == 404
    assert client.post("/api/storage/disks/sda/test", {"type": "offline"}).status == 400
    assert client.post("/api/storage/disks/sda/test", {"type": "short; reboot"}).status == 400
    assert engine.called("media", "smart_test") == []
