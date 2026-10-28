import pytest
from fastapi.testclient import TestClient

from app.__main__ import passwd
from app.topology import ConfigError
from app.users import Users, hash_password, verify_hash
from test_login import login, new_app


@pytest.fixture
def web():
    with TestClient(new_app()) as c:
        assert login(c).status_code == 200
        yield c


def add(c, name="maria", pw="clave-larga-1", role="viewer"):
    return c.post("/api/users", json={"username": name, "password": pw, "role": role})


def test_hash_is_salted_and_verifies():
    a, b = hash_password("secreto-123"), hash_password("secreto-123")
    assert a != b and a.startswith("scrypt$") and "secreto" not in a
    assert verify_hash("secreto-123", a) and not verify_hash("otra", a) and not verify_hash("x", "basura")


def test_first_start_seeds_admin_and_lists_without_hashes(web):
    rows = web.get("/api/users").json()
    assert [(r["username"], r["role"]) for r in rows] == [("admin", "admin")]
    assert "pw_hash" not in web.get("/api/users").text
    assert web.get("/api/me").json()["role"] == "admin"


def test_create_user_and_login_with_it(web):
    assert add(web).status_code == 200
    assert add(web).status_code == 400                                   # repetido
    assert add(web, "MARIA", "otra-clave-22").status_code == 400         # mayúsculas cuentan como repetido
    assert add(web, "x y", "clave-larga-1").status_code == 400           # nombre inválido
    assert add(web, "juan", "corta").status_code == 400                  # clave corta
    assert add(web, "juan", "clave-larga-1", "root").status_code == 400  # rol inválido
    with TestClient(web.app) as other:
        assert login(other, "maria", "clave-larga-1").status_code == 200
        assert login(other, "maria", "mala").status_code == 401
        assert other.get("/api/me").json()["user"] == "maria"


def test_viewer_can_look_but_not_change(web):
    add(web)
    with TestClient(web.app) as v:
        login(v, "maria", "clave-larga-1")
        assert v.get("/api/state").status_code == 200
        assert v.get("/api/network").status_code == 200
        assert v.get("/", follow_redirects=False).status_code == 200
        for r in (v.get("/api/users"), v.get("/api/routers"), v.get("/api/settings"),
                  v.post("/api/users", json={"username": "zz", "password": "clave-larga-1"}),
                  v.post("/api/alerts/ack_all")):
            assert r.status_code == 403, r.request.url
        assert v.delete("/api/users/1").status_code == 403
        assert v.put("/api/users/1", json={"password": "hackeada-123"}).status_code == 403
        # pero sí puede cambiar su propia clave
        assert v.post("/api/me/password", json={"current": "clave-larga-1", "new": "nueva-clave-5"}).status_code == 200


def test_admin_resets_password_and_kills_that_users_sessions(web):
    uid = add(web).json()["id"]
    with TestClient(web.app) as v:
        login(v, "maria", "clave-larga-1")
        assert v.get("/api/state").status_code == 200
        assert web.put(f"/api/users/{uid}", json={"password": "reseteada-77"}).status_code == 200
        assert v.get("/api/state").status_code == 401
        assert login(v, "maria", "clave-larga-1").status_code == 401
        assert login(v, "maria", "reseteada-77").status_code == 200
    assert web.get("/api/state").status_code == 200                    # el admin sigue adentro


def test_change_own_password_needs_current_and_rules(web):
    assert web.post("/api/me/password", json={"current": "mal", "new": "nueva-clave-5"}).status_code == 400
    assert web.post("/api/me/password", json={"current": "pw-admin", "new": "corta"}).status_code == 400
    assert web.post("/api/me/password", json={"current": "pw-admin", "new": "nueva-clave-5"}).status_code == 200
    with TestClient(web.app) as c:
        assert login(c, pw="pw-admin").status_code == 401
        assert login(c, pw="nueva-clave-5").status_code == 200


def test_anonymous_cannot_change_password():
    with TestClient(new_app()) as c:
        assert c.post("/api/me/password", json={"current": "a", "new": "bbbbbbbbb"}).status_code == 401


def test_cannot_delete_self_or_last_admin_or_demote_last_admin(web):
    me = web.get("/api/users").json()[0]["id"]
    assert web.delete(f"/api/users/{me}").status_code == 400
    assert web.put(f"/api/users/{me}", json={"role": "viewer"}).status_code == 400
    mid = add(web, "otro", "clave-larga-1", "admin").json()["id"]
    assert web.put(f"/api/users/{mid}", json={"role": "viewer"}).status_code == 200
    assert web.delete(f"/api/users/{mid}").status_code == 200
    assert web.delete("/api/users/999").status_code == 404


def test_basic_auth_still_works_for_scripts(web):
    add(web)
    with TestClient(web.app) as c:
        assert c.get("/api/state", auth=("maria", "clave-larga-1")).status_code == 200
        assert c.get("/api/settings", auth=("maria", "clave-larga-1")).status_code == 403
        assert c.get("/api/settings", auth=("admin", "pw-admin")).status_code == 200


def test_cli_passwd_resets_or_creates_admin(tmp_path, monkeypatch):
    db = str(tmp_path / "m.db")
    new_app(db_path=db)                                  # crea el admin sembrado
    monkeypatch.setenv("MKMON_DB", db)
    answers = iter(["recuperada-88", "recuperada-88"])
    monkeypatch.setattr("getpass.getpass", lambda *_: next(answers))
    assert passwd(None) == 0
    with TestClient(new_app(db_path=db)) as c:
        assert login(c, pw="recuperada-88").status_code == 200
    answers = iter(["nuevo-admin-1", "nuevo-admin-1"])
    assert passwd("rescate") == 0
    with TestClient(new_app(db_path=db)) as c:
        assert login(c, "rescate", "nuevo-admin-1").status_code == 200
        assert c.get("/api/settings").status_code == 200
    answers = iter(["uno-uno-uno", "dos-dos-dos"])
    assert passwd("rescate") == 1                        # no coinciden


def test_users_class_enforces_rules():
    from app.db import DB
    u = Users(DB(":memory:"))
    a = u.create("ana", "clave-larga-1", "admin")
    assert u.authenticate("ANA", "clave-larga-1")["id"] == a
    assert u.authenticate("nadie", "x") is None
