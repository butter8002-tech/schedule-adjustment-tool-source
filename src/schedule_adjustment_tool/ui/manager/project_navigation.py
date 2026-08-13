"""Manager supporting-menu controls for project access and reload actions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import streamlit as st

from schedule_adjustment_tool.domain.auth import Principal, hash_project_access_password
from schedule_adjustment_tool.domain.models import Config
from schedule_adjustment_tool.storage import StorageConflictError, StorageError


@dataclass(frozen=True)
class ProjectNavigationServices:
    """App-level cache and dialog operations used by the support menu."""

    status_message: Callable[[str], object]
    update_config_and_clear_candidates: Callable[[str, Config, dict], None]
    refresh_project_list: Callable[[], object]
    refresh_project_data: Callable[[str], object]
    record_refresh_time: Callable[[str], None]
    last_refresh_time: Callable[[str], str]
    open_project_creation_dialog: Callable[[list[dict], Principal], None]


def render_manager_project_access_settings(
    project_id: str,
    config: Config,
    *,
    services: ProjectNavigationServices,
) -> None:
    """Edit the optional project password from the supporting menu."""

    current_password_is_set = bool(config.project_access_password_hash)
    st.caption("参加者や担当者が企画を開くときに使うパスワードを設定します。")
    with st.form(f"manager_project_access_settings_{project_id}"):
        st.caption(
            "現在の設定: "
            + ("設定済み" if current_password_is_set else "未設定")
        )
        new_password = st.text_input(
            "新しい企画操作パスワード",
            type="password",
            help="空欄のまま保存すると、現在の設定を維持します。",
        )
        clear_password = st.checkbox(
            "企画操作パスワードを削除",
            disabled=not current_password_is_set,
        )
        save_clicked = st.form_submit_button(
            "アクセス設定を保存",
            type="primary",
        )
    if not save_clicked:
        return

    updates: dict[str, str] = {}
    if clear_password:
        updates["project_access_password_hash"] = ""
    elif new_password:
        try:
            updates["project_access_password_hash"] = (
                hash_project_access_password(new_password)
            )
        except ValueError as error:
            st.error(str(error))
            return
    if not updates:
        st.info("保存する変更はありません。")
        return

    try:
        with services.status_message("アクセス設定を保存しています..."):
            services.update_config_and_clear_candidates(
                project_id,
                config,
                updates,
            )
    except (StorageError, StorageConflictError) as error:
        st.error(str(error))
        return
    st.success("アクセス設定を保存しました。")
    st.rerun()


def render_manager_project_sidebar_actions(
    project_id: str,
    projects: list[dict],
    principal: Principal,
    *,
    services: ProjectNavigationServices,
) -> None:
    """Render project reload and creation actions in the sidebar."""

    with st.sidebar:
        st.caption("企画メニュー")
        loaded_at = services.last_refresh_time(f"project:{project_id}")
        if loaded_at:
            st.caption(f"企画データ最終読み込み: {loaded_at}")
        if st.button(
            "企画一覧を再読み込み",
            key="manager_reload_project_list",
            icon=":material/refresh:",
            width="stretch",
        ):
            services.refresh_project_list()
            services.record_refresh_time("project_list")
            st.session_state.pop("active_project_id", None)
            st.rerun()
        if st.button(
            "企画データを再読み込み",
            key=f"manager_reload_project_data_{project_id}",
            icon=":material/sync:",
            width="stretch",
        ):
            with services.status_message("選択中の企画データを再読み込みしています..."):
                services.refresh_project_data(project_id)
                st.session_state.pop("candidate_reasons", None)
                services.record_refresh_time(f"project:{project_id}")
            st.rerun()
        if st.button(
            "新しい企画を作成",
            key="manager_open_project_creator",
            icon=":material/add_circle:",
            type="primary",
            width="stretch",
        ):
            services.open_project_creation_dialog(projects, principal)
        st.divider()
