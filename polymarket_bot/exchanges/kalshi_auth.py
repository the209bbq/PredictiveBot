"""Kalshi request signing. Official SDKs are RSA-only; this supports Ed25519 too.

https://docs.kalshi.com/getting_started/api_keys
Pre-sign text is timestamp + METHOD + path. Path includes /trade-api/v2 and
excludes the query string. Ed25519 signs that text directly; RSA uses RSA-PSS
with SHA-256 (MGF1 SHA-256, salt length = digest length). Signature is base64.
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes

from polymarket_bot.guard import DemoOrderError

_PEM_BLOCK = re.compile(
    r"-----BEGIN ([A-Z0-9 ]+KEY)-----(.*?)-----END \1-----",
    re.DOTALL | re.IGNORECASE,
)


def normalize_pem(raw: str) -> str:
    """Rebuild a PEM when env vars collapse newlines to spaces or use \\n."""
    text = (raw or "").strip().strip("'").strip('"')
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\n", "\n").replace("\\r", "")
    match = _PEM_BLOCK.search(text)
    if not match:
        raise DemoOrderError(
            "KALSHI_DEMO_PRIVATE_KEY is not a PEM private key "
            "(expected -----BEGIN ... KEY-----)."
        )
    label = match.group(1).upper().replace("_", " ")
    body = re.sub(r"\s+", "", match.group(2))
    if not body:
        raise DemoOrderError("KALSHI_DEMO_PRIVATE_KEY PEM body is empty.")
    wrapped = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {label}-----\n{wrapped}\n-----END {label}-----\n"


def load_private_key(raw: str) -> PrivateKeyTypes:
    pem = normalize_pem(raw)
    key = serialization.load_pem_private_key(
        pem.encode("utf-8"),
        password=None,
        backend=default_backend(),
    )
    if not isinstance(key, (Ed25519PrivateKey, rsa.RSAPrivateKey)):
        raise DemoOrderError(f"Unsupported Kalshi key type: {type(key).__name__}")
    return key


def load_demo_credentials() -> tuple[str, PrivateKeyTypes]:
    """Key ID + private key from env. Contents first; path is optional."""
    key_id = (os.environ.get("KALSHI_DEMO_API_KEY_ID") or "").strip()
    pem = os.environ.get("KALSHI_DEMO_PRIVATE_KEY") or ""
    if not pem.strip():
        key_path = (os.environ.get("KALSHI_DEMO_PRIVATE_KEY_PATH") or "").strip()
        if key_path:
            pem = Path(key_path).read_text()
    if not key_id or not pem.strip():
        raise DemoOrderError(
            "Kalshi demo keys missing. Set KALSHI_DEMO_API_KEY_ID and "
            "KALSHI_DEMO_PRIVATE_KEY (PEM contents). "
            "KALSHI_DEMO_PRIVATE_KEY_PATH is optional."
        )
    return key_id, load_private_key(pem)


def signing_path(base_url: str, path: str) -> str:
    """Full URL path from the API root, with query string stripped."""
    if path.startswith("http://") or path.startswith("https://"):
        parsed = urlparse(path)
    else:
        parsed = urlparse(base_url.rstrip("/") + "/" + path.lstrip("/"))
    return parsed.path.split("?")[0]


def sign_request(private_key: PrivateKeyTypes, timestamp: str, method: str, path: str) -> str:
    path_without_query = path.split("?")[0]
    message = f"{timestamp}{method.upper()}{path_without_query}".encode("utf-8")
    if isinstance(private_key, Ed25519PrivateKey):
        signature = private_key.sign(message)
    elif isinstance(private_key, rsa.RSAPrivateKey):
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
    else:
        raise DemoOrderError(f"Unsupported Kalshi key type: {type(private_key).__name__}")
    return base64.b64encode(signature).decode("ascii")


def access_headers(*, key_id: str, private_key: PrivateKeyTypes, method: str, sign_path: str, timestamp: str) -> dict[str, str]:
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": sign_request(private_key, timestamp, method, sign_path),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }
