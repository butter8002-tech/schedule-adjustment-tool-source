"""Destructive project maintenance, recovery, audit, and JSON export controls."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.app_config import load_app_settings
from schedule_adjustment_tool.domain.models import now_iso
from schedule_adjustment_tool.storage import (
    export_all_data,
    restore_deleted_project,
)
from schedule_adjustment_tool.ui.manager.app_cache import (
    AUDIT_LOGS_CACHE_KEY,
    BACKUPS_CACHE_KEY,
    DELETED_PROJECTS_CACHE_KEY,
    clear_audit_logs_cache,
    clear_deleted_projects_cache,
    load_audit_logs_cached,
    load_backups_cached,
    load_deleted_projects_cached,
    load_project_list_cached,
    load_system_settings_cached,
    record_audit_event_and_clear_cache,
    status_message,
)
from schedule_adjustment_tool.ui.manager.project_cache import refresh_project_list_cache
from schedule_adjustment_tool.ui.manager.system_callbacks import (
    backup_restore_confirmation_dialog,
    delete_project_confirmation_dialog,
    reset_confirmation_dialog,
)


APP_SETTINGS = load_app_settings()
SYSTEM_EXPORT_CACHE_KEY = "system_export_cache"


def render_system_maintenance_section() -> None:
    """Render destructive actions separately from ordinary project management."""

    projects = load_project_list_cached()
    st.warning(
        "この画面にはリセット・削除・復元があります。"
        "対象企画と範囲を確認してから実行してください。"
    )
    st.subheader("企画データのリセット")
    reset_project_id = st.selectbox(
        "リセットする企画",
        [str(project["id"]) for project in projects],
        format_func=lambda value: next(
            str(project.get("title", "名称未設定"))
            for project in projects
            if project.get("id") == value
        ),
        key="reset_project_selector",
    )
    reset_type = st.radio(
        "リセット範囲",
        [
            "参加可能日時と提出状態のみ",
            "生成候補と確定結果のみ",
            "企画内の全データ",
        ],
        key="system_reset_type",
    )
    if st.button("選択範囲をリセット", type="primary"):
        reset_confirmation_dialog(reset_project_id, reset_type)

    st.divider()
    st.subheader("企画の削除")
    delete_project_id = st.selectbox(
        "削除する企画",
        [str(project["id"]) for project in projects],
        format_func=lambda value: next(
            str(project.get("title", "名称未設定"))
            for project in projects
            if project.get("id") == value
        ),
        key="delete_project_selector",
    )
    if st.button("選択した企画を削除", disabled=len(projects) <= 1):
        selected_project = next(
            project for project in projects if project.get("id") == delete_project_id
        )
        delete_project_confirmation_dialog(
            delete_project_id,
            str(selected_project.get("title", "名称未設定")),
        )
    if len(projects) <= 1:
        st.caption("最後の1企画は削除できません。")

    st.divider()
    st.subheader("削除済み企画・バックアップ")
    if st.button("削除済み企画を読み込む/更新"):
        load_deleted_projects_cached(force=True)
    deleted_projects = (
        load_deleted_projects_cached()
        if DELETED_PROJECTS_CACHE_KEY in st.session_state
        else []
    )
    if deleted_projects:
        restore_project_id = st.selectbox(
            "復元する削除済み企画",
            [str(project["id"]) for project in deleted_projects],
            format_func=lambda value: next(
                project["title"]
                for project in deleted_projects
                if project["id"] == value
            ),
        )
        if st.button("削除済み企画を復元"):
            with status_message("削除済み企画を復元しています..."):
                restore_deleted_project(restore_project_id)
                refresh_project_list_cache()
                clear_deleted_projects_cache()
                clear_audit_logs_cache()
            st.success("企画を復元しました。")
            st.rerun()
    else:
        st.caption("必要な時だけ削除済み企画を読み込んでください。")
    backup_project_id = st.selectbox(
        "バックアップを確認する企画",
        [str(project["id"]) for project in projects],
        format_func=lambda value: next(
            project["title"] for project in projects if project["id"] == value
        ),
    )
    if st.button("バックアップ一覧を読み込む/更新"):
        load_backups_cached(backup_project_id, force=True)
    backups = (
        load_backups_cached(backup_project_id)
        if (
            isinstance(st.session_state.get(BACKUPS_CACHE_KEY), dict)
            and backup_project_id in st.session_state[BACKUPS_CACHE_KEY]
        )
        else []
    )
    if backups:
        backup_id = st.selectbox(
            "復元するバックアップ",
            [int(backup["id"]) for backup in backups],
            format_func=lambda value: next(
                (
                    f"{backup['created_at']} / {backup['kind']} "
                    f"version {backup['version']}"
                )
                for backup in backups
                if int(backup["id"]) == value
            ),
        )
        if st.button("選択したバックアップを復元"):
            backup_restore_confirmation_dialog(backup_id, backup_project_id)
    else:
        st.caption("必要な時だけバックアップ一覧を読み込んでください。")

    st.divider()
    st.subheader("監査ログ")
    if st.button("監査ログを読み込む/更新"):
        load_audit_logs_cached(force=True)
    audit_logs = (
        load_audit_logs_cached()
        if AUDIT_LOGS_CACHE_KEY in st.session_state
        else []
    )
    if audit_logs:
        st.dataframe(pd.DataFrame(audit_logs), hide_index=True, width="stretch")
    else:
        st.caption("必要な時だけ監査ログを読み込んでください。")

    st.divider()
    st.subheader("システムデータ出力")
    if APP_SETTINGS.allow_json_exports:
        export_fingerprint = tuple(
            (
                str(project.get("id", "")),
                str(project.get("updated_at", "")),
                str(project.get("deleted_at", "")),
            )
            for project in projects
        )
        prepared_export = st.session_state.get(SYSTEM_EXPORT_CACHE_KEY)
        if (
            isinstance(prepared_export, dict)
            and prepared_export.get("fingerprint") != export_fingerprint
        ):
            prepared_export = None
            st.session_state.pop(SYSTEM_EXPORT_CACHE_KEY, None)
        if st.button("全企画データJSONを準備"):
            with status_message("全企画データのJSONを準備しています..."):
                all_projects_export = {
                    "system_settings": load_system_settings_cached(),
                    "projects": [
                        {
                            "organization": project,
                            "data": export_all_data(str(project["id"])),
                        }
                        for project in projects
                    ],
                    "exported_at": now_iso(),
                }
                prepared_export = {
                    "fingerprint": export_fingerprint,
                    "bytes": json.dumps(
                        all_projects_export, ensure_ascii=False, indent=2
                    ).encode("utf-8"),
                }
                st.session_state[SYSTEM_EXPORT_CACHE_KEY] = prepared_export
        if isinstance(prepared_export, dict):
            st.download_button(
                "準備済みJSONをダウンロード",
                prepared_export["bytes"],
                file_name="schedule_system_export.json",
                mime="application/json",
                on_click=record_audit_event_and_clear_cache,
                args=("system.exported",),
            )
        else:
            st.caption("全企画データは大きいため、必要な時だけ準備してください。")
    else:
        st.caption(
            "JSON出力は無効です。必要な場合だけ "
            "`SCHEDULE_ALLOW_JSON_EXPORTS=true` を設定してください。"
        )
