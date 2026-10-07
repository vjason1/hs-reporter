from conftest import wait_for


def run_preset(client, share, pid, **extra):
    p = next(x for x in client.get("/api/catalog").json()["presets"] if x["id"] == pid)
    d = {k: v for k, v in p.items() if k not in ("id", "label")}
    d.update(name=p["label"], share_id=share["id"], paths=["/proj"], **extra)
    did = client.post("/api/definitions", json=d).json()["id"]
    rid = client.post(f"/api/definitions/{did}/run").json()["run_id"]
    return did, wait_for(client, rid)


def test_share_browse_and_path_safety(client, share):
    assert client.get(f"/api/shares/{share['id']}/browse", params={"path": "/proj"}).json()["dirs"] == \
        ["a", "b", "empty"]
    assert client.get(f"/api/shares/{share['id']}/browse", params={"path": "/../../etc"}).status_code == 400


def test_per_folder_report(client, share):
    _, r = run_preset(client, share, "folder_usage")
    assert r["status"] == "done"
    folders = [row["Folder"] for row in r["view"]["rows"]]
    assert folders == ["/proj", "/proj/a", "/proj/b", "/proj/empty"]
    empty = r["view"]["rows"][-1]
    assert empty["Files"] == 0 and empty["Space used"] == 0
    prom = client.get(f"/api/runs/{r['id']}/export", params={"format": "prometheus"}).text
    assert 'node_directory_size_bytes{share="lab",directory="/proj/a"}' in prom


def test_volume_report_and_analysis_exports(client, share):
    _, r = run_preset(client, share, "top10_vol")
    assert r["status"] == "done" and r["has_files"]
    rid = r["id"]
    summary = client.get(f"/api/runs/{rid}/export",
                         params={"format": "analysis", "meta": "false", "unit": "gb", "decimals": 3}).text
    assert summary.splitlines()[0] == "storage_volume,files,space_used_gb"
    assert "vol-a::/export,400,0.174" in summary
    files = client.get(f"/api/runs/{rid}/export", params={"format": "files", "meta": "false"}).text
    assert files.splitlines()[1] == "vol-a::/export,1,/proj/big.iso,7278611"
    table = client.get(f"/api/runs/{rid}/export", params={"format": "csv", "size_unit": "mb"}).text
    assert table.splitlines()[0].startswith("Storage volume,Files,Space used (MB)")


def test_run_with_edited_expression(client, share):
    did, _ = run_preset(client, share, "owner_usage")
    exp = "IS_FILE?SUMS_TABLE{|KEY=OWNER,|VALUE={1FILE/files,SPACE_USED/bytes}}"
    rid = client.post(f"/api/definitions/{did}/run", json={"expression": exp}).json()["run_id"]
    r = wait_for(client, rid)
    assert r["expression"] == exp and "edited" in r["trigger"]


def test_missing_folder_fails_clearly(client, share):
    p = {"name": "x", "share_id": share["id"], "paths": ["/nope"], "mode": "sum",
         "sum": {"group_by": [], "metrics": ["file_count"]}}
    r = wait_for(client, client.post("/api/run", json=p).json()["run_id"])
    assert r["status"] == "failed" and "doesn't exist" in r["error"]


def test_schedules(client, share):
    did, _ = run_preset(client, share, "volume_usage")
    bad = client.post("/api/schedules", json={"definition_id": did, "cron": "nope"})
    assert bad.status_code == 422
    s = client.post("/api/schedules", json={"definition_id": did, "cron": "0 2 * * *", "keep_last": 3}).json()
    assert s["next_run"] and s["definition_name"]


def test_settings_api(client):
    r = client.get("/api/settings").json()
    assert r["values"]["crawl_preset"] == "normal" and r["crawl"]["concurrency"] == 4
    assert set(r["crawl_presets"]) == {"normal", "gentle", "slowest"}
    bad = client.put("/api/settings", json={"max_hs_processes": 0})
    assert bad.status_code == 422 and "between 1 and 32" in bad.json()["detail"]
    r = client.put("/api/settings", json={"crawl_preset": "gentle", "nfs_options": "vers=4.2"}).json()
    assert r["crawl"]["pause"] == 1.0
    assert client.get("/api/catalog").json()["mount_defaults"]["nfs"] == "vers=4.2"   # used for new mounts
    assert client.get("/api/plan-catalog").json()["crawl"]["preset"] == "gentle"
    r = client.post("/api/settings/reset").json()
    assert r["values"]["crawl_preset"] == "normal" and r["values"]["nfs_options"] == "vers=3,nolock"


def test_pause_between_commands(client, share):
    import time
    # 4 folders (one level), one at a time, 0.3s between commands: at least 3 pauses
    client.put("/api/settings", json={"crawl_preset": "custom", "crawl_concurrency": 1,
                                      "crawl_pause": 0.3, "crawl_list_rate": 0})
    try:
        t0 = time.time()
        _, r = run_preset(client, share, "folder_usage")
        assert r["status"] == "done" and len(r["view"]["rows"]) == 4
        assert r["throttle"]["pause"] == 0.3
        assert time.time() - t0 >= 0.9
    finally:
        client.post("/api/settings/reset")


def test_paced_folder_listing(client, share):
    import time
    # A full walk lists 5 folders (/proj, a, a/a1, b, empty); at 5 per second that's >= 0.8s
    client.put("/api/settings", json={"crawl_preset": "custom", "crawl_concurrency": 4,
                                      "crawl_pause": 0, "crawl_list_rate": 5})
    try:
        t0 = time.time()
        _, r = run_preset(client, share, "dir_walk")
        assert r["status"] == "done" and len(r["view"]["rows"]) == 5
        assert time.time() - t0 >= 0.8
    finally:
        client.post("/api/settings/reset")


def test_missing_hs_fails_the_run_clearly(client, share, monkeypatch):
    from app import reports
    monkeypatch.setattr(reports, "HS_BIN", "/nonexistent/hs")
    p = {"name": "x", "share_id": share["id"], "paths": ["/proj"], "mode": "sum",
         "sum": {"group_by": [], "metrics": ["file_count"]}}
    r = wait_for(client, client.post("/api/run", json=p).json()["run_id"], timeout=5)
    assert r["status"] == "failed"
    assert "Couldn't start /nonexistent/hs" in r["error"] and "HSR_HS_BIN" in r["error"]


def test_import_shares_from_share_list(client):
    from pathlib import Path
    text = (Path(__file__).parent / "fixtures" / "share-list.txt").read_text()
    parsed = client.post("/api/shares/import/parse", json={"text": text}).json()
    assert [s["name"] for s in parsed["shares"]] == ["stowerstiertest"] and parsed["root_excluded"] == 1
    s = parsed["shares"][0]
    assert s["path"] == "/stowerstiertest" and s["state"] == "PUBLISHED" and not s["insecure_allowed"]
    pick = [{"name": s["name"], "path": s["path"]}]

    bad = client.post("/api/shares/import", json={"server": "", "protocols": ["nfs"], "shares": pick})
    assert bad.status_code == 422 and "IP address or FQDN" in bad.json()["detail"]
    assert client.post("/api/shares/import", json={"server": "10.200.10.160", "protocols": [], "shares": pick}).status_code == 422
    nouser = client.post("/api/shares/import", json={"server": "10.200.10.160", "protocols": ["smb"], "shares": pick})
    assert "username" in nouser.json()["detail"]

    r = client.post("/api/shares/import", json={"server": "anvil.example.com", "protocols": ["nfs", "smb"],
                                                 "shares": pick, "username": "svc", "password": "pw",
                                                 "auto_mount": False}).json()
    got = {c["name"]: c for c in r["created"]}
    assert set(got) == {"stowerstiertest (NFS)", "stowerstiertest (SMB)"}
    assert got["stowerstiertest (NFS)"]["export"] == "/stowerstiertest"     # NFS: the share's path
    assert got["stowerstiertest (SMB)"]["export"] == "stowerstiertest"      # SMB: the share's name
    assert got["stowerstiertest (SMB)"]["username"] == "svc" and "password" not in got["stowerstiertest (SMB)"]

    again = client.post("/api/shares/import", json={"server": "ANVIL.example.com", "protocols": ["nfs"],
                                                     "shares": pick + [{"name": "root", "path": "/"}]}).json()
    assert again["created"] == [] and again["skipped"][0]["reason"] == "already added as stowerstiertest (NFS)"
    for c in r["created"]:
        client.delete(f"/api/shares/{c['id']}")
