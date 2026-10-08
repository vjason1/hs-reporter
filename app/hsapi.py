"""Pull cluster information from the Hammerspace management REST API (sys-mgmt v1.2), as an
alternative to pasting CLI output:

    POST /login                    form-encoded username and password; the session is a cookie
    GET  /shares                   -> the same structure as clusterinfo.parse_share_list
    GET  /storage-volumes          -> clusterinfo.parse_volumes
    GET  /object-storage-volumes   -> clusterinfo.parse_object_volumes
    GET  /volume-groups            -> clusterinfo.parse_volume_groups
    GET  /objectives               -> clusterinfo.parse_objectives

Lists are paged with page / page.size. Availability and durability are counts of nines
(2 -> 99%, 3 -> 99.9%). Locations are polymorphic (a `_type` discriminator): a node location
has `node`, a volume location has `storageVolume`, and a volume group is a location itself;
they're recognized by those fields, with `_type` and `uoid.objectType` as fallbacks.

Connection settings live in <data>/cluster-api.json (0600). The last raw responses are kept
in <data>/cluster-api-responses.json so a mapping problem can be diagnosed.
"""
import json
import os
import threading

import httpx

from . import clusterinfo, store

PATH = store.DATA_DIR / "cluster-api.json"
RAW_PATH = store.DATA_DIR / "cluster-api-responses.json"
DEFAULTS = {"host": "", "port": 8443, "username": "admin", "password": "", "verify_tls": False,
            "base_path": "/mgmt/v1.2/rest"}
PAGE_SIZE = 500
_lock = threading.RLock()
_transport = None   # tests swap in a fake API


class ApiError(Exception):
    pass


# ------------------------------------------------------------------ connection settings

def connection() -> dict:
    try:
        with open(PATH) as f:
            saved = json.load(f)
    except (OSError, json.JSONDecodeError):
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def public_connection() -> dict:
    c = connection()
    return {**{k: v for k, v in c.items() if k != "password"}, "has_password": bool(c["password"]),
            "configured": bool(c["host"] and c["username"])}


def save_connection(body: dict) -> dict:
    c = connection()
    host = (body.get("host", c["host"]) or "").strip()
    host = host.removeprefix("https://").removeprefix("http://").rstrip("/")
    if host and (any(ch.isspace() for ch in host) or "/" in host):
        raise ApiError("Enter the cluster's IP address or FQDN, without a path")
    try:
        port = int(body.get("port", c["port"]) or 8443)
    except (TypeError, ValueError):
        raise ApiError("The port must be a number")
    if not 1 <= port <= 65535:
        raise ApiError("The port must be between 1 and 65535")
    base = "/" + (body.get("base_path", c["base_path"]) or DEFAULTS["base_path"]).strip().strip("/")
    c.update(host=host, port=port, username=(body.get("username", c["username"]) or "").strip(),
             verify_tls=bool(body.get("verify_tls", c["verify_tls"])), base_path=base)
    if body.get("save_password") is False:
        c["password"] = ""
    elif body.get("password"):
        c["password"] = body["password"]
    with _lock:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(c, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, PATH)
    return public_connection()


# ------------------------------------------------------------------ client

