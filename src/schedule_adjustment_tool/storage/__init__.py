from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Mapping
from typing import Any


BACKEND_MODULES = {
    "sqlite": "schedule_adjustment_tool.storage.sqlite_storage",
    "turso": "schedule_adjustment_tool.storage.turso_storage",
}
PRODUCTION_DEPLOYMENT_MODES = {"cloud", "prod", "production"}
DEPLOYMENT_MODE_ENV_NAMES = (
    "SCHEDULE_DEPLOYMENT_MODE",
    "SCHEDULE_ENVIRONMENT",
    "SCHEDULE_RUNTIME_MODE",
)
_secrets_read_failed = False


def _secret_value(name: str, default: Any = None) -> Any:
    global _secrets_read_failed
    try:
        import streamlit as st

        return st.secrets.get(name, default)
    except Exception:
        _secrets_read_failed = True
        return default


def _secret_section(name: str) -> dict[str, Any]:
    value = _secret_value(name, {})
    return dict(value) if isinstance(value, Mapping) else {}


def _apply_streamlit_storage_secrets() -> None:
    storage = _secret_section("schedule_storage")
    deployment_mode = next(
        (
            os.getenv(name, "").strip()
            for name in DEPLOYMENT_MODE_ENV_NAMES
            if os.getenv(name, "").strip()
        ),
        "",
    )
    if not deployment_mode:
        deployment_mode = str(
            storage.get("deployment_mode")
            or storage.get("mode")
            or _secret_value("SCHEDULE_DEPLOYMENT_MODE")
            or _secret_value("SCHEDULE_ENVIRONMENT")
            or _secret_value("SCHEDULE_RUNTIME_MODE")
            or ""
        ).strip()
    mapped_values = {
        "SCHEDULE_DEPLOYMENT_MODE": deployment_mode,
        "SCHEDULE_STORAGE_BACKEND": storage.get("backend")
        or _secret_value("SCHEDULE_STORAGE_BACKEND"),
        "TURSO_DATABASE_URL": storage.get("database_url")
        or _secret_value("TURSO_DATABASE_URL"),
        "TURSO_AUTH_TOKEN": storage.get("auth_token")
        or _secret_value("TURSO_AUTH_TOKEN"),
        "TURSO_ENCRYPTION_CIPHER": storage.get("encryption_cipher")
        or storage.get("cipher")
        or _secret_value("TURSO_ENCRYPTION_CIPHER"),
        "TURSO_ENCRYPTION_HEXKEY": storage.get("encryption_hexkey")
        or storage.get("hexkey")
        or _secret_value("TURSO_ENCRYPTION_HEXKEY"),
        "SCHEDULE_DATABASE_PATH": storage.get("database_path")
        or storage.get("sqlite_path")
        or _secret_value("SCHEDULE_DATABASE_PATH"),
        "SCHEDULE_PASSWORD_SECRET_KEY": _secret_section("schedule_security").get(
            "password_secret_key"
        )
        or _secret_value("SCHEDULE_PASSWORD_SECRET_KEY"),
    }
    if (
        not mapped_values["SCHEDULE_STORAGE_BACKEND"]
        and mapped_values["TURSO_DATABASE_URL"]
        and str(deployment_mode).strip().lower()
        not in PRODUCTION_DEPLOYMENT_MODES
    ):
        mapped_values["SCHEDULE_STORAGE_BACKEND"] = "turso"
    for key, value in mapped_values.items():
        if value is not None and not os.getenv(key):
            os.environ[key] = str(value)


def _validate_deployment_storage() -> None:
    """Reject an accidental empty SQLite database in cloud/production mode."""

    mode = os.getenv("SCHEDULE_DEPLOYMENT_MODE", "").strip().lower()
    if mode not in PRODUCTION_DEPLOYMENT_MODES:
        return
    backend = os.getenv("SCHEDULE_STORAGE_BACKEND", "").strip().lower()
    database_url = os.getenv("TURSO_DATABASE_URL", "").strip()
    auth_token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
    configured_from_environment = all(
        os.getenv(name, "").strip()
        for name in (
            "SCHEDULE_STORAGE_BACKEND",
            "TURSO_DATABASE_URL",
            "TURSO_AUTH_TOKEN",
        )
    )
    if (
        _secrets_read_failed
        and not configured_from_environment
    ) or backend != "turso" or not database_url or not auth_token:
        raise RuntimeError(
            "Cloud/production storage configuration is unavailable."
        )


def _backend_name() -> str:
    _apply_streamlit_storage_secrets()
    _validate_deployment_storage()
    return os.getenv("SCHEDULE_STORAGE_BACKEND", "sqlite").strip().lower() or "sqlite"


def _load_backend():
    backend = _backend_name()
    module_name = BACKEND_MODULES.get(backend)
    if not module_name:
        supported = ", ".join(sorted(BACKEND_MODULES))
        raise RuntimeError(
            f"Unsupported SCHEDULE_STORAGE_BACKEND={backend!r}. "
            f"Supported backends: {supported}."
        )
    return importlib.import_module(module_name)


_backend = _load_backend()
sys.modules[__name__] = _backend
