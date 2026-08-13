from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from schedule_adjustment_tool.domain.app_config import load_app_settings, setting_value
from schedule_adjustment_tool.storage import (
    create_user,
    database_identifier,
    get_user_by_username,
    load_login_snapshot,
    load_system_settings,
    memberships_for_user,
    record_login,
    set_audit_actor,
)
from schedule_adjustment_tool.storage.performance import measure_storage_operation


ROLE_SYSTEM_ADMIN = "システム管理"
ROLE_SCHEDULE_MANAGER = "スケジュール担当者"
ROLE_PARTICIPANT = "参加者"
PBKDF2_ITERATIONS = 600_000
_BOOTSTRAP_CHECKED: set[tuple[str, str]] = set()
LOGGER = logging.getLogger("schedule_adjustment_tool.auth")


@dataclass
class Principal:
    user_id: str
    username: str
    is_system_admin: bool = False
    is_schedule_manager: bool = False
    is_participant: bool = False
    memberships: list[dict[str, Any]] = field(default_factory=list)
    authentication_enabled: bool = False
    maintenance_mode_enabled: bool = False

    def project_roles(self, project_id: str) -> set[str]:
        return {
            str(membership["role"])
            for membership in self.memberships
            if membership["project_id"] == project_id
        }

    def participant_id(self, project_id: str) -> str:
        return next(
            (
                str(membership.get("participant_id") or "")
                for membership in self.memberships
                if membership["project_id"] == project_id
                and membership["role"] == "participant"
            ),
            "",
        )

    def can_select_all_participants(self, project_id: str) -> bool:
        if self.is_participant:
            return True
        return any(
            membership["project_id"] == project_id
            and membership["role"] == "participant"
            and not membership.get("participant_id")
            for membership in self.memberships
        )


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("パスワードは12文字以上にしてください。")
    return _hash_password(password)


def hash_project_access_password(password: str) -> str:
    """Hash a project access password without an account-password length rule."""

    if not password:
        raise ValueError("企画操作パスワードを入力してください。")
    return _hash_password(password)


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


def bootstrap_admin_from_environment() -> None:
    username = str(setting_value("SCHEDULE_BOOTSTRAP_ADMIN_USERNAME", "")).strip()
    password = str(setting_value("SCHEDULE_BOOTSTRAP_ADMIN_PASSWORD", ""))
    if not username or not password:
        return
    cache_key = (database_identifier(), username.casefold())
    if cache_key in _BOOTSTRAP_CHECKED:
        return
    if get_user_by_username(username):
        _BOOTSTRAP_CHECKED.add(cache_key)
        return
    create_user(username, hash_password(password), is_system_admin=True)
    _BOOTSTRAP_CHECKED.add(cache_key)


def invalidate_bootstrap_admin_cache(username: str | None = None) -> None:
    """Invalidate the process-local bootstrap check after account changes."""

    if username is None:
        _BOOTSTRAP_CHECKED.clear()
        return
    normalized = str(username).casefold()
    _BOOTSTRAP_CHECKED.difference_update(
        key for key in _BOOTSTRAP_CHECKED if key[1] == normalized
    )


def authenticate(username: str, password: str) -> Principal | None:
    with measure_storage_operation(
        "login",
        logger=LOGGER,
    ) as metrics:
        snapshot = load_login_snapshot(username)
        if not snapshot:
            metrics.set(auth_result="not_found")
            return None
        user = snapshot["user"]
        if not user or not user["active"]:
            metrics.set(auth_result="inactive")
            return None
        verify_started = time.perf_counter()
        password_valid = verify_password(password, user["password_hash"])
        metrics.set(
            password_verify_seconds=round(
                time.perf_counter() - verify_started, 6
            )
        )
        if not password_valid:
            metrics.set(auth_result="invalid_password")
            return None
        system_settings = snapshot.get("system_settings", {})
        principal = Principal(
            user_id=user["id"],
            username=user["username"],
            is_system_admin=bool(user["is_system_admin"]),
            is_schedule_manager=bool(user.get("is_schedule_manager", False)),
            is_participant=bool(user.get("is_participant", False)),
            memberships=list(snapshot.get("memberships", [])),
            authentication_enabled=True,
            maintenance_mode_enabled=bool(
                system_settings.get("maintenance_mode", False)
            ),
        )
        metrics.set(
            project_count=len(
                {
                    str(item.get("project_id"))
                    for item in principal.memberships
                    if item.get("project_id")
                }
            )
        )
        # During maintenance a non-admin must not trigger even the login-audit
        # write.  Authentication itself remains a read-only check so that the
        # account can receive the maintenance notice.
        if principal.is_system_admin or not principal.maintenance_mode_enabled:
            record_login(user["id"])
        set_audit_actor(principal.username)
        metrics.set(
            auth_result=(
                "success_maintenance"
                if principal.maintenance_mode_enabled
                else "success"
            )
        )
        return principal


def demo_principal() -> Principal:
    principal = Principal(
        user_id="demo",
        username="demo-local",
        is_system_admin=True,
        authentication_enabled=False,
    )
    set_audit_actor(principal.username)
    return principal


def allowed_operation_roles(principal: Principal) -> list[str]:
    if not principal.authentication_enabled:
        return [ROLE_SYSTEM_ADMIN, ROLE_SCHEDULE_MANAGER, ROLE_PARTICIPANT]
    roles: list[str] = []
    if principal.is_system_admin:
        roles.append(ROLE_SYSTEM_ADMIN)
    if principal.is_system_admin or principal.is_schedule_manager:
        roles.append(ROLE_SCHEDULE_MANAGER)
    elif any(
        membership["role"] == "manager" for membership in principal.memberships
    ):
        roles.append(ROLE_SCHEDULE_MANAGER)
    if any(
        membership["role"] == "participant" for membership in principal.memberships
    ) or principal.is_participant:
        roles.append(ROLE_PARTICIPANT)
    return roles


def can_access_project(
    principal: Principal, project_id: str, operation_role: str
) -> bool:
    if not principal.authentication_enabled or principal.is_system_admin:
        return True
    if operation_role == ROLE_SCHEDULE_MANAGER:
        return principal.is_schedule_manager or "manager" in principal.project_roles(
            project_id
        )
    if operation_role == ROLE_PARTICIPANT and principal.is_participant:
        return True
    roles = principal.project_roles(project_id)
    if operation_role == ROLE_PARTICIPANT:
        return "participant" in roles
    return False


def auth_required() -> bool:
    return load_app_settings().auth_required


def maintenance_mode_enabled(principal: Principal | None = None) -> bool:
    if principal is not None and principal.authentication_enabled:
        return bool(principal.maintenance_mode_enabled)
    return bool(load_system_settings().get("maintenance_mode", False))