class Session:
    """Log in once, then fetch lists with the session cookie."""

    def __init__(self, password: str | None = None):
        c = connection()
        if not c["host"]:
            raise ApiError("Set up the cluster API in Settings first")
        self.password = password or c["password"]
        if not self.password:
            raise ApiError("Enter the cluster API password")
        self.c = c
        self.base = f"https://{c['host']}:{c['port']}{c['base_path']}"
        self.raw = {}

    async def __aenter__(self):
        self.http = httpx.AsyncClient(base_url=self.base, verify=self.c["verify_tls"], timeout=60,
                                      transport=_transport, follow_redirects=False)
        try:
            r = await self.http.post("/login", data={"username": self.c["username"], "password": self.password},
                                     headers={"Accept": "application/json"})
        except httpx.ConnectError as e:
            await self.http.aclose()
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                raise ApiError("The cluster's TLS certificate couldn't be verified. If it's self-signed, turn off "
                               "\"Verify the cluster's TLS certificate\" under Settings → Cluster API.")
            raise ApiError(f"Couldn't reach {self.base}: {e}")
        except httpx.HTTPError as e:
            await self.http.aclose()
            raise ApiError(f"Couldn't reach {self.base}: {e}")
        if r.status_code in (401, 403):
            await self.http.aclose()
            raise ApiError("The cluster rejected the username or password")
        if r.status_code >= 400:
            await self.http.aclose()
            raise ApiError(f"Login failed: HTTP {r.status_code} from {self.base}/login")
        return self

    async def __aexit__(self, *exc):
        await self.http.aclose()

    async def list(self, path: str, **params) -> list:
        """Every item of a list endpoint, page by page."""
        items, page = [], 0
        while True:
            try:
                r = await self.http.get(path, params={**params, "page": page, "page.size": PAGE_SIZE},
                                        headers={"Accept": "application/json"})
            except httpx.HTTPError as e:
                raise ApiError(f"GET {path} failed: {e}")
            if r.status_code in (401, 403):
                raise ApiError(f"The cluster refused GET {path} after logging in (HTTP {r.status_code}). "
                               "Check that the user can read cluster configuration.")
            if r.status_code >= 400:
                raise ApiError(f"GET {path}: HTTP {r.status_code}")
            try:
                data = r.json()
            except ValueError:
                raise ApiError(f"GET {path} didn't return JSON")
            if isinstance(data, dict):  # tolerate a wrapped page
                data = data.get("content") or data.get("items") or data.get("data") or []
            items.extend(data)
            if len(data) < PAGE_SIZE or page > 200:
                break
            page += 1
        self.raw[path] = items
        return items

    def keep_raw(self):
        try:
            with _lock:
                tmp = RAW_PATH.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    json.dump({"base": self.base, "responses": self.raw}, f, indent=1, default=str)
                os.chmod(tmp, 0o600)
                os.replace(tmp, RAW_PATH)
        except OSError:
            pass


# ------------------------------------------------------------------ mapping helpers

def _nines(n):
    """Count of nines -> fraction: 2 -> 0.99, 3 -> 0.999. Percentages are accepted too."""
    if n is None:
        return None
    try:
        n = float(n)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n <= 20:
        return 1 - 10 ** (-n)
    if n <= 100:
        return n / 100
    return None


def _list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        for k in ("list", "items", "content", "elements"):
            if isinstance(v.get(k), list):
                return v[k]
    return []


def _otype(x: dict) -> str:
    return ((x.get("uoid") or {}).get("objectType") or "").upper()


def location(x) -> tuple | None:
    """A location -> ('node' | 'volume' | 'group', name)."""
    if not isinstance(x, dict):
        return None
    t = (x.get("_type") or "").lower()
    ot = _otype(x)
    if isinstance(x.get("node"), dict):
        return ("node", x["node"].get("name"))
    if isinstance(x.get("storageVolume"), dict):
        return ("volume", x["storageVolume"].get("name"))
    if "volumes" in x or "expressions" in x or "group" in t or ot == "VOLUME_GROUP":
        return ("group", x.get("name"))
    if ot in ("STORAGE_VOLUME", "OBJECT_STORAGE_VOLUME", "BASE_STORAGE_VOLUME") or "volume" in t:
        return ("volume", x.get("name"))
    if ot in ("NODE", "NODE_LOCATION") or "node" in t:
        return ("node", x.get("name"))
    return None


def _caps(v: dict) -> dict:
    sc = v.get("storageCapabilities") or {}
    prot = sc.get("protection") or {}
    perf = sc.get("performance") or {}
    avail = prot.get("effectiveAvailability") or prot.get("availability")
    dur = prot.get("effectiveDurability") or prot.get("durability")
    rd = perf.get("readPerformance") or {}
    wr = perf.get("writePerformance") or {}
    delay = prot.get("onlineDelay")
    return {"availability": _nines(avail), "durability": _nines(dur),
            "online_delay": float(delay) if isinstance(delay, (int, float)) else 0,
            "read_iops": rd.get("iops"), "write_iops": wr.get("iops"),
            "read_bw": rd.get("bandwidth"), "write_bw": wr.get("bandwidth"),
            "read_latency_ms": rd.get("latency"), "write_latency_ms": wr.get("latency"),
            "high_threshold": perf.get("utilizationThreshold")}


def _locations(v: dict) -> list:
    return [l for l in (location(x) for x in _list(v.get("associatedLocations"))) if l and l[1]]


def _node(v: dict, locs: list) -> str:
    nodes = [n for k, n in locs if k == "node"]
    if nodes:
        return nodes[0]
    name = v.get("name") or ""
    return name.split("::")[0] if "::" in name else name  # unknown: its own failure domain


# ------------------------------------------------------------------ mapping

