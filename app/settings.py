"""Global settings: crawl speed, limits and mount defaults, for reporting and objective planning.

Edited on the Settings page and stored in <data>/settings.json. Environment variables give
the initial values; once saved in the GUI, the stored values win. Changes apply to new
commands without a restart.
"""
import asyncio
import json
import os
import threading

from . import store

PATH = store.DATA_DIR / "settings.json"
_lock = threading.RLock()
_cache = {"mtime": None, "values": None}


def _env(name, default, cast):
    v = os.environ.get(name)
    try:
        return cast(v) if v not in (None, "") else default
    except ValueError:
        return default


# Crawl speed presets: hs commands at once, seconds between commands, folder listings per second
CRAWL_PRESETS = {
    "normal": {"label": "Normal", "concurrency": 4, "pause": 0.0, "list_rate": 0.0},
    "gentle": {"label": "Gentle", "concurrency": 1, "pause": 1.0, "list_rate": 20.0},
    "slowest": {"label": "Slowest", "concurrency": 1, "pause": 5.0, "list_rate": 5.0},
}

# key: (type, min, max, label)
SPEC = {
    "crawl_concurrency": (int, 1, 16, "hs commands at once (custom crawl speed)"),
    "crawl_pause": (float, 0, 3600, "Pause between commands (custom crawl speed)"),
    "crawl_list_rate": (float, 0, 1000, "Folder listings per second (custom crawl speed)"),
    "max_hs_processes": (int, 1, 32, "hs commands at once, across everything"),
    "max_concurrent": (int, 1, 16, "Reports and scans at once"),
    "run_timeout": (int, 30, 86400, "Timeout per hs command"),
    "max_output_mb": (int, 1, 2048, "Output kept per hs command"),
    "max_folders": (int, 1, 200000, "Most folders per per-folder report"),
    "plan_max_files": (int, 1000, 50_000_000, "Most entries per objective-planning scan"),
    "plan_batch": (int, 1, 1000, "Files per hs eval call when gathering file by file"),
}


def defaults() -> dict:
    """Initial values, from the environment where set (the variables documented before the
    Settings page existed keep working)."""
    env_crawl = any(os.environ.get(k) for k in ("HSR_FOLDER_CONCURRENCY", "HSR_COMMAND_PAUSE", "HSR_LIST_RATE"))
    normal = CRAWL_PRESETS["normal"]
    return {
        "crawl_preset": os.environ.get("HSR_CRAWL_SPEED") or ("custom" if env_crawl else "normal"),
        "crawl_concurrency": _env("HSR_FOLDER_CONCURRENCY", normal["concurrency"], int),
        "crawl_pause": _env("HSR_COMMAND_PAUSE", normal["pause"], float),
        "crawl_list_rate": _env("HSR_LIST_RATE", normal["list_rate"], float),
        "max_hs_processes": _env("HSR_MAX_HS_PROCESSES", 4, int),
        "max_concurrent": _env("HSR_MAX_CONCURRENT", 2, int),
        "run_timeout": _env("HSR_RUN_TIMEOUT", 3600, int),
        "max_output_mb": _env("HSR_MAX_OUTPUT_MB", 50, int),
        "max_folders": _env("HSR_MAX_FOLDERS", 2000, int),
        "plan_max_files": _env("HSR_PLAN_MAX_FILES", 1_000_000, int),
        "plan_batch": _env("HSR_PLAN_BATCH", 100, int),
        "nfs_options": os.environ.get("HSR_NFS_OPTIONS") or "vers=3,nolock",
        "smb_options": os.environ.get("HSR_SMB_OPTIONS") or "vers=3.0,noserverino,cache=none,actimeo=0",
    }


def _saved() -> dict:
    try:
        mtime = PATH.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] != mtime:
        try:
            with open(PATH) as f:
                _cache["values"] = json.load(f)
        except (OSError, json.JSONDecodeError):
            _cache["values"] = {}
        _cache["mtime"] = mtime
    return _cache["values"] or {}


def get() -> dict:
    with _lock:
        return {**defaults(), **{k: v for k, v in _saved().items() if k in defaults()}}


class SettingsError(ValueError):
    pass


def validate(patch: dict) -> dict:
    out = {}
    for k, v in patch.items():
        if k == "crawl_preset":
            if v not in (*CRAWL_PRESETS, "custom"):
                raise SettingsError("Crawl speed must be normal, gentle, slowest or custom")
            out[k] = v
        elif k in ("nfs_options", "smb_options"):
            v = str(v or "").strip()
            if not v or any(c.isspace() for c in v):
                raise SettingsError("Mount options are comma-separated, without spaces (e.g. vers=3,nolock)")
            out[k] = v
        elif k in SPEC:
            cast, lo, hi, label = SPEC[k]
            try:
                n = cast(v)
            except (TypeError, ValueError):
                raise SettingsError(f"{label} must be a number")
            if not lo <= n <= hi:
                raise SettingsError(f"{label} must be between {lo:g} and {hi:g}")
            out[k] = n
    return out


def update(patch: dict) -> dict:
    clean = validate(patch)
    with _lock:
        # Keep only values that differ from the defaults, so environment variables still
        # set everything that hasn't been changed in the GUI.
        d = defaults()
        saved = {k: v for k, v in {**_saved(), **clean}.items() if k in d and v != d[k]}
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(saved, f, indent=2)
        os.replace(tmp, PATH)
        _cache["mtime"] = None
    return get()


def reset() -> dict:
    with _lock:
        PATH.unlink(missing_ok=True)
        _cache["mtime"] = None
    return get()


def crawl() -> dict:
    """The crawl speed in effect: a preset or the custom values."""
    s = get()
    p = s["crawl_preset"]
    if p in CRAWL_PRESETS:
        c = CRAWL_PRESETS[p]
        return {"preset": p, "concurrency": c["concurrency"], "pause": c["pause"], "list_rate": c["list_rate"]}
    return {"preset": "custom", "concurrency": s["crawl_concurrency"], "pause": s["crawl_pause"],
            "list_rate": s["crawl_list_rate"]}


class Limiter:
    """Like a semaphore, but its limit is read from settings each time, so a change on the
    Settings page applies to queued work within a second, without a restart."""

    def __init__(self, key: str):
        self.key = key
        self.active = 0
        self._cond = None

    def limit(self) -> int:
        return max(1, int(get()[self.key]))

    async def __aenter__(self):
        if self._cond is None:
            self._cond = asyncio.Condition()
        async with self._cond:
            while self.active >= self.limit():
                try:
                    await asyncio.wait_for(self._cond.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
            self.active += 1

    async def __aexit__(self, *exc):
        async with self._cond:
            self.active -= 1
            self._cond.notify_all()
