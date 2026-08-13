"""Destructive-operation confirmations for manager lifecycle actions.

The storage layer remains responsible for transactions, CAS checks, backups,
and audit data. These dialogs only obtain consent and refresh the UI caches
after the existing operation succeeds.
"""

from __future__ import annotations

import streamlit as st

from schedule_adjustment_tool.storage import (
    StorageConflictError,
    StorageError,
    delete_common_participant,
    delete_project,
    load_project_data,
    reset_project_data,
    restore_backup,
)
from schedule_adjustment_tool.ui.manager.app_cache import (
    clear_audit_logs_cache,
    clear_backups_cache,
    clear_common_participants_cache,
    clear_deleted_projects_cache,
    clear_memberships_cache,
    clear_prepared_exports,
    status_message,
)
from schedule_adjustment_tool.ui.manager.project_cache import (
    clear_project_data_cache,
    refresh_project_data_cache,
    refresh_project_list_cache,
)


def _run_with_status(message: str, operation, *args, **kwargs):
    with st.spinner(message, show_time=True):
        return operation(*args, **kwargs)

@st.dialog("データリセットの確認")
def reset_confirmation_dialog(project_id: str, reset_type: str) -> None:
    st.warning(f"「{reset_type}」を実行します。この操作は対象データを初期化します。")
    columns = st.columns(2)
    if columns[0].button("リセットを実行", type="primary"):
        with status_message("企画データをリセットしています..."):
            reset_project_data(
                project_id,
                reset_availability=reset_type == "参加可能日時と提出状態のみ",
                reset_candidates=reset_type == "生成候補と確定結果のみ",
                reset_all=reset_type == "企画内の全データ",
            )
        refresh_project_data_cache(project_id)
        clear_backups_cache(project_id)
        clear_audit_logs_cache()
        st.rerun()
    if columns[1].button("キャンセル"):
        st.rerun()


@st.dialog("企画削除の確認")
def delete_project_confirmation_dialog(
    project_id: str, project_title: str
) -> None:
    st.error(f"企画「{project_title}」を削除します。削除後は退避領域へ移動します。")
    columns = st.columns(2)
    if columns[0].button("削除を実行", type="primary"):
        _run_with_status("企画を削除しています...", delete_project, project_id)
        clear_project_data_cache(project_id)
        refresh_project_list_cache()
        clear_deleted_projects_cache()
        clear_backups_cache(project_id)
        clear_audit_logs_cache()
        if st.session_state.get("active_project_id") == project_id:
            st.session_state.pop("active_project_id", None)
        st.rerun()
    if columns[1].button("キャンセル"):
        st.rerun()


@st.dialog("参加者削除の確認")
def common_participant_delete_confirmation_dialog(profile: dict) -> None:
    participant_id = str(profile.get("id", ""))
    participant_name = str(profile.get("name", ""))
    project_count = int(profile.get("project_count", 0) or 0)
    st.error(f"登録済み参加者「{participant_name}」を削除します。")
    st.warning(
        f"登録中の{project_count}企画から名簿・入力・参加者アカウントの"
        "企画紐付けを外し、それらの企画の保存済み候補を削除します。"
        "本人アカウント自体は削除しません。"
        "確定日程で使用中の場合は削除を中止します。"
    )
    columns = st.columns(2)
    if columns[0].button(
        "登録済み参加者を削除",
        type="primary",
        key=f"confirm_common_participant_delete_{participant_id}",
    ):
        try:
            with status_message("登録済み参加者を削除しています..."):
                result = delete_common_participant(
                    participant_id,
                    expected_updated_at=str(
                        profile.get("updated_at", "")
                    )
                    or None,
                )
                clear_common_participants_cache()
                clear_project_data_cache()
                clear_memberships_cache()
                clear_prepared_exports()
                clear_audit_logs_cache()
        except (StorageConflictError, StorageError) as error:
            st.warning(str(error))
        else:
            st.success(
                f"{result['name']}さんを登録済み参加者から削除しました。"
            )
            st.rerun()
    if columns[1].button(
        "キャンセル",
        key=f"cancel_common_participant_delete_{participant_id}",
    ):
        st.rerun()


@st.dialog("バックアップ復元の確認")
def backup_restore_confirmation_dialog(
    backup_id: int, project_id: str = ""
) -> None:
    expected_key = f"backup_restore_expected_revision_{backup_id}"
    if project_id and expected_key not in st.session_state:
        current = load_project_data(project_id).get("confirmed")
        st.session_state[expected_key] = str(
            (current or {}).get("schedule_revision", {}).get("id", "")
        )
    st.warning(
        "現在の内容を選択したバックアップ時点へ戻します。"
        "現在値も復元前バックアップとして保存されます。"
    )
    columns = st.columns(2)
    if columns[0].button("復元を実行", type="primary"):
        try:
            restore_kwargs = (
                {"expected_revision_id": st.session_state[expected_key]}
                if expected_key in st.session_state
                else {}
            )
            _run_with_status(
                "バックアップを復元しています...",
                restore_backup,
                backup_id,
                **restore_kwargs,
            )
        except (StorageConflictError, StorageError) as error:
            st.warning(str(error))
        else:
            st.session_state.pop(expected_key, None)
            clear_project_data_cache()
            refresh_project_list_cache()
            clear_backups_cache()
            clear_audit_logs_cache()
            st.rerun()
    if columns[1].button("キャンセル"):
        st.session_state.pop(expected_key, None)
        st.rerun()


@st.dialog("保存済み候補の置換確認")
def candidate_replacement_confirmation_dialog(
    project_id: str,
    project_title: str,
    candidate_count: int,
) -> None:
    st.warning(
        f"企画「{project_title}」の保存済み候補{candidate_count}件を、"
        "次の探索結果で置き換えます。確定日程は変更しません。"
    )
    st.caption(
        "既存候補を残したまま探す場合は、キャンセルして「追加探索」を選んでください。"
    )
    columns = st.columns(2)
    if columns[0].button(
        "置換して探索を開始",
        type="primary",
        key=f"confirm_candidate_replacement_{project_id}",
    ):
        st.session_state[f"candidate_replacement_approved_{project_id}"] = True
        st.rerun()
    if columns[1].button(
        "キャンセル",
        key=f"cancel_candidate_replacement_{project_id}",
    ):
        st.rerun()
