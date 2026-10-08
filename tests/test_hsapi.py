import time
from pathlib import Path

import httpx
import pytest

from app import clusterinfo, hsapi
import fake_hs_api
from test_objplan import tree, wait_scan  # noqa: F401  (the /plans folder fixture)

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def api(monkeypatch):
    """The fake cluster API, a small page size (to exercise paging), and a saved connection."""
    monkeypatch.setattr(hsapi, "_transport", httpx.ASGITransport(app=fake_hs_api.app))
    monkeypatch.setattr(hsapi, "PAGE_SIZE", 2)
    hsapi.PATH.unlink(missing_ok=True)
    hsapi.save_connection({"host": "10.200.10.160", "username": "admin", "password": "secret"})
    yield
    hsapi.PATH.unlink(missing_ok=True)
    hsapi.RAW_PATH.unlink(missing_ok=True)


def cli(name):
    return clusterinfo.parse_any((FIX / f"{name}.txt").read_text())[1]


def test_api_matches_cli_import(api):
    import asyncio
    info = asyncio.run(hsapi.fetch_cluster_info())
    # storage volumes: same names, nodes, access, free space, groups, protection
    by_api = {v["name"]: v for v in info["volumes"]}
    for v in cli("volume-list"):
        a = by_api[v["name"]]
        assert (a["node"], a["read_only"], a["free"], a["groups"]) == (v["node"], v["read_only"], v["free"], v["groups"])
        assert round(a["availability"], 6) == round(v["availability"], 6) and round(a["durability"], 6) == round(v["durability"], 6)
        assert f"{v['node']}::/hsvol0" in a["aliases"]
    o_api, o_cli = info["object_volumes"][0], cli("object-volume-list")[0]
    assert (o_api["name"], o_api["node"], o_api["total"], o_api["online_delay"]) == ("minio-obj::gfs", "minio-obj", None, 300)
    assert round(o_api["durability"], 9) == round(o_cli["durability"], 9)
    assert {g["name"]: g["volumes"] for g in info["volume_groups"]} == {g["name"]: g["volumes"] for g in cli("volume-group-list")}
    # objectives: same effects as the CLI list; hidden ones left out
    cli_obj = {o["name"]: o for o in cli("objective-list")}
    for o in info["objectives"]:
        if o["name"] in cli_obj:
            assert o["effects"] == cli_obj[o["name"]]["effects"], o["name"]
            assert [tuple(t) for t in o["place_on"]] == [tuple(t) for t in cli_obj[o["name"]]["place_on"]]
    assert "internal-hidden" not in {o["name"] for o in info["objectives"]}
    assert round([o for o in info["objectives"] if o["name"] == "availability-3-nines"][0]["availability"], 6) == 0.999


def test_plan_from_api_matches_plan_from_cli(client, share, tree, api):
    p = client.post("/api/plans", json={"name": "api", "share_id": share["id"], "root": "/plans",
                                        "fields": ["MODIFY_AGE"]}).json()
    q = client.post("/api/plans", json={"name": "cli", "share_id": share["id"], "root": "/plans",
                                        "fields": ["MODIFY_AGE"]}).json()
    r = client.post(f"/api/plans/{p['id']}/cluster/fetch").json()
    assert r["counts"] == {"volumes": 2, "object_volumes": 1, "volume_groups": 7, "objectives": 10}
    assert r["plan"]["cluster"]["sources"]["objectives"] == "api"
    for name in ("volume-list", "object-volume-list", "volume-group-list", "objective-list"):
        client.post(f"/api/plans/{q['id']}/cluster", json={"text": (FIX / f"{name}.txt").read_text()})
    rows = [{"objective": "place-on-object-volumes", "condition": "MODIFY_AGE>30*DAYS"},
            {"objective": "availability-3-nines", "condition": "MODIFY_AGE<=30*DAYS"}]
    results = []
    for plan in (p, q):
        client.post(f"/api/plans/{plan['id']}/scan")
        wait_scan(client, plan["id"])
        client.put(f"/api/plans/{plan['id']}", json={"rows": rows})
        results.append(client.post(f"/api/plans/{plan['id']}/calculate").json())
    strip = lambda res: sorted((t["name"], t["projected"], t["instances"]) for t in res["targets"])
    assert strip(results[0]) == strip(results[1])
    assert results[0]["copies"] == results[1]["copies"]


