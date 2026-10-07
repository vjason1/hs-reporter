"""Report building and execution.

Every expression fragment here is taken from hstk's own built-in reports
(hstk/hscli.py: usage owner/user/mime/alignment/volume/objectives/virus-scan,
status open/errors, dump volumes/objectives) so the generated HammerScript
matches forms Hammerspace is known to accept.
"""
import asyncio
import csv
import io
import json
import os
import re
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path

from . import hsparse, shares, store

HS_BIN = os.environ.get("HSR_HS_BIN", "hs")
RUN_TIMEOUT = int(os.environ.get("HSR_RUN_TIMEOUT", "3600"))
MAX_OUTPUT = int(os.environ.get("HSR_MAX_OUTPUT_MB", "50")) * 1024 * 1024
MAX_CONCURRENT = int(os.environ.get("HSR_MAX_CONCURRENT", "2"))
EXPORT_DIR = store.DATA_DIR / "exports"

_sem: asyncio.Semaphore | None = None

# Display/export size units. Decimal, matching Hammerspace's KBYTES/MBYTES.
DISPLAY_UNITS = {"auto": None, "bytes": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12}
UNIT_LABEL = {"bytes": "bytes", "kb": "KB", "mb": "MB", "gb": "GB", "tb": "TB"}

# ---------------------------------------------------------------- catalog

# Scalar group-by keys (can be combined, e.g. owner + group -> KEY={OWNER,OWNER_GROUP})
GROUP_KEYS = {
    "owner":      {"label": "Owner", "exp": "OWNER"},
    "group":      {"label": "Group", "exp": "OWNER_GROUP"},
    "mime":       {"label": "MIME type", "exp": "ATTRIBUTES.MIME.STRING"},
    "type":       {"label": "File type", "exp": "TYPE"},
    "alignment":  {"label": "Alignment", "exp": "OVERALL_ALIGNMENT"},
    "virus_scan": {"label": "Virus scan state", "exp": "ATTRIBUTES.VIRUS_SCAN"},
    "errors":     {"label": "Error state", "exp": "ERRORS"},
}
# Row-expanding group-by keys (one file can land in several buckets). Storage volume can
# be combined with scalar keys (KEY={OWNER,OWNER_GROUP,INSTANCES[PARENT.ROW].VOLUME});
# active objective is used on its own.
ROW_GROUPS = {
    "objective": {"label": "Active objective"},
    "volume":    {"label": "Storage volume"},
}
EXCLUSIVE_GROUPS = {"objective"}

# Metrics become the VALUE tuple of SUMS_TABLE, in this order. The /files and /bytes suffixes
# make hs return plain numbers: exact bytes instead of
# values auto-scaled to KBYTES/MBYTES and rounded. Display units are applied afterwards.
METRICS = {
    "file_count": {"label": "Files", "exp": "1FILE/files", "numeric": True, "unit": "count"},
    "space_used": {"label": "Space used", "exp": "SPACE_USED/bytes", "numeric": True, "unit": "bytes"},
    "size":       {"label": "Logical size", "exp": "SIZE/bytes", "numeric": True, "unit": "bytes"},
    "top_files":  {"label": "Largest files", "exp": "TOP{n}_TABLE{{DPATH,SPACE_USED/bytes}}",
                   "numeric": False, "unit": "files"},
}
FOLDER_DEPTHS = (1, 2, 3, 4, 5, "all")
MAX_FOLDERS = int(os.environ.get("HSR_MAX_FOLDERS", "2000"))
FOLDER_CONCURRENCY = int(os.environ.get("HSR_FOLDER_CONCURRENCY", "4"))

# Crawl speed. A single hs sum is one server-side operation the reporter can't slow down;
# what it controls is how many hs commands it sends, how fast, and how fast it lists folders.
COMMAND_PAUSE = float(os.environ.get("HSR_COMMAND_PAUSE", "0"))      # seconds between hs commands
LIST_RATE = float(os.environ.get("HSR_LIST_RATE", "0"))              # folder listings per second, 0 = no limit
MAX_HS_PROCESSES = int(os.environ.get("HSR_MAX_HS_PROCESSES", "4"))  # hs commands at once, all reports
THROTTLE_PRESETS = {
    "normal":  {"label": "Normal", "concurrency": FOLDER_CONCURRENCY, "pause": COMMAND_PAUSE, "list_rate": LIST_RATE},
    "gentle":  {"label": "Gentle", "concurrency": 1, "pause": 1.0, "list_rate": 20},
    "slowest": {"label": "Slowest", "concurrency": 1, "pause": 5.0, "list_rate": 5},
}
_hs_slots: asyncio.Semaphore | None = None


def throttle_for(defn: dict) -> dict:
    """The crawl-speed settings for a report: a preset, or custom values within safe bounds."""
    t = defn.get("throttle") or {}
    preset = t.get("preset") or "normal"
    if preset in THROTTLE_PRESETS:
        base = THROTTLE_PRESETS[preset]
        return {"preset": preset, "concurrency": base["concurrency"], "pause": base["pause"],
                "list_rate": base["list_rate"]}
    try:
        c = int(t.get("concurrency", 1))
        p = float(t.get("pause", 0))
        r = float(t.get("list_rate", 0))
    except (TypeError, ValueError):
        raise DefinitionError("Crawl speed values must be numbers")
    if not 1 <= c <= 16:
        raise DefinitionError("hs commands at once must be between 1 and 16")
    if not 0 <= p <= 3600:
        raise DefinitionError("The pause must be between 0 and 3600 seconds")
    if not 0 <= r <= 1000:
        raise DefinitionError("Folder listings per second must be between 0 (no limit) and 1000")
    return {"preset": "custom", "concurrency": c, "pause": p, "list_rate": r}
TOP_N_CHOICES = (10, 100)

