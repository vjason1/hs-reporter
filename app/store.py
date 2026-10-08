"""Tiny JSON-file persistence. Good enough for a single-container admin tool."""
import json
import os
import threading
import time
import uuid
from pathlib import Path

DATA_DIR = Path(os.environ.get("HSR_DATA_DIR", "/data"))
_lock = threading.RLock()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> float:
    return time.time()


def _atomic_write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, default=str)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Collection:
    """A dict of records kept in one JSON file (shares, definitions, schedules)."""

    def __init__(self, name: str):
        self.path = DATA_DIR / f"{name}.json"

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        with open(self.path) as f:
            return json.load(f)

    def all(self) -> list[dict]:
        with _lock:
            return sorted(self._load().values(), key=lambda r: r.get("created", 0))

    def get(self, rid: str) -> dict | None:
        with _lock:
            return self._load().get(rid)

    def put(self, rec: dict) -> dict:
        with _lock:
            data = self._load()
            rec.setdefault("id", new_id())
            rec.setdefault("created", now())
            rec["updated"] = now()
            data[rec["id"]] = rec
            _atomic_write(self.path, data)
            return rec

    def delete(self, rid: str) -> bool:
        with _lock:
            data = self._load()
            if rid not in data:
                return False
            del data[rid]
            _atomic_write(self.path, data)
            return True


class RunStore:
    """Runs can be large, so each one lives in its own file."""

    def __init__(self):
        self.dir = DATA_DIR / "runs"

    def put(self, run: dict) -> dict:
        with _lock:
            _atomic_write(self.dir / f"{run['id']}.json", run)
        return run

    def get(self, rid: str) -> dict | None:
        p = self.dir / f"{rid}.json"
        if not p.exists():
            return None
        with open(p) as f:
            return json.load(f)

    def delete(self, rid: str) -> None:
        (self.dir / f"{rid}.json").unlink(missing_ok=True)

    def summaries(self) -> list[dict]:
        out = []
        if not self.dir.exists():
            return out
        for p in self.dir.glob("*.json"):
            try:
                with open(p) as f:
                    r = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            out.append({k: r.get(k) for k in (
                "id", "name", "definition_id", "schedule_id", "trigger", "status",
                "started", "finished", "row_count", "error", "share_name", "cluster_name", "mode")})
        return sorted(out, key=lambda r: r.get("started") or 0, reverse=True)


shares = Collection("shares")
definitions = Collection("definitions")
schedules = Collection("schedules")
runs = RunStore()
