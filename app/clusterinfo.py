"""Parse the Hammerspace CLI exports used for objective planning:

    volume-list --full          storage volumes   (Access type, Capacity, Node, Capabilities, Locations)
    object-volume-list --full   object volumes    (Total capacity, Logical used, Capabilities, Locations)
    volume-group-list --full    volume groups     (Volumes: [Type: ..., Name: ...])
    objective-list --full       objectives        (Place on, Confine to, Exclude from, Availability, ...)

Each export is a series of blocks starting with "ID:"; keys start in column 0 and continuation
lines are indented. The kind of export is detected from its keys.
"""
import re

_DEC = {"B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12, "PB": 10**15, "EB": 10**18,
        "KIB": 2**10, "MIB": 2**20, "GIB": 2**30, "TIB": 2**40, "PIB": 2**50}


def parse_size(text):
    """'42.9GB' -> bytes; 'Unlimited' -> None."""
    if text is None:
        return None
    m = re.match(r"\s*([\d.]+)\s*([KMGTPE]?i?B)\b", text, re.I)
    if not m:
        return None
    return int(round(float(m.group(1)) * _DEC[m.group(2).upper()]))


def parse_pct(text):
    m = re.match(r"\s*([\d.]+)\s*%", text or "")
    return float(m.group(1)) / 100 if m else None


