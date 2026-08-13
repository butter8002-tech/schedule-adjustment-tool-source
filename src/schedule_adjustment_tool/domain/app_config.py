from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Asia/Tokyo"
DEFAULT_MAX_TARGET_PERIOD_DAYS = 90
MAX_MAX_TARGET_PERIOD_DAYS = 366
DEFAULT_BACKUP_LIMIT = 20
MAX_BACKUP_LIMIT = 200
DEFAULT_DELETED_RETENTION_DAYS = 30
DEFAULT_AUDIT_RETENTION_DAYS = 365
MAX_RETENTION_DAYS = 3650


def secret_value(name: str, default: Any = None) -> Any:
    try:
        import streamlit as st

        return st.secrets.get(name, default)
    except Exception:
        return default


def setting_value(name: str, default: Any = None) -> Any:
    value = os.getenv(name)
    if value is not None:
        return value
    return secret_value(name, default)


def bounded_int(
    value: Any,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        normalized = default
    return max(minimum, min(maximum, normalized))


def env_bool(name: str, default: bool = False) -> bool:
    value = setting_value(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    return bounded_int(
        setting_value(name, default),
        default,
        minimum,
        maximum,
    )


def configured_timezone_name() -> str:
    configured = setting_value("SCHEDULE_TIMEZONE", DEFAULT_TIMEZONE)
    timezone_name = str(configured or "").strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(timezone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return DEFAULT_TIMEZONE
    return timezone_name


def configured_timezone() -> ZoneInfo:
    return ZoneInfo(configured_timezone_name())


def local_now(current: datetime | None = None) -> datetime:
    timezone = configured_timezone()
    if current is None:
        return datetime.now(timezone)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone)
    return current.astimezone(timezone)


def local_today(current: datetime | None = None) -> date:
    return local_now(current).date()


def normalize_deadline(value: str | datetime) -> datetime | None:
    if isinstance(value, datetime):
        deadline = value
    elif isinstance(value, str) and value:
        try:
            deadline = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    timezone = configured_timezone()
    if deadline.tzinfo is None:
        return deadline.replace(tzinfo=timezone)
    return deadline.astimezone(timezone)


def deadline_has_passed(
    value: str | datetime,
    *,
    current: datetime | None = None,
) -> bool:
    deadline = normalize_deadline(value)
    if deadline is None:
        return False
    return local_now(current) >= deadline


def configured_max_target_period_days() -> int:
    return env_int(
        "SCHEDULE_MAX_TARGET_PERIOD_DAYS",
        DEFAULT_MAX_TARGET_PERIOD_DAYS,
        1,
        MAX_MAX_TARGET_PERIOD_DAYS,
    )


def configured_backup_limit() -> int:
    return env_int(
        "SCHEDULE_BACKUP_LIMIT",
        DEFAULT_BACKUP_LIMIT,
        1,
        MAX_BACKUP_LIMIT,
    )


def configured_deleted_retention_days(
    default: Any = DEFAULT_DELETED_RETENTION_DAYS,
) -> int:
    normalized_default = bounded_int(
        default,
        DEFAULT_DELETED_RETENTION_DAYS,
        1,
        MAX_RETENTION_DAYS,
    )
    return env_int(
        "SCHEDULE_DELETED_RETENTION_DAYS",
        normalized_default,
        1,
        MAX_RETENTION_DAYS,
    )


def configured_audit_retention_days() -> int:
    return env_int(
        "SCHEDULE_AUDIT_RETENTION_DAYS",
        DEFAULT_AUDIT_RETENTION_DAYS,
        1,
        MAX_RETENTION_DAYS,
    )


@dataclass(frozen=True)
class AppSettings:
    auth_required: bool
    timezone: str
    max_search_seconds: int
    max_candidates_per_search: int
    max_stored_candidates: int
    max_text_length: int
    max_description_length: int
    max_failed_logins: int
    login_lock_seconds: int
    allow_json_exports: bool


def load_app_settings() -> AppSettings:
    timezone = configured_timezone_name()
    return AppSettings(
        # A public deployment must be protected unless the operator explicitly
        # opts into the local/demo mode with SCHEDULE_AUTH_REQUIRED=false.
        auth_required=env_bool("SCHEDULE_AUTH_REQUIRED", True),
        timezone=timezone,
        max_search_seconds=env_int(
            "SCHEDULE_MAX_SEARCH_SECONDS", 600, 1, 1200
        ),
        max_candidates_per_search=env_int(
            "SCHEDULE_MAX_CANDIDATES", 50, 1, 200
        ),
        max_stored_candidates=env_int(
            "SCHEDULE_MAX_STORED_CANDIDATES", 50, 1, 200
        ),
        max_text_length=env_int("SCHEDULE_MAX_TEXT_LENGTH", 120, 20, 1000),
        max_description_length=env_int(
            "SCHEDULE_MAX_DESCRIPTION_LENGTH", 4000, 200, 20000
        ),
        max_failed_logins=env_int("SCHEDULE_MAX_FAILED_LOGINS", 5, 1, 100),
        login_lock_seconds=env_int(
            "SCHEDULE_LOGIN_LOCK_SECONDS", 300, 10, 3600
        ),
        allow_json_exports=env_bool("SCHEDULE_ALLOW_JSON_EXPORTS", False),
    )
