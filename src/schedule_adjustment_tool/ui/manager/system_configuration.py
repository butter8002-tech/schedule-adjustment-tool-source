"""Global settings and maintenance-mode controls for system administrators."""

from __future__ import annotations

import streamlit as st

from schedule_adjustment_tool.domain.app_config import load_app_settings
from schedule_adjustment_tool.storage import save_system_settings
from schedule_adjustment_tool.ui.manager.app_cache import (
    clear_audit_logs_cache,
    load_system_settings_cached,
    set_cached_system_settings,
    status_message,
)


APP_SETTINGS = load_app_settings()


def render_system_settings_section() -> None:
    """Render settings shared by every project and participant screen."""

    st.subheader("全体設定")
    st.caption("期、データ保持期間、参加者向けの説明を設定します。")
    settings = load_system_settings_cached()
    with st.form("system_settings_form"):
        active_cohorts = st.multiselect(
            "現在有効な期",
            list(range(1, 101)),
            default=settings["active_cohorts"],
            format_func=lambda value: f"{value}期",
            help="参加者属性で選択できる期です。年度更新時にもここだけを変更します。",
        )
        retention_columns = st.columns([1, 3])
        data_retention_days = int(
            retention_columns[0].number_input(
                "削除データ保持日数",
                min_value=1,
                max_value=3650,
                value=int(settings.get("data_retention_days", 30)),
            )
        )
        privacy_notice = retention_columns[1].text_area(
            "参加者向けプライバシー説明",
            value=settings.get("privacy_notice", ""),
            max_chars=APP_SETTINGS.max_description_length,
            help="利用目的、閲覧者、保存期間、問い合わせ先を記載します。",
        )
        save_system_settings_clicked = st.form_submit_button(
            "全体設定を保存", type="primary"
        )
    if save_system_settings_clicked:
        if not active_cohorts:
            st.warning("有効な期を1つ以上選択してください。")
        else:
            settings_to_save = {
                "active_cohorts": active_cohorts,
                "privacy_notice": privacy_notice,
                "data_retention_days": data_retention_days,
                "maintenance_mode": bool(
                    settings.get("maintenance_mode", False)
                ),
            }
            with status_message("全体設定を保存しています..."):
                save_system_settings(settings_to_save)
                set_cached_system_settings(
                    {
                        **settings_to_save,
                        "active_cohorts": sorted(active_cohorts),
                    }
                )
                clear_audit_logs_cache()
            st.success("全体設定を保存しました。")
            st.rerun()


def render_system_maintenance_mode_section() -> None:
    """Render the access gate used during a planned maintenance period."""

    st.subheader("メンテナンス")
    st.caption(
        "メンテナンス中にすると、システム管理者以外が認証した後は"
        "「メンテナンス中」だけを表示し、操作画面を開かせません。"
        "ログイン記録を含むDB書き込みも行いません。"
        "システム管理者は通常どおりすべての操作区分を利用できます。"
    )
    settings = load_system_settings_cached()
    feedback_key = "system_maintenance_mode_feedback"
    with st.form("system_maintenance_mode_form"):
        maintenance_mode = st.checkbox(
            "メンテナンス中",
            value=bool(settings.get("maintenance_mode", False)),
            help="システム管理者以外の操作画面とDB書き込みを停止します。",
        )
        save_clicked = st.form_submit_button(
            "メンテナンス設定を保存", type="primary"
        )
    feedback = st.session_state.pop(feedback_key, None)
    if feedback:
        st.success(str(feedback))
    if save_clicked:
        updated_settings = {
            **settings,
            "maintenance_mode": bool(maintenance_mode),
        }
        with status_message("メンテナンス設定を保存しています..."):
            save_system_settings(updated_settings)
            set_cached_system_settings(updated_settings)
            clear_audit_logs_cache()
        st.session_state[feedback_key] = "メンテナンス設定を保存しました。"
        st.rerun()
