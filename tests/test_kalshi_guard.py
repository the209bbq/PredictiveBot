import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from polymarket_bot.exchanges.kalshi_auth import (
    load_demo_credentials,
    load_private_key,
    normalize_pem,
    sign_request,
    signing_path,
)
from polymarket_bot.guard import (
    DemoOrderError,
    assert_kalshi_demo_orders_allowed,
    is_kalshi_demo_url,
    is_kalshi_production_url,
)


def _ed25519_pem() -> tuple[Ed25519PrivateKey, str]:
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return key, pem


def _rsa_pem() -> tuple[rsa.RSAPrivateKey, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return key, pem


def test_url_classification():
    assert is_kalshi_demo_url("https://demo-api.kalshi.co/trade-api/v2")
    assert is_kalshi_demo_url("https://external-api.demo.kalshi.co/trade-api/v2")
    assert is_kalshi_production_url("https://external-api.kalshi.com/trade-api/v2")
    assert is_kalshi_production_url("https://api.elections.kalshi.com/trade-api/v2")
    assert not is_kalshi_production_url("https://demo-api.kalshi.co/trade-api/v2")
    assert not is_kalshi_production_url("https://external-api.demo.kalshi.co/trade-api/v2")


def test_demo_orders_refuse_production_url():
    try:
        assert_kalshi_demo_orders_allowed(
            enabled=True,
            base_url="https://external-api.kalshi.com/trade-api/v2",
        )
        raise AssertionError("should have raised")
    except DemoOrderError as exc:
        assert "production" in str(exc).lower()


def test_demo_orders_refuse_when_disabled():
    try:
        assert_kalshi_demo_orders_allowed(
            enabled=False,
            base_url="https://demo-api.kalshi.co/trade-api/v2",
        )
        raise AssertionError("should have raised")
    except DemoOrderError:
        pass


def test_demo_orders_allow_both_demo_hosts():
    assert_kalshi_demo_orders_allowed(
        enabled=True,
        base_url="https://demo-api.kalshi.co/trade-api/v2",
    )
    assert_kalshi_demo_orders_allowed(
        enabled=True,
        base_url="https://external-api.demo.kalshi.co/trade-api/v2",
    )


def test_normalize_pem_collapsed_spaces():
    _key, pem = _ed25519_pem()
    collapsed = " ".join(pem.split())
    assert "\n" not in collapsed
    restored = normalize_pem(collapsed)
    loaded = load_private_key(collapsed)
    assert isinstance(loaded, Ed25519PrivateKey)
    assert loaded.public_key().public_bytes_raw() == _key.public_key().public_bytes_raw()
    assert "-----BEGIN PRIVATE KEY-----" in restored
    assert all(len(line) <= 64 for line in restored.splitlines() if not line.startswith("-----"))


def test_normalize_pem_literal_escaped_newlines():
    _key, pem = _ed25519_pem()
    escaped = pem.replace("\n", "\\n")
    loaded = load_private_key(escaped)
    assert isinstance(loaded, Ed25519PrivateKey)
    assert loaded.public_key().public_bytes_raw() == _key.public_key().public_bytes_raw()


def test_signing_path_includes_v2_and_strips_query():
    path = signing_path(
        "https://demo-api.kalshi.co/trade-api/v2",
        "/portfolio/balance?limit=5",
    )
    assert path == "/trade-api/v2/portfolio/balance"
    assert "?" not in path


def test_ed25519_signs_presign_text_directly():
    key, pem = _ed25519_pem()
    loaded = load_private_key(pem)
    timestamp = "1703123456789"
    method = "GET"
    path = "/trade-api/v2/portfolio/balance"
    got = sign_request(loaded, timestamp, method, path)
    expected = base64.b64encode(key.sign(f"{timestamp}{method}{path}".encode())).decode()
    assert got == expected
    loaded.public_key().verify(base64.b64decode(got), f"{timestamp}{method}{path}".encode())


def test_rsa_signs_with_pss_sha256():
    key, pem = _rsa_pem()
    loaded = load_private_key(pem)
    assert isinstance(loaded, rsa.RSAPrivateKey)
    timestamp = "1703123456789"
    method = "POST"
    path = "/trade-api/v2/portfolio/events/orders"
    got = sign_request(loaded, timestamp, method, path)
    key.public_key().verify(
        base64.b64decode(got),
        f"{timestamp}{method}{path}".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_load_demo_credentials_prefers_env_contents(monkeypatch, tmp_path):
    key, pem = _ed25519_pem()
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "demo-key-id")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", " ".join(pem.split()))
    monkeypatch.delenv("KALSHI_DEMO_PRIVATE_KEY_PATH", raising=False)
    key_id, loaded = load_demo_credentials()
    assert key_id == "demo-key-id"
    assert isinstance(loaded, Ed25519PrivateKey)
    assert loaded.public_key().public_bytes_raw() == key.public_key().public_bytes_raw()


def test_load_demo_credentials_path_fallback(monkeypatch, tmp_path):
    _key, pem = _rsa_pem()
    path = tmp_path / "kalshi.pem"
    path.write_text(pem)
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "from-path")
    monkeypatch.delenv("KALSHI_DEMO_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY_PATH", str(path))
    key_id, loaded = load_demo_credentials()
    assert key_id == "from-path"
    assert isinstance(loaded, rsa.RSAPrivateKey)


def test_load_demo_credentials_missing(monkeypatch):
    monkeypatch.delenv("KALSHI_DEMO_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_DEMO_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("KALSHI_DEMO_PRIVATE_KEY_PATH", raising=False)
    try:
        load_demo_credentials()
        raise AssertionError("should have raised")
    except DemoOrderError:
        pass
