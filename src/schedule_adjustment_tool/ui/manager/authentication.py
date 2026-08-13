"""Authentication and optional per-project access gate for the manager app."""

from __future__ import annotations

import streamlit as st

from schedule_adjustment_tool.domain.auth import (
    Principal,
    verify_password,
)
from schedule_adjustment_tool.ui.authentication import (
    render_authentication as render_shared_authentication,
)

PROJECT_ACCESS_AUTHENTICATED_KEY = "project_access_authenticated"


def render_authentication() -> Principal:
    return render_shared_authentication(show_project_creation_notice=True)


def render_project_access_gate(
    project_id: str,
    config,
    principal: Principal,
) -> bool:
    """Require the optional project password before manager operations."""

    password_hash = str(
        getattr(config, "project_access_password_hash", "") or ""
    )
    if not password_hash or principal.is_system_admin:
        return True

    authenticated_projects = st.session_state.setdefault(
        PROJECT_ACCESS_AUTHENTICATED_KEY,
        {},
    )
    session_key = f"{principal.user_id}:{project_id}"
    if authenticated_projects.get(session_key) == password_hash:
        return True

    st.subheader("企画パスワード")
    st.info("この企画を操作するには、アクセス設定で指定したパスワードが必要です。")
    with st.form(f"project_access_password_form_{project_id}"):
        password = st.text_input("企画操作パスワード", type="password")
        submitted = st.form_submit_button("企画を開く", type="primary")
    if submitted:
        if verify_password(password, password_hash):
            authenticated_projects[session_key] = password_hash
            st.rerun()
        st.error("企画操作パスワードが正しくありません。")
    return False
