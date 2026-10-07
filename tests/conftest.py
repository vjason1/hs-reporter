"""Test setup: point the app at a temporary data dir, a temporary 'share', and fake_hs.py.
The app reads its settings at import time, so the environment is set before importing it."""
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = Path(tempfile.mkdtemp(prefix="hsr-test-"))
SHARE = _tmp / "external"
for d in ("proj/a/a1", "proj/b", "proj/empty", "proj/.hidden"):
    (SHARE / d).mkdir(parents=True, exist_ok=True)
# Git or an unzip can drop the execute bit; the stand-in hs must be runnable.
FAKE_HS = Path(__file__).parent / "fake_hs.py"
FAKE_HS.chmod(0o755)
os.environ.update({
    "HSR_DATA_DIR": str(_tmp / "data"),
    "HSR_LOCAL_ROOT": str(SHARE),
    "HSR_MOUNT_ROOT": str(_tmp / "mnt"),
    "HSR_HS_BIN": str(Path(__file__).parent / "fake_hs.py"),
})
os.environ.pop("HSR_USER", None)


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def share(client):
    return client.post("/api/shares", json={"name": "lab", "kind": "local", "local_path": str(SHARE)}).json()


def wait_for(client, run_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/runs/{run_id}").json()
        if r["status"] not in ("queued", "running"):
            return r
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not finish")
