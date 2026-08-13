"""Manager-only editor for participant availability responses."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import streamlit as st

from schedule_adjustment_tool.domain.models import Config, Participant, eligible_dates
from schedule_adjustment_tool.ui.availability_grid_component import availability_grid
from schedule_adjustment_tool.ui.manager.participants import participant_choice_sort_key


@dataclass(frozen=True)
class ResponseEditorServices:
    """Dialog and helper callbacks owned by the application composition layer."""

    input_status_labels: dict[str, str]
    render_availability_input_guide: Callable[[], None]
    availability_grid: Callable[..., tuple[set[str], set[str], str | None]]
    confirm_manager_response: Callable[..., None]
    confirm_response_restore: Callable[..., None]


def render_manager_participant_response_editor(
    project_id: str,
    config: Config,
    participants: list[Participant],
    *,
    embedded: bool = False,
    services: ResponseEditorServices,
) -> None:
    """Render a manager override without discarding a participant's response."""

    if embedded:
        st.markdown("#### 参加者の回答を代理入力・修正")
    else:
        st.header("参加可能日時の代理入力")
    st.caption(
        "メッセージ等で受け取った回答や調整後の内容を代理入力として保存します。"
        "代理入力がある間はその内容を日程作成に使い、本人の入力も別に残します。"
    )
    saved_result = st.session_state.pop(
        f"manager_response_saved_{project_id}",
        None,
    )
    if isinstance(saved_result, dict):
        saved_result = dict(saved_result)
    if not participants:
        st.info("代理入力する参加者が名簿にいません。")
        return

    sorted_participants = sorted(participants, key=participant_choice_sort_key)
    participant_by_id = {
        participant.id: participant for participant in sorted_participants
    }
    selected_participant_id = st.selectbox(
        "代理入力する参加者",
        list(participant_by_id),
        format_func=lambda participant_id: _participant_option_label(
            participant_by_id[participant_id],
            services.input_status_labels,
        ),
        key=f"manager_response_participant_{project_id}",
    )
    participant = participant_by_id[selected_participant_id]
    status_columns = st.columns(5)
    status_columns[0].metric(
        "日程作成に使う回答",
        "代理入力" if participant.response_source == "manager" else "本人の入力",
    )
    status_columns[1].metric(
        "現在の入力状態",
        services.input_status_labels.get(
            participant.input_status,
            participant.input_status,
        ),
    )
    status_columns[2].metric("対面可能", f"{len(participant.availability)}コマ")
    status_columns[3].metric(
        "Zoom可能",
        f"{len(participant.zoom_availability)}コマ",
    )
    status_columns[4].metric(
        "日調対象",
        "対象" if participant.active and participant.approved else "対象外",
    )
    if participant.response_source == "manager":
        _render_saved_participant_response(
            project_id,
            participant,
            services,
        )
    if not participant.approved:
        st.warning("この参加者は登録承認前です。回答は保存できますが日調対象外です。")
    elif not participant.active:
        st.info("この参加者は現在、日調対象から外れています。")

    st.subheader("参加可能日時")
    services.render_availability_input_guide()
    selected_slots, selected_zoom_slots, action = services.availability_grid(
        eligible_dates(config),
        periods=config.enabled_periods,
        availability=set(participant.availability),
        zoom_availability=set(participant.zoom_availability),
        key=(
            f"manager_availability_{project_id}_{participant.id}_"
            f"{participant.storage_version}"
        ),
        disabled=False,
        action_buttons=[
            {
                "action": "manager_draft",
                "label": "下書きとして保存",
                "primary": False,
            },
            {
                "action": "manager_submitted",
                "label": "提出済みとして保存",
                "primary": True,
            },
        ],
    )
    if action in {"manager_draft", "manager_submitted"}:
        services.confirm_manager_response(
            project_id,
            participant.to_dict(),
            selected_slots,
            selected_zoom_slots,
            "submitted" if action == "manager_submitted" else "draft",
        )
    if isinstance(saved_result, dict):
        if saved_result.get("restored"):
            st.success(
                f"{saved_result.get('participant_name', '参加者')}を"
                "本人の入力へ戻しました。"
            )
        else:
            st.success(
                f"{saved_result.get('participant_name', '参加者')}の代理入力を"
                f"{saved_result.get('status_label', '')}として保存しました。"
            )


def _participant_option_label(
    participant: Participant,
    input_status_labels: dict[str, str],
) -> str:
    return (
        f"{participant.name} / "
        f"{input_status_labels.get(participant.input_status, participant.input_status)}"
        f"{'' if participant.active else ' / 日調対象外'}"
    )


def _render_saved_participant_response(
    project_id: str,
    participant: Participant,
    services: ResponseEditorServices,
) -> None:
    participant_backup = participant.participant_response
    input_status = str(participant_backup.get("input_status", "not_started"))
    st.info(
        "現在は代理入力を日程作成に使用しています。"
        "本人の入力は次の内容で保存されています: "
        f"{services.input_status_labels.get(input_status, input_status)} / "
        f"対面可能 {len(participant_backup.get('availability', []))}コマ / "
        f"Zoom可能 {len(participant_backup.get('zoom_availability', []))}コマ"
    )
    if st.button(
        "本人の入力に戻す",
        key=f"restore_participant_response_{project_id}_{participant.id}",
    ):
        services.confirm_response_restore(project_id, participant.to_dict())
