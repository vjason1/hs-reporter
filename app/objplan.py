"""Objective planning: scan a share for the metadata objective conditions need, model where the
data would be placed under a proposed set of share objectives, and project space per volume.

The placement model is an estimate of Hammerspace's behaviour, not the cluster's own engine:

  * Each place-on objective that applies to a file needs one instance in its target (a volume
    group means any one of its volumes). Several place-on objectives mean several instances.
  * confine-to limits every instance to its target; exclude-from keeps instances out of it.
  * availability-N / durability-N add instances, each in a different failure domain (storage
    node), until the combined value 1 - prod(1 - v) reaches the target. With volumes at 99%
    availability, availability-3-nines needs two instances.
  * keep-online needs an instance on a volume with no online delay; performance-* needs one on a
    volume with enough IOPS.
  * Without optimize-for-capacity, a local (file storage) instance is kept as well; with it,
    only instances some objective requires remain.
  * Files no placement objective covers use the default placement (one instance).
  * Read-only volumes are never targets (data moves off them). Files under the object-volume
    size threshold (40 bytes) are kept in the cluster metadata instead of on object volumes.
  * Within a group, each instance goes to the eligible volume with the most free space left.
"""
import asyncio
import json
import math
import os
import shlex
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import clusterinfo, objexpr, reports, settings, shares, store
from .hsvalue import Time, Typed, parse_stream

PLAN_DIR = store.DATA_DIR / "plans"
BLOCK = 4096
plans = store.Collection("plans")

PLACEMENT_EFFECTS = {"place", "confine", "exclude", "availability", "durability", "online",
                     "optimize_capacity", "performance", "do_not_move"}
DEFAULT_SETTINGS = {"object_min_bytes": 40, "size_basis": "size", "default_placement": None,
                    "keep_local_without_optimize": True}


class PlanError(ValueError):
    pass


# ------------------------------------------------------------------ storage

def items_path(pid: str) -> Path:
    return PLAN_DIR / pid / "items.jsonl"


