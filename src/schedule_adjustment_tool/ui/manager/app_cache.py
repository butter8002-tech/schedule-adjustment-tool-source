"""Streamlit session caches shared by manager screens."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import streamlit as st

from schedule_adjustment_tool.storage import (
    all_memberships,
    ensure_projects,
    list_audit_logs,
    list_backups,
    list_common_participants,
    list_projects,
    list_users,
    load_system_settings,
    record_audit_event,
)


PROJECT_LIST_CACHE_KEY = "project_list_cache"
SYSTEM_SETTINGS_CACHE_KEY = "system_settings_cache"
USERS_CACHE_KEY = "users_cache"
MEMBERSHIPS_CACHE_KEY = "memberships_cache"
COMMON_PARTICIPANTS_CACHE_KEY = "common_participants_cache"
PENDING_USERS_CACHE_KEY = "pending_users_cache"
PENDING_MEMBERSHIPS_CACHE_KEY = "pending_memberships_cache"
DELETED_PROJECTS_CACHE_KEY = "deleted_projects_cache"
BACKUPS_CACHE_KEY = "backups_cache"
AUDIT_LOGS_CACHE_KEY = "audit_logs_cache"
PREPARED_EXPORTS_KEY = "prepared_exports"
PROJECT_OPERATION_FEEDBACK_KEY = "project_operation_feedback"


def run_with_status(message: str, operation, *args, **kwargs):
    with st.spinner(message, show_time=True):
        return operation(*args, **kwargs)


def status_message(message: str):
    return st.spinner(message, show_time=True)


def set_project_operation_feedback(
    project_id: str,
    message: str,
    *,
    kind: str = "success",
    operation_key: str = "default",
) -> None:
    feedback_by_project = st.session_state.setdefault(
        PROJECT_OPERATION_FEEDBACK_KEY,
        {},
    )
    project_feedback = feedback_by_project.setdefault(project_id, {})
    project_feedback[operation_key] = {"kind": kind, "message": message}


def render_project_operation_feedback(
    project_id: str,
    operation_key: str,
) -> None:
    feedback_by_project = st.session_state.get(
        PROJECT_OPERATION_FEEDBACK_KEY,
        {},
    )
    project_feedback = feedback_by_project.get(project_id, {})
    feedback = project_feedback.pop(operation_key, None)
    if not project_feedback:
        feedback_by_project.pop(project_id, None)
    if not feedback:
        return
    renderer = {
        "info": st.info,
        "warning": st.warning,
        "error": st.error,
    }.get(str(feedback.get("kind", "success")), st.success)
    renderer(str(feedback.get("message", "")))

def load_project_list_cached(*, force: bool = False) -> list[dict]:
    if force or PROJECT_LIST_CACHE_KEY not in st.session_state:
        with status_message("企画一覧を読み込んでいます..."):
            st.session_state[PROJECT_LIST_CACHE_KEY] = ensure_projects()
    return deepcopy(st.session_state[PROJECT_LIST_CACHE_KEY])


def load_system_settings_cached(*, force: bool = False) -> dict:
    if force or SYSTEM_SETTINGS_CACHE_KEY not in st.session_state:
        st.session_state[SYSTEM_SETTINGS_CACHE_KEY] = run_with_status(
            "全体設定を読み込んでいます...",
            load_system_settings,
        )
    return deepcopy(st.session_state[SYSTEM_SETTINGS_CACHE_KEY])


def set_cached_system_settings(settings: dict) -> None:
    st.session_state[SYSTEM_SETTINGS_CACHE_KEY] = deepcopy(settings)


def load_users_cached(*, force: bool = False) -> list[dict]:
    if force or USERS_CACHE_KEY not in st.session_state:
        st.session_state[USERS_CACHE_KEY] = run_with_status(
            "アカウント情報を読み込んでいます...",
            list_users,
        )
    return deepcopy(st.session_state[USERS_CACHE_KEY])


def refresh_users_cache() -> list[dict]:
    return load_users_cached(force=True)


def load_memberships_cached(*, force: bool = False) -> list[dict]:
    if force or MEMBERSHIPS_CACHE_KEY not in st.session_state:
        st.session_state[MEMBERSHIPS_CACHE_KEY] = run_with_status(
            "権限情報を読み込んでいます...",
            all_memberships,
        )
    return deepcopy(st.session_state[MEMBERSHIPS_CACHE_KEY])


def refresh_memberships_cache() -> list[dict]:
    return load_memberships_cached(force=True)


def clear_memberships_cache() -> None:
    st.session_state.pop(MEMBERSHIPS_CACHE_KEY, None)


def load_common_participants_cached(*, force: bool = False) -> list[dict]:
    if force or COMMON_PARTICIPANTS_CACHE_KEY not in st.session_state:
        st.session_state[COMMON_PARTICIPANTS_CACHE_KEY] = run_with_status(
            "登録済み参加者一覧を読み込んでいます...",
            list_common_participants,
        )
    return deepcopy(st.session_state[COMMON_PARTICIPANTS_CACHE_KEY])


def clear_common_participants_cache() -> None:
    st.session_state.pop(COMMON_PARTICIPANTS_CACHE_KEY, None)


def set_cached_common_participants(profiles: list[dict]) -> None:
    """Replace the session roster cache after a successful batch update."""

    st.session_state[COMMON_PARTICIPANTS_CACHE_KEY] = deepcopy(profiles)


def pending_users() -> list[dict]:
    return st.session_state.setdefault(PENDING_USERS_CACHE_KEY, [])


def clear_pending_users() -> None:
    st.session_state.pop(PENDING_USERS_CACHE_KEY, None)


def pending_memberships() -> list[dict]:
    return st.session_state.setdefault(PENDING_MEMBERSHIPS_CACHE_KEY, [])


def clear_pending_memberships() -> None:
    st.session_state.pop(PENDING_MEMBERSHIPS_CACHE_KEY, None)


def load_deleted_projects_cached(*, force: bool = False) -> list[dict]:
    if force or DELETED_PROJECTS_CACHE_KEY not in st.session_state:
        with status_message("削除済み企画を読み込んでいます..."):
            st.session_state[DELETED_PROJECTS_CACHE_KEY] = [
                project
                for project in list_projects(include_deleted=True)
                if project.get("deleted_at")
            ]
    return deepcopy(st.session_state[DELETED_PROJECTS_CACHE_KEY])


def clear_deleted_projects_cache() -> None:
    st.session_state.pop(DELETED_PROJECTS_CACHE_KEY, None)


def load_backups_cached(project_id: str, *, force: bool = False) -> list[dict]:
    cache = st.session_state.setdefault(BACKUPS_CACHE_KEY, {})
    if force or project_id not in cache:
        cache[project_id] = run_with_status(
            "バックアップ一覧を読み込んでいます...",
            list_backups,
            project_id,
        )
    return deepcopy(cache[project_id])


def clear_backups_cache(project_id: str | None = None) -> None:
    if project_id is None:
        st.session_state.pop(BACKUPS_CACHE_KEY, None)
        return
    backups = st.session_state.get(BACKUPS_CACHE_KEY)
    if isinstance(backups, dict):
        backups.pop(project_id, None)


def load_audit_logs_cached(*, force: bool = False) -> list[dict]:
    if force or AUDIT_LOGS_CACHE_KEY not in st.session_state:
        st.session_state[AUDIT_LOGS_CACHE_KEY] = run_with_status(
            "監査ログを読み込んでいます...",
            list_audit_logs,
            500,
        )
    return deepcopy(st.session_state[AUDIT_LOGS_CACHE_KEY])


def clear_audit_logs_cache() -> None:
    st.session_state.pop(AUDIT_LOGS_CACHE_KEY, None)


def record_audit_event_and_clear_cache(
    action: str,
    *,
    project_id: str | None = None,
    target: str = "",
    detail: dict | None = None,
) -> None:
    record_audit_event(
        action,
        project_id=project_id,
        target=target,
        detail=detail,
    )
    clear_audit_logs_cache()


def prepared_exports() -> dict[str, dict]:
    return st.session_state.setdefault(PREPARED_EXPORTS_KEY, {})


def clear_prepared_exports(project_id: str | None = None) -> None:
    if project_id is None:
        st.session_state.pop(PREPARED_EXPORTS_KEY, None)
        return
    cache = st.session_state.get(PREPARED_EXPORTS_KEY)
    if isinstance(cache, dict):
        for key in [
            key
            for key, value in cache.items()
            if str(value.get("project_id", "")) == project_id
        ]:
            cache.pop(key, None)


def export_cache_token(payload: object) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()[:20]
