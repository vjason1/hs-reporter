import os
import time
from pathlib import Path

import pytest

from conftest import SHARE

FIX = Path(__file__).parent / "fixtures"
DAY = 86400


@pytest.fixture(scope="module")
def tree():
    """/plans/{hot,cold/archive} with a tiny file, a new file and an old tagged file."""
    root = SHARE / "plans"
    (root / "hot").mkdir(parents=True, exist_ok=True)
    (root / "cold" / "archive").mkdir(parents=True, exist_ok=True)
    (root / "hot" / "tiny.txt").write_bytes(b"x" * 12)
    (root / "hot" / "work.dat").write_bytes(b"w" * 100_000)
    old = root / "cold" / "archive" / "old.bin"
    old.write_bytes(b"o" * 300_000)
    t = time.time() - 120 * DAY
    os.utime(old, (t, t))
    return root


def wait_scan(client, pid, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        p = client.get(f"/api/plans/{pid}").json()
        if p["scan"]["status"] not in ("running",):
            return p
        time.sleep(0.1)
    raise AssertionError("scan did not finish")


@pytest.fixture(scope="module")
def plan(client, share, tree):
    p = client.post("/api/plans", json={"name": "Tiering", "share_id": share["id"], "root": "/plans",
                                        "fields": ["MODIFY_AGE"], "metas": ['GET_TAG("tier")']}).json()
    for name in ("volume-list", "object-volume-list", "volume-group-list", "objective-list"):
        r = client.post(f"/api/plans/{p['id']}/cluster", json={"text": (FIX / f"{name}.txt").read_text()})
        assert r.status_code == 200, r.text
    client.post(f"/api/plans/{p['id']}/scan")
    return wait_scan(client, p["id"])


def test_scan_gathers_only_chosen_fields(plan):
    s = plan["scan"]
    assert s["status"] == "done" and s["method"] == "recursive"
    assert s["files"] == 3 and s["folders"] == 3
    assert s["fields"] == ["SIZE", "SPACE_USED", "MODIFY_AGE", 'GET_TAG("tier")']
    assert s["expression"] == '{DPATH,SIZE,SPACE_USED,MODIFY_AGE,GET_TAG("tier")}'


def test_scan_falls_back_to_per_file(client, share, tree, monkeypatch):
    monkeypatch.setenv("FAKE_HS_NO_RECURSIVE", "1")
    p = client.post("/api/plans", json={"name": "Fallback", "share_id": share["id"], "root": "/plans"}).json()
    client.post(f"/api/plans/{p['id']}/scan")
    s = wait_scan(client, p["id"])["scan"]
    assert s["status"] == "done" and s["method"] == "per-file" and s["gathered"] == 3


def test_cluster_info_parsed(plan):
    assert plan["cluster_counts"] == {"volumes": 2, "object_volumes": 1, "volume_groups": 7, "objectives": 63}


def test_condition_check_and_preview(client, plan):
    pid = plan["id"]
    ok = client.post(f"/api/plans/{pid}/check", json={"condition": "MODIFY_AGE>30*DAYS"}).json()
    assert ok["ok"] and ok["matches"]["files"] == 1 and ok["matches"]["examples"] == ["/cold/archive/old.bin"]
    tag = client.post(f"/api/plans/{pid}/check", json={"condition": 'GET_TAG("tier")=="cold"'}).json()
    assert tag["matches"]["files"] == 1
    scoped = client.post(f"/api/plans/{pid}/check", json={"condition": "", "scope": {"type": "folder", "path": "/hot"}}).json()
    assert scoped["matches"]["files"] == 2
    missing = client.post(f"/api/plans/{pid}/check", json={"condition": "ACCESS_AGE>1*DAYS"}).json()
    assert missing["not_scanned"] == ["ACCESS_AGE"]
    bad = client.post(f"/api/plans/{pid}/check", json={"condition": "MODFY_AGE>1"}).json()
    assert not bad["ok"] and "MODIFY_AGE" in bad["error"]
    folders = client.get(f"/api/plans/{pid}/folders", params={"path": "/cold"}).json()
    assert folders["dirs"] == ["archive"]


def test_calculate_tiering(client, plan):
    pid = plan["id"]
    rows = [{"objective": "place-on-object-volumes", "condition": "MODIFY_AGE>30*DAYS"},
            {"objective": "optimize-for-capacity", "condition": "MODIFY_AGE>30*DAYS"},
            {"objective": "availability-3-nines", "condition": "MODIFY_AGE<=30*DAYS"}]
    client.put(f"/api/plans/{pid}", json={"rows": rows})
    r = client.post(f"/api/plans/{pid}/calculate").json()
    vols = {v["name"]: v for v in r["volumes"]}
    assert vols["minio-obj::gfs"]["projected"] == 300_000          # old file, object only
    assert vols["dsx1-1"]["files"] == 2 and vols["dsx2-1"]["files"] == 2  # two copies of the new files
    assert r["copies"] == {"1": 1, "2": 2}
    assert r["uncovered"]["files"] == 0
    assert r["commands"][0] == ('share-objective-add --name "lab" --objective "place-on-object-volumes" '
                                '--applicability "MODIFY_AGE>30*DAYS"')


def test_coverage_gap_and_small_objects(client, plan):
    pid = plan["id"]
    rows = [{"objective": "place-on-object-volumes", "condition": "SIZE<1*KBYTES"},
            {"objective": "keep-online", "condition": "MODIFY_AGE>30*DAYS"}]
    client.put(f"/api/plans/{pid}", json={"rows": rows})
    r = client.post(f"/api/plans/{pid}/calculate").json()
    assert r["metadata_files"] == 1                                  # the 12-byte file on object storage
    assert r["uncovered"]["files"] == 1 and r["uncovered"]["examples"] == ["/hot/work.dat"]
    assert r["uncovered"]["suggestion"] == "NOT ((SIZE<1*KBYTES) OR (MODIFY_AGE>30*DAYS))"
    assert any(w["kind"] == "coverage" for w in r["warnings"])


def test_condition_on_unscanned_field_blocks_calculation(client, plan):
    pid = plan["id"]
    client.put(f"/api/plans/{pid}", json={"rows": [{"objective": "keep-online", "condition": "ACCESS_AGE>1*DAYS"}]})
    r = client.post(f"/api/plans/{pid}/calculate")
    assert r.status_code == 422 and "ACCESS_AGE" in r.json()["detail"]
    p = client.get(f"/api/plans/{pid}").json()
    assert p["needed"]["fields"] == ["ACCESS_AGE"]


def test_name_and_path_patterns(client, plan):
    pid = plan["id"]
    chk = lambda c: client.post(f"/api/plans/{pid}/check", json={"condition": c}).json()
    assert chk("FNMATCH('*.bin', NAME)?TRUE")["matches"]["files"] == 1
    assert chk("!FNMATCH('*.bin', NAME)?TRUE")["matches"]["files"] == 2
    # PATH is from the share root: the plan models /plans
    assert chk("FNMATCH('./plans/hot/*', PATH)?TRUE")["matches"]["files"] == 2
    assert chk("FNMATCH('*/archive/*', PATH)?TRUE")["matches"]["examples"] == ["/cold/archive/old.bin"]
    assert chk("FNMATCH('*.bin', NAME)?TRUE")["not_scanned"] == []      # names come from the walk


def test_commands_use_share_paths(client, plan):
    pid = plan["id"]
    rows = [{"objective": "keep-online", "scope": {"type": "folder", "path": "/hot"}},
            {"objective": "place-on-object-volumes", "condition": "FNMATCH('*/archive/*', PATH)?TRUE"},
            {"objective": "availability-3-nines", "condition": "!FNMATCH('*/archive/*', PATH)?TRUE"}]
    client.put(f"/api/plans/{pid}", json={"rows": rows})
    r = client.post(f"/api/plans/{pid}/calculate").json()
    assert r["uncovered"]["files"] == 0
    assert r["commands"] == [
        'share-objective-add --name "lab" --objective "keep-online" --path "/plans/hot"',
        'share-objective-add --name "lab" --objective "place-on-object-volumes" --applicability "FNMATCH(\'*/archive/*\', PATH)?TRUE"',
        'share-objective-add --name "lab" --objective "availability-3-nines" --applicability "!FNMATCH(\'*/archive/*\', PATH)?TRUE"',
    ]
    client.put(f"/api/plans/{pid}", json={"rows": [
        {"objective": "keep-online", "scope": {"type": "file", "path": "/hot", "name": "work.dat"}},
        {"objective": "keep-online", "scope": {"type": "folder", "path": "/"}}]})
    cmds = client.get(f"/api/plans/{pid}/commands").text.splitlines()
    assert cmds == ['share-objective-add --name "lab" --objective "keep-online" --path "/plans/hot/work.dat"',
                    'share-objective-add --name "lab" --objective "keep-online" --path "/plans"']


PLACE_ON_DSX = """
ID:                      00000000-0000-0000-0000-00000000d5c0
Name:                    place-on-DSX
Description:             Places data on the DSX volume group.
Internal ID:             999
Priority:                0
Place on:                
                         [volume group: DSX]
"""


def test_results_grouped_by_targeted_volume_group(client, plan):
    pid = plan["id"]
    objectives = (FIX / "objective-list.txt").read_text() + PLACE_ON_DSX
    assert client.post(f"/api/plans/{pid}/cluster", json={"text": objectives}).status_code == 200
    client.put(f"/api/plans/{pid}", json={"rows": [
        {"objective": "place-on-DSX", "condition": "MODIFY_AGE<=30*DAYS"},
        {"objective": "place-on-shared-object-volumes", "condition": "MODIFY_AGE>30*DAYS"}]})
    r = client.post(f"/api/plans/{pid}/calculate").json()
    t = {x["name"]: x for x in r["targets"]}
    # DSX is one row: free space totalled across dsx1-1 and dsx2-1, members not listed separately
    assert set(t) == {"DSX", "shared-object-volumes"}
    dsx = t["DSX"]
    assert dsx["kind"] == "group" and sorted(dsx["members"]) == ["dsx1-1", "dsx2-1"]
    assert dsx["free"] == 2 * 42_300_000_000
    # the two new files, plus the local copy kept for the old file (no optimize-for-capacity)
    assert dsx["instances"] == 3 and dsx["files"] == 3
    obj = t["shared-object-volumes"]
    assert obj["free"] is None and obj["projected"] == 300_000 and obj["instances"] == 1
    # totals agree with the per-volume figures
    assert sum(x["projected"] for x in r["targets"]) == r["projected"]
