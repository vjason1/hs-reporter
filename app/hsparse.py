"""Parse the text that `hs sum` / `hs eval` print and normalize it for analysis.

Observed format (Hammerspace 4.6, `hs -j sum`), one line per SUMS_TABLE row, tab-separated:

    <key JSON>  TAB  <value JSON>  TAB  <empty>  TAB  []

  key    {"HAMMERSCRIPT":"STORAGE_VOLUME('dsx-1::/hsvol0')"}, or a string / list for tuples
  value  [{"FILES":400},{"MBYTES":40.354},{"TOP10_TABLE":[{"KEY":[[{"MBYTES":15.38},"./a.so"]]}, ...]}]

Sizes arrive scaled with the unit in the name (BYTES, KBYTES, MBYTES, ...). They are decimal:
a 4096-byte file reports as 4.096 KBYTES. Everything here is converted back to integer bytes.
Hammerspace rounds to about 5 significant digits, so converted bytes are approximate.

This module has no dependencies on the rest of the app so it can run standalone (see hs2csv.py).
"""
import json
import posixpath
import re

UNITS = {"BYTES": 1, "KBYTES": 10**3, "MBYTES": 10**6, "GBYTES": 10**9,
         "TBYTES": 10**12, "PBYTES": 10**15, "EBYTES": 10**18}
COUNT_NAMES = {"FILES", "COUNT", "FILE", "INODES", "DIRS"}
_TOP_RE = re.compile(r"^TOP\d+_TABLE$", re.I)
_TEXT_SIZE_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([KMGTPE]?BYTES)\s*$", re.I)
_CALL_RE = re.compile(r"^[A-Z_][A-Z0-9_]*\('(.*)'\)$")


def to_bytes(unit: str, num) -> int:
    return int(round(float(num) * UNITS[unit.upper()]))


def looks_like_hs_text(text: str) -> bool:
    """True when output is Hammerspace's tab-separated form rather than one JSON document."""
    s = text.strip()
    if not s:
        return False
    try:
        json.loads(s)
        return False
    except json.JSONDecodeError:
        pass
    lines = [l for l in s.splitlines() if l.strip()]
    return any("\t" in l for l in lines) or all(_TEXT_SIZE_RE.match(l) for l in lines)


def _cell(text: str):
    t = text.strip()
    if t == "":
        return None
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = _TEXT_SIZE_RE.match(t)
        if m:
            return {m.group(2).upper(): float(m.group(1))}
        return t


# ------------------------------------------------------------------ keys

def key_parts(k) -> list:
    """Unwrap a key into plain values: {"HAMMERSCRIPT":"STORAGE_VOLUME('x')"} -> ["x"]."""
    if k is None or k == "" or k == []:
        return []
    if isinstance(k, str):
        m = _CALL_RE.match(k)
        v = m.group(1) if m else k
        # Owners come back as 'name|uid'; keep the name
        if m and re.fullmatch(r"[^|]+\|\d+", v):
            v = v.split("|")[0]
        return ["(none)" if v in ("#EMPTY", "") else v]
    if isinstance(k, (int, float, bool)):
        return [k]
    if isinstance(k, dict):
        if len(k) == 1:
            return key_parts(next(iter(k.values())))
        return [json.dumps(k, separators=(",", ":"))]
    if isinstance(k, list):
        out = []
        for x in k:
            out.extend(key_parts(x))
        return out
    return [str(k)]


# ---------------------------------------------------------------- values

def _flatten(x):
    if isinstance(x, list):
        for y in x:
            yield from _flatten(y)
    else:
        yield x


def _top_entries(val) -> list[dict]:
    """[{"KEY":[[{"MBYTES":7.27},"./p"]]}, ...] -> [{"space_used": 7270000, "path": "./p"}]."""
    out = []
    for entry in val if isinstance(val, list) else []:
        inner = entry
        if isinstance(entry, dict) and len(entry) == 1:
            inner = next(iter(entry.values()))
        size, path = None, None
        for leaf in _flatten(inner if isinstance(inner, list) else [inner]):
            if isinstance(leaf, dict) and len(leaf) == 1:
                (n, v), = leaf.items()
                if n.upper() in UNITS and size is None:
                    size = to_bytes(n, v)
            elif isinstance(leaf, str) and path is None:
                path = leaf
            elif isinstance(leaf, (int, float)) and size is None:
                size = leaf
        if path is not None or size is not None:
            out.append({"space_used": size, "path": path})
    return out


def typed_item(name, val) -> dict:
    """Classify one {"NAME": value} element."""
    n = str(name).upper()
    if n in UNITS and isinstance(val, (int, float)):
        return {"kind": "bytes", "name": n, "value": to_bytes(n, val)}
    if n in COUNT_NAMES and isinstance(val, (int, float)):
        return {"kind": "count", "name": n, "value": val}
    if _TOP_RE.match(n):
        return {"kind": "top", "name": n, "value": _top_entries(val)}
    if isinstance(val, (int, float)):
        return {"kind": "number", "name": n, "value": val}
    parts = key_parts(val)
    return {"kind": "text", "name": n,
            "value": parts[0] if len(parts) == 1 else json.dumps(val, separators=(",", ":"))}


def value_items(v) -> list[dict]:
    if v is None:
        return []
    if isinstance(v, list):
        items = []
        for x in v:
            items.extend(value_items(x))
        return items
    if isinstance(v, dict):
        items = []
        for n, x in v.items():
            # Wrappers such as {"VALUE": [[...]]}: look inside rather than treat as text
            if isinstance(x, (list, dict)) and not _TOP_RE.match(str(n)) and str(n).upper() not in UNITS:
                items.extend(value_items(x))
            else:
                items.append(typed_item(n, x))
        return items
    if isinstance(v, (int, float)):
        return [{"kind": "number", "name": "VALUE", "value": v}]
    return [{"kind": "text", "name": "VALUE", "value": v}]


