"""A stand-in for the Hammerspace management API (sys-mgmt v1.2), shaped after its swagger
models and mirroring the cluster in tests/fixtures (the CLI exports), so the API import can be
checked against the CLI import."""
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

BASE = "/mgmt/v1.2/rest"
USER, PASSWORD = "admin", "secret"
app = FastAPI()


def uoid(uuid, t):
    return {"uuid": uuid, "objectType": t}


def node_loc(name):
    return {"_type": "NODE_LOCATION", "uoid": uoid(f"n-{name}", "NODE_LOCATION"), "node": {"name": name}}


def group_ref(name):
    return {"_type": "VOLUME_GROUP", "uoid": uoid(f"g-{name}", "VOLUME_GROUP"), "name": name}


def caps(avail, dur, delay=0, iops=None):
    return {"protection": {"availability": avail, "durability": dur, "effectiveAvailability": avail,
                           "effectiveDurability": dur, "onlineDelay": delay},
            "performance": {"readPerformance": {"iops": iops}, "writePerformance": {"iops": iops}}}


GB = 1_000_000_000
STORAGE_VOLUMES = [
    {"uoid": uoid("2de1bb3f-d768-433c-a18b-953e0bf3db1c", "STORAGE_VOLUME"), "name": "dsx2-1",
     "accessType": "READ_WRITE", "operState": "UP", "adminState": "UP", "storageVolumeState": "OK",
     "logicalVolume": {"exportPath": "/hsvol0", "capacity": {"total": 42_900_000_000, "used": 589_000_000, "free": 42_300_000_000}},
     "storageCapabilities": caps(2, 3, iops=14415),
     "associatedLocations": [group_ref("DSX"), node_loc("jv-a-dsx-2.asgard.local"), group_ref("all"), group_ref("Tier2")]},
    {"uoid": uoid("a2cbb190-d8b6-4ba8-bb21-58aa0d927e7f", "STORAGE_VOLUME"), "name": "dsx1-1",
     "accessType": "READ_WRITE", "operState": "UP", "adminState": "UP", "storageVolumeState": "OK",
     "logicalVolume": {"exportPath": "/hsvol0", "capacity": {"total": 42_900_000_000, "used": 575_700_000, "free": 42_300_000_000}},
     "storageCapabilities": caps(2, 3, iops=24720),
     "associatedLocations": [group_ref("DSX"), group_ref("Tier1"), group_ref("all"), node_loc("jv-a-dsx-1.asgard.local")]},
]
OBJECT_VOLUMES = [
    {"uoid": uoid("b32553af-2f1b-4b79-ae18-3cff7f591bd3", "OBJECT_STORAGE_VOLUME"), "name": "minio-obj::gfs",
     "operState": "UP", "adminState": "UP", "storageVolumeState": "OK", "totalCapacity": None, "logicalUsed": 0,
     "storageCapabilities": caps(4, 9, delay=300),
     "associatedLocations": [group_ref("all"), group_ref("object-volumes"), group_ref("shared-object-volumes"),
                             node_loc("minio-obj")]},
]
vol = lambda name: {"_type": "STORAGE_VOLUME", "name": name}
VOLUME_GROUPS = [
    {"name": "all", "state": "VALID", "volumes": [vol("dsx1-1"), vol("dsx2-1"), vol("minio-obj::gfs")]},
    {"name": "object-volumes", "state": "VALID", "volumes": [vol("minio-obj::gfs")]},
    {"name": "shared-object-volumes", "state": "VALID", "volumes": [vol("minio-obj::gfs")]},
    {"name": "virus-scanners", "state": "VALID", "volumes": []},
    {"name": "DSX", "state": "VALID", "volumes": [vol("dsx2-1"), vol("dsx1-1")]},
    {"name": "Tier1", "state": "VALID", "volumes": [vol("dsx1-1")]},
    {"name": "Tier2", "state": "VALID", "volumes": [vol("dsx2-1")]},
]


def objective(name, comment="", placement=None, protection=None, expression=None, hidden=False):
    return {"uoid": uoid(f"o-{name}", "OBJECTIVE"), "name": name, "comment": comment, "hidden": hidden,
            "placementObjective": placement, "protection": protection, "expression": expression}