def parse_delay(text):
    """'Online' -> 0 seconds; '5 minutes' -> 300."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if t.startswith("online"):
        return 0
    m = re.match(r"([\d.]+)\s*(second|minute|hour|day)s?", t)
    if not m:
        return None
    return float(m.group(1)) * {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[m.group(2)]


def blocks(text: str) -> list[dict]:
    """Split an export into blocks of {key: {"value": first-line text, "more": [continuation lines]}}."""
    out, cur, last = [], None, None
    for raw in text.replace("\r", "").splitlines():
        if not raw.strip():
            continue
        if re.match(r"total \d+\s*$", raw):
            continue
        m = re.match(r"^([A-Za-z][A-Za-z0-9 ()/-]*?):\s*(.*)$", raw)
        if m and not raw[0].isspace():
            key, val = m.group(1).strip(), m.group(2).strip()
            if key == "ID":
                cur = {}
                out.append(cur)
            if cur is None:
                continue
            cur[key] = {"value": val, "more": []}
            last = key
        elif cur is not None and last:
            cur[last]["more"].append(raw.strip())
    return out


def _val(b, key):
    e = b.get(key)
    return e["value"] if e else None


def _kv_lines(entry):
    """Sub-keys of a block entry: first line and continuation lines like 'Max read IOPs: 14415'."""
    if not entry:
        return {}
    out = {}
    for line in [entry["value"]] + entry["more"]:
        m = re.match(r"^([A-Za-z][A-Za-z0-9 ()/-]*?):\s+(.*)$", line)
        if m:
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def _bracket_items(entry):
    """'[Type: Storage Volume, ID: ..., Name: dsx1-1]' lines -> list of dicts."""
    items = []
    if not entry:
        return items
    for line in [entry["value"]] + entry["more"]:
        for m in re.finditer(r"\[([^\]]*)\]", line):
            d = {}
            for part in re.split(r",\s*(?=[A-Za-z ]+:)", m.group(1)):
                if ":" in part:
                    k, v = part.split(":", 1)
                    d[k.strip()] = v.strip()
            if d:
                items.append(d)
    return items


def _targets(entry):
    """'[volume group: object-volumes]' or 'volume group: x' -> [('group'|'volume', name)]."""
    out = []
    if not entry:
        return out
    for line in [entry["value"]] + entry["more"]:
        for m in re.finditer(r"(volume group|object volume|storage volume|volume)\s*:\s*([^\],]+)", line, re.I):
            kind = "group" if m.group(1).lower() == "volume group" else "volume"
            out.append((kind, m.group(2).strip()))
    return out


def detect_kind(bs: list[dict]) -> str | None:
    keys = set().union(*[b.keys() for b in bs]) if bs else set()
    if "Access type" in keys or "Capacity" in keys:
        return "volumes"
    if "Total capacity" in keys or "Logical used" in keys or "Physical used" in keys:
        return "object_volumes"
    if "Volumes" in keys or "Expressions" in keys:
        return "volume_groups"
    if "Priority" in keys or "Description" in keys:
        return "objectives"
    return None


def _caps(b):
    c = _kv_lines(b.get("Capabilities"))
    num = lambda k: float(re.sub(r"[^\d.]", "", c[k])) if c.get(k) and re.search(r"\d", c[k]) else None
    return {
        "availability": parse_pct(c.get("Availability (effective)") or c.get("Availability")),
        "durability": parse_pct(c.get("Durability (effective)") or c.get("Durability")),
        "online_delay": parse_delay(c.get("Online delay")),
        "read_iops": num("Max read IOPs"), "write_iops": num("Max write IOPs"),
        "read_bw": num("Max read bandwidth") * 1000 if num("Max read bandwidth") else None,   # KB/s -> B/s
        "write_bw": num("Max write bandwidth") * 1000 if num("Max write bandwidth") else None,
        "read_latency_ms": num("Min read latency"), "write_latency_ms": num("Min write latency"),
        "high_threshold": parse_pct(c.get("High threshold")),
    }


def _is_read_only(access: str) -> bool:
    """'Read Only', 'Read-only', 'ReadOnly', 'RO'."""
    a = (access or "").strip().lower()
    return bool(re.search(r"read[\s_-]?only", a)) or a == "ro"


def parse_volumes(text):
    vols = []
    for b in blocks(text):
        cap = _val(b, "Capacity") or ""
        total = re.search(r"Total:\s*([^,\]]+)", cap)
        used = re.search(r"Used:\s*([^,(\]]+)", cap)
        free = re.search(r"Free:\s*([^,\]]+)", cap)
        locs = _bracket_items(b.get("Locations"))
        access = (_val(b, "Access type") or "Read Write").strip()
        node = _val(b, "Node") or ""
        path = _val(b, "Path") or ""
        aliases = {_val(b, "Name"), f"{node}::{path}" if node and path else None}
        aliases |= {l.get("Name") for l in locs if l.get("Type") == "Storage Volume"}
        vols.append({
            "id": _val(b, "ID"), "name": _val(b, "Name"), "kind": "storage",
            "node": node, "path": path, "state": _val(b, "State"),
            "oper_state": _val(b, "Oper state"), "admin_state": _val(b, "Admin state"),
            "access": access, "read_only": _is_read_only(access),
            "total": parse_size(total.group(1)) if total else None,
            "used": parse_size(used.group(1)) if used else None,
            "free": parse_size(free.group(1)) if free else None,
            "groups": sorted({l["Name"] for l in locs if l.get("Type") == "Volume Group" and l.get("Name")}),
            "aliases": sorted(a for a in aliases if a),
            **_caps(b),
        })
    return vols


def parse_object_volumes(text):
    vols = []
    for b in blocks(text):
        locs = _bracket_items(b.get("Locations"))
        total_txt = _val(b, "Total capacity") or ""
        total = None if "unlimited" in total_txt.lower() else parse_size(total_txt)
        used = parse_size(_val(b, "Logical used")) or parse_size(_val(b, "Physical used")) or 0
        access = (_val(b, "Access type") or "Read Write").strip()
        vols.append({
            "id": _val(b, "ID"), "name": _val(b, "Name"), "kind": "object",
            "node": _val(b, "Node") or _val(b, "Name"), "path": "", "state": _val(b, "State"),
            "oper_state": _val(b, "Oper state"), "admin_state": _val(b, "Admin state"),
            "access": access, "read_only": _is_read_only(access),
            "total": total, "used": used, "free": (total - used) if total else None,
            "groups": sorted({l["Name"] for l in locs if l.get("Type") == "Volume Group" and l.get("Name")}),
            "aliases": sorted({_val(b, "Name")} | {l.get("Name") for l in locs
                                                   if l.get("Type") == "Object Storage Volume" and l.get("Name")}),
            **_caps(b),
        })
    return vols


def parse_volume_groups(text):
    groups = []
    for b in blocks(text):
        members = [i.get("Name") for i in _bracket_items(b.get("Volumes")) if i.get("Name")]
        groups.append({"id": _val(b, "ID"), "name": _val(b, "Name"), "state": _val(b, "State"),
                       "volumes": members})
    return groups


def parse_objectives(text):
    objs = []
    for b in blocks(text):
        name = _val(b, "Name")
        o = {
            "id": _val(b, "ID"), "name": name, "description": _val(b, "Description") or "",
            "availability": parse_pct(_val(b, "Availability")),
            "durability": parse_pct(_val(b, "Durability")),
            "place_on": _targets(b.get("Place on")),
            "confine_to": _targets(b.get("Confine to")),
            "exclude_from": _targets(b.get("Exclude from")),
            "online_delay": parse_delay(_val(b, "Allowed online delay")),
            "optimize_for_capacity": (_val(b, "Optimize for capacity") or "").lower() == "true",
            "do_not_move": (_val(b, "Do not move") or "").lower() == "true",
            "expression": _val(b, "Expression"),
            "read_min_iops": None, "write_min_iops": None,
        }
        for key, field in (("Read min IOPs", "read_min_iops"), ("Write min IOPs", "write_min_iops"),
                           ("Read min throughput", "read_min_bw"), ("Write min throughput", "write_min_bw")):
            v = _val(b, key)
            if v and re.search(r"\d", v):
                o[field] = float(re.match(r"[\d.]+", v).group(0))
        o["effects"] = objective_effects(o)
        objs.append(o)
    return objs


def objective_effects(o) -> list[str]:
    """What an objective does to where data lives (used by the capacity model)."""
    e = []
    if o["place_on"]:
        e.append("place")
    if o["confine_to"]:
        e.append("confine")
    if o["exclude_from"]:
        e.append("exclude")
    if o["availability"]:
        e.append("availability")
    if o["durability"]:
        e.append("durability")
    if o["online_delay"] is not None:
        e.append("online")
    if o["optimize_for_capacity"]:
        e.append("optimize_capacity")
    if o["do_not_move"]:
        e.append("do_not_move")
    if o.get("read_min_iops") or o.get("write_min_iops"):
        e.append("performance")
    if o["expression"]:
        e.append("expression")
    return e


PARSERS = {"volumes": parse_volumes, "object_volumes": parse_object_volumes,
           "volume_groups": parse_volume_groups, "objectives": parse_objectives}


def parse_any(text: str, kind: str | None = None) -> tuple[str, list]:
    bs = blocks(text)
    kind = kind or detect_kind(bs)
    if kind not in PARSERS:
        raise ValueError("Couldn't recognize this export. Use the output of volume-list --full, "
                         "object-volume-list --full, volume-group-list --full or objective-list --full.")
    items = PARSERS[kind](text)
    if not items:
        raise ValueError("No entries found in this export")
    return kind, items
