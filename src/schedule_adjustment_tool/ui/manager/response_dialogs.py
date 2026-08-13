"""Confirmations for manager-provided participant responses."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import streamlit as st

from schedule_adjustment_tool.domain.models import Participant
from schedule_adjustment_tool.storage import (
    StorageConflictError,
    clear_manager_response_override,
)
from schedule_adjustment_tool.ui.manager.app_cache import status_message


@dataclass(frozen=True)
class ResponseDialogServices:
    """Existing operations invoked after manager response confirmation."""

    input_status_labels: dict[str, str]
    save_manager_response: Callable[..., None]
    refresh_project_participants: Callable[[str], list[Participant]]
    clear_candidate_state: Callable[[str], None]

@st.dialog("参加可能日時の代理入力を保存")
def manager_participant_response_confirmation_dialog(
    project_id: str,
    participant_payload: dict[str, object],
    selected_slots: set[str],
    selected_zoom_slots: set[str],
    input_status: str,
    *,
    operations: ResponseDialogServices,
) -> None:
    participant = Participant.from_dict(participant_payload)
    status_label = "提出済み" if input_status == "submitted" else "下書き"
    st.write(f"対象: **{participant.name}**")
    st.write(
        f"保存後の状態: **{status_label}** / "
        f"対面可能 {len(selected_slots)}コマ / "
        f"Zoom可能 {len(selected_zoom_slots)}コマ"
    )
    st.info(
        "この内容を日程作成に使用します。参加者本人の入力は別に残り、"
        "あとから本人の入力へ戻せます。"
    )
    st.warning(
        "回答を変更すると、現在の保存候補はいったん削除されます。"
        "確定日程は変更されません。"
    )
    action_columns = st.columns(2)
    if action_columns[0].button(
        f"{status_label}として保存を実行",
        type="primary",
        key=f"confirm_manager_response_{project_id}_{participant.id}",
    ):
        try:
            with status_message("参加可能日時を代理入力として保存しています..."):
                operations.save_manager_response(
                    project_id,
                    participant,
                    selected_slots=selected_slots,
                    selected_zoom_slots=selected_zoom_slots,
                    input_status=input_status,
                )
        except StorageConflictError as error:
            st.error(str(error))
        else:
            st.session_state[f"manager_response_saved_{project_id}"] = {
                "participant_name": participant.name,
                "status_label": status_label,
            }
            st.rerun()
    if action_columns[1].button(
        "戻って確認する",
        key=f"cancel_manager_response_{project_id}_{participant.id}",
    ):
        st.rerun()


@st.dialog("本人の入力に戻す")
def manager_response_restore_confirmation_dialog(
    project_id: str,
    participant_payload: dict[str, object],
    *,
    operations: ResponseDialogServices,
) -> None:
    participant = Participant.from_dict(participant_payload)
    backup = participant.participant_response
    st.write(f"対象: **{participant.name}**")
    st.write(
        "戻した後: "
        f"**{operations.input_status_labels.get(str(backup.get('input_status', 'not_started')), str(backup.get('input_status', 'not_started')))}** / "
        f"対面可能 {len(backup.get('availability', []))}コマ / "
        f"Zoom可能 {len(backup.get('zoom_availability', []))}コマ"
    )
    st.info(
        "担当者が保存した回答の使用を終了し、"
        "参加者本人が最後に保存した回答を日程作成に使用します。"
    )
    st.warning(
        "参照する回答が変わるため、現在の保存候補はいったん削除されます。"
        "確定日程は変更されません。"
    )
    columns = st.columns(2)
    if columns[0].button(
        "本人の入力に戻す",
        type="primary",
        key=f"confirm_restore_participant_response_{project_id}_{participant.id}",
    ):
        try:
            with status_message("本人の入力へ戻しています..."):
                clear_manager_response_override(
                    project_id,
                    participant.id,
                    expected_version=participant.storage_version,
                )
                operations.refresh_project_participants(project_id)
                operations.clear_candidate_state(project_id)
        except StorageConflictError as error:
            st.error(str(error))
        else:
            st.session_state[f"manager_response_saved_{project_id}"] = {
                "participant_name": participant.name,
                "status_label": "本人の入力へ戻しました",
                "restored": True,
            }
            st.rerun()
    if columns[1].button(
        "キャンセル",
        key=f"cancel_restore_participant_response_{project_id}_{participant.id}",
    ):
        st.rerun()
