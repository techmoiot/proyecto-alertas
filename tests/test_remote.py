import base64
import importlib.util
import ipaddress
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.wg import RemoteError, WGManager, generate_keypair, public_from_private, valid_key, x25519
from test_api import AUTH, FakeClient, make_cfg

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ criptografía


def test_x25519_matches_rfc7748_vectors():
    k = bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
    u = bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
    assert x25519(k, u).hex() == "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552"
    alice = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    bob = bytes.fromhex("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
    base = (9).to_bytes(32, "little")
    pa, pb = x25519(alice, base), x25519(bob, base)
    assert pa.hex() == "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    assert pb.hex() == "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f"
    assert x25519(alice, pb) == x25519(bob, pa)           # secreto compartido idéntico


def test_generated_keys_look_like_wireguard_keys():
    priv, pub = generate_keypair()
    assert valid_key(priv) and valid_key(pub) and priv != pub
    assert public_from_private(priv) == pub
    raw = base64.b64decode(priv)
    assert raw[0] & 7 == 0 and raw[31] & 0x80 == 0 and raw[31] & 0x40 == 0x40     # clave "clamped"
    assert generate_keypair()[0] != priv
    assert not valid_key("corta") and not valid_key("A" * 44)


# ----------------------------------------------------------------------- manager


@pytest.fixture
def env(tmp_path):
    pub = tmp_path / "server.pub"
    pub.write_text(generate_keypair()[1] + "\n")
    run = tmp_path / "run"
    run.mkdir()
    return dict(wg_state_dir=str(tmp_path / "state"), wg_run_dir=str(run), wg_pub_path=str(pub))


@pytest.fixture
def mgr(db, env):
    return WGManager(db, make_cfg(**env))


def add_router(db, name="Remoto", host="10.77.0.2", enabled=0):
    return db.execute(
        "INSERT INTO routers(name, host, port, username, password, enabled, updated_at) VALUES(?,?,?,?,?,?,?)",
        (name, host, 8728, "mkmonitor", "pw", enabled, time.time())).lastrowid


def test_ip_allocation_skips_server_and_reuses_freed_addresses(mgr, db):
    assert mgr.server_ip == "10.77.0.1"
    r1, r2, r3 = (add_router(db, f"R{i}", f"10.9.0.{i}") for i in (1, 2, 3))
    ips = [mgr.create_peer(r)["ip"] for r in (r1, r2, r3)]
    assert ips == ["10.77.0.2", "10.77.0.3", "10.77.0.4"]
    mgr.remove_peer(r2)
    assert mgr.create_peer(add_router(db, "R4", "10.9.0.4"))["ip"] == "10.77.0.3"


def test_network_full_raises(db, env):
    m = WGManager(db, make_cfg(**{**env, "wg_network": "10.77.0.0/30"}))     # 2 hosts: servidor + 1
    m.create_peer(add_router(db, "A", "1.1.1.1"))
    with pytest.raises(RemoteError, match="No quedan"):
        m.create_peer(add_router(db, "B", "1.1.1.2"))


def test_peers_file_contains_only_public_data(mgr, db, env):
    rid = add_router(db, "Torre Norte")
    peer = mgr.create_peer(rid)
    text = (Path(env["wg_state_dir"]) / "peers.conf").read_text()
    assert f"PublicKey = {peer['public_key']}" in text and f"AllowedIPs = {peer['ip']}/32" in text
    assert peer["private_key"] not in text and "Torre Norte" in text
    assert (Path(env["wg_state_dir"]) / "apply.trigger").exists()
    mgr.remove_peer(rid)
    assert "PublicKey" not in (Path(env["wg_state_dir"]) / "peers.conf").read_text()


def test_write_error_is_reported_not_raised(db, env, tmp_path):
    blocker = tmp_path / "archivo"
    blocker.write_text("x")
    m = WGManager(db, make_cfg(**{**env, "wg_state_dir": str(blocker / "sub")}))
    m.create_peer(add_router(db))
    assert "No se pudo escribir" in m.status()["write_error"]


def test_endpoint_validation_and_persistence(mgr):
    assert mgr.endpoint() == ""
    assert mgr.set_endpoint(" vpn.ejemplo.com ") == "vpn.ejemplo.com" and mgr.endpoint() == "vpn.ejemplo.com"
    assert mgr.set_endpoint("200.1.2.3") == "200.1.2.3"
    for bad in ("http://x.com", "x.com:51820", "a b", "x;rm -rf /", '"x"'):
        with pytest.raises(RemoteError):
            mgr.set_endpoint(bad)


def test_status_and_activation_when_tunnel_connects(mgr, db, env):
    rid = add_router(db, enabled=0)
    peer = mgr.create_peer(rid)
    assert mgr.status()["helper_running"] is False
    mgr.tick()
    assert db.one("SELECT enabled FROM routers WHERE id=?", (rid,))["enabled"] == 0
    hs = Path(env["wg_run_dir"]) / "handshakes.txt"
    hs.write_text(f"{peer['public_key']}\t0\n")                    # peer sin handshake todavía
    mgr._last_tick = 0
    mgr.tick()
    assert db.one("SELECT enabled FROM routers WHERE id=?", (rid,))["enabled"] == 0
    hs.write_text(f"{peer['public_key']}\t{int(time.time())}\nbasura sin formato\n")
    mgr._last_tick = 0
    mgr.tick()
    assert db.one("SELECT enabled FROM routers WHERE id=?", (rid,))["enabled"] == 1
    st = mgr.status()
    assert st["helper_running"] and st["peers"][0]["connected"] and st["peers"][0]["activated"]
    hs.write_text(f"{peer['public_key']}\t{int(time.time()) - 3600}\n")
    assert mgr.status()["peers"][0]["connected"] is False           # handshake viejo = caído


# ------------------------------------------------------------------------ script


def test_script_has_every_value_and_balanced_braces(mgr, db):
    mgr.set_endpoint("vpn.ejemplo.com")
    rid = add_router(db, "Torre Norte")
    peer = mgr.create_peer(rid)
    router = db.one("SELECT * FROM routers WHERE id=?", (rid,))
    text = mgr.render_script(router, peer, 5140)
    assert "__" not in re.sub(r"#.*", "", text)                      # no quedaron marcadores sin reemplazar
    for needle in (mgr.server_pub(), "vpn.ejemplo.com", peer["private_key"], "10.77.0.2/24", "pw",
                   ":local srvIp \"10.77.0.1\"", ":local syslogPort 5140", ":local srvPort 51821"):
        assert needle in text, needle
    body = re.sub(r"#.*", "", text)
    assert body.count("{") == body.count("}")
    assert text.index("/interface wireguard add") < text.index("/ip service set api")    # primero el túnel


def test_script_needs_endpoint_and_server_key(db, env, tmp_path):
    router = {"name": "x", "username": "u", "password": "p", "port": 8728}
    peer = {"private_key": "k", "ip": "10.77.0.2"}
    m = WGManager(db, make_cfg(**{**env, "wg_pub_path": str(tmp_path / "no-existe")}))
    with pytest.raises(RemoteError, match="WireGuard"):
        m.render_script(router, peer, 5140)
    m2 = WGManager(db, make_cfg(**env))
    with pytest.raises(RemoteError, match="IP pública"):
        m2.render_script(router, peer, 5140)


# ----------------------------------------------------------- ayudante root (parser)


def load_helper():
    spec = importlib.util.spec_from_file_location("wg_apply", ROOT / "scripts" / "wg-apply.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_root_helper_accepts_only_strict_peers():
    h = load_helper()
    net, srv = ipaddress.ip_network("10.77.0.0/24"), ipaddress.ip_address("10.77.0.1")
    good = generate_keypair()[1]
    other = generate_keypair()[1]
    text = "\n".join([
        "[Peer]", "# ok", f"PublicKey = {good}", "AllowedIPs = 10.77.0.2/32", "",
        "[Interface]", "PostUp = rm -rf /", "",                                   # inyección: se ignora
        "[Peer]", f"PublicKey = {other}", "AllowedIPs = 10.77.0.1/32", "",         # IP del servidor: no
        "[Peer]", f"PublicKey = {other}", "AllowedIPs = 192.168.0.5/32", "",       # fuera de la red: no
        "[Peer]", f"PublicKey = {other}", "AllowedIPs = 10.77.0.0/24", "",         # no es /32: no
        "[Peer]", "PublicKey = no-es-una-clave", "AllowedIPs = 10.77.0.9/32", "",
        "[Peer]", f"PublicKey = {other}", "AllowedIPs = 10.77.0.7/32, 0.0.0.0/0", "",   # rutas extra: no
        "[Peer]", f"PublicKey = {other}", "AllowedIPs = 10.77.0.3/32",                 # último, sin línea en blanco
    ])
    assert h.parse_peers(text, net, srv) == [(good, "10.77.0.2"), (other, "10.77.0.3")]
    assert "PostUp" not in str(h.parse_peers(text, net, srv))


# --------------------------------------------------------------------------- API


@pytest.fixture
def api(env):
    FakeClient.fail = False
    app = create_app(make_cfg(**env), start_poller=False, client_factory=FakeClient)
    with TestClient(app) as c:
        c.auth = AUTH
        c.env = env
        yield c


def test_remote_api_requires_admin(env):
    app = create_app(make_cfg(**env), start_poller=False, client_factory=FakeClient)
    with TestClient(app) as c:
        for m, p in (("get", "/api/remote/status"), ("put", "/api/remote/endpoint"), ("post", "/api/remote/routers"),
                     ("get", "/api/remote/routers/1/script")):
            assert getattr(c, m)(p).status_code == 401, p


def test_remote_router_lifecycle(api):
    assert api.post("/api/remote/routers", json={"name": "Torre"}).status_code == 409        # falta el endpoint
    assert api.put("/api/remote/endpoint", json={"endpoint": "http://x"}).status_code == 400
    assert api.put("/api/remote/endpoint", json={"endpoint": "vpn.ejemplo.com"}).json()["endpoint"] == "vpn.ejemplo.com"
    r = api.post("/api/remote/routers", json={"name": "Torre"})
    assert r.status_code == 200, r.text
    rid, ip = r.json()["id"], r.json()["ip"]
    assert ip == "10.77.0.2"
    assert api.post("/api/remote/routers", json={"name": "torre"}).status_code == 400         # nombre repetido

    row = [x for x in api.get("/api/routers").json() if x["id"] == rid][0]
    assert row["host"] == ip and row["port"] == 8728 and not row["enabled"] and row["username"] == "mkmonitor"
    script = api.get(f"/api/remote/routers/{rid}/script")
    assert script.status_code == 200 and "magi-wg" in script.text and ip + "/24" in script.text
    secret_pw = api._transport.app.state.db.one("SELECT password FROM routers WHERE id=?", (rid,))["password"]
    assert secret_pw in script.text and len(secret_pw) >= 20                                   # contraseña generada por el servidor
    assert script.text == api.get(f"/api/remote/routers/{rid}/script").text                    # reproducible

    st = api.get("/api/remote/status").json()
    assert st["server_ready"] and st["endpoint"] == "vpn.ejemplo.com" and st["peers"][0]["name"] == "Torre"
    assert api.get("/api/remote/routers/999/script").status_code == 404

    peers = (Path(api.env["wg_state_dir"]) / "peers.conf").read_text()
    assert "10.77.0.2/32" in peers
    assert api.delete(f"/api/routers/{rid}").status_code == 200
    assert "10.77.0.2" not in (Path(api.env["wg_state_dir"]) / "peers.conf").read_text()
    assert api.get("/api/remote/status").json()["peers"] == []


def test_remote_requires_server_key(tmp_path):
    env = dict(wg_state_dir=str(tmp_path / "s"), wg_run_dir=str(tmp_path), wg_pub_path=str(tmp_path / "nada"))
    app = create_app(make_cfg(**env), start_poller=False, client_factory=FakeClient)
    with TestClient(app) as c:
        c.auth = AUTH
        c.put("/api/remote/endpoint", json={"endpoint": "1.2.3.4"})
        r = c.post("/api/remote/routers", json={"name": "X"})
        assert r.status_code == 409 and "WireGuard" in r.json()["detail"]
        assert c.get("/api/routers").json() == []
