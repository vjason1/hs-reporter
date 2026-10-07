import json

import pytest

from app import auth


@pytest.fixture
def clean_auth(monkeypatch):
    """Start without sign-in; whatever a test sets up is removed afterwards."""
    monkeypatch.delenv("HSR_USER", raising=False)
    monkeypatch.delenv("HSR_PASSWORD", raising=False)
    auth.PATH.unlink(missing_ok=True)
    auth._verified.clear()
    yield monkeypatch
    auth.PATH.unlink(missing_ok=True)
    auth._verified.clear()


def test_environment_sign_in(client, clean_auth):
    clean_auth.setenv("HSR_USER", "admin")
    clean_auth.setenv("HSR_PASSWORD", "from-compose")
    assert client.get("/api/shares").status_code == 401
    assert client.get("/api/health").status_code == 200                 # probes stay open
    assert client.get("/api/shares", auth=("admin", "from-compose")).status_code == 200
    assert client.get("/api/auth", auth=("admin", "from-compose")).json()["source"] == "environment"


def test_change_password(client, clean_auth):
    clean_auth.setenv("HSR_USER", "admin")
    clean_auth.setenv("HSR_PASSWORD", "from-compose")
    old = ("admin", "from-compose")
    post = lambda body, a=old: client.post("/api/auth/password", json=body, auth=a)
    assert "isn't right" in post({"current": "nope", "new": "n3w-password", "confirm": "n3w-password"}).json()["detail"]
    assert "at least 8" in post({"current": "from-compose", "new": "short", "confirm": "short"}).json()["detail"]
    assert "don't match" in post({"current": "from-compose", "new": "n3w-password", "confirm": "other-pass"}).json()["detail"]
    r = post({"current": "from-compose", "new": "n3w-password", "confirm": "n3w-password"})
    assert r.status_code == 200 and r.json()["source"] == "settings"
    assert client.get("/api/shares", auth=old).status_code == 401       # the old password stops working
    assert client.get("/api/shares", auth=("admin", "n3w-password")).status_code == 200
    stored = json.loads(auth.PATH.read_text())
    assert "n3w-password" not in auth.PATH.read_text() and stored["algorithm"].startswith("pbkdf2")
    # Lost password: removing auth.json goes back to HSR_USER / HSR_PASSWORD
    auth.PATH.unlink()
    assert client.get("/api/shares", auth=old).status_code == 200


def test_turn_on_sign_in(client, clean_auth):
    assert client.get("/api/auth").json()["enabled"] is False
    bad = client.post("/api/auth/enable", json={"username": "a b", "password": "longenough", "confirm": "longenough"})
    assert bad.status_code == 422
    r = client.post("/api/auth/enable", json={"username": "ops", "password": "longenough", "confirm": "longenough"})
    assert r.json() == {"enabled": True, "username": "ops", "source": "settings"}
    assert client.get("/api/shares").status_code == 401
    assert client.get("/api/shares", auth=("ops", "longenough")).status_code == 200
    again = client.post("/api/auth/enable", json={"username": "x", "password": "longenough", "confirm": "longenough"},
                        auth=("ops", "longenough"))
    assert "already on" in again.json()["detail"]
