"""Confirmations for configuration and participant changes after publication."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import streamlit as st

from schedule_adjustment_tool.domain.models import Config, Participant
from schedule_adjustment_tool.storage import StorageConflictError, StorageError
from schedule_adjustment_tool.ui.manager.app_cache import (
    set_project_operation_feedback,
    status_message,
)


@dataclass(frozen=True)
class ProjectChangeDialogServices:
    """Existing app operations needed after a user confirms a change."""

    apply_config_updates: Callable[[str, Config, dict], None]
    reset_individual_conditions: Callable[[str, list[Participant], Config], None]
    save_global_conditions: Callable[
        [str, Config, list[Participant], dict], None
    ]
    apply_participant_updates: Callable[[str, list[Participant]], None]
    save_project_settings: Callable[..., None]
    refresh_project_participants: Callable[[str], list[Participant]]
    clear_candidate_state: Callable[[str], None]
    mark_step_started: Callable[[str, str], None]

@st.dialog("個別条件を初期化する確認")
def global_condition_change_confirmation_dialog(
    project_id: str,
    config_payload: dict[str, object],
    participants_payload: list[dict[str, object]],
    updates: dict[str, object],
    confirmed: dict | None = None,
    *,
    candidate_count: int = 0,
    operations: ProjectChangeDialogServices,
) -> None:
    st.warning(
        "全体の成立条件を変更すると、現在の個別条件を新しい全体設定に"
        "合わせて初期化します。"
    )
    st.caption(
        "個別条件の役割指定、必要回数、超過上限、練習会参加数を"
        "いったん既定値へ戻します。保存後に必要な人だけ一括編集できます。"
    )
    if candidate_count:
        st.error(
            f"この保存により、保存済み候補{candidate_count}件を削除します。"
        )
    if confirmed:
        st.warning(
            "公開中の日程があるため、この変更を保存すると、"
            "現在公開されている日程と条件が合わなくなる可能性があります。"
        )
        st.caption(
            "保存後は公開中の日程を確認し、必要なら候補作成からやり直してください。"
        )
    columns = st.columns(2)
    if columns[0].button(
        "変更して初期化する",
        type="primary",
        key=f"confirm_global_condition_change_{project_id}",
    ):
        original_config = Config.from_dict(config_payload)
        participants = [
            Participant.from_dict(payload)
            for payload in participants_payload
        ]
        try:
            with status_message("成立条件と個別条件を保存しています..."):
                operations.save_global_conditions(
                    project_id,
                    original_config,
                    participants,
                    updates,
                )
        except (StorageError, StorageConflictError, ValueError) as error:
            st.error(str(error))
        else:
            operations.mark_step_started(project_id, "conditions")
            set_project_operation_feedback(
                project_id,
                "役割・参加条件と個別条件を保存しました。",
                operation_key="role_conditions",
            )
            st.rerun()
    if columns[1].button(
        "変更を取り消す",
        key=f"cancel_global_condition_change_{project_id}",
    ):
        st.rerun()

@st.dialog("設定変更の確認")
def published_schedule_change_confirmation_dialog(
    project_id: str,
    config_payload: dict[str, object],
    participants_payload: list[dict[str, object]],
    updates: dict[str, object],
    success_message: str,
    workflow_step_id: str | None,
    *,
    candidate_count: int = 0,
    published_schedule_exists: bool = True,
    feedback_operation_key: str = "settings",
    feedback_session_key: str | None = None,
    cancel_reset_keys: tuple[str, ...] = (),
    operations: ProjectChangeDialogServices,
) -> None:
    if candidate_count:
        st.error(
            f"この保存により、保存済み候補{candidate_count}件を削除します。"
        )
    if published_schedule_exists:
        st.warning(
            "公開中の日程があるため、この変更を保存すると、"
            "現在公開されている日程と条件が合わなくなる可能性があります。"
        )
        st.caption(
            "変更後は、公開中の日程を確認し、必要なら候補作成からやり直してください。"
        )
    columns = st.columns(2)
    if columns[0].button(
        "確認して変更を保存",
        type="primary",
        key=f"confirm_published_schedule_change_{project_id}",
    ):
        original_config = Config.from_dict(config_payload)
        try:
            with status_message("公開中の日程への影響を確認して保存しています..."):
                operations.apply_config_updates(
                    project_id,
                    original_config,
                    updates,
                )
        except (StorageError, StorageConflictError, ValueError) as error:
            st.error(str(error))
        else:
            if workflow_step_id:
                operations.mark_step_started(project_id, workflow_step_id)
            if feedback_session_key:
                st.session_state[feedback_session_key] = {
                    "kind": "success",
                    "message": success_message,
                }
            else:
                set_project_operation_feedback(
                    project_id,
                    success_message,
                    operation_key=feedback_operation_key,
                )
            st.rerun()
    if columns[1].button(
        "キャンセル",
        key=f"cancel_published_schedule_change_{project_id}",
    ):
        for key in cancel_reset_keys:
            st.session_state.pop(key, None)
        st.rerun()


@st.dialog("回答受付を再開する確認")
def response_reopen_confirmation_dialog(
    project_id: str,
    config_payload: dict[str, object],
    participants_payload: list[dict[str, object]],
    updates: dict[str, object],
    confirmed: dict | None,
    operations: ProjectChangeDialogServices,
) -> None:
    del participants_payload
    config = Config.from_dict(config_payload)
    st.warning(
        "回答締切から回答受付中へ戻します。"
        "本当に回答受付を再開してよいか確認してください。"
    )
    if not bool(updates.get("allow_edits_after_deadline")):
        st.info(
            "回答受付を再開する代わりに、締切後も参加者による編集を許可"
            "する設定をONにすることを推奨します。"
        )
    if confirmed:
        st.warning(
            "公開中の日程があるため、回答を再開すると現在の日程と"
            "回答条件が合わなくなる可能性があります。"
        )
    columns = st.columns(2)
    if columns[0].button(
        "回答受付を再開する",
        type="primary",
        key=f"confirm_response_reopen_{project_id}",
    ):
        if not bool(updates.get("allow_edits_after_deadline")):
            st.session_state[
                f"response_window_reopen_notice_{project_id}"
            ] = True
        try:
            # The dialog above already covers the published-schedule warning.
            # Do not open a second dialog while applying the confirmed change.
            operations.save_project_settings(
                project_id,
                config,
                [],
                updates,
                success_message="回答受付設定を保存しました。",
                confirmed=confirmed,
                published_conflict=False,
                feedback_operation_key="response_window",
            )
        except (StorageError, StorageConflictError, ValueError) as error:
            st.error(str(error))
    if columns[1].button(
        "キャンセル",
        key=f"cancel_response_reopen_{project_id}",
    ):
        st.rerun()


@st.dialog("公開中の日程への影響を確認")
def published_participant_change_confirmation_dialog(
    project_id: str,
    updated_participants_payload: list[dict[str, object]],
    *,
    change_label: str,
    workflow_step_id: str | None = None,
    candidate_count: int = 0,
    published_schedule_exists: bool = True,
    feedback_operation_key: str = "individual_conditions",
    operations: ProjectChangeDialogServices,
) -> None:
    if published_schedule_exists:
        st.warning(
            f"公開中の日程があるため、参加者の{change_label}を変更すると、"
            "現在の日程と参加条件が合わなくなる可能性があります。"
        )
        st.caption(
            "保存後は公開中の日程を確認し、必要なら候補を作り直してください。"
        )
    if candidate_count:
        st.error(
            f"この保存により、保存済み候補{candidate_count}件を削除します。"
        )
    columns = st.columns(2)
    if columns[0].button(
        "確認して変更を保存",
        type="primary",
        key=f"confirm_published_membership_change_{project_id}",
    ):
        updated_participants = [
            Participant.from_dict(payload)
            for payload in updated_participants_payload
        ]
        try:
            with status_message("参加者の変更を保存しています..."):
                operations.apply_participant_updates(
                    project_id,
                    updated_participants,
                )
        except (StorageError, StorageConflictError, ValueError) as error:
            st.error(str(error))
        else:
            if workflow_step_id:
                operations.mark_step_started(project_id, workflow_step_id)
            set_project_operation_feedback(
                project_id,
                f"参加者の{change_label}を保存しました。",
                operation_key=feedback_operation_key,
            )
            st.rerun()
    if columns[1].button(
        "キャンセル",
        key=f"cancel_published_membership_change_{project_id}",
    ):
        st.rerun()


def published_membership_change_confirmation_dialog(
    project_id: str,
    updated_participants_payload: list[dict[str, object]],
    *,
    candidate_count: int = 0,
    published_schedule_exists: bool = True,
    feedback_operation_key: str = "participant_membership",
    operations: ProjectChangeDialogServices,
) -> None:
    published_participant_change_confirmation_dialog(
        project_id,
        updated_participants_payload,
        change_label="参加者設定",
        candidate_count=candidate_count,
        published_schedule_exists=published_schedule_exists,
        feedback_operation_key=feedback_operation_key,
        workflow_step_id="participants",
        operations=operations,
    )
