"""Shared authentication UI used by the manager and participant apps."""

from __future__ import annotations

import logging
import time as time_module

import streamlit as st

from schedule_adjustment_tool.domain.app_config import load_app_settings
from schedule_adjustment_tool.domain.auth import (
    Principal,
    auth_required,
    authenticate,
    bootstrap_admin_from_environment,
    demo_principal,
)
from schedule_adjustment_tool.storage import set_audit_actor
from schedule_adjustment_tool.storage.performance import log_storage_event
from schedule_adjustment_tool.ui.application_metadata import (
    APP_NAME,
    VERSION_LABEL,
)


APP_SETTINGS = load_app_settings()
LOGGER = logging.getLogger("schedule_adjustment_tool.auth")


def _reset_role_selection_state() -> None:
    """Do not carry a prior account's manager selection into a new login."""

    for key in (
        "active_project_id",
        "manager_ui_project_selector",
        "operation_role",
    ):
        st.session_state.pop(key, None)


def render_authentication(
    *,
    show_project_creation_notice: bool = False,
) -> Principal:
    """Authenticate a user or return the explicitly enabled demo principal."""

    bootstrap_admin_from_environment()
    if not auth_required():
        st.info(
            "現在はログインせずに利用できる状態です。"
            "公開前に認証設定を確認してください。"
        )
        return demo_principal()

    principal = st.session_state.get("authenticated_principal")
    if isinstance(principal, Principal):
        rerender_started = st.session_state.pop(
            "login_rerender_started_at", None
        )
        if isinstance(rerender_started, (float, int)):
            rerender_elapsed = time_module.perf_counter() - float(
                rerender_started
            )
            if rerender_elapsed >= 0:
                log_storage_event(
                    LOGGER,
                    "login_rerender",
                    rerender_seconds=round(rerender_elapsed, 6),
                    total_seconds=round(rerender_elapsed, 6),
                )
        set_audit_actor(principal.username)
        # Reuse the value loaded during authentication.  Reading the setting
        # again here would add a database round trip on every Streamlit rerun.
        if principal.is_system_admin or not principal.maintenance_mode_enabled:
            columns = st.columns([5, 1])
            columns[0].caption(f"ログイン中: {principal.username}")
            columns[0].caption(VERSION_LABEL)
            if columns[1].button("ログアウト"):
                st.session_state.pop("authenticated_principal", None)
                _reset_role_selection_state()
                st.rerun()
        return principal

    st.title(APP_NAME)
    st.caption(VERSION_LABEL)
    if show_project_creation_notice:
        project_creation_notice = st.session_state.pop(
            "project_creation_notice",
            None,
        )
        if project_creation_notice:
            st.success(project_creation_notice)

    now = time_module.monotonic()
    locked_until = float(st.session_state.get("login_locked_until", 0.0))
    if locked_until > now:
        st.error(
            f"ログイン試行回数が上限に達しました。"
            f" 約{int(locked_until - now) + 1}秒後に再試行してください。"
        )
        st.stop()

    st.subheader("ログイン")
    with st.form("login_form"):
        username = st.text_input(
            "ユーザー名",
            max_chars=APP_SETTINGS.max_text_length,
        )
        password = st.text_input("パスワード", type="password", max_chars=256)
        submitted = st.form_submit_button("ログイン", type="primary")
    if submitted:
        authenticated = authenticate(username, password)
        if authenticated:
            _reset_role_selection_state()
            st.session_state["authenticated_principal"] = authenticated
            st.session_state["login_rerender_started_at"] = (
                time_module.perf_counter()
            )
            st.session_state.pop("failed_login_count", None)
            st.session_state.pop("login_locked_until", None)
            st.rerun()
        failed = int(st.session_state.get("failed_login_count", 0)) + 1
        st.session_state["failed_login_count"] = failed
        if failed >= APP_SETTINGS.max_failed_logins:
            st.session_state["login_locked_until"] = (
                now + APP_SETTINGS.login_lock_seconds
            )
        st.error("ユーザー名またはパスワードが正しくありません。")
    st.stop()
