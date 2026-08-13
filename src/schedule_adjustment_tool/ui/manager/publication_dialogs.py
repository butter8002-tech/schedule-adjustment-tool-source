"""Confirmation dialogs for published-schedule revision operations.

Revision creation, restoration, and CAS validation remain implemented by
storage. The dialogs only describe the operation and refresh cached UI state.
"""

from __future__ import annotations

import streamlit as st

from schedule_adjustment_tool.storage import (
    clear_confirmed_candidate,
    restore_schedule_revision,
)
from schedule_adjustment_tool.ui.manager.app_cache import clear_prepared_exports, status_message
from schedule_adjustment_tool.ui.manager.project_cache import (
    refresh_project_config_cache,
    set_cached_confirmed,
)

@st.dialog("確定取消の確認")
def confirmed_schedule_clear_confirmation_dialog(
    project_id: str,
    project_title: str,
    expected_revision_id: str,
) -> None:
    st.warning(
        f"企画「{project_title}」の確定を取り消し、回答締切の状態へ戻します。"
    )
    st.caption(
        "保存済み候補と日程の変更履歴は残ります。"
        "再度確定するまで参加者には確定日程として表示されません。"
    )
    columns = st.columns(2)
    if columns[0].button(
        "確定を取り消す",
        type="primary",
        key=f"confirm_clear_schedule_{project_id}",
    ):
        with status_message("確定を取り消しています..."):
            clear_confirmed_candidate(
                project_id,
                project_status="closed",
                expected_revision_id=expected_revision_id,
            )
            set_cached_confirmed(project_id, None)
            refresh_project_config_cache(project_id)
            clear_prepared_exports(project_id)
        st.rerun()
    if columns[1].button(
        "キャンセル",
        key=f"cancel_clear_schedule_{project_id}",
    ):
        st.rerun()


@st.dialog("変更履歴から復元")
def schedule_revision_restore_confirmation_dialog(
    project_id: str,
    project_title: str,
    restore_revision_id: str,
    restore_revision_number: int,
    expected_revision_id: str,
) -> None:
    st.warning(
        f"企画「{project_title}」の変更履歴 {restore_revision_number} の内容を、"
        "現在の日程として復元します。"
    )
    st.caption(
        "現在の日程は上書き削除されず、履歴に残ります。"
        "復元後の日程を必ず確認してください。"
    )
    columns = st.columns(2)
    if columns[0].button(
        "この履歴を復元",
        type="primary",
        key=f"confirm_restore_schedule_revision_{restore_revision_id}",
    ):
        with status_message("選択した日程を復元しています..."):
            restored = restore_schedule_revision(
                project_id,
                restore_revision_id,
                expected_revision_id=expected_revision_id,
            )
            set_cached_confirmed(project_id, restored)
            refresh_project_config_cache(project_id)
            clear_prepared_exports(project_id)
        st.rerun()
    if columns[1].button(
        "キャンセル",
        key=f"cancel_restore_schedule_revision_{restore_revision_id}",
    ):
        st.rerun()