# Per-file fields for hs eval listings, returned as a tuple {F1,F2,...}
EVAL_FIELDS = {
    "dpath":      {"label": "File path", "exp": "DPATH"},
    "owner":      {"label": "Owner", "exp": "OWNER"},
    "group":      {"label": "Group", "exp": "OWNER_GROUP"},
    "space_used": {"label": "Space used", "exp": "SPACE_USED", "unit": "bytes"},
    "size":       {"label": "Logical size", "exp": "SIZE", "unit": "bytes"},
    "type":       {"label": "File type", "exp": "TYPE"},
    "mime":       {"label": "MIME type", "exp": "ATTRIBUTES.MIME.STRING"},
    "alignment":  {"label": "Alignment", "exp": "OVERALL_ALIGNMENT"},
    "objectives": {"label": "Active objectives", "exp": "LIST_OBJECTIVES_ACTIVE"},
    "instances":  {"label": "Instances", "exp": "INSTANCES"},
    "errors":     {"label": "Errors", "exp": "ERRORS"},
    "is_open":    {"label": "Open", "exp": "IS_OPEN"},
}

# Share/system level queries run with hs eval on the chosen path
SYSTEM_QUERIES = {
    "storage_volumes": {"label": "Storage volumes", "exp": "STORAGE_VOLUMES"},
    "volume_status":   {"label": "Volume status",
                        "exp": "{|::#A=storage_volumes.name[row],|::#B=storage_volumes.volume_status[row],"
                               "|::#C=storage_volumes.oper_status[row]}[rows(storage_volumes)]",
                        "columns": ["Volume", "Volume status", "Operational status"]},
    "volume_groups":   {"label": "Volume groups", "exp": "VOLUME_GROUPS.NAME"},
    "objectives":      {"label": "Smart objectives", "exp": "SMART_OBJECTIVES.NAME"},
    "collections":     {"label": "Collections", "exp": "collections"},
    "replication":     {"label": "Replication details", "exp": "replication_details"},
    "sweep":           {"label": "Sweep details", "exp": "sweep_details"},
    "assimilation":    {"label": "Assimilation details", "exp": "assimilation_details"},
    "dump_inode":      {"label": "Inode dump", "exp": "DUMP_INODE"},
}

# Ready-made starting points shown in the designer
# Default reports offered when creating a new report
PRESETS = [
    {"id": "folder_usage", "label": "File count and capacity per folder", "mode": "sum",
     "sum": {"group_by": [], "metrics": ["file_count", "space_used"], "top_n": 10},
     "folders": {"enabled": True, "depth": 1}},
    {"id": "dir_walk", "label": "Every folder, full directory walk", "mode": "sum",
     "sum": {"group_by": [], "metrics": ["file_count", "space_used"], "top_n": 10},
     "folders": {"enabled": True, "depth": "all"}},
    {"id": "volume_usage", "label": "File count and capacity per storage volume", "mode": "sum",
     "sum": {"group_by": ["volume"], "metrics": ["file_count", "space_used"], "top_n": 10}},
    {"id": "total_top10", "label": "Total file count and capacity, plus top 10 files", "mode": "sum",
     "sum": {"group_by": [], "metrics": ["file_count", "space_used", "top_files"], "top_n": 10}},
    {"id": "user_vol", "label": "User usage per storage volume", "mode": "sum",
     "sum": {"group_by": ["owner", "group", "volume"], "metrics": ["file_count", "space_used"], "top_n": 10}},
    {"id": "top10_vol", "label": "Top 10 files per volume, not accessed in 2+ days", "mode": "sum",
     "sum": {"group_by": ["volume"], "metrics": ["file_count", "space_used", "top_files"], "top_n": 10},
     "filters": {"access_age_days": 2}},

    {"id": "owner_usage", "label": "Capacity by owner", "mode": "sum",
     "sum": {"group_by": ["owner"], "metrics": ["file_count", "space_used", "top_files"], "top_n": 10}},
    {"id": "mime", "label": "Capacity by file type (MIME)", "mode": "sum",
     "sum": {"group_by": ["mime"], "metrics": ["file_count", "space_used", "top_files"], "top_n": 10}},
    {"id": "alignment", "label": "Objective alignment", "mode": "sum",
     "sum": {"group_by": ["alignment"], "metrics": ["file_count", "space_used", "top_files"], "top_n": 10}},
    {"id": "objectives", "label": "Capacity by active objective", "mode": "sum",
     "sum": {"group_by": ["objective"], "metrics": ["file_count", "space_used", "top_files"], "top_n": 10}},
    {"id": "virus", "label": "Virus scan state", "mode": "sum",
     "sum": {"group_by": ["virus_scan"], "metrics": ["file_count", "space_used"], "top_n": 10}},
    {"id": "open_files", "label": "Open files", "mode": "sum",
     "sum": {"group_by": [], "metrics": ["file_count", "space_used", "top_files"], "top_n": 10},
     "filters": {"only_open": True}},
    {"id": "listing", "label": "File listing (per-file eval)", "mode": "eval",
     "eval": {"source": "fields", "fields": ["dpath", "owner", "space_used", "alignment"],
              "recursive": True, "files_only": True}},
    {"id": "volumes", "label": "Storage volume status", "mode": "eval",
     "eval": {"source": "system", "system_query": "volume_status"}},
]


def catalog() -> dict:
    strip = lambda d: {k: {kk: vv for kk, vv in v.items() if kk != "exp"} for k, v in d.items()}
    return {
        "group_keys": strip(GROUP_KEYS), "row_groups": ROW_GROUPS, "metrics": strip(METRICS),
        "top_n_choices": TOP_N_CHOICES, "eval_fields": strip(EVAL_FIELDS),
        "system_queries": {k: {"label": v["label"]} for k, v in SYSTEM_QUERIES.items()},
        "presets": PRESETS,
        "throttle_presets": {k: {kk: vv for kk, vv in v.items()} for k, v in THROTTLE_PRESETS.items()},
        "max_hs_processes": MAX_HS_PROCESSES,
        "folder_depths": FOLDER_DEPTHS, "size_units": list(DISPLAY_UNITS),
        "mount_defaults": {"nfs": shares.NFS_DEFAULT_OPTS, "smb": shares.SMB_DEFAULT_OPTS},
    }


class DefinitionError(ValueError):
    pass


# ---------------------------------------------------------------- building