def map_volume(v: dict) -> dict:
    locs = _locations(v)
    lv = v.get("logicalVolume") or {}
    cap = lv.get("capacity") or {}
    node = _node(v, locs)
    path = lv.get("exportPath") or ""
    access = v.get("accessType") or "READ_WRITE"
    total = cap.get("total") or lv.get("totalCapacity") or v.get("effectiveTotalCapacity")
    used = cap.get("used") if cap.get("used") is not None else v.get("spaceUsed")
    free = cap.get("free") if cap.get("free") is not None else (total - used if total and used is not None else None)
    aliases = {v.get("name"), f"{node}::{path}" if node and path else None, *(lv.get("aliases") or [])}
    aliases |= {n for k, n in locs if k == "volume"}
    return {
        "id": (v.get("uoid") or {}).get("uuid"), "name": v.get("name"), "kind": "storage", "node": node,
        "path": path, "state": v.get("storageVolumeState"), "oper_state": (v.get("operState") or "").title() or None,
        "admin_state": (v.get("adminState") or "").title() or None,
        "access": access.replace("_", " ").title(), "read_only": access.upper() == "READ_ONLY",
        "total": total, "used": used, "free": free,
        "groups": sorted({n for k, n in locs if k == "group"}),
        "aliases": sorted(a for a in aliases if a), **_caps(v),
    }


def map_object_volume(v: dict) -> dict:
    locs = _locations(v)
    node = _node(v, locs)
    total = v.get("totalCapacity")
    total = total if isinstance(total, (int, float)) and total > 0 else None   # unlimited
    used = v.get("logicalUsed") or 0
    access = v.get("accessType") or "READ_WRITE"
    aliases = {v.get("name")} | {n for k, n in locs if k == "volume"}
    return {
        "id": (v.get("uoid") or {}).get("uuid"), "name": v.get("name"), "kind": "object", "node": node,
        "path": "", "state": v.get("storageVolumeState"), "oper_state": (v.get("operState") or "").title() or None,
        "admin_state": (v.get("adminState") or "").title() or None,
        "access": access.replace("_", " ").title(), "read_only": access.upper() == "READ_ONLY",
        "total": total, "used": used, "free": (total - used) if total else None,
        "groups": sorted({n for k, n in locs if k == "group"}),
        "aliases": sorted(a for a in aliases if a), **_caps(v),
    }


def map_group(g: dict) -> dict:
    return {"id": (g.get("uoid") or {}).get("uuid"), "name": g.get("name"), "state": g.get("state"),
            "volumes": [v.get("name") for v in _list(g.get("volumes")) if isinstance(v, dict) and v.get("name")]}


def _targets(locs) -> list:
    """Locations an objective names -> [(kind, name)]; groups and volumes, and nodes."""
    out = []
    for x in _list(locs) if not isinstance(locs, list) else locs:
        loc = location(x)
        if loc and loc[1]:
            out.append(loc)
    return out


def map_objective(o: dict) -> dict:
    po = o.get("placementObjective") or {}
    prot = o.get("protection") or {}
    place = []
    for entry in _list(po.get("placeOnLocations")):
        alts = _targets(entry.get("placeOn") if isinstance(entry, dict) else entry)
        if len(alts) == 1:
            place.append(alts[0])
        elif alts:  # one instance on any of several locations
            place.append(("anyof", alts))
    rd = o.get("readPerformance") or {}
    wr = o.get("writePerformance") or {}
    delay = po.get("allowedOnlineDelay")
    obj = {
        "id": (o.get("uoid") or {}).get("uuid"), "name": o.get("name"), "description": o.get("comment") or "",
        "availability": _nines(prot.get("availability")), "durability": _nines(prot.get("durability")),
        "place_on": place, "confine_to": _targets(po.get("confineTo")), "exclude_from": _targets(po.get("excludeFrom")),
        "online_delay": float(delay) if isinstance(delay, (int, float)) and delay >= 0 else None,
        "optimize_for_capacity": bool(po.get("capacityOptimize")), "do_not_move": bool(po.get("doNotMove")),
        "expression": o.get("expression") or None,
        "read_min_iops": rd.get("minIops") or None, "write_min_iops": wr.get("minIops") or None,
    }
    obj["effects"] = clusterinfo.objective_effects(obj)
    return obj


