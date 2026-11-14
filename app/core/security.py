import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.config import get_settings
from app.models.entities import UserAccount


_password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
_demo_attempts: dict[str, deque[float]] = defaultdict(deque)
_demo_attempts_lock = threading.Lock()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _password_hasher.verify(hashed, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def password_hash_needs_upgrade(hashed: str) -> bool:
    if not hashed.startswith("$argon2id$"):
        return True
    try:
        return _password_hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return True


def enforce_demo_rate_limit(request: Request, attempts: int, window_seconds: int) -> None:
    client = request.client.host if request.client else "unknown"
    try:
        if ipaddress.ip_address(client).is_private:
            client = str(ipaddress.ip_address(request.headers.get("x-real-ip", client)))
    except ValueError:
        pass
    now = time.monotonic()
    cutoff = now - window_seconds
    with _demo_attempts_lock:
        bucket = _demo_attempts[client]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= attempts:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "体验码尝试过于频繁，请稍后重试",
                headers={"Retry-After": str(window_seconds)},
            )
        bucket.append(now)


def _credentials(request: Request) -> tuple[str, str]:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Basic authorization")
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username, password
    except Exception as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Basic authorization") from exc


def create_demo_token(username: str, secret: str, ttl_seconds: int) -> str:
    payload = {
        "exp": int(time.time()) + ttl_seconds,
        "scope": "demo",
        "sub": username,
        "jti": secrets.token_urlsafe(24),
        "v": 1,
    }
    encoded = _base64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_base64url_encode(signature)}"


def verify_demo_token(token: str, secret: str) -> str:
    try:
        encoded, provided_signature = token.split(".", 1)
        decoded_signature = _base64url_decode(provided_signature)
        if _base64url_encode(decoded_signature) != provided_signature:
            raise ValueError("non-canonical signature encoding")
        expected_signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(decoded_signature, expected_signature):
            raise ValueError("signature mismatch")
        decoded_payload = _base64url_decode(encoded)
        if _base64url_encode(decoded_payload) != encoded:
            raise ValueError("non-canonical payload encoding")
        payload = json.loads(decoded_payload.decode("utf-8"))
        if payload.get("scope") != "demo" or payload.get("v") != 1:
            raise ValueError("invalid token scope")
        if not isinstance(payload.get("jti"), str) or not 20 <= len(payload["jti"]) <= 128:
            raise ValueError("missing random session identity")
        if int(payload.get("exp", 0)) <= int(time.time()):
            raise ValueError("token expired")
        username = str(payload.get("sub", "")).strip()
        if not username:
            raise ValueError("missing subject")
        return username
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired demo token") from exc


def valid_demo_access_code(provided: str, expected: str) -> bool:
    return hmac.compare_digest(provided.strip().encode("utf-8"), expected.strip().encode("utf-8"))


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> UserAccount:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        username = verify_demo_token(header.split(" ", 1)[1], get_settings().demo_token_secret)
        user = db.query(UserAccount).filter(UserAccount.username == username).first()
        if user is None or "ROLE_ADMIN" in user.roles:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Demo account unavailable")
        request.state.demo_access = True
        token = header.split(" ", 1)[1]
        payload = json.loads(_base64url_decode(token.split(".", 1)[0]))
        request.state.demo_key = hashlib.sha256(token.encode()).hexdigest()
        request.state.demo_expires_at = int(payload["exp"])
    else:
        username, password = _credentials(request)
        user = db.query(UserAccount).filter(UserAccount.username == username).first()
        if user is None or not verify_password(password, user.password_hash):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad credentials")
        request.state.demo_access = False
    return user


def require_admin(user: Annotated[UserAccount, Depends(current_user)]) -> UserAccount:
    if "ROLE_ADMIN" not in user.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user