def _conditions(filters: dict) -> list[str]:
    f = filters or {}
    conds = []
    if f.get("min_size") not in (None, ""):
        conds.append(f"SPACE_USED>={int(f['min_size'])}")
    if f.get("max_size") not in (None, ""):
        conds.append(f"SPACE_USED<={int(f['max_size'])}")
    if f.get("only_open"):
        conds.append("IS_OPEN")
    if f.get("only_online"):
        conds.append("IS_ONLINE")
    if f.get("only_errors"):
        conds.append("ERRORS")
    if f.get("access_age_days") not in (None, "", 0, "0"):
        conds.append(f"ACCESS_AGE>={int(f['access_age_days'])}DAYS")
    if (f.get("custom_condition") or "").strip():
        conds.append(f["custom_condition"].strip())
    return conds


def _chain(prefix: str, conds: list[str], body: str) -> str:
    # Nested ternaries, the same pattern hstk uses (IS_FILE?ISTABLE(..)?SUMS_TABLE..)
    return prefix + "".join(f"({c})?" for c in conds) + body


def _tuple(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else "{" + ",".join(parts) + "}"


def build(defn: dict) -> dict:
    """Build the report, then apply a hand-edited expression if the report has one.

    The edited text replaces only the HammerScript; columns, grouping and per-folder settings
    still come from the report's options so results map onto the same fields. If an edit
    changes the shape of what hs returns, unrecognized values get columns of their own.
    """
    b = _build_generated(defn)
    b["throttle"] = throttle_for(defn)
    override = " ".join((defn.get("expression_override") or "").split())  # hs -e takes one line
    if override and defn.get("mode") != "custom":
        b = {**b, "generated_expression": b["expression"], "expression": override,
             "edited": override != b["expression"], "byte_scale": _byte_scale(override)}
    else:
        b = {**b, "generated_expression": b["expression"], "edited": False}
    return b


_SCALE = {"BYTE": 1, "BYTES": 1, "KBYTE": 10**3, "KBYTES": 10**3, "MBYTE": 10**6, "MBYTES": 10**6,
          "GBYTE": 10**9, "GBYTES": 10**9, "TBYTE": 10**12, "TBYTES": 10**12, "PBYTE": 10**15, "PBYTES": 10**15}


def _byte_scale(exp: str) -> int:
    """If an edited expression asks for sizes in one unit (SPACE_USED/gbytes), the numbers
    hs returns are in that unit; this factor converts them back to bytes."""
    units = {u.upper() for u in re.findall(r"(?:SPACE_USED|SIZE)\s*/\s*([KMGTP]?BYTES?)\b", exp, re.I)}
    return _SCALE[units.pop()] if len(units) == 1 else 1


def _build_generated(defn: dict) -> dict:
    """Return {'expression', 'columns', 'column_meta', 'flags'} for a definition."""
    mode = defn.get("mode", "sum")
    conds = _conditions(defn.get("filters"))

    if mode == "custom" or (mode in ("sum", "eval") and defn.get("custom_expression")):
        exp = (defn.get("custom_expression") or "").strip()
        if not exp:
            raise DefinitionError("Enter a HammerScript expression")
        cols = [c.strip() for c in (defn.get("custom_columns") or "").split(",") if c.strip()]
        verb = defn.get("custom_verb", "sum") if mode == "custom" else mode
        return {"verb": verb, "expression": exp, "key_columns": [],
                "value_columns": cols, "column_meta": {c: {} for c in cols},
                "shape": "generic"}

    if mode == "sum":
        s = defn.get("sum") or {}
        group_by = [g for g in s.get("group_by") or []]
        metrics = [m for m in s.get("metrics") or [] if m in METRICS] or ["file_count"]
        top_n = int(s.get("top_n") or 10)
        if top_n not in TOP_N_CHOICES:
            raise DefinitionError(f"Largest-files count must be one of {TOP_N_CHOICES}")
        unknown = [g for g in group_by if g not in GROUP_KEYS and g not in ROW_GROUPS]
        if unknown:
            raise DefinitionError(f"Unknown grouping: {', '.join(unknown)}")
        exclusive = [g for g in group_by if g in EXCLUSIVE_GROUPS]
        if exclusive and len(group_by) > 1:
            raise DefinitionError(f"{ROW_GROUPS[exclusive[0]]['label']} can't be combined with other groupings")
        scalars = [g for g in GROUP_KEYS if g in group_by]  # catalog order keeps columns stable

        def value(space_exp="SPACE_USED/bytes"):
            parts = []
            for m in metrics:
                e = METRICS[m]["exp"].replace("{n}", str(top_n))
                if m == "space_used":
                    e = space_exp
                parts.append(e)
            return _tuple(parts)

        if not group_by:
            body = value()
            key_cols = []
        elif "objective" in group_by:
            body = ("SUMS_TABLE{|::KEY=LIST_OBJECTIVES_ACTIVE[ROW],|::VALUE=" + value() +
                    "}[ROWS(LIST_OBJECTIVES_ACTIVE)]")
            key_cols = [ROW_GROUPS["objective"]["label"]]
        elif "volume" in group_by and not scalars:
            body = ("ROWS(INSTANCES)?SUMS_TABLE{|::KEY=INSTANCES[ROW].VOLUME,|::VALUE=" +
                    value("INSTANCES[ROW].SPACE_USED/bytes") + "}[ROWS(INSTANCES)]"
                    ":SUMS_TABLE{|KEY=#EMPTY,|::VALUE=" + value() + "}")
            key_cols = [ROW_GROUPS["volume"]["label"]]
        elif "volume" in group_by:
            # scalar keys plus each instance's volume
            key = "{" + ",".join([GROUP_KEYS[g]["exp"] for g in scalars] + ["INSTANCES[PARENT.ROW].VOLUME"]) + "}"
            body = "SUMS_TABLE{|::KEY=" + key + ",|::VALUE=" + value() + "}[ROWS(INSTANCES)]"
            key_cols = [GROUP_KEYS[g]["label"] for g in scalars] + [ROW_GROUPS["volume"]["label"]]
        else:
            key = _tuple([GROUP_KEYS[g]["exp"] for g in scalars])
            body = "SUMS_TABLE{|KEY=" + key + ",|VALUE=" + value() + "}"
            key_cols = [GROUP_KEYS[g]["label"] for g in scalars]

        exp = _chain("IS_FILE?", conds, body)
        val_cols = [METRICS[m]["label"] for m in metrics]
        meta = {METRICS[m]["label"]: {"unit": METRICS[m]["unit"], "numeric": METRICS[m]["numeric"]}
                for m in metrics}
        folders = defn.get("folders") or {}
        per_folder = None
        if folders.get("enabled"):
            depth = folders.get("depth", 1)
            depth = "all" if depth in ("all", None, "") else int(depth)
            if depth not in FOLDER_DEPTHS:
                raise DefinitionError("Folder depth must be 1-5 or all")
            per_folder = {"depth": depth, "include_hidden": bool(folders.get("include_hidden"))}
        return {"verb": "sum", "expression": exp, "key_columns": key_cols,
                "value_columns": val_cols, "column_meta": meta,
                "shape": "total" if not group_by else "table", "per_folder": per_folder}

    if mode == "eval":
        e = defn.get("eval") or {}
        source = e.get("source", "fields")
        if source == "system":
            q = SYSTEM_QUERIES.get(e.get("system_query"))
            if not q:
                raise DefinitionError("Choose a system query")
            cols = q.get("columns", [])
            return {"verb": "eval", "expression": q["exp"], "key_columns": [],
                    "value_columns": cols, "column_meta": {c: {} for c in cols},
                    "shape": "generic"}
        fields = [f for f in e.get("fields") or [] if f in EVAL_FIELDS]
        if not fields:
            raise DefinitionError("Choose at least one field")
        body = _tuple([EVAL_FIELDS[f]["exp"] for f in fields])
        prefix = "IS_FILE?" if e.get("files_only", True) else ""
        exp = _chain(prefix, conds, body)
        cols = [EVAL_FIELDS[f]["label"] for f in fields]
        meta = {EVAL_FIELDS[f]["label"]: {"unit": EVAL_FIELDS[f].get("unit"),
                                          "numeric": bool(EVAL_FIELDS[f].get("unit"))}
                for f in fields}
        return {"verb": "eval", "expression": exp, "key_columns": [],
                "value_columns": cols, "column_meta": meta, "shape": "records"}

    raise DefinitionError(f"Unknown mode {mode}")


def command_for(defn: dict, built: dict, path: str) -> list[str]:
    cmd = [HS_BIN, "-j", built["verb"]]
    if built["verb"] == "eval" and (defn.get("eval") or {}).get("recursive"):
        cmd.append("-r")
    if defn.get("nonfiles"):
        cmd.append("--nonfiles")
    cmd += ["-e", built["expression"], path]
    return cmd


def preview(defn: dict) -> dict:
    built = build(defn)
    share = store.shares.get(defn.get("share_id") or "")
    paths = defn.get("paths") or ["/"]
    cmds = []
    for p in paths:
        root = str(shares.share_root(share)) if share else "/mnt/hs/<share>"
        full = (root.rstrip("/") + "/" + p.lstrip("/")).rstrip("/") or "/"
        cmds.append(shlex.join(command_for(defn, built, full)))
    pf = built.get("per_folder")
    if pf:
        depth = "every level" if pf["depth"] == "all" else f"{pf['depth']} level{'s' if pf['depth'] > 1 else ''}"
        cmds = [f"# once for each folder, {depth} below each path (plus the path itself):"] + \
            [c.rsplit(" ", 1)[0] + " <folder>" for c in cmds[:1]]
    cols = _lead_columns(built, len(paths)) + built["key_columns"] + built["value_columns"]
    return {**built, "commands": cmds, "columns": cols}


def _lead_columns(built: dict, n_paths: int) -> list[str]:
    if built.get("per_folder"):
        return ["Folder"]
    return ["Path"] if n_paths > 1 else []


# ---------------------------------------------------------------- parsing

def parse_output(text: str):
    s = text.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    items = []
    for line in s.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            items.append(line)
    return items


def _num(v):
    if isinstance(v, (int, float)) or v is None:
        return v
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return v
    return v


def _kv(d: dict):
    """Find key/value members of a dict regardless of the casing Hammerspace used."""
    low = {str(k).lower(): k for k in d}
    if "key" in low and "value" in low and len(d) <= 3:
        return d[low["key"]], d[low["value"]]
    return None


def _pairs(obj) -> list | None:
    """Turn a SUMS_TABLE result into [(key, value), ...]; None if it isn't one."""
    if isinstance(obj, dict):
        kv = _kv(obj)
        if kv:
            return [kv]
        for wrap in ("sums_table", "SUMS_TABLE", "table", "rows", "result"):
            if wrap in obj and isinstance(obj[wrap], (list, dict)):
                return _pairs(obj[wrap])
        return list(obj.items())
    if isinstance(obj, list):
        out = []
        for el in obj:
            if isinstance(el, dict) and _kv(el):
                out.append(_kv(el))
            elif isinstance(el, (list, tuple)) and len(el) == 2:
                out.append((el[0], el[1]))
            else:
                return None
        return out
    return None


def _top_files(v):
    """Normalize a TOPn_TABLE into [{'space_used':..,'path':..}]."""
    rows = []
    items = v.items() if isinstance(v, dict) else (v if isinstance(v, list) else [])
    for it in items:
        if isinstance(it, tuple):
            it = list(it)
        if isinstance(it, dict):
            vals = list(it.values())
            if len(vals) == 1 and isinstance(vals[0], (list, dict)):
                it = vals[0] if isinstance(vals[0], list) else list(vals[0].values())
            else:
                it = vals
        if isinstance(it, list) and len(it) >= 2:
            a, b = it[0], it[1]
            if isinstance(a, list) and len(a) >= 2:
                a, b = a[0], a[1]
            size, path = (a, b) if not isinstance(_num(a), str) else (b, a)
            rows.append({"space_used": _num(size), "path": path})
        else:
            return v
    rows.sort(key=lambda r: r["space_used"] if isinstance(r["space_used"], (int, float)) else 0,
              reverse=True)
    return rows


def _spread(value, cols: list[str], meta: dict) -> dict:
    if not cols:
        return _flatten(value, "Value")
    if isinstance(value, dict):
        vals = list(value.values())
        if len(vals) == len(cols) and len(cols) > 1:
            value = vals
        elif len(cols) > 1:
            return _flatten(value, "")
    if len(cols) == 1 or not isinstance(value, list):
        value = [value]
    row = {}
    for i, c in enumerate(cols):
        v = value[i] if i < len(value) else None
        if meta.get(c, {}).get("unit") == "files":
            row[c] = _top_files(v)
        elif meta.get(c, {}).get("numeric"):
            row[c] = _num(v)
        else:
            row[c] = v
    if len(value) > len(cols):
        for j, extra in enumerate(value[len(cols):], 1):
            row[f"Extra {j}"] = extra
    return row


def _flatten(obj, prefix="") -> dict:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.update(_flatten(v, key))
            else:
                out[key] = v
        return out
    return {prefix or "Value": obj}


def _generic_rows(obj, cols: list[str]) -> list[dict]:
    if obj is None:
        return []
    if isinstance(obj, list):
        if all(isinstance(x, dict) for x in obj):
            return [_flatten(x) for x in obj]
        if cols and all(isinstance(x, list) for x in obj):
            return [dict(zip(cols, x)) for x in obj]
        if all(isinstance(x, list) for x in obj):
            return [{f"Col {i+1}": v for i, v in enumerate(x)} for x in obj]
        return [{"Value": x} if not isinstance(x, dict) else _flatten(x) for x in obj]
    if isinstance(obj, dict):
        if obj and all(isinstance(v, dict) for v in obj.values()):
            return [{"Key": k, **_flatten(v)} for k, v in obj.items()]
        if obj and all(isinstance(v, list) for v in obj.values()) and cols:
            return [{"Key": k, **dict(zip(cols, v))} for k, v in obj.items()]
        return [{"Key": k, "Value": v} for k, v in obj.items()]
    return [{"Value": obj}]


def tabulate(parsed, built: dict) -> list[dict]:
    keys, vals, meta = built["key_columns"], built["value_columns"], built["column_meta"]
    shape = built["shape"]

    if shape == "total":
        v = parsed[0] if isinstance(parsed, list) and len(parsed) == 1 else parsed
        if v is None:
            return []
        return [_spread(v, vals, meta)]

    if shape == "table":
        pairs = _pairs(parsed)
        if pairs is None and isinstance(parsed, list) and len(parsed) == 1:
            pairs = _pairs(parsed[0])
        if pairs is None:
            return _generic_rows(parsed, vals)
        rows = []
        for k, v in pairs:
            if len(keys) > 1 and isinstance(k, (list, tuple)):
                krow = {c: (k[i] if i < len(k) else None) for i, c in enumerate(keys)}
            elif len(keys) > 1 and isinstance(k, dict):
                kv = list(k.values())
                krow = {c: (kv[i] if i < len(kv) else None) for i, c in enumerate(keys)}
            else:
                krow = {keys[0]: "(none)" if k in ("#EMPTY", "", None) else k}
            rows.append({**krow, **_spread(v, vals, meta)})
        return rows

    if shape == "records":
        items = parsed if isinstance(parsed, list) else [parsed]
        return [_spread(it, vals, meta) for it in items if it is not None]

    return _generic_rows(parsed, vals)


PARSER_VERSION = 4


def rows_from_output(text: str, built: dict) -> list[dict]:
    rows = _rows_from_output(text, built)
    scale = built.get("byte_scale") or 1
    if scale != 1:
        sized = [c for c, m in built["column_meta"].items() if (m or {}).get("unit") == "bytes"]
        tops = [c for c, m in built["column_meta"].items() if (m or {}).get("unit") == "files"]
        for r in rows:
            for c in sized:
                if isinstance(r.get(c), (int, float)):
                    r[c] = int(round(r[c] * scale))
            for c in tops:
                for f in r.get(c) or []:
                    if isinstance(f, dict) and isinstance(f.get("space_used"), (int, float)):
                        f["space_used"] = int(round(f["space_used"] * scale))
    return rows


def _rows_from_output(text: str, built: dict) -> list[dict]:
    """Parse hs output, whether it came back as tab-separated text or as JSON."""
    units = {c: (m or {}).get("unit") for c, m in built["column_meta"].items()}
    if hsparse.looks_like_hs_text(text):
        records = hsparse.parse_lines(text)
    else:
        parsed = parse_output(text)
        records = hsparse.records_from_json(parsed, built["shape"])
        if records is None:
            return tabulate(parsed, built)
    return hsparse.rows_from_records(records, built["key_columns"], built["value_columns"], units)


def _label_rows(rows: list[dict], output: dict, multi: bool) -> list[dict]:
    if output.get("folder") is not None:
        return [{"Folder": output["folder"], **x} for x in rows]
    if multi:
        return [{"Path": output["path"], **x} for x in rows]
    return rows


def _rows_for_run(run: dict, built: dict) -> list[dict]:
    outs = run.get("outputs") or []
    multi = len({o.get("path") for o in outs}) > 1
    rows = []
    for o in outs:
        if o.get("rc") != 0:
            continue
        rows.extend(_label_rows(rows_from_output(o.get("stdout") or "", built), o, multi))
    return rows


def ensure_parsed(run: dict) -> dict:
    """Re-parse runs stored by an older parser, from their saved raw output."""
    if run.get("parser_version") == PARSER_VERSION or not run.get("outputs"):
        return run
    if run.get("status") in ("queued", "running"):
        return run
    if any(o.get("truncated") or len(o.get("stdout") or "") >= 2_000_000 for o in run["outputs"]):
        return run  # saved output is incomplete; keep the original rows
    try:
        built = build(run["definition"])
    except Exception:
        return run
    rows = _rows_for_run(run, built)
    n_paths = len((run["definition"].get("paths") or ["/"]))
    lead = _lead_columns(built, n_paths)
    preferred = lead + built["key_columns"] + built["value_columns"]
    run.update(rows=rows, row_count=len(rows), columns=all_columns(rows, preferred),
               column_meta=built["column_meta"],
               key_columns=(["Folder"] if built.get("per_folder") else []) + built["key_columns"],
               parser_version=PARSER_VERSION)
    store.runs.put(run)
    return run


def all_columns(rows: list[dict], preferred: list[str]) -> list[str]:
    cols = [c for c in preferred if any(c in r for r in rows)] if rows else list(preferred)
    for r in rows:
        for c in r:
            if c not in cols:
                cols.append(c)
    return cols


def apply_display(run: dict, override: dict | None = None) -> dict:
    """Pick, order, sort and limit columns per the definition's display settings."""
    disp = {**(run.get("definition", {}).get("display") or {}), **(override or {})}
    rows = run.get("rows") or []
    available = run.get("columns") or []
    chosen = [c for c in (disp.get("fields") or []) if c in available] or available
    sort_by = disp.get("sort_by")
    if sort_by in available:
        def sk(r):
            v = r.get(sort_by)
            if isinstance(v, (int, float)):
                return (0, v, "")
            if isinstance(v, list):
                return (0, len(v), "")
            return (1, 0, str(v or ""))
        rows = sorted(rows, key=sk, reverse=bool(disp.get("sort_desc", True)))
    limit = disp.get("limit")
    if limit:
        rows = rows[: int(limit)]
    unit = disp.get("size_unit") if disp.get("size_unit") in DISPLAY_UNITS else "auto"
    disp["size_unit"] = unit
    return {"columns": chosen, "available": available, "rows":
            [{c: r.get(c) for c in chosen} for r in rows], "display": disp,
            "column_meta": run.get("column_meta") or {}}


def _in_unit(v, unit: str, decimals: int = 3):
    f = DISPLAY_UNITS.get(unit)
    if not isinstance(v, (int, float)) or not f:
        return v
    return int(v) if f == 1 else round(v / f, decimals)


def to_csv(view: dict) -> str:
    """'Table as shown': chosen columns, order and sort. Sizes in the chosen unit
    (bytes when the display unit is auto), with the unit in the header."""
    meta = view.get("column_meta") or {}
    unit = view.get("display", {}).get("size_unit") or "auto"
    unit = "bytes" if unit == "auto" else unit
    is_size = lambda c: (meta.get(c) or {}).get("unit") == "bytes"
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"{c} ({UNIT_LABEL[unit]})" if is_size(c) else c for c in view["columns"]])
    for r in view["rows"]:
        out = []
        for c in view["columns"]:
            v = r.get(c)
            if isinstance(v, list) and v and isinstance(v[0], dict) and "path" in v[0]:
                v = "; ".join(f"{x['path']} ({_in_unit(x['space_used'], unit)} {UNIT_LABEL[unit]})" for x in v)
            elif isinstance(v, (list, dict)):
                v = json.dumps(v)
            elif is_size(c):
                v = _in_unit(v, unit)
            out.append("" if v is None else v)
        w.writerow(out)
    return buf.getvalue()


