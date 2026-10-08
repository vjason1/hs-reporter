import time
from pathlib import Path

import httpx
import pytest

from app import clusterinfo, hsapi
import fake_hs_api
from test_objplan import tree, wait_scan  # noqa: F401  (the /plans folder fixture)

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def api(client, monkeypatch):
    """The fake cluster API (any address reaches it), a small page size to exercise paging,
    and two linked clusters: A (password saved) and B (password not saved)."""
    monkeypatch.setattr(hsapi, "_transport", httpx.ASGITransport(app=fake_hs_api.app))
    monkeypatch.setattr(hsapi, "PAGE_SIZE", 2)
    a = client.post("/api/clusters", json={"name": "JV-HS-A", "host": "10.200.10.160", "username": "admin",
                                           "password": "secret"}).json()
    b = client.post("/api/clusters", json={"name": "DK-HS-A", "host": "10.200.10.175", "username": "admin"}).json()
    yield {"a": a, "b": b}
    for c in client.get("/api/clusters").json():
        client.delete(f"/api/clusters/{c['id']}")


def cli(name):
    return clusterinfo.parse_any((FIX / f"{name}.txt").read_text())[1]


def test_api_matches_cli_import(api):
    import asyncio
    info = asyncio.run(hsapi.fetch_cluster_info(hsapi.get_cluster(api["a"]["id"])))
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
    r = client.post(f"/api/plans/{p['id']}/cluster/fetch", json={"cluster_id": api["a"]["id"]}).json()
    assert r["counts"] == {"volumes": 2, "object_volumes": 1, "volume_groups": 7, "objectives": 10}
    assert r["plan"]["cluster"]["sources"]["objectives"] == "api"
    assert r["plan"]["cluster"]["source_cluster"]["objectives"] == "JV-HS-A"
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
    r = client.post(f"/api/clusters/{api['a']['id']}/shares").json()
    assert [s["name"] for s in r["shares"]] == ["stowerstiertest"] and r["root_excluded"] == 1
    assert r["cluster_name"] == "JV-HS-A" and r["cluster_host"] == "10.200.10.160"
    assert r["server"] == "10.200.10.126" and r["shares"][0]["insecure_allowed"] is False   # DSX data address
    raw = client.get(f"/api/clusters/{api['a']['id']}/responses")
    assert raw.status_code == 200 and "/shares" in raw.json()["responses"]
    assert client.get(f"/api/clusters/{api['b']['id']}/responses").status_code == 404   # per cluster


def test_cluster_settings_and_errors(client, api):
    a, b = api["a"], api["b"]
    listed = {c["name"]: c for c in client.get("/api/clusters").json()}
    assert listed["JV-HS-A"]["has_password"] and not listed["DK-HS-A"]["has_password"]
    assert "password" not in listed["JV-HS-A"] and listed["JV-HS-A"]["port"] == 8443
    assert client.post(f"/api/clusters/{a['id']}/test").json() == {
        "ok": True, "cluster": "JV-HS-A", "base": "https://10.200.10.160:8443/mgmt/v1.2/rest"}
    wrong = client.post(f"/api/clusters/{a['id']}/test", json={"password": "nope"})
    assert wrong.status_code == 422 and wrong.json()["detail"] == "JV-HS-A rejected the username or password"
    ask = client.post(f"/api/clusters/{b['id']}/shares")                 # B's password isn't saved
    assert ask.status_code == 422 and "API password for DK-HS-A" in ask.json()["detail"]
    assert client.post(f"/api/clusters/{b['id']}/shares", json={"password": "secret"}).status_code == 200
    dup = client.post("/api/clusters", json={"name": "jv-hs-a", "host": "10.1.1.1"})
    assert dup.status_code == 422 and "already a cluster named" in dup.json()["detail"]
    assert client.post("/api/clusters", json={"name": "x", "host": "https://a.example.com/mgmt"}).status_code == 422
    r = client.put(f"/api/clusters/{b['id']}", json={"host": "https://dk-anvil.example.com"}).json()
    assert r["host"] == "dk-anvil.example.com" and r["name"] == "DK-HS-A"


def test_same_share_from_two_clusters(client, api):
    a, b = api["a"], api["b"]
    pick = [{"name": "stowerstiertest", "path": "/stowerstiertest"}]
    ra = client.post("/api/shares/import", json={"cluster_id": a["id"], "server": "10.200.10.126",
                                                  "protocols": ["nfs"], "shares": pick, "auto_mount": False}).json()
    rb = client.post("/api/shares/import", json={"cluster_id": b["id"], "server": "10.200.20.126",
                                                  "protocols": ["nfs"], "shares": pick, "auto_mount": False}).json()
    assert len(ra["created"]) == len(rb["created"]) == 1                 # same share name, different clusters
    sa, sb = ra["created"][0], rb["created"][0]
    assert (sa["cluster_name"], sb["cluster_name"]) == ("JV-HS-A", "DK-HS-A")
    assert sa["cluster_share_name"] == "stowerstiertest"
    again = client.post("/api/shares/import", json={"cluster_id": a["id"], "server": "10.200.10.127",
                                                     "protocols": ["nfs"], "shares": pick}).json()
    assert again["created"] == [] and "already added" in again["skipped"][0]["reason"]   # same cluster, other DSX
    # a plan on B's share loads from B and defaults --name to the share's name on the cluster
    plan = client.post("/api/plans", json={"name": "b", "share_id": sb["id"], "root": "/"}).json()
    assert plan["linked_cluster"]["name"] == "DK-HS-A" and plan["cluster_share"] == "stowerstiertest"
    loaded = client.post(f"/api/plans/{plan['id']}/cluster/fetch", json={"password": "secret"}).json()
    assert loaded["plan"]["cluster"]["source_cluster"]["volumes"] == "DK-HS-A"
    # deleting a cluster keeps its shares, unlinked
    gone = client.delete(f"/api/clusters/{b['id']}").json()
    assert gone["unlinked_shares"] == 1
    sb_now = next(s for s in client.get("/api/shares").json() if s["id"] == sb["id"])
    assert sb_now["cluster_id"] is None and sb_now["cluster_name"] is None
    assert client.post(f"/api/plans/{plan['id']}/cluster/fetch").status_code == 422     # now: choose a cluster
    for s in (sa, sb):
        client.delete(f"/api/shares/{s['id']}")
    client.delete(f"/api/plans/{plan['id']}")


def test_migration_from_single_connection(client, api, monkeypatch):
    import json
    from app import store
    for c in client.get("/api/clusters").json():
        client.delete(f"/api/clusters/{c['id']}")
    share = client.post("/api/shares", json={"name": "old", "kind": "nfs", "server": "10.200.10.126", "export": "/x"}).json()
    hsapi.LEGACY_PATH.write_text(json.dumps({"host": "10.200.10.160", "port": 8443, "username": "admin",
                                             "password": "secret", "verify_tls": False}))
    rec = hsapi.migrate_legacy(store.shares)
    assert rec["name"] == "10.200.10.160" and rec["password"] == "secret"
    assert not hsapi.LEGACY_PATH.exists() and hsapi.LEGACY_PATH.with_suffix(".json.migrated").exists()
    assert store.shares.get(share["id"])["cluster_id"] == rec["id"]          # the only cluster: shares linked
    assert hsapi.migrate_legacy(store.shares) is None                        # runs once
    hsapi.LEGACY_PATH.with_suffix(".json.migrated").unlink()
    client.delete(f"/api/shares/{share['id']}")


def test_mount_addresses_from_real_interfaces():
    import json
    real = json.loads((FIX / "network-interfaces.json").read_text())
    a = hsapi.mount_addresses(real)
    # the Anvil's ens224 has the DATA role too, but only DSX data addresses are mount addresses
    assert [(d["address"], d["node"], d["interface"]) for d in a["data"]] == \
        [("10.200.10.126", "jv-t4-dsx-1.asgard.local", "ens224")]
    assert a["anvil"] == ["10.200.10.125"]


def test_share_fetch_suggests_dsx_data_address(client, api):
    r = client.post(f"/api/clusters/{api['a']['id']}/shares").json()
    assert r["cluster_host"] == "10.200.10.160"
    assert r["server"] == "10.200.10.126"                 # a DSX data address, not the API host
    assert [d["address"] for d in r["addresses"]["data"]] == ["10.200.10.126", "10.200.10.127"]   # dsx-2 via /nodes
    assert "10.200.20.127" not in str(r["addresses"])      # MGMT-only interface left out
    assert r["addresses"]["anvil"] == ["10.200.10.125"]
    m = client.post(f"/api/clusters/{api['a']['id']}/mount-addresses").json()
    assert [d["address"] for d in m["data"]] == ["10.200.10.126", "10.200.10.127"]


def test_shares_on_the_anvil_address_are_flagged(client, api):
    a = api["a"]["id"]
    s = client.post("/api/shares", json={"name": "wrong", "kind": "nfs", "server": "10.200.10.160", "export": "/x",
                                         "cluster_id": a}).json()
    t = client.post("/api/shares", json={"name": "right", "kind": "nfs", "server": "10.200.10.126", "export": "/x",
                                         "cluster_id": a}).json()
    u = client.post("/api/shares", json={"name": "unlinked", "kind": "nfs", "server": "10.200.10.175", "export": "/x"}).json()
    flags = {x["name"]: x["anvil_address"] for x in client.get("/api/shares").json() if x["name"] in ("wrong", "right", "unlinked")}
    assert flags == {"wrong": True, "right": False, "unlinked": True}   # unlinked: checked against every cluster
    bad = client.post("/api/shares", json={"name": "x", "kind": "nfs", "server": "1.2.3.4", "export": "/x", "cluster_id": "nope"})
    assert bad.status_code == 422
    for x in (s, t, u):
        client.delete(f"/api/shares/{x['id']}")
