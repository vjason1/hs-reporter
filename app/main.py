import asyncio
import re
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import auth, clusterinfo, objexpr, objplan, reports, scheduler, settings, shares, store

STATIC = Path(__file__).parent / "static"
_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for r in store.runs.summaries():  # anything "running" at shutdown was interrupted
        if r["status"] in ("queued", "running"):
            full = store.runs.get(r["id"])
            full.update(status="failed", error="Interrupted by a service restart")
            store.runs.put(full)
    for d in store.definitions.all():  # older versions filled descriptions with "Based on <script>"
        if (d.get("description") or "").startswith("Based on ") and d["description"].rstrip(")").endswith(
                (".sh", ".py", "report 1", "report 2", "report 3")):
            d["description"] = ""
            store.definitions.put(d)
    await shares.automount_all()
    scheduler.start()
    yield
    scheduler.scheduler.shutdown(wait=False)


app = FastAPI(title="Hammerspace Reporter", lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Covers the API and the static GUI alike; /api/health stays open for probes."""
    if request.url.path != "/api/health" and auth.config()["enabled"] and \
            not await asyncio.to_thread(auth.check_header, request.headers.get("authorization")):
        return Response("Sign in required", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Hammerspace Reporter"'})
    return await call_next(request)


def _404(what):
    raise HTTPException(404, f"{what} not found")


def _bg(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


@app.get("/api/health")
def health():
    return {"ok": True, "hs": reports.HS_BIN}


@app.get("/api/catalog")
def catalog():
    return reports.catalog()


# ------------------------------------------------------------------ shares

SHARE_FIELDS = ("name", "kind", "server", "export", "options", "username", "password",
                "domain", "local_path", "auto_mount", "notes")


def _public_share(s: dict) -> dict:
    out = {k: v for k, v in s.items() if k != "password"}
    out["has_password"] = bool(s.get("password"))
    try:
        out["status"] = shares.status(s)
    except shares.ShareError as e:
        out["status"] = {"mounted": False, "error": str(e)}
    return out


def _validate_share(s: dict):
    if not (s.get("name") or "").strip():
        raise HTTPException(422, "Give the share a name")
    if s.get("kind") not in ("nfs", "smb", "local"):
        raise HTTPException(422, "Connection type must be nfs, smb or local")
    if s["kind"] in ("nfs", "smb") and not (s.get("server") and s.get("export")):
        raise HTTPException(422, "Server and export are required")
    if s["kind"] == "local":
        if not s.get("local_path"):
            raise HTTPException(422, "Path is required")
        try:
            shares.share_root(s)
        except shares.ShareError as e:
            raise HTTPException(422, str(e))


@app.get("/api/shares")
def list_shares():
    return [_public_share(s) for s in store.shares.all()]


@app.post("/api/shares")
def create_share(body: dict = Body(...)):
    s = {k: body.get(k) for k in SHARE_FIELDS}
    _validate_share(s)
    return _public_share(store.shares.put(s))


@app.post("/api/shares/import/parse")
def parse_share_list(body: dict = Body(...)):
    """Read `share-list` output; the root share is left out (reports run inside a share)."""
    try:
        found = clusterinfo.parse_share_list(body.get("text") or "")
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"shares": [s for s in found if not s["is_root"]], "root_excluded": sum(s["is_root"] for s in found)}


_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$|^\[?[0-9A-Fa-f:]+\]?$")


@app.post("/api/shares/import")
async def import_shares(body: dict = Body(...)):
    """Add shares picked from share-list output, over NFS, SMB or both."""
    server = (body.get("server") or "").strip()
    if not server or not _HOST_RE.match(server):
        raise HTTPException(422, "Enter the cluster's IP address or FQDN")
    protocols = [p for p in ("nfs", "smb") if p in (body.get("protocols") or [])]
    if not protocols:
        raise HTTPException(422, "Choose NFS, SMB or both")
    if "smb" in protocols and not (body.get("username") or "").strip():
        raise HTTPException(422, "SMB needs a username")
    picked = [s for s in body.get("shares") or [] if s.get("name") and (s.get("path") or "") != "/" and s.get("name") != "root"]
    if not picked:
        raise HTTPException(422, "Choose at least one share")
    both = len(protocols) == 2
    existing = store.shares.all()

    def exists(kind, export):
        return next((e for e in existing if e.get("kind") == kind and (e.get("server") or "").lower() == server.lower()
                     and (e.get("export") or "").strip("/") == export.strip("/")), None)

    created, skipped, mount_errors = [], [], []
    for s in picked:
        for kind in protocols:
            export = s["path"] if kind == "nfs" else s["name"]
            name = s["name"] + (f" ({kind.upper()})" if both else "")
            dup = exists(kind, export)
            if dup:
                skipped.append({"name": name, "reason": f"already added as {dup['name']}"})
                continue
            rec = {"name": name, "kind": kind, "server": server, "export": export, "options": None,
                   "auto_mount": bool(body.get("auto_mount", True)), "notes": f"Imported from share-list ({s['name']})"}
            if kind == "smb":
                rec.update(username=body.get("username").strip(), password=body.get("password") or "",
                           domain=(body.get("domain") or "").strip() or None)
            rec = store.shares.put(rec)
            existing.append(rec)
            if body.get("mount"):
                try:
                    await shares.mount(rec)
                except (shares.ShareError, asyncio.TimeoutError) as e:
                    mount_errors.append({"name": name, "error": str(e)[-300:]})
            created.append(_public_share(rec))
    return {"created": created, "skipped": skipped, "mount_errors": mount_errors}


@app.put("/api/shares/{sid}")
async def update_share(sid: str, body: dict = Body(...)):
    s = store.shares.get(sid) or _404("Share")
    changed = {k: body[k] for k in SHARE_FIELDS if k in body}
    if "password" in changed and not changed["password"]:
        changed.pop("password")  # blank means "keep existing"
    remount = any(changed.get(k) != s.get(k) for k in ("server", "export", "options", "kind", "local_path"))
    if remount and shares.is_mounted(s):
        await shares.unmount(s)
    s.update(changed)
    _validate_share(s)
    return _public_share(store.shares.put(s))


@app.delete("/api/shares/{sid}")
async def delete_share(sid: str):
    s = store.shares.get(sid) or _404("Share")
    try:
        await shares.unmount(s)
    except shares.ShareError:
        pass
    store.shares.delete(sid)
    (shares.CRED_DIR / sid).unlink(missing_ok=True)
    return {"deleted": sid}


@app.post("/api/shares/{sid}/mount")
async def mount_share(sid: str):
    s = store.shares.get(sid) or _404("Share")
    try:
        await shares.mount(s)
    except (shares.ShareError, asyncio.TimeoutError) as e:
        raise HTTPException(400, f"Mount failed: {e}")
    return _public_share(s)


@app.post("/api/shares/{sid}/remount")
async def remount_share(sid: str):
    s = store.shares.get(sid) or _404("Share")
    try:
        await shares.remount(s)
    except (shares.ShareError, asyncio.TimeoutError) as e:
        raise HTTPException(400, f"Remount failed: {e}")
    return _public_share(s)


@app.post("/api/shares/{sid}/unmount")
async def unmount_share(sid: str):
    s = store.shares.get(sid) or _404("Share")
    try:
        await shares.unmount(s)
    except shares.ShareError as e:
        raise HTTPException(400, f"Unmount failed: {e}")
    return _public_share(s)


@app.get("/api/shares/{sid}/browse")
def browse_share(sid: str, path: str = "/"):
    s = store.shares.get(sid) or _404("Share")
    if not shares.is_mounted(s):
        raise HTTPException(409, "Mount the share to browse it")
    try:
        return shares.browse(s, path)
    except shares.ShareError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------- definitions

DEF_FIELDS = ("name", "description", "share_id", "paths", "mode", "sum", "eval", "filters",
              "custom_expression", "custom_columns", "custom_verb", "nonfiles", "display", "export",
              "folders", "expression_override")


def _clean_def(body: dict) -> dict:
    d = {k: body.get(k) for k in DEF_FIELDS if k in body}
    d["paths"] = [p.strip() or "/" for p in (d.get("paths") or ["/"])] or ["/"]
    return d


@app.post("/api/preview")
def preview(body: dict = Body(...)):
    try:
        return reports.preview(_clean_def(body))
    except (reports.DefinitionError, shares.ShareError, ValueError) as e:
        raise HTTPException(422, str(e))


@app.get("/api/definitions")
def list_definitions():
    return store.definitions.all()


@app.get("/api/definitions/{did}")
def get_definition(did: str):
    return store.definitions.get(did) or _404("Report")


def _check_def(d: dict):
    if not (d.get("name") or "").strip():
        raise HTTPException(422, "Give the report a name")
    if not store.shares.get(d.get("share_id") or ""):
        raise HTTPException(422, "Choose a share")
    try:
        reports.build(d)
    except (reports.DefinitionError, ValueError) as e:
        raise HTTPException(422, str(e))


@app.post("/api/definitions")
def create_definition(body: dict = Body(...)):
    d = _clean_def(body)
    _check_def(d)
    return store.definitions.put(d)


@app.put("/api/definitions/{did}")
def update_definition(did: str, body: dict = Body(...)):
    d = store.definitions.get(did) or _404("Report")
    d.update(_clean_def({**d, **body}))
    _check_def(d)
    return store.definitions.put(d)


@app.delete("/api/definitions/{did}")
def delete_definition(did: str):
    store.definitions.delete(did) or _404("Report")
    return {"deleted": did}


def _start(defn: dict, trigger="manual"):
    if (defn.get("expression_override") or "").strip() and defn.get("mode") != "custom":
        trigger = trigger + " (edited HammerScript)"
    run = reports.create_run(defn, trigger=trigger)
    _bg(reports.execute(run))
    return {"run_id": run["id"]}


@app.post("/api/definitions/{did}/run")
async def run_definition(did: str, body: dict | None = Body(None)):
    """Run a saved report. An optional {"expression": "..."} runs it once with that
    HammerScript instead, without changing the saved report."""
    defn = store.definitions.get(did) or _404("Report")
    exp = ((body or {}).get("expression") or "").strip()
    if exp:
        defn = {**defn, "expression_override": exp} if defn.get("mode") != "custom" else \
            {**defn, "custom_expression": exp}
    try:
        reports.build(defn)
    except (reports.DefinitionError, ValueError) as e:
        raise HTTPException(422, str(e))
    return _start(defn)


@app.post("/api/run")
async def run_adhoc(body: dict = Body(...)):
    d = _clean_def(body)
    d.setdefault("name", "Unsaved report")
    if not store.shares.get(d.get("share_id") or ""):
        raise HTTPException(422, "Choose a share")
    try:
        reports.build(d)
    except (reports.DefinitionError, ValueError) as e:
        raise HTTPException(422, str(e))
    return _start(d, trigger="ad hoc")


# -------------------------------------------------------------------- runs

def _override(fields: str | None, sort_by: str | None, sort_desc: bool | None, limit: int | None,
              size_unit: str | None = None):
    o = {}
    if size_unit is not None:
        o["size_unit"] = size_unit
    if fields is not None:
        o["fields"] = [f for f in fields.split(",") if f]
    if sort_by is not None:
        o["sort_by"] = sort_by
    if sort_desc is not None:
        o["sort_desc"] = sort_desc
    if limit is not None:
        o["limit"] = limit
    return o


@app.get("/api/runs")
def list_runs(definition_id: str | None = None, schedule_id: str | None = None, limit: int = 200):
    rs = store.runs.summaries()
    if definition_id:
        rs = [r for r in rs if r.get("definition_id") == definition_id]
    if schedule_id:
        rs = [r for r in rs if r.get("schedule_id") == schedule_id]
    return rs[:limit]


def _saved_export(run: dict) -> dict:
    """Export settings saved on the report now, not the copy taken when the run started.
    Without saved export settings, the report's display size unit is the starting point."""
    current = store.definitions.get(run.get("definition_id") or "") or run.get("definition") or {}
    saved = dict(current.get("export") or {})
    unit = (current.get("display") or {}).get("size_unit")
    if "unit" not in saved and unit in reports.SIZE_UNITS:
        saved["unit"] = unit
    return saved


@app.get("/api/runs/{rid}")
def get_run(rid: str, fields: str | None = None, sort_by: str | None = None,
            sort_desc: bool | None = None, limit: int | None = None, size_unit: str | None = None):
    run = reports.ensure_parsed(store.runs.get(rid) or _404("Run"))
    view = reports.apply_display(run, _override(fields, sort_by, sort_desc, limit, size_unit))
    slim = {k: v for k, v in run.items() if k not in ("rows",)}
    has_files = any(reports._is_top_list(r.get(c)) for r in run.get("rows") or [] for c in r)
    if len(slim.get("outputs") or []) > 60:  # per-folder runs: keep the page light
        slim["outputs_total"] = len(slim["outputs"])
        slim["outputs"] = slim["outputs"][:60]
    return {**slim, "view": view, "has_files": has_files,
            "has_folders": "Folder" in (run.get("columns") or []),
            "export_options": reports.export_options(_saved_export(run))}


@app.get("/api/runs/{rid}/export")
def export_run(rid: str, format: str = Query("csv", pattern="^(csv|json|raw|analysis|files|prometheus)$"),
               fields: str | None = None, sort_by: str | None = None,
               sort_desc: bool | None = None, limit: int | None = None,
               meta: bool | None = None, unit: str | None = None, decimals: int | None = None,
               paths: str | None = None, size_unit: str | None = None):
    run = reports.ensure_parsed(store.runs.get(rid) or _404("Run"))
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in run["name"])[:60]
    if format in ("analysis", "files"):
        saved = _saved_export(run)
        t = reports.analysis_tables(run, {**saved, **{k: v for k, v in {
            "meta": meta, "unit": unit, "decimals": decimals, "paths": paths}.items() if v is not None}})
        cols, rows = t["summary" if format == "analysis" else "files"]
        suffix = "summary" if format == "analysis" else "largest-files"
        return Response(reports.table_csv(cols, rows), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{safe}-{suffix}.csv"'})
    if format == "prometheus":
        return PlainTextResponse(reports.to_prometheus(run), headers={
            "Content-Disposition": f'attachment; filename="{safe}.prom"'})
    view = reports.apply_display(run, _override(fields, sort_by, sort_desc, limit, size_unit))
    if format == "csv":
        return Response(reports.to_csv(view), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{safe}.csv"'})
    if format == "raw":
        text = "\n\n".join(f"# {o['command']}\n{o['stdout']}" for o in run.get("outputs", []))
        return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="{safe}.txt"'})
    body = {"name": run["name"], "started": run["started"], "expression": run.get("expression"),
            "columns": view["columns"], "rows": view["rows"]}
    return Response(json.dumps(body, indent=2, default=str), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.json"'})


@app.delete("/api/runs/{rid}")
def delete_run(rid: str):
    store.runs.delete(rid)
    return {"deleted": rid}


# --------------------------------------------------------------- schedules

SCHED_FIELDS = ("name", "definition_id", "cron", "enabled", "keep_last", "export_csv")


def _public_sched(s: dict) -> dict:
    d = store.definitions.get(s.get("definition_id") or "")
    return {**s, "next_run": scheduler.next_run(s["id"]),
            "definition_name": d["name"] if d else None}


def _check_sched(s: dict):
    if not store.definitions.get(s.get("definition_id") or ""):
        raise HTTPException(422, "Choose a saved report")
    try:
        scheduler.trigger_for(s.get("cron") or "")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/schedules")
def list_schedules():
    return [_public_sched(s) for s in store.schedules.all()]


@app.post("/api/schedules")
def create_schedule(body: dict = Body(...)):
    s = {k: body.get(k) for k in SCHED_FIELDS}
    s.setdefault("enabled", True)
    s["enabled"] = s["enabled"] is not False
    _check_sched(s)
    s = store.schedules.put(s)
    scheduler.sync(s)
    return _public_sched(s)


@app.put("/api/schedules/{sid}")
def update_schedule(sid: str, body: dict = Body(...)):
    s = store.schedules.get(sid) or _404("Schedule")
    s.update({k: body[k] for k in SCHED_FIELDS if k in body})
    _check_sched(s)
    s = store.schedules.put(s)
    scheduler.sync(s)
    return _public_sched(s)


@app.delete("/api/schedules/{sid}")
def delete_schedule(sid: str):
    store.schedules.delete(sid) or _404("Schedule")
    scheduler.remove(sid)
    return {"deleted": sid}


@app.post("/api/schedules/{sid}/run")
async def run_schedule_now(sid: str):
    store.schedules.get(sid) or _404("Schedule")
    _bg(scheduler.run_schedule(sid))
    return {"started": sid}


@app.get("/api/exports")
def list_exports():
    root = reports.EXPORT_DIR
    if not root.exists():
        return []
    files = [p for p in root.rglob("*.csv")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"path": str(p.relative_to(root)), "size": p.stat().st_size,
             "modified": p.stat().st_mtime} for p in files[:500]]


@app.get("/api/exports/{folder}/{name}")
def get_export(folder: str, name: str):
    p = (reports.EXPORT_DIR / folder / name).resolve()
    if reports.EXPORT_DIR.resolve() not in p.parents or not p.is_file():
        _404("Export")
    return FileResponse(p, filename=name, media_type="text/csv")


# ---------------------------------------------------------------- settings



def _settings_view():
    return {"values": settings.get(), "defaults": settings.defaults(), "crawl": settings.crawl(),
            "crawl_presets": settings.CRAWL_PRESETS,
            "limits": {k: {"min": v[1], "max": v[2], "label": v[3]} for k, v in settings.SPEC.items()}}


@app.get("/api/settings")
def get_settings():
    return _settings_view()


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    try:
        settings.update(body)
    except settings.SettingsError as e:
        raise HTTPException(422, str(e))
    return _settings_view()


@app.get("/api/auth")
def get_auth():
    return {**auth.config(), "min_length": auth.MIN_LENGTH}


@app.post("/api/auth/password")
def change_password(body: dict = Body(...)):
    try:
        return auth.change_password(body.get("current"), body.get("new"), body.get("confirm"))
    except auth.AuthError as e:
        raise HTTPException(422, str(e))


@app.post("/api/auth/enable")
def enable_sign_in(body: dict = Body(...)):
    try:
        return auth.enable(body.get("username"), body.get("password"), body.get("confirm"))
    except auth.AuthError as e:
        raise HTTPException(422, str(e))


@app.post("/api/settings/reset")
def reset_settings():
    settings.reset()
    return _settings_view()


# ------------------------------------------------------- objective planning

PLAN_FIELDS = ("name", "share_id", "root", "fields", "metas", "rows", "settings", "cluster_share")


def _plan(pid):
    return objplan.plans.get(pid) or _404("Plan")


def _public_plan(p: dict) -> dict:
    share = store.shares.get(p.get("share_id") or "")
    out = {k: v for k, v in p.items() if k != "result"}
    out["share_name"] = share["name"] if share else None
    out["needed"] = dict(zip(("fields", "metas"), objplan.needed_fields(p)))
    out["checks"] = objplan.validate_rows(p)
    c = p.get("cluster") or {}
    out["cluster_counts"] = {k: len(c.get(k) or []) for k in clusterinfo.PARSERS}
    out["has_result"] = bool(p.get("result"))
    return out


@app.get("/api/plan-catalog")
def plan_catalog():
    return {"fields": {k: {"kind": v[0], "label": v[1]} for k, v in objexpr.FIELDS.items()},
            "walk_fields": sorted(objexpr.WALK_FIELDS), "required": objexpr.REQUIRED_FIELDS,
            "meta_funcs": sorted(objexpr.META_FUNCS), "defaults": objplan.DEFAULT_SETTINGS,
            "crawl": settings.crawl()}


@app.get("/api/plans")
def list_plans():
    out = []
    for p in objplan.plans.all():
        share = store.shares.get(p.get("share_id") or "")
        out.append({"id": p["id"], "name": p.get("name"), "share_name": share["name"] if share else None,
                    "root": p.get("root") or "/", "rows": len(p.get("rows") or []),
                    "scan": {k: (p.get("scan") or {}).get(k) for k in ("status", "files", "finished")},
                    "calculated": (p.get("result") or {}).get("calculated")})
    return out


def _clean_plan(body: dict, current: dict | None = None) -> dict:
    p = {**(current or {}), **{k: body[k] for k in PLAN_FIELDS if k in body}}
    if not (p.get("name") or "").strip():
        raise HTTPException(422, "Give the plan a name")
    p["root"] = "/" + (p.get("root") or "/").strip("/")
    p["fields"] = [f for f in dict.fromkeys(p.get("fields") or []) if f in objexpr.FIELDS]
    metas = []
    for m in p.get("metas") or []:
        chk = objexpr.check(m)
        if not chk["ok"] or not chk["metas"]:
            raise HTTPException(422, f"{m}: use GET_TAG(\"name\"), HAS_LABEL(\"name\") and similar")
        metas.append(chk["metas"][0])
    p["metas"] = list(dict.fromkeys(metas))
    rows = []
    for r in p.get("rows") or []:
        r = {"id": r.get("id") or store.new_id(), "objective": r.get("objective") or "",
             "scope": r.get("scope") or {"type": "share"}, "condition": r.get("condition") or ""}
        rows.append(r)
    p["rows"] = rows
    p["settings"] = {**objplan.DEFAULT_SETTINGS, **(p.get("settings") or {})}
    return p


@app.post("/api/plans")
def create_plan(body: dict = Body(...)):
    p = _clean_plan({"fields": ["MODIFY_AGE"], **body})
    if not store.shares.get(p.get("share_id") or ""):
        raise HTTPException(422, "Choose a share")
    if not p.get("cluster_share"):
        p["cluster_share"] = store.shares.get(p["share_id"])["name"]
    return _public_plan(objplan.plans.put(p))


@app.get("/api/plans/{pid}")
def get_plan(pid: str):
    return _public_plan(_plan(pid))


@app.put("/api/plans/{pid}")
def update_plan(pid: str, body: dict = Body(...)):
    p = _clean_plan(body, _plan(pid))
    return _public_plan(objplan.plans.put(p))


@app.delete("/api/plans/{pid}")
def delete_plan(pid: str):
    objplan.plans.delete(pid) or _404("Plan")
    import shutil
    shutil.rmtree(objplan.PLAN_DIR / pid, ignore_errors=True)
    return {"deleted": pid}


@app.post("/api/plans/{pid}/scan")
async def scan_plan(pid: str):
    p = _plan(pid)
    if (p.get("scan") or {}).get("status") == "running":
        raise HTTPException(409, "A scan is already running")

    async def go():
        try:
            await objplan.scan(objplan.plans.get(pid))
        except Exception as e:
            cur = objplan.plans.get(pid)
            if cur:
                cur["scan"] = {**(cur.get("scan") or {}), "status": "failed", "error": str(e), "phase": None,
                               "finished": store.now()}
                objplan.plans.put(cur)

    p["scan"] = {**(p.get("scan") or {}), "status": "running", "phase": "starting", "error": None}
    objplan.plans.put(p)
    _bg(go())
    return {"started": pid}


@app.post("/api/plans/{pid}/cluster")
def upload_cluster(pid: str, body: dict = Body(...)):
    p = _plan(pid)
    try:
        kind, items = clusterinfo.parse_any(body.get("text") or "", body.get("kind") or None)
    except ValueError as e:
        raise HTTPException(422, str(e))
    c = p.get("cluster") or {}
    c[kind] = items
    c.setdefault("uploaded", {})[kind] = store.now()
    p["cluster"] = c
    p.pop("result", None)
    objplan.plans.put(p)
    return {"kind": kind, "count": len(items), "plan": _public_plan(p)}


@app.delete("/api/plans/{pid}/cluster/{kind}")
def clear_cluster(pid: str, kind: str):
    p = _plan(pid)
    (p.get("cluster") or {}).pop(kind, None)
    objplan.plans.put(p)
    return _public_plan(p)


@app.post("/api/plans/{pid}/check")
def check_condition(pid: str, body: dict = Body(...)):
    p = _plan(pid)
    cond = body.get("condition") or ""
    res = objexpr.check(cond) if cond.strip() else {"ok": True, "fields": [], "metas": []}
    gathered = set((p.get("scan") or {}).get("fields") or []) | objexpr.WALK_FIELDS
    res["not_scanned"] = [f for f in res.get("fields", []) + res.get("metas", []) if f not in gathered]
    if res["ok"] and p.get("scan") and not res["not_scanned"]:
        res["matches"] = objplan.preview(p, cond, body.get("scope"))
    return res


@app.get("/api/plans/{pid}/folders")
def plan_folders(pid: str, path: str = "/"):
    return {"path": "/" + path.strip("/"), "dirs": objplan.folders(_plan(pid), path)}


@app.post("/api/plans/{pid}/calculate")
async def calculate_plan(pid: str):
    p = _plan(pid)
    try:
        res = await asyncio.to_thread(objplan.calculate, p)
    except objplan.PlanError as e:
        raise HTTPException(422, str(e))
    p = _plan(pid)
    p["result"] = res
    objplan.plans.put(p)
    return {**res, "commands": objplan.commands(p)}


@app.get("/api/plans/{pid}/result")
def plan_result(pid: str):
    p = _plan(pid)
    if not p.get("result"):
        _404("Result")
    return {**p["result"], "commands": objplan.commands(p)}


@app.get("/api/plans/{pid}/commands")
def plan_commands(pid: str):
    p = _plan(pid)
    return PlainTextResponse("\n".join(objplan.commands(p)) + "\n", headers={
        "Content-Disposition": f'attachment; filename="{(p.get("name") or "plan").replace(" ", "_")}-objectives.txt"'})


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
