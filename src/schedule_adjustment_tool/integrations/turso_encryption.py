from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SUPPORTED_TURSO_CIPHERS = {
    "aes128gcm",
    "aes256gcm",
    "aegis128l",
    "aegis128x2",
    "aegis128x4",
    "aegis256",
    "aegis256x2",
    "aegis256x4",
}
HEX_KEY_PATTERN = re.compile(r"^[0-9a-fA-F]+$")
PLACEHOLDER_KEYS = {
    "YOUR_HEX_KEY",
    "YOUR_DATABASE_HEX_KEY",
    "replace-with-64-hex-characters",
}


def build_encrypted_database_url(
    database_url: str,
    *,
    cipher: str = "",
    hexkey: str = "",
) -> str:
    """Add Turso encryption URI parameters when a cipher/key is configured."""
    clean_url = database_url.strip()
    parsed = urlsplit(clean_url)
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    query_values = {key: value for key, value in query_items}
    configured_cipher = cipher.strip() or query_values.get("cipher", "").strip()
    configured_hexkey = hexkey.strip() or query_values.get("hexkey", "").strip()
    if not configured_cipher and not configured_hexkey:
        return clean_url
    if not configured_cipher or not configured_hexkey:
        raise ValueError("Turso暗号化にはcipherとhexkeyの両方が必要です。")
    if parsed.scheme != "file":
        raise ValueError(
            "Turso暗号化URIパラメータは file: で始まるローカルDB用です。"
            " libsql:// のCloud DBでは TURSO_ENCRYPTION_CIPHER と"
            " TURSO_ENCRYPTION_HEXKEY を未設定にしてください。"
        )

    normalized_cipher = configured_cipher.lower()
    if normalized_cipher not in SUPPORTED_TURSO_CIPHERS:
        supported = ", ".join(sorted(SUPPORTED_TURSO_CIPHERS))
        raise ValueError(f"Turso暗号化cipherが不正です。利用可能: {supported}")
    if configured_hexkey in PLACEHOLDER_KEYS:
        raise ValueError("Turso暗号化hexkeyにプレースホルダー値が設定されています。")
    if not HEX_KEY_PATTERN.fullmatch(configured_hexkey):
        raise ValueError("Turso暗号化hexkeyは16進文字列で指定してください。")
    if len(configured_hexkey) not in {32, 64}:
        raise ValueError("Turso暗号化hexkeyは32文字または64文字で指定してください。")

    filtered_items = [
        (key, value)
        for key, value in query_items
        if key not in {"cipher", "hexkey"}
    ]
    filtered_items.extend(
        [
            ("cipher", normalized_cipher),
            ("hexkey", configured_hexkey.lower()),
        ]
    )
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(filtered_items),
            parsed.fragment,
        )
    )
