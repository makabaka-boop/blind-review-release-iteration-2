import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import Depends, Header, HTTPException

SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me")
TOKEN_TTL_SECONDS = 12 * 3600


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + "$" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except ValueError:
        return False


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def make_token(user_id: int, role: str) -> str:
    payload = {"sub": user_id, "role": role, "exp": int(time.time()) + TOKEN_TTL_SECONDS}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return body + "." + _b64(sig)


def decode_token(token: str) -> dict:
    try:
        body, sig = token.split(".", 1)
        expected = hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64(expected), sig):
            raise HTTPException(status_code=401, detail="invalid token")
        payload = json.loads(_unb64(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise HTTPException(status_code=401, detail="token expired")
        return payload
    except (ValueError, KeyError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="invalid token")


def current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="authentication required")
    payload = decode_token(authorization[len("Bearer ") :])
    return {"id": int(payload["sub"]), "role": payload["role"]}


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="forbidden")
    return user


def require_reviewer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "reviewer":
        raise HTTPException(status_code=403, detail="forbidden")
    return user