def _jsonable(v):
    if isinstance(v, Time):
        return float(v)
    if isinstance(v, Typed):
        return str(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


_cache: dict[str, tuple[float, list]] = {}


def load_items(pid: str):
    """Scanned inventory, cached in memory until the file changes."""
    p = items_path(pid)
    if not p.exists():
        return []
    mtime = p.stat().st_mtime
    hit = _cache.get(pid)
    if hit and hit[0] == mtime:
        return hit[1]
    with open(p) as f:
        items = [json.loads(line) for line in f if line.strip()]
    _cache.clear()  # keep one plan in memory
    _cache[pid] = (mtime, items)
    return items


def share_path(root: str, p: str) -> str:
    """A path within the modeled folder -> the same path from the share's root."""
    base = "/" + (root or "/").strip("/")
    return p if base == "/" else base + p


def file_item(it: dict, root: str = "/") -> dict:
    """A scanned file as the values a condition sees. PATH is from the share's root, as
    Hammerspace sees it ("./projects/a/file.txt"), so FNMATCH('*/projects/*', PATH) works
    whichever folder the plan models."""
    v = it.get("v") or {}
    path = "." + share_path(root, it["p"])
    item = {**v, "NAME": it["n"], "PATH": path, "DPATH": path,
            "IS_FILE": not it.get("l"), "IS_SYMLINK": bool(it.get("l")), "IS_DIRECTORY": False}
    for lbl in v.get("ALL_LABELS") or []:
        item[objexpr.meta_key("HAS_LABEL", lbl)] = True
    return item


def preview(plan: dict, condition: str, scope: dict | None) -> dict:
    """How many scanned files a condition (within a scope) matches."""
    cond = objexpr.Condition(condition) if (condition or "").strip() else None
    now = float((plan.get("scan") or {}).get("scanned_at") or time.time())
    n = b = 0
    examples = []
    for it in load_items(plan["id"]):
        if it.get("d") or it.get("v") is None or not _scope_match(scope, it["p"]):
            continue
        if cond is None or cond.evaluate(file_item(it, plan.get("root")), now):
            n += 1
            b += int((it["v"] or {}).get("SIZE") or 0)
            if len(examples) < 5:
                examples.append(it["p"])
    return {"files": n, "bytes": b, "examples": examples}


def folders(plan: dict, path: str = "/") -> list[str]:
    """Scanned folders directly inside path, for picking a scope."""
    base = "/" + path.strip("/")
    prefix = "" if base == "/" else base
    out = []
    for it in load_items(plan["id"]):
        if it.get("d") and it["p"].rsplit("/", 1)[0] == prefix:
            out.append(it["n"])
    return sorted(out, key=str.lower)


def _labels(v):
    """ALL_LABELS (a LABELS_TABLE) as a list of label names, for HAS_LABEL()."""
    if isinstance(v, dict):
        v = [v]
    names = []
    for row in v or []:
        if isinstance(row, dict):
            names += [str(x) for x in row.values()]
        elif isinstance(row, list):
            names += [str(x) for x in row]
        else:
            names.append(str(row))
    return names


# ------------------------------------------------------------------ scan

def _walk(root: Path, list_rate: float, limit: int, on_progress=None):
    """Every folder and file below root. Folder names matter: paths and scopes refer to them."""
    entries, truncated = [], False
    interval = 1.0 / list_rate if list_rate else 0
    last = 0.0
    stack = [root]
    while stack:
        cur = stack.pop()
        if interval:
            wait = last + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            last = time.monotonic()
        try:
            with os.scandir(cur) as it:
                ents = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for e in ents:
            if e.name in (".snapshot", ".fsnapshot"):
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                is_link = e.is_symlink()
            except OSError:
                continue
            rel = "/" + os.path.relpath(e.path, root).replace(os.sep, "/")
            entries.append({"p": rel, "n": e.name, "d": is_dir, "l": is_link})
            if is_dir:
                stack.append(Path(e.path))
            if len(entries) >= limit:
                return entries, True
        if on_progress:
            on_progress(len(entries))
    return entries, truncated


def _record_values(value, fields):
    """A tuple from {DPATH,F1,F2,...} -> (dpath, {F1: v1, ...})."""
    if not isinstance(value, list) or not value or not isinstance(value[0], str):
        return None, None
    vals = {}
    for i, f in enumerate(fields):
        v = value[i + 1] if i + 1 < len(value) else None
        vals[f] = _labels(v) if f == "ALL_LABELS" else _jsonable(v)
    return value[0], vals


def _norm_dpath(dpath: str) -> str:
    d = dpath.strip()
    if d.startswith("./"):
        d = d[1:]
    elif d == ".":
        d = "/"
    elif not d.startswith("/"):
        d = "/" + d
    return d.rstrip("/") or "/"


async def scan(plan: dict) -> dict:
    """Walk the folder, then gather only the chosen fields with hs eval."""
    pid = plan["id"]
    share = store.shares.get(plan.get("share_id") or "")
    if not share:
        raise PlanError("Choose a share")
    if not shares.is_mounted(share):
        await shares.mount(share)
    root = shares.resolve_in_share(share, plan.get("root") or "/")
    if not root.is_dir():
        raise PlanError(f"{plan.get('root')} isn't a folder on {share['name']}")
    th = settings.crawl()
    max_files, batch = settings.get()["plan_max_files"], settings.get()["plan_batch"]
    fields = [f for f in dict.fromkeys(objexpr.REQUIRED_FIELDS + list(plan.get("fields") or []))
              if f in objexpr.FIELDS and f not in objexpr.WALK_FIELDS]
    metas = list(dict.fromkeys(plan.get("metas") or []))
    for m in metas:
        objexpr.Condition(m)  # validates GET_TAG("x") etc.
    exp = objexpr.scan_expression(fields, metas)
    gathered = fields + metas

    st = {"status": "running", "phase": "walking", "started": store.now(), "expression": exp,
          "fields": gathered, "files": 0, "folders": 0, "errors": 0, "method": None}

    def save(**kw):
        st.update(kw)
        plan["scan"] = dict(st)
        plans.put(plan)

    save()
    progress = {"t": time.time()}

    def walked(n):
        if time.time() - progress["t"] > 2:
            progress["t"] = time.time()
            save(found=n)

    entries, cut = await asyncio.to_thread(_walk, root, th["list_rate"], max_files, walked)
    files = [e for e in entries if not e["d"]]
    save(phase="gathering", files=len(files), folders=len(entries) - len(files), truncated=cut, done=0)
    by_path = {e["p"]: e for e in files}
    values: dict[str, dict] = {}
    samples = []

    # 1) One recursive evaluation: a single gateway call for the whole tree (eval_rec).
    if files:
        cmd = [reports.HS_BIN, "eval", "-r", "-e", exp, str(root)]
        res = await reports._run_hs(cmd, root, share)
        if res["rc"] == 0:
            try:
                for _, v in parse_stream(res["stdout"]):
                    dpath, vals = _record_values(v, gathered)
                    if dpath is not None and _norm_dpath(dpath) in by_path:
                        values[_norm_dpath(dpath)] = vals
            except ValueError:
                pass
        if res["stderr"].strip():
            samples.append(res["stderr"].strip()[-400:])
        st["method"] = "recursive" if values else None

    # 2) Batched per-file evaluation for anything the recursive call didn't return.
    missing = [e["p"] for e in files if e["p"] not in values]
    if missing:
        st["method"] = "recursive + per-file" if values else "per-file"
        batches = [missing[i:i + batch] for i in range(0, len(missing), batch)]
        sem = asyncio.Semaphore(th["concurrency"])
        done = {"n": len(values), "t": time.time()}

        async def run_batch(paths):
            async with sem:
                full = [str(root / p.lstrip("/")) for p in paths]
                res = await reports._run_hs([reports.HS_BIN, "eval", "-e", exp, *full], root, share)
                if th["pause"]:
                    await asyncio.sleep(th["pause"])
            if res["rc"] != 0 and not res["stdout"].strip():
                st["errors"] += len(paths)
                if len(samples) < 3:
                    samples.append((res["stderr"] or f"exit {res['rc']}").strip()[-400:])
                return
            try:
                parsed = list(parse_stream(res["stdout"]))
            except ValueError as e:
                st["errors"] += len(paths)
                if len(samples) < 3:
                    samples.append(f"Couldn't parse hs output: {e}")
                return
            for i, (header, v) in enumerate(parsed):
                target = None
                if header:
                    target = "/" + os.path.relpath(header, root).replace(os.sep, "/")
                elif len(paths) == 1:
                    target = paths[0]
                _, vals = _record_values(v, gathered)
                if target in by_path and vals is not None:
                    values[target] = vals
            done["n"] = len(values)
            if time.time() - done["t"] > 2:
                done["t"] = time.time()
                save(done=done["n"])

        await asyncio.gather(*(run_batch(b) for b in batches))

    # 3) Write the inventory: folders (for paths) and files with their values.
    p = items_path(pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    total = 0
    with open(tmp, "w") as f:
        for e in entries:
            rec = {"p": e["p"], "n": e["n"], "d": e["d"]}
            if not e["d"]:
                v = values.get(e["p"])
                rec["v"] = v
                if v and isinstance(v.get("SIZE"), (int, float)):
                    total += v["SIZE"]
                rec["l"] = e["l"]
            f.write(json.dumps(rec) + "\n")
    os.replace(tmp, p)
    got = sum(1 for e in files if e["p"] in values)
    status = "done" if got == len(files) else ("partial" if got else ("done" if not files else "failed"))
    save(status=status, phase=None, crawl=th, finished=store.now(), scanned_at=store.now(), done=got,
         gathered=got, bytes=total, error_samples=samples,
         error=None if status != "failed" else ("hs returned no metadata. " + (samples[0] if samples else "")))
    return plan


# ------------------------------------------------------------------ cluster model

def cluster_model(plan: dict) -> dict:
    c = plan.get("cluster") or {}
    vols = {}
    for v in (c.get("volumes") or []) + (c.get("object_volumes") or []):
        vols[v["name"]] = v
    alias = {}
    for v in vols.values():
        for a in v.get("aliases") or []:
            alias[a] = v["name"]
        alias[v["name"]] = v["name"]
    groups = {}
    for g in c.get("volume_groups") or []:
        groups[g["name"]] = [alias.get(m, m) for m in g["volumes"]]
    # volumes also list their groups: include memberships the group export may not resolve
    for v in vols.values():
        for g in v.get("groups") or []:
            groups.setdefault(g, [])
            if v["name"] not in groups[g]:
                groups[g].append(v["name"])
    objectives = {o["name"]: o for o in c.get("objectives") or []}
    writable = {n for n, v in vols.items() if not v.get("read_only")
                and (v.get("oper_state") or "Up").lower() != "down"}
    return {"vols": vols, "alias": alias, "groups": groups, "objectives": objectives, "writable": writable}


def _resolve(targets, model):
    """[('group','x'),('volume','y')] -> set of volume names, plus names that weren't found."""
    out, missing = set(), []
    for kind, name in targets:
        if kind == "group":
            if name in model["groups"]:
                out |= set(model["groups"][name])
            else:
                missing.append(f"volume group {name}")
        else:
            v = model["alias"].get(name)
            if v:
                out.add(v)
            else:
                missing.append(f"volume {name}")
    return out, missing


def _combined(vals):
    p = 1.0
    for v in vals:
        p *= (1 - (v or 0))
    return 1 - p


# ------------------------------------------------------------------ calculate

def _scope_match(scope, path):
    t = (scope or {}).get("type", "share")
    if t == "share":
        return True
    folder = "/" + (scope.get("path") or "").strip("/")
    if t == "folder":
        return folder == "/" or path == folder or path.startswith(folder + "/")
    if t == "file":
        base = folder.rstrip("/") if folder != "/" else ""
        return path == f"{base}/{scope.get('name', '')}"
    return False


def _scope_text(scope):
    t = (scope or {}).get("type", "share")
    if t == "share":
        return "whole share"
    if t == "folder":
        return "folder " + ("/" + (scope.get("path") or "").strip("/"))
    return "file " + ("/" + (scope.get("path") or "").strip("/")).rstrip("/") + "/" + scope.get("name", "")


def validate_rows(plan, model=None):
    """Per-row checks used by the GUI and before calculating."""
    model = model or cluster_model(plan)
    gathered = set((plan.get("scan") or {}).get("fields") or []) | objexpr.WALK_FIELDS
    out = []
    for r in plan.get("rows") or []:
        info = {"id": r["id"], "errors": [], "warnings": [], "fields": [], "metas": []}
        o = model["objectives"].get(r.get("objective"))
        if not o:
            info["errors"].append("Objective not in the uploaded objective list" if model["objectives"]
                                  else "Upload objective-list --full to check objectives")
        else:
            for key in ("place_on", "confine_to", "exclude_from"):
                vs, missing = _resolve(o[key], model)
                for m in missing:
                    info["warnings"].append(f"{m} isn't in the uploaded cluster info")
                if key == "place_on" and o[key] and not (vs & model["writable"]) and not missing:
                    info["warnings"].append("No writable volume in its target, so it can't be satisfied")
            if "expression" in o["effects"]:
                info["warnings"].append("Expression-based objective: its own conditions aren't modeled")
            if not (set(o["effects"]) & PLACEMENT_EFFECTS) and "expression" not in o["effects"]:
                info["notes"] = "No effect on placement or space"
        cond = (r.get("condition") or "").strip()
        if cond:
            chk = objexpr.check(cond)
            if not chk["ok"]:
                info["errors"].append(f"Condition: {chk['error']}")
            else:
                info["fields"], info["metas"] = chk["fields"], chk["metas"]
                not_scanned = [f for f in chk["fields"] + chk["metas"] if f not in gathered]
                if not_scanned and plan.get("scan"):
                    info["errors"].append("Not gathered by the scan: " + ", ".join(not_scanned)
                                          + ". Add them to the fields and scan again.")
        out.append(info)
    return out


def needed_fields(plan) -> tuple[list[str], list[str]]:
    fields, metas = set(), set()
    for r in plan.get("rows") or []:
        chk = objexpr.check(r.get("condition") or "") if (r.get("condition") or "").strip() else None
        if chk and chk["ok"]:
            fields |= set(chk["fields"])
            metas |= set(chk["metas"])
    return sorted(fields - objexpr.WALK_FIELDS), sorted(metas)


def calculate(plan: dict) -> dict:
    model = cluster_model(plan)
    if not model["vols"]:
        raise PlanError("Upload volume-list --full and/or object-volume-list --full first")
    if not model["objectives"]:
        raise PlanError("Upload objective-list --full first")
    if not plan.get("scan") or plan["scan"].get("status") not in ("done", "partial"):
        raise PlanError("Scan the share first")
    checks = validate_rows(plan, model)
    errs = [f"Row {i + 1}: {e}" for i, c in enumerate(checks) for e in c["errors"]]
    if errs:
        raise PlanError("Fix these first: " + "; ".join(errs))

    s = {**DEFAULT_SETTINGS, **(plan.get("settings") or {})}
    now = float(plan["scan"].get("scanned_at") or time.time())
    vols, writable = model["vols"], model["writable"]

    # default placement for files no placement objective covers
    dp = s.get("default_placement")
    if dp:
        default_set, missing = _resolve([(dp.get("kind", "group"), dp.get("name"))], model)
        default_set &= writable
    else:
        default_set = {n for n in writable if vols[n]["kind"] == "storage"} or set(writable)

    rows = []
    for r in plan.get("rows") or []:
        o = model["objectives"][r["objective"]]
        cond = objexpr.Condition(r["condition"]) if (r.get("condition") or "").strip() else None
        place = [(t, _resolve([t], model)[0]) for t in o["place_on"]]
        rows.append({"row": r, "obj": o, "cond": cond, "scope": r.get("scope") or {"type": "share"},
                     "placement": bool(set(o["effects"]) & PLACEMENT_EFFECTS),
                     "place": place, "confine": _resolve(o["confine_to"], model)[0] if o["confine_to"] else None,
                     "exclude": _resolve(o["exclude_from"], model)[0] if o["exclude_from"] else set()})

    # Volume groups the plan places data on: results are shown per group rather than per volume.
    targeted = {name for x in rows for (kind, name), _ in x["place"] if kind == "group" and name in model["groups"]}
    if dp and dp.get("kind", "group") == "group" and dp.get("name") in model["groups"]:
        targeted.add(dp["name"])
    by_size = sorted(targeted, key=lambda g: (len(model["groups"][g]), g))
    tgt_needed = defaultdict(int)
    tgt_inst = Counter()
    tgt_files = Counter()

    free = {n: (v["free"] if v.get("free") is not None else math.inf) for n, v in vols.items()}
    proj = defaultdict(int)          # volume -> projected bytes from this share
    inst_count = Counter()           # volume -> instances
    file_count = Counter()           # volume -> files with an instance there
    copies_hist = Counter()
    row_hits = Counter()
    row_bytes = Counter()
    uncovered = []
    uncovered_bytes = 0
    uncovered_files = 0
    issues = defaultdict(lambda: {"files": 0, "bytes": 0, "examples": []})
    meta_files = meta_bytes = 0
    unchanged_files = unchanged_bytes = 0
    files_total = bytes_total = 0
    unknown = 0

    def issue(key, it, size):
        b = issues[key]
        b["files"] += 1
        b["bytes"] += size
        if len(b["examples"]) < 5:
            b["examples"].append(it["p"])

    def pick(cands, used_domains, prefer=None):
        """Eligible volume with the most free space left, preferring new failure domains."""
        cands = [c for c in cands if c in writable]
        if not cands:
            return None
        fresh = [c for c in cands if vols[c]["node"] not in used_domains] or cands
        if prefer:
            pref = [c for c in fresh if c in prefer]
            fresh = pref or fresh
        return max(fresh, key=lambda c: (free[c], c))

    for it in load_items(plan["id"]):
        if it.get("d"):
            continue
        v = it.get("v")
        files_total += 1
        if v is None:
            unknown += 1
            continue
        size = int(v.get("SIZE") or 0)
        bytes_total += size
        item = file_item(it, plan.get("root"))

        applied = [x for x in rows if _scope_match(x["scope"], it["p"])
                   and (x["cond"] is None or x["cond"].evaluate(item, now))]
        for x in applied:
            row_hits[x["row"]["id"]] += 1
            row_bytes[x["row"]["id"]] += size
        placing = [x for x in applied if x["placement"]]
        if not placing:
            uncovered_files += 1
            uncovered_bytes += size
            if len(uncovered) < 10:
                uncovered.append(it["p"])
        objs = [x["obj"] for x in applied]

        if any(o["do_not_move"] for o in objs):
            unchanged_files += 1
            unchanged_bytes += size
            continue

        allowed = set(writable)
        for x in applied:
            allowed -= x["exclude"]
            if x["confine"] is not None:
                allowed &= x["confine"]
        if not allowed:
            issue("No volume allowed (confine-to and exclude-from leave nothing)", it, size)
            continue

        instances = []      # volume names
        labels = []         # what each instance was placed for: ("group"|"volume", name) or None
        domains = set()

        def add(cands, prefer=None, label=None):
            vname = pick(set(cands) & allowed, domains, prefer)
            if vname:
                instances.append(vname)
                labels.append(label)
                domains.add(vols[vname]["node"])
            return vname

        # place-on: one instance per objective, unless an existing one already satisfies it
        for x in applied:
            for (tkind, tname), target in x["place"]:
                if set(instances) & target:
                    continue
                if not add(target, label=(tkind, tname) if tkind == "group" else None):
                    issue(f"{x['obj']['name']}: no allowed writable volume in its target", it, size)

        # keep-online / allowed online delay
        delays = [o["online_delay"] for o in objs if o["online_delay"] is not None]
        if delays:
            limit = min(delays)
            if not any((vols[i].get("online_delay") or 0) <= limit for i in instances):
                if not add({n for n in allowed if (vols[n].get("online_delay") or 0) <= limit}, default_set):
                    issue("keep-online: no allowed volume without an online delay", it, size)

        # performance
        need_iops = max([o.get("read_min_iops") or 0 for o in objs] + [0])
        if need_iops:
            if not any((vols[i].get("read_iops") or 0) >= need_iops for i in instances):
                fast = {n for n in allowed if (vols[n].get("read_iops") or 0) >= need_iops}
                if not add(fast):
                    issue(f"Performance objective: no volume reaches {int(need_iops):,} read IOPS", it, size)

        # baseline: files with no placement instance yet go to the default placement
        optimize = any(o["optimize_for_capacity"] for o in objs)
        if not instances:
            dlabel = ("group", dp["name"]) if dp and dp.get("kind", "group") == "group" else None
            if not add(default_set, label=dlabel) and not add(allowed):
                issue("No writable volume available", it, size)
                continue
        elif not optimize and s.get("keep_local_without_optimize", True):
            # a local copy is kept unless optimize-for-capacity says otherwise
            if not any(vols[i]["kind"] == "storage" for i in instances):
                local = {n for n in allowed if vols[n]["kind"] == "storage"}
                if local:
                    add(local, default_set)

        # availability / durability: more instances in other failure domains
        for key, label in (("availability", "Availability"), ("durability", "Durability")):
            target = max([o[key] or 0 for o in objs] + [0])
            if not target:
                continue
            first_group = default_set
            while _combined(max((vols[i].get(key) or 0 for i in instances if vols[i]["node"] == d), default=0)
                            for d in {vols[i]["node"] for i in instances}) < target - 1e-12:
                cands = {n for n in allowed if vols[n]["node"] not in domains}
                if not cands:
                    issue(f"{label} target can't be met: not enough separate storage systems", it, size)
                    break
                same_kind = {n for n in cands if vols[n]["kind"] == vols[instances[0]]["kind"]} if instances else cands
                add(same_kind or cands, first_group)

        copies_hist[len(instances)] += 1
        # Instances added for other reasons (a local copy, an availability copy, default placement)
        # count toward a targeted group their volume is in: this file's own group first.
        mine = [l[1] for l in labels if l and l[0] == "group"]
        for i, vname in enumerate(instances):
            if labels[i] is None:
                g = next((g for g in mine if vname in model["groups"][g]), None) or \
                    next((g for g in by_size if vname in model["groups"][g]), None)
                labels[i] = ("group", g) if g else ("volume", vname)
        for label in set(labels):
            tgt_files[label] += 1
        for i, vname in enumerate(instances):
            vol = vols[vname]
            if vol["kind"] == "object":
                if size < s["object_min_bytes"]:
                    meta_files += 1
                    meta_bytes += size
                    use = 0
                else:
                    use = size
            else:
                if s["size_basis"] == "space_used" and isinstance(v.get("SPACE_USED"), (int, float)):
                    use = int(v["SPACE_USED"])
                else:
                    use = int(math.ceil(size / BLOCK) * BLOCK) if size else 0
            proj[vname] += use
            tgt_needed[labels[i]] += use
            tgt_inst[labels[i]] += 1
            free[vname] -= use
            inst_count[vname] += 1
            file_count[vname] += 1

    # --- results
    vol_rows = []
    for n, v in sorted(vols.items(), key=lambda kv: (kv[1]["kind"], kv[0])):
        p = proj.get(n, 0)
        vol_rows.append({
            "name": n, "kind": v["kind"], "node": v["node"], "groups": v.get("groups") or [],
            "read_only": bool(v.get("read_only")), "excluded": n not in writable,
            "total": v.get("total"), "used": v.get("used"), "free": v.get("free"),
            "projected": p, "instances": inst_count.get(n, 0), "files": file_count.get(n, 0),
            "pct_of_free": (p / v["free"]) if v.get("free") else None,
            "over": bool(v.get("free") is not None and p > v["free"]),
            "availability": v.get("availability"), "durability": v.get("durability"),
            "online_delay": v.get("online_delay"),
        })

    # Per target: one row per volume group the plan places data on (free space totalled across its
    # writable volumes), and one row per volume outside those groups.
    vinfo = {v["name"]: v for v in vol_rows}
    in_groups = set()
    targets = []
    for g in sorted(targeted, key=lambda g: (-tgt_needed.get(("group", g), 0), g)):
        members = [m for m in model["groups"][g] if m in writable]
        in_groups |= set(members)
        frees = [vols[m].get("free") for m in members]
        gfree = None if any(f is None for f in frees) else sum(frees)
        totals = [vols[m].get("total") for m in members]
        need = tgt_needed.get(("group", g), 0)
        targets.append({"kind": "group", "name": g, "members": members,
                        "member_kinds": sorted({vols[m]["kind"] for m in members}),
                        "free": gfree, "total": None if any(t is None for t in totals) else sum(totals),
                        "projected": need, "instances": tgt_inst.get(("group", g), 0),
                        "files": tgt_files.get(("group", g), 0),
                        "pct_of_free": (need / gfree) if gfree else None,
                        "over": bool(gfree is not None and need > gfree)})
    for v in vol_rows:
        if v["excluded"] or v["name"] in in_groups:
            continue
        label = ("volume", v["name"])
        targets.append({"kind": "volume", "name": v["name"], "volume_kind": v["kind"], "node": v["node"],
                        "members": [v["name"]], "free": v["free"], "total": v["total"],
                        "projected": tgt_needed.get(label, 0), "instances": tgt_inst.get(label, 0),
                        "files": tgt_files.get(label, 0), "pct_of_free": v["pct_of_free"], "over": v["over"]})
    not_targets = [{"name": v["name"], "kind": v["kind"], "node": v["node"], "read_only": v["read_only"]}
                   for v in vol_rows if v["excluded"]]

    warnings = []
    placement_rows = [x for x in rows if x["placement"]]
    result_uncovered = {"files": uncovered_files, "bytes": uncovered_bytes, "examples": uncovered}
    if placement_rows and result_uncovered["files"]:
        result_uncovered["suggestion"] = _catch_all(rows)
        warnings.append({"level": "warn", "kind": "coverage",
                         "text": f"{result_uncovered['files']:,} files ({_h(uncovered_bytes)}) aren't targeted by any "
                                 "placement objective, so they fall back to the default placement. Add an objective "
                                 "for the rest of the files, for example with the suggested condition."})
    elif not placement_rows:
        warnings.append({"level": "info", "kind": "coverage",
                         "text": "No placement objectives: every file uses the default placement."})
    for k, b in issues.items():
        warnings.append({"level": "warn", "kind": "unsatisfiable", "text": f"{k}: {b['files']:,} files ({_h(b['bytes'])})",
                         "examples": b["examples"]})
    for t in targets:
        if t["kind"] == "group" and t["over"]:
            warnings.append({"level": "error", "kind": "capacity",
                             "text": f"Volume group {t['name']} would need {_h(t['projected'])} but its volumes have "
                                     f"{_h(t['free'])} free in total"})
    for vr in vol_rows:
        if vr["over"]:
            warnings.append({"level": "error", "kind": "capacity",
                             "text": f"{vr['name']} would need {_h(vr['projected'])} but has {_h(vr['free'])} free"})
    if unknown:
        warnings.append({"level": "warn", "kind": "scan",
                         "text": f"{unknown:,} files have no metadata from the scan and weren't modeled"})
    ro = [v["name"] for v in vol_rows if v["read_only"]]
    if ro:
        warnings.append({"level": "info", "kind": "readonly",
                         "text": "Read-only volumes aren't used as targets; data on them is assumed to move: " + ", ".join(ro)})

    row_stats = [{"id": x["row"]["id"], "files": row_hits.get(x["row"]["id"], 0),
                  "bytes": row_bytes.get(x["row"]["id"], 0)} for x in rows]
    total_proj = sum(proj.values())
    res = {
        "calculated": store.now(), "as_of": now, "files": files_total, "bytes": bytes_total,
        "unknown_files": unknown, "volumes": vol_rows, "targets": targets, "not_targets": not_targets,
        "rows": row_stats,
        "copies": {str(k): c for k, c in sorted(copies_hist.items())},
        "instances": sum(inst_count.values()), "projected": total_proj,
        "metadata_files": meta_files, "metadata_bytes": meta_bytes,
        "unchanged_files": unchanged_files, "unchanged_bytes": unchanged_bytes,
        "uncovered": result_uncovered, "warnings": warnings,
        "default_placement": sorted(default_set), "settings": s,
    }
    return res


def _catch_all(rows):
    """A condition matching the files the share-wide placement rows miss."""
    share_rows = [x for x in rows if x["placement"] and (x["scope"] or {}).get("type", "share") == "share"]
    conds = [x["row"]["condition"].strip() for x in share_rows if (x["row"].get("condition") or "").strip()]
    if len(conds) != len(share_rows) or not conds:
        return None
    return "NOT (" + " OR ".join(f"({c})" for c in conds) + ")"


def _h(n):
    if n is None:
        return "unlimited"
    for u in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000 or u == "PB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1000


# ------------------------------------------------------------------ commands

def _q(text: str) -> str:
    """Double-quote for the Hammerspace CLI, as in its documentation's examples."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def commands(plan: dict) -> list[str]:
    """share-objective-add commands for the plan, per the CLI's own help:

        share-objective-add --name <share> --objective <objective> [--path <path in share>]
                            [--applicability <statement>]

    --name is always the share; --path is a folder or file within it (omitted for the whole
    share); a row's condition is the applicability statement (TRUE when omitted)."""
    share = plan.get("cluster_share") or "<share>"
    out = []
    for r in plan.get("rows") or []:
        cmd = f"share-objective-add --name {_q(share)} --objective {_q(r['objective'])}"
        sc = r.get("scope") or {"type": "share"}
        if sc.get("type") in ("folder", "file"):
            # scopes are chosen within the modeled folder; the cluster needs paths from the share root
            folder = share_path(plan.get("root"), "/" + (sc.get("path") or "").strip("/")).rstrip("/")
            path = f"{folder}/{sc.get('name') or ''}" if sc["type"] == "file" else folder
            if path:  # a folder scope at the share's root is the share itself: no --path
                cmd += f" --path {_q(path)}"
        cond = (r.get("condition") or "").strip()
        if cond:
            cmd += f" --applicability {_q(cond)}"
        out.append(cmd)
    return out
