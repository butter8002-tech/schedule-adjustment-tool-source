from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Any


PASSWORD_SECRET_PREFIX = "pwv1"
PASSWORD_SECRET_KEY_ENV = "SCHEDULE_PASSWORD_SECRET_KEY"


class PasswordSecretError(RuntimeError):
    pass


def _secret_value(name: str, default: Any = None) -> Any:
    try:
        import streamlit as st

        return st.secrets.get(name, default)
    except Exception:
        return default


def _secret_section(name: str) -> dict[str, Any]:
    value = _secret_value(name, {})
    return dict(value) if isinstance(value, dict) else {}


def password_secret_key_configured() -> bool:
    return bool(_password_secret_key_hex())


def _password_secret_key_hex() -> str:
    security = _secret_section("schedule_security")
    value = (
        os.getenv(PASSWORD_SECRET_KEY_ENV)
        or security.get("password_secret_key")
        or security.get("password_encryption_key")
        or _secret_value(PASSWORD_SECRET_KEY_ENV)
        or ""
    )
    return str(value).strip()


def _master_key() -> bytes:
    key_hex = _password_secret_key_hex()
    if not key_hex:
        raise PasswordSecretError(
            "配布用パスワードの暗号化鍵が未設定です。"
            f" {PASSWORD_SECRET_KEY_ENV} または"
            " [schedule_security].password_secret_key を設定してください。"
        )
    try:
        key = bytes.fromhex(key_hex)
    except ValueError as error:
        raise PasswordSecretError(
            "配布用パスワードの暗号化鍵は16進文字列で指定してください。"
        ) from error
    if len(key) != 32:
        raise PasswordSecretError(
            "配布用パスワードの暗号化鍵は32バイト（64 hex文字）で指定してください。"
        )
    return key


def _derive_key(label: bytes) -> bytes:
    return hmac.new(_master_key(), label, hashlib.sha256).digest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(
            hmac.new(
                key,
                nonce + counter.to_bytes(4, "big"),
                hashlib.sha256,
            ).digest()
        )
        counter += 1
    return bytes(output[:length])


def encrypt_password_secret(password: str) -> str:
    if not password:
        return ""
    plaintext = password.encode("utf-8")
    nonce = secrets.token_bytes(16)
    encryption_key = _derive_key(b"schedule-password-secret-encryption-v1")
    mac_key = _derive_key(b"schedule-password-secret-authentication-v1")
    stream = _keystream(encryption_key, nonce, len(plaintext))
    ciphertext = bytes(left ^ right for left, right in zip(plaintext, stream))
    authenticated = (
        PASSWORD_SECRET_PREFIX.encode("ascii") + b"|" + nonce + b"|" + ciphertext
    )
    tag = hmac.new(mac_key, authenticated, hashlib.sha256).digest()
    return ":".join(
        [
            PASSWORD_SECRET_PREFIX,
            _b64encode(nonce),
            _b64encode(ciphertext),
            _b64encode(tag),
        ]
    )


def decrypt_password_secret(secret_value: str) -> str:
    if not secret_value:
        return ""
    parts = secret_value.split(":")
    if len(parts) != 4 or parts[0] != PASSWORD_SECRET_PREFIX:
        return ""
    try:
        nonce = _b64decode(parts[1])
        ciphertext = _b64decode(parts[2])
        tag = _b64decode(parts[3])
    except ValueError as error:
        raise PasswordSecretError("配布用パスワードの暗号文形式が不正です。") from error
    mac_key = _derive_key(b"schedule-password-secret-authentication-v1")
    authenticated = (
        PASSWORD_SECRET_PREFIX.encode("ascii") + b"|" + nonce + b"|" + ciphertext
    )
    expected_tag = hmac.new(mac_key, authenticated, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise PasswordSecretError("配布用パスワードの復号に失敗しました。")
    encryption_key = _derive_key(b"schedule-password-secret-encryption-v1")
    stream = _keystream(encryption_key, nonce, len(ciphertext))
    plaintext = bytes(left ^ right for left, right in zip(ciphertext, stream))
    return plaintext.decode("utf-8")