def parse_lines(text: str) -> list[dict]:
    """Each output line -> {"key": [...], "items": [...], "extra": [...]}."""
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cells = [_cell(c) for c in line.split("\t")] if "\t" in line else [None, _cell(line)]
        key, value = (cells + [None, None])[:2]
        extra = [c for c in cells[2:] if c not in (None, [], "", {})]
        records.append({"key": key_parts(key), "items": value_items(value), "extra": extra})
    return records


# ---------------------------------------------------------------- mapping

def assign(items: list[dict], cols: list[str], units: dict) -> dict:
    """Map typed items onto named columns.

    units[col] is "count", "bytes", "files" (a largest-files list) or None (anything).
    Items go to the first unfilled column of a compatible kind, in order, which follows
    the VALUE tuple order the expression was built with. Leftovers get their own columns.
    """
    compat = {"count": {"count", "number"}, "bytes": {"bytes", "number"}, "files": {"top"},
              None: {"count", "bytes", "number", "text", "top"}}
    row, used = {}, set()
    for it in items:
        target = next((c for c in cols if c not in used and it["kind"] in compat[units.get(c)]), None)
        if target is None:
            # Unexpected extra value: name it by kind; sizes are already bytes, so
            # don't keep a unit name like "mbytes" that no longer matches the number.
            base = "bytes" if it["kind"] == "bytes" else it["name"].lower()
            target, i = base, 2
            while target in row:
                target, i = f"{base}_{i}", i + 1
        row[target] = it["value"]
        used.add(target)
    return row


def _kv_of(d: dict):
    low = {str(k).lower(): k for k in d}
    if "key" in low and "value" in low:
        return d[low["key"]], d[low["value"]]
    return None


def records_from_json(obj, shape: str) -> list[dict] | None:
    """The same records as parse_lines, from output that arrived as JSON (hs -j).

    Handles [[key, value, ...], ...], [{"key":..,"value":..}, ...], {key: value, ...} and,
    for totals and per-file listings, bare values. Returns None when the structure isn't
    recognized, so the caller can fall back to generic handling.
    """
    if obj is None:
        return []
    if shape == "total":
        return [{"key": [], "items": value_items(obj), "extra": []}]
    if shape == "records":
        items = obj if isinstance(obj, list) else [obj]
        return [{"key": [], "items": value_items(it), "extra": []} for it in items if it is not None]
    if shape != "table":
        return None
    o = obj
    # hs -j wraps tables: {"SUMS_TABLE": [{"KEY": ..., "VALUE": [[...]]}, ...]}
    if isinstance(o, dict) and not _kv_of(o):
        lists = [v for v in o.values() if isinstance(v, list)]
        if len(o) == 1 and lists:
            o = lists[0]
        elif "SUMS_TABLE" in o:
            o = o["SUMS_TABLE"]
    while isinstance(o, list) and len(o) == 1 and isinstance(o[0], list) and o[0] and \
            all(isinstance(e, list) for e in o[0]):
        o = o[0]
    if isinstance(o, list) and o and all(isinstance(e, list) and len(e) >= 2 for e in o):
        return [{"key": key_parts(e[0]), "items": value_items(e[1]),
                 "extra": [x for x in e[2:] if x not in (None, "", [], {})]} for e in o]
    if isinstance(o, list) and o and all(isinstance(e, dict) and _kv_of(e) for e in o):
        return [{"key": key_parts(_kv_of(e)[0]), "items": value_items(_kv_of(e)[1]), "extra": []}
                for e in o]
    if isinstance(o, dict):
        kv = _kv_of(o)
        pairs = [kv] if kv else list(o.items())
        return [{"key": key_parts(k), "items": value_items(v), "extra": []} for k, v in pairs]
    return None


def rows_from_text(text: str, key_cols: list[str], value_cols: list[str], units: dict) -> list[dict]:
    return rows_from_records(parse_lines(text), key_cols, value_cols, units)


def rows_from_records(records: list[dict], key_cols: list[str], value_cols: list[str],
                      units: dict) -> list[dict]:
    rows = []
    for rec in records:
        row = {}
        parts = rec["key"]
        if key_cols:
            if not parts:
                parts = ["(none)"]
            for i, c in enumerate(key_cols):
                if i < len(parts):
                    row[c] = parts[i] if i < len(key_cols) - 1 else (
                        parts[i] if len(parts) <= len(key_cols) else " / ".join(map(str, parts[i:])))
                else:
                    row[c] = None
        elif parts:
            row["Key"] = parts[0] if len(parts) == 1 else " / ".join(map(str, parts))
        items = rec["items"]
        if not items or all(it["kind"] == "text" and it["value"] in ("#EMPTY", "") for it in items):
            # #EMPTY means no files matched: report zeros
            row.update({c: ([] if units.get(c) == "files" else 0) for c in value_cols
                        if units.get(c) in ("count", "bytes", "files")})
        else:
            row.update(assign(items, value_cols, units))
        for i, e in enumerate(rec["extra"], 1):
            row[f"Extra {i}"] = json.dumps(e, separators=(",", ":")) if not isinstance(e, str) else e
        rows.append(row)
    return rows


# ---------------------------------------------------------------- analysis CSV

def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_") or "value"


def join_path(base: str, rel) -> str | None:
    if rel is None:
        return None
    rel = str(rel)
    if rel.startswith("/"):
        return rel
    return posixpath.normpath(posixpath.join(base or "/", rel))