OBJECTIVES = [
    objective("place-on-object-volumes", "Places data on object volumes.",
              {"placeOnLocations": [{"placeOn": [group_ref("object-volumes")]}]}),
    objective("place-on-shared-object-volumes", "", {"placeOnLocations": [{"placeOn": [group_ref("shared-object-volumes")]}]}),
    objective("place-on-virus-scanners", "", {"placeOnLocations": [{"placeOn": [group_ref("virus-scanners")]}]}),
    objective("confine-to-object-volumes", "", {"confineTo": [group_ref("object-volumes")]}),
    objective("exclude-from-object-volumes", "", {"excludeFrom": [group_ref("object-volumes")]}),
    objective("keep-online", "Place data on a volume with zero online delay.", {"allowedOnlineDelay": 0}),
    objective("optimize-for-capacity", "", {"capacityOptimize": True}),
    objective("availability-3-nines", "", None, {"availability": 3}),
    objective("durability-5-nines", "", None, {"durability": 5}),
    objective("virus-scan-operation", "", expression="IF GET_TAG(\"skip_virus_scan\") THEN {SLO('x')}"),
    objective("internal-hidden", hidden=True),
]
SHARES = [
    {"uoid": uoid("e0bc6fe9-6c88-4bae-bac7-a0fcfdb2c132", "SHARE"), "name": "root", "path": "/", "internalId": 1,
     "shareState": "PUBLISHED", "shareLifecycle": "CREATED", "smbBrowsable": True, "isReferral": False,
     "exportOptions": [{"subnet": "*", "accessPermissions": "RW", "rootSquash": False, "insecure": False, "securityOptions": ["SYS"]}]},
    {"uoid": uoid("8792ba0c-985a-459b-8b49-aba2b8a4d58c", "SHARE"), "name": "stowerstiertest", "path": "/stowerstiertest",
     "internalId": 12, "shareState": "PUBLISHED", "shareLifecycle": "CREATED", "smbBrowsable": True, "isReferral": False,
     "exportOptions": [{"subnet": "*", "accessPermissions": "RW", "rootSquash": False, "insecure": False, "securityOptions": ["SYS"]}]},
]
# Network interfaces: the real response from the lab cluster (Anvil + jv-t4-dsx-1), plus a second
# DSX whose data interface doesn't name its node type (found through /nodes) and whose other
# interface only has the MGMT role.
import json as _json
from pathlib import Path as _Path
INTERFACES = _json.loads((_Path(__file__).parent / "fixtures" / "network-interfaces.json").read_text())
_dsx2 = {"_type": "NODE", "uoid": {"uuid": "d2", "objectType": "NODE"}, "name": "jv-t4-dsx-2.asgard.local"}
INTERFACES += [
    {"_type": "NETWORK_IF", "name": "ens224", "node": _dsx2, "roles": ["DATA"],
     "ipAddresses": [{"address": "10.200.10.127", "prefixLength": 24}]},
    {"_type": "NETWORK_IF", "name": "ens192", "node": _dsx2, "roles": ["MGMT"],
     "ipAddresses": [{"address": "10.200.20.127", "prefixLength": 24}]},
]
NODES = [{**{k: v for k, v in i["node"].items()}} for i in INTERFACES[:4:2]] + \
    [{"uoid": {"uuid": "d2", "objectType": "NODE"}, "name": "jv-t4-dsx-2.asgard.local", "productNodeType": "DSX"}]
LISTS = {"/network-interfaces": INTERFACES, "/nodes": NODES,
         "/storage-volumes": STORAGE_VOLUMES, "/object-storage-volumes": OBJECT_VOLUMES,
         "/volume-groups": VOLUME_GROUPS, "/objectives": OBJECTIVES, "/shares": SHARES,
         "/cntl": [{"name": "JV-HS-A", "uoid": uoid("c1", "CLUSTER")}]}


@app.post(BASE + "/login")
async def login(request: Request):
    form = parse_qs((await request.body()).decode())   # application/x-www-form-urlencoded
    if (form.get("username", [""])[0], form.get("password", [""])[0]) != (USER, PASSWORD):
        return JSONResponse({"message": "bad credentials"}, status_code=401)
    r = JSONResponse(None)
    r.set_cookie("JSESSIONID", "session-1")
    return r


@app.get(BASE + "/{name:path}")
def listing(name: str, request: Request):
    if request.cookies.get("JSESSIONID") != "session-1":
        return JSONResponse({"message": "not logged in"}, status_code=401)
    items = LISTS.get("/" + name)
    if items is None:
        return JSONResponse({"message": "no such list"}, status_code=404)
    page = int(request.query_params.get("page", 0))
    size = int(request.query_params.get("page.size", 20))
    return items[page * size:(page + 1) * size]