def to_prometheus(run: dict) -> str:
    """Prometheus text format for per-folder reports."""
    meta = run.get("column_meta") or {}
    keys = [k for k in (run.get("key_columns") or []) if k != "Folder"]
    count_col = next((c for c in run.get("columns") or [] if (meta.get(c) or {}).get("unit") == "count"), None)
    size_cols = [c for c in run.get("columns") or [] if (meta.get(c) or {}).get("unit") == "bytes"]
    esc = lambda v: str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    share = run.get("share_name") or ""
    metrics = []
    if size_cols:
        metrics.append(("node_directory_size_bytes", "Space used below the directory, in bytes", size_cols[0]))
    for c in size_cols[1:]:
        metrics.append((f"node_directory_{hsparse.slug(c)}_bytes", f"{c} below the directory, in bytes", c))
    if count_col:
        metrics.append(("node_directory_file_count", "Files below the directory", count_col))
    lines = []
    for name, help_text, col in metrics:
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
        for r in run.get("rows") or []:
            v = r.get(col)
            if not isinstance(v, (int, float)):
                continue
            labels = {"share": share, "directory": r.get("Folder") or r.get("Path") or "/",
                      **{hsparse.slug(k): r.get(k) for k in keys}}
            lbl = ",".join(f'{k}="{esc(v2)}"' for k, v2 in labels.items())
            lines.append(f"{name}{{{lbl}}} {v}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- execution

async def _exec(cmd: list[str], cwd: str) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out = bytearray()
    truncated = False

    async def pump():
        nonlocal truncated
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            if len(out) < MAX_OUTPUT:
                out.extend(chunk[: MAX_OUTPUT - len(out)])
            else:
                truncated = True

    try:
        _, err = await asyncio.wait_for(asyncio.gather(pump(), proc.stderr.read()), RUN_TIMEOUT)
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"rc": -1, "stdout": out.decode(errors="replace"),
                "stderr": f"Timed out after {RUN_TIMEOUT}s", "truncated": truncated}
    return {"rc": proc.returncode, "stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace")[-20000:], "truncated": truncated}


def _is_stale_error(stderr: str) -> bool:
    return "Stale file handle" in stderr or "Errno 116" in stderr


def create_run(defn: dict, trigger: str = "manual", schedule_id: str | None = None) -> dict:
    share = store.shares.get(defn.get("share_id") or "")
    run = {
        "id": store.new_id(), "name": defn.get("name") or "Untitled report",
        "definition_id": defn.get("id"), "schedule_id": schedule_id, "trigger": trigger,
        "definition": defn, "share_name": share["name"] if share else None,
        "mode": defn.get("mode"), "status": "queued", "started": store.now(),
        "finished": None, "outputs": [], "rows": [], "columns": [], "row_count": 0, "error": None,
    }
    return store.runs.put(run)


def _expand_folders(root: Path, depth, include_hidden: bool, limit: int,
                    list_rate: float = 0, on_progress=None) -> tuple[list[Path], bool]:
    """root plus its subfolders down to `depth` levels ("all" = every level), sorted.
    Lists one folder at a time, at most `list_rate` listings per second (0 = no limit)."""
    out, truncated = [root], False
    stack = [(root, 0)]
    interval = 1.0 / list_rate if list_rate else 0
    last = 0.0
    while stack:
        cur, d = stack.pop()
        if depth != "all" and d >= depth:
            continue
        if interval:
            wait = last + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            last = time.monotonic()
        if on_progress:
            on_progress(len(out))
        try:
            with os.scandir(cur) as it:
                subs = sorted((e for e in it if e.is_dir(follow_symlinks=False)
                               and (include_hidden or not e.name.startswith("."))),
                              key=lambda e: e.name.lower())
        except OSError:
            continue
        for e in subs:
            if len(out) >= limit:
                return sorted(out), True
            out.append(Path(e.path))
        stack.extend((Path(e.path), d + 1) for e in reversed(subs))
    return sorted(out, key=lambda p: str(p).lower()), truncated