def map_share(s: dict) -> dict:
    exports = []
    for e in _list(s.get("exportOptions")):
        sec = e.get("securityOptions") or []
        exports.append({"client": e.get("subnet"), "access": e.get("accessPermissions"),
                        "root_squash": bool(e.get("rootSquash")), "insecure": bool(e.get("insecure")),
                        "security": [x if isinstance(x, str) else (x.get("name") or str(x)) for x in sec]})
    path = s.get("path") or ""
    return {"name": s.get("name"), "path": path, "id": (s.get("uoid") or {}).get("uuid"),
            "internal_id": s.get("internalId"), "lifecycle": s.get("shareLifecycle"), "state": s.get("shareState"),
            "smb_browsable": bool(s.get("smbBrowsable")), "referral": bool(s.get("isReferral")),
            "exports": exports, "is_root": path == "/" or s.get("name") == "root",
            "insecure_allowed": any(e["insecure"] for e in exports)}


# ------------------------------------------------------------------ mount addresses

def mount_addresses(interfaces: list, nodes: list | None = None) -> dict:
    """Where shares can be mounted from: addresses on DSX nodes' interfaces with the DATA role.
    The Anvil (cluster management) address is not a mount address, even where its interface also
    has the DATA role; its addresses are returned separately so the GUI can warn about them."""
    node_type = {}
    for n in nodes or []:
        t = n.get("productNodeType")
        if t:
            node_type[(n.get("uoid") or {}).get("uuid")] = t
            node_type[n.get("name")] = t
    data, anvil = [], set()
    for ni in interfaces:
        node = ni.get("node") or {}
        ntype = node.get("productNodeType") or node_type.get((node.get("uoid") or {}).get("uuid")) \
            or node_type.get(node.get("name"))
        roles = [r.upper() for r in ni.get("roles") or []]
        addrs = [a.get("address") for a in ni.get("ipAddresses") or [] if a.get("address")]
        if ntype == "ANVIL":
            anvil.update(addrs)
            if (node.get("mgmtIpAddress") or {}).get("address"):
                anvil.add(node["mgmtIpAddress"]["address"])
        if ntype == "DSX" and "DATA" in roles:
            for a in addrs:
                data.append({"address": a, "node": node.get("name"), "interface": ni.get("name"),
                             "roles": roles, "link_speed": ni.get("linkSpeed"), "mtu": ni.get("mtu"),
                             "rdma": bool(ni.get("rdmaAvailable"))})
    for n in nodes or []:
        if n.get("productNodeType") == "ANVIL" and (n.get("mgmtIpAddress") or {}).get("address"):
            anvil.add(n["mgmtIpAddress"]["address"])
    data.sort(key=lambda d: (d["node"] or "", d["interface"] or "", d["address"]))
    seen, unique = set(), []
    for d in data:
        if d["address"] not in seen:
            seen.add(d["address"])
            unique.append(d)
    return {"data": unique, "anvil": sorted(anvil - seen)}


async def _addresses(s: "Session") -> dict:
    interfaces = await s.list("/network-interfaces")
    try:
        nodes = await s.list("/nodes")
    except ApiError:
        nodes = []   # interfaces normally carry their node's type; /nodes is a fallback
    return mount_addresses(interfaces, nodes)


async def fetch_mount_addresses(password: str | None = None) -> dict:
    async with Session(password) as s:
        try:
            return await _addresses(s)
        finally:
            s.keep_raw()


# ------------------------------------------------------------------ fetches

async def test(password: str | None = None) -> dict:
    async with Session(password) as s:
        clusters = await s.list("/cntl")
        name = (clusters[0] or {}).get("name") if clusters else None
        return {"ok": True, "cluster": name, "base": s.base}


async def fetch_shares(password: str | None = None) -> tuple[list[dict], dict]:
    """Shares, and the DSX data addresses to mount them from, in one session."""
    async with Session(password) as s:
        try:
            shares = [map_share(x) for x in await s.list("/shares")]
            try:
                addresses = await _addresses(s)
            except ApiError:
                addresses = {"data": [], "anvil": []}
            return shares, addresses
        finally:
            s.keep_raw()


async def fetch_cluster_info(password: str | None = None) -> dict:
    """All four lists the objective planner uses, keyed like the CLI uploads."""
    async with Session(password) as s:
        try:
            return {
                "volumes": [map_volume(v) for v in await s.list("/storage-volumes")],
                "object_volumes": [map_object_volume(v) for v in await s.list("/object-storage-volumes")],
                "volume_groups": [map_group(g) for g in await s.list("/volume-groups")],
                "objectives": [map_objective(o) for o in await s.list("/objectives") if not o.get("hidden")],
            }
        finally:
            s.keep_raw()
