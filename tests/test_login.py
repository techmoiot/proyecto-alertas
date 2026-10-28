import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from test_api import FakeClient, make_cfg


def new_app(**over):
    FakeClient.fail = False
    return create_app(make_cfg(**over), start_poller=False, client_factory=FakeClient)


@pytest.fixture
def web():
    with TestClient(new_app()) as c:
        yield c


def login(c, user="admin", pw="pw-admin", remember=False):
    return c.post("/api/login", json={"username": user, "password": pw, "remember": remember})


def test_login_page_is_public_and_branded(web):
    r = web.get("/login")
    assert r.status_code == 200 and "MAGI" in r.text and "MELCHIOR" in r.text and "CASPER" in r.text
    assert 'type="password"' in r.text and 'autocomplete="current-password"' in r.text
    assert "<script src" not in r.text            # sin recursos externos: la pantalla funciona sin sesión


def test_unauthenticated_page_redirects_and_api_returns_401_without_browser_popup(web):
    r = web.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    r = web.get("/api/state", headers={"X-Requested-With": "MAGI"})
    assert r.status_code == 401 and "www-authenticate" not in r.headers      # así el navegador no abre el cuadro viejo
    assert "Basic" in web.get("/api/state").headers["www-authenticate"]


def test_login_sets_httponly_cookie_and_unlocks_everything(web):
    assert login(web, pw="mala").status_code == 401
    assert login(web, user="otro").status_code == 401
    r = login(web)
    assert r.status_code == 200 and r.json()["user"] == "admin"
    set_cookie = r.headers["set-cookie"]
    assert "magi_session=" in set_cookie and "HttpOnly" in set_cookie and "SameSite=lax" in set_cookie
    assert "Max-Age" not in set_cookie                                         # sin «mantener»: cookie de sesión
    assert web.get("/api/state").status_code == 200
    assert web.get("/", follow_redirects=False).status_code == 200
    assert web.get("/app.js").status_code == 200
    me = web.get("/api/me").json()
    assert me["authed"] is True and me["user"] == "admin"
    assert web.get("/login", follow_redirects=False).status_code == 302        # ya con sesión: va al panel
    assert web.post("/api/routers", json={"name": "x", "host": "1.1.1.1", "username": "u", "password": "p"}).status_code == 200


def test_remember_me_sets_a_30_day_cookie():
    with TestClient(new_app()) as c:
        r = login(c, remember=True)
        assert "Max-Age=2592000" in r.headers["set-cookie"]


def test_logout_closes_the_session(web):
    login(web)
    assert web.post("/api/logout").status_code == 200
    web.cookies.clear()
    assert web.get("/api/state").status_code == 401


def test_tampered_or_garbage_cookies_are_rejected(web):
    login(web)
    good = web.cookies.get("magi_session")
    user, exp, sig = good.rsplit("|", 2)
    for bad in (good[:-2] + "00", f"{user}|9999999999|{sig}", "basura", "", f"otro|{exp}|{sig}", f"{user}|1|{sig}"):
        web.cookies.clear()
        web.cookies.set("magi_session", bad)
        assert web.get("/api/state").status_code == 401, bad


def test_expired_session_is_rejected(web, monkeypatch):
    import time as _t

    login(web)
    real = _t.time
    monkeypatch.setattr("app.main.time.time", lambda: real() + 13 * 3600)        # pasaron 13 h (>12 h)
    assert web.get("/api/state").status_code == 401


def test_basic_auth_still_works_for_scripts(web):
    assert web.get("/api/state", auth=("admin", "pw-admin")).status_code == 200
    assert web.get("/api/state", auth=("admin", "mala")).status_code == 401


def test_brute_force_is_rate_limited_per_ip(web):
    for _ in range(6):
        assert login(web, pw="x").status_code == 401
    r = login(web, pw="x")
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert login(web).status_code == 429                                        # ni con la clave correcta durante el bloqueo


def test_successful_login_resets_the_counter(web):
    for _ in range(4):
        login(web, pw="x")
    assert login(web).status_code == 200
    for _ in range(4):
        assert login(web, pw="x").status_code == 401


def test_public_view_allows_reading_without_login_but_not_changes():
    with TestClient(new_app(public_view=True)) as c:
        assert c.get("/api/state").status_code == 200
        assert c.get("/", follow_redirects=False).status_code == 200
        assert c.get("/api/me").json() == {"authed": False, "user": None, "role": None, "public_view": True}
        assert c.post("/api/routers", json={}).status_code in (401, 422)
        assert c.get("/api/settings").status_code == 401


def test_changing_the_password_invalidates_old_sessions(tmp_path):
    db = str(tmp_path / "m.db")
    with TestClient(new_app(db_path=db)) as c:
        login(c)
        old = c.cookies.get("magi_session")
        assert c.post("/api/me/password", json={"current": "pw-admin", "new": "otra-clave-9"}).status_code == 200
        assert c.get("/api/state").status_code == 200                      # su propia sesión se renueva
    with TestClient(new_app(db_path=db, admin_password="ignorada-ahora")) as c2:
        c2.cookies.set("magi_session", old)
        assert c2.get("/api/state").status_code == 401                     # la sesión vieja ya no vale
        assert login(c2, pw="ignorada-ahora").status_code == 401           # el entorno solo siembra el primer arranque
        assert login(c2, pw="pw-admin").status_code == 401
        assert login(c2, pw="otra-clave-9").status_code == 200