async def _run_hs(cmd: list[str], full: Path, share: dict) -> dict:
    global _hs_slots
    if _hs_slots is None:
        _hs_slots = asyncio.Semaphore(MAX_HS_PROCESSES)
    async with _hs_slots:  # server-wide cap, whatever each report asks for
        return await _run_hs_unlimited(cmd, full, share)


async def _run_hs_unlimited(cmd: list[str], full: Path, share: dict) -> dict:
    cwd = str(full if full.is_dir() else full.parent)
    res = await _exec(cmd, cwd=cwd)
    if res["rc"] != 0 and _is_stale_error(res["stderr"]) and share["kind"] != "local":
        # The NFS/SMB session was dropped (sleep, network change, failover). Remount, retry once.
        try:
            await shares.remount(share)
            res = await _exec(cmd, cwd=cwd)
            res["stderr"] = "[remounted after a stale file handle and retried]\n" + res["stderr"]
        except Exception as e:
            res["stderr"] += f"\n[remount after stale file handle failed: {e}]"
    return res


async def execute(run: dict, export: bool = False) -> dict:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(MAX_CONCURRENT)
    defn = run["definition"]
    notes = []
    try:
        built = build(defn)
        share = store.shares.get(defn.get("share_id") or "")
        if not share:
            raise DefinitionError("The share for this report no longer exists")
        if not shares.is_mounted(share):
            await shares.mount(share)  # also clears a stale mount first
        run["expression"] = built["expression"]
        paths = defn.get("paths") or ["/"]
        root = shares.share_root(share).resolve()
        starts = []  # (path as entered, full path)
        for p in paths:
            full = shares.resolve_in_share(share, p)
            if not full.exists():
                raise DefinitionError(f"{p} doesn't exist on {share['name']}")
            starts.append((p, full))
    except Exception as e:
        run.update(status="failed", error=str(e), finished=store.now())
        return store.runs.put(run)

    th = built["throttle"]
    async with _sem:
        run.update(status="running", started=store.now(), throttle=th, targets_done=0)
        pf = built.get("per_folder")
        targets = []  # (path as entered, full path, folder label or None)
        if pf:
            run["phase"] = "listing"
            store.runs.put(run)
            listed = {"n": 0, "saved_at": time.time()}

            def on_progress(n):
                if time.time() - listed["saved_at"] > 2:  # show discovery progress
                    listed["saved_at"] = time.time()
                    run["folders_found"] = len(targets) + n
                    store.runs.put(run)

            for p, full in starts:
                folders, cut = await asyncio.to_thread(
                    _expand_folders, full, pf["depth"], pf["include_hidden"], MAX_FOLDERS - len(targets),
                    th["list_rate"], on_progress)
                if cut:
                    notes.append(f"Stopped at {MAX_FOLDERS} folders (HSR_MAX_FOLDERS); deeper folders were skipped.")
                for f in folders:
                    rel = "/" + str(f.resolve().relative_to(root)) if f.resolve() != root else "/"
                    targets.append((p, f, rel.replace("//", "/")))
        else:
            targets = [(p, full, None) for p, full in starts]
        run.update(phase="scanning", target_count=len(targets))
        store.runs.put(run)
        # Commands at once applies to per-folder reports; several selected paths run one by one.
        fsem = asyncio.Semaphore(th["concurrency"] if pf else 1)

        progress = {"done": 0, "saved_at": time.time()}

        async def one(t):
            rel, full, folder = t
            async with fsem:
                cmd = command_for(defn, built, str(full))
                t0 = time.time()
                res = await _run_hs(cmd, full, share)
                secs = round(time.time() - t0, 2)
                progress["done"] += 1
                if th["pause"] and progress["done"] < len(targets):
                    await asyncio.sleep(th["pause"])  # hold the slot: a pause between commands
            if len(targets) > 1 and time.time() - progress["saved_at"] > 2:
                progress["saved_at"] = time.time()  # let the results page show progress
                run["targets_done"] = progress["done"]
                store.runs.put(run)
            return t, cmd, res, secs

        results = await asyncio.gather(*(one(t) for t in targets))
        multi = len(paths) > 1
        rows, errors = [], []
        for (rel, full, folder), cmd, res, secs in results:
            out = {"path": rel, "folder": folder, "command": shlex.join(cmd), "rc": res["rc"],
                   "seconds": secs, "stdout": res["stdout"][:2_000_000],
                   "stderr": res["stderr"], "truncated": res["truncated"]}
            if res["rc"] == 0:
                rows.extend(_label_rows(rows_from_output(res["stdout"], built), out, multi))
            else:
                errors.append(f"{folder or rel}: {(res['stderr'] or 'exit ' + str(res['rc'])).strip()[-500:]}")
            run["outputs"].append(out)

        preferred = _lead_columns(built, len(paths)) + built["key_columns"] + built["value_columns"]
        run.update(rows=rows, row_count=len(rows), columns=all_columns(rows, preferred),
                   column_meta=built["column_meta"],
                   key_columns=(["Folder"] if built.get("per_folder") else []) + built["key_columns"],
                   parser_version=PARSER_VERSION, finished=store.now(),
                   status="failed" if errors and not rows else ("partial" if errors else "done"),
                   error="\n".join(errors[:50] + notes) or None)
        store.runs.put(run)

    if export and run["status"] != "failed":
        write_export(run)
    return run