def test_shares_from_api(client, api):
    r = client.post("/api/shares/import/fetch").json()
    assert [s["name"] for s in r["shares"]] == ["stowerstiertest"] and r["root_excluded"] == 1
    assert r["server"] == "10.200.10.126" and r["shares"][0]["insecure_allowed"] is False   # DSX data address
    raw = client.get("/api/cluster-api/responses")
    assert raw.status_code == 200 and "/shares" in raw.json()["responses"]


def test_connection_settings_and_errors(client, api):
    pub = client.get("/api/cluster-api").json()
    assert pub["has_password"] and "password" not in pub and pub["port"] == 8443
    assert client.post("/api/cluster-api/test").json() == {"ok": True, "cluster": "JV-HS-A",
                                                           "base": "https://10.200.10.160:8443/mgmt/v1.2/rest"}
    wrong = client.post("/api/cluster-api/test", json={"password": "nope"})
    assert wrong.status_code == 422 and "rejected the username or password" in wrong.json()["detail"]
    client.put("/api/cluster-api", json={"save_password": False})              # don't keep the password
    assert client.get("/api/cluster-api").json()["has_password"] is False
    ask = client.post("/api/shares/import/fetch")
    assert ask.status_code == 422 and "password" in ask.json()["detail"]
    assert client.post("/api/shares/import/fetch", json={"password": "secret"}).status_code == 200
    bad = client.put("/api/cluster-api", json={"host": "https://anvil.example.com/mgmt"})
    assert bad.status_code == 422
    assert client.put("/api/cluster-api", json={"host": "https://anvil.example.com"}).json()["host"] == "anvil.example.com"


def test_mount_addresses_from_real_interfaces():
    import json
    real = json.loads((FIX / "network-interfaces.json").read_text())
    a = hsapi.mount_addresses(real)
    # the Anvil's ens224 has the DATA role too, but only DSX data addresses are mount addresses
    assert [(d["address"], d["node"], d["interface"]) for d in a["data"]] == \
        [("10.200.10.126", "jv-t4-dsx-1.asgard.local", "ens224")]
    assert a["anvil"] == ["10.200.10.125"]


def test_share_fetch_suggests_dsx_data_address(client, api):
    r = client.post("/api/shares/import/fetch").json()
    assert r["cluster_host"] == "10.200.10.160"
    assert r["server"] == "10.200.10.126"                 # a DSX data address, not the API host
    assert [d["address"] for d in r["addresses"]["data"]] == ["10.200.10.126", "10.200.10.127"]   # dsx-2 via /nodes
    assert "10.200.20.127" not in str(r["addresses"])      # MGMT-only interface left out
    assert r["addresses"]["anvil"] == ["10.200.10.125"]
    m = client.post("/api/cluster-api/mount-addresses").json()
    assert [d["address"] for d in m["data"]] == ["10.200.10.126", "10.200.10.127"]


def test_shares_on_the_anvil_address_are_flagged(client, api):
    s = client.post("/api/shares", json={"name": "wrong", "kind": "nfs", "server": "10.200.10.160", "export": "/x"}).json()
    t = client.post("/api/shares", json={"name": "right", "kind": "nfs", "server": "10.200.10.126", "export": "/x"}).json()
    flags = {x["name"]: x["anvil_address"] for x in client.get("/api/shares").json() if x["name"] in ("wrong", "right")}
    assert flags == {"wrong": True, "right": False}
    for x in (s, t):
        client.delete(f"/api/shares/{x['id']}")