# ---------------------------------------------------------------- analysis CSV

def _is_top_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, dict) and "path" in x for x in v) and bool(v)


SIZE_UNITS = {"bytes": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12}
EXPORT_DEFAULTS = {"meta": True, "unit": "bytes", "decimals": 2, "paths": "full"}


def export_options(raw: dict | None) -> dict:
    o = {**EXPORT_DEFAULTS, **{k: v for k, v in (raw or {}).items() if v is not None}}
    o["meta"] = o["meta"] not in (False, "0", "false", 0)
    o["unit"] = o["unit"] if o["unit"] in SIZE_UNITS else "bytes"
    o["decimals"] = max(0, min(6, int(o["decimals"])))
    o["paths"] = o["paths"] if o["paths"] in ("full", "relative") else "full"
    return o


def analysis_tables(run: dict, options: dict | None = None) -> dict:
    """Flat, spreadsheet-friendly tables.

    options: meta      include run_id/run_started_utc/report/share on every row
             unit      bytes (integers) or kb/mb/gb/tb (decimal, rounded to `decimals`)
             paths     full (report path + relative path) or relative (as Hammerspace gives it)
    """
    o = export_options(options)
    run = ensure_parsed(run)
    meta = run.get("column_meta") or {}
    rows = run.get("rows") or []
    cols = run.get("columns") or []
    key_cols = run.get("key_columns")
    if key_cols is None:
        try:
            key_cols = build(run["definition"])["key_columns"]
        except Exception:
            key_cols = []
    paths = (run.get("definition") or {}).get("paths") or ["/"]
    multi_path = len(paths) > 1
    started = datetime.fromtimestamp(run["started"], timezone.utc).isoformat(timespec="seconds")
    base = {"run_id": run["id"], "run_started_utc": started, "report": run.get("name"),
            "share": run.get("share_name")} if o["meta"] else {}
    with_path = o["meta"] or multi_path  # the report path only varies across paths

    factor, suffix = SIZE_UNITS[o["unit"]], "_" + o["unit"]

    def size(v):
        if not isinstance(v, (int, float)):
            return v
        return int(v) if factor == 1 else round(v / factor, o["decimals"])

    top_cols = [c for c in cols if (meta.get(c) or {}).get("unit") == "files"
                or any(_is_top_list(r.get(c)) for r in rows)]
    data_cols = [c for c in cols if c not in top_cols and c != "Path"]
    is_size = lambda c: (meta.get(c) or {}).get("unit") == "bytes"

    def name(c):
        n = hsparse.slug(c)
        return n + suffix if is_size(c) and not n.endswith(suffix) else n

    def cell(c, v):
        if isinstance(v, (list, dict)):
            return json.dumps(v, separators=(",", ":"))
        return size(v) if is_size(c) else v

    def file_path(rp, rel):
        if rel is None:
            return None
        if o["paths"] == "relative":
            return str(rel)[2:] if str(rel).startswith("./") else str(rel)
        return hsparse.join_path(rp, rel)

    lead = list(base) + (["report_path"] if with_path else [])
    s_cols = lead + [name(c) for c in data_cols]
    f_cols = lead + [hsparse.slug(c) for c in key_cols] + \
        (["list"] if len(top_cols) > 1 else []) + ["rank", "file_path", "space_used" + suffix]
    s_rows, f_rows = [], []
    for r in rows:
        rp = r.get("Path") or paths[0]
        top_base = r.get("Folder") or rp  # largest-files paths are relative to where hs ran
        lead_vals = {**base, **({"report_path": rp} if with_path else {})}
        s_rows.append({**lead_vals, **{name(c): cell(c, r.get(c)) for c in data_cols}})
        for tc in top_cols:
            for rank, f in enumerate(r.get(tc) or [], 1):
                if not isinstance(f, dict):
                    continue
                f_rows.append({**lead_vals, **{hsparse.slug(k): r.get(k) for k in key_cols},
                               **({"list": tc} if len(top_cols) > 1 else {}),
                               "rank": rank, "file_path": file_path(top_base, f.get("path")),
                               "space_used" + suffix: size(f.get("space_used"))})
    return {"summary": (s_cols, s_rows), "files": (f_cols, f_rows), "has_files": bool(top_cols),
            "options": o}


def table_csv(cols: list[str], rows: list[dict], header: bool = True) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if header:
        w.writerow(cols)
    for r in rows:
        w.writerow(["" if r.get(c) is None else r.get(c) for c in cols])
    return buf.getvalue()


def _append_history(path: Path, cols: list[str], rows: list[dict]) -> None:
    """One growing CSV per schedule for trend analysis. If the report's columns change,
    the old history is set aside rather than mixed with a different layout."""
    if path.exists():
        with open(path, newline="") as f:
            existing = next(csv.reader(f), [])
        if existing != cols:
            path.rename(path.with_name(path.stem + time.strftime("-until-%Y%m%d-%H%M%S") + ".csv"))
    new = not path.exists()
    with open(path, "a", newline="") as f:
        f.write(table_csv(cols, rows, header=new))


def write_export(run: dict) -> list[Path]:
    folder = EXPORT_DIR / (run.get("schedule_id") or "manual")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(run["started"]))
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in run["name"])[:60]
    current = store.definitions.get(run.get("definition_id") or "") or run.get("definition") or {}
    t = analysis_tables(run, current.get("export"))
    written = []
    p = folder / f"{safe}-{stamp}-summary.csv"
    p.write_text(table_csv(*t["summary"]))
    written.append(p)
    _append_history(folder / f"{safe}-history.csv", *t["summary"])
    if t["has_files"]:
        p = folder / f"{safe}-{stamp}-largest-files.csv"
        p.write_text(table_csv(*t["files"]))
        written.append(p)
        _append_history(folder / f"{safe}-largest-files-history.csv", *t["files"])
    return written
