"""Response-status and availability-review screens for the manager UI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.models import Config, Participant
from schedule_adjustment_tool.domain.participant_attributes import display_department
from schedule_adjustment_tool.ui.calendar_views import (
    availability_calendar_frames,
    availability_full_calendar,
)


@dataclass(frozen=True)
class ResponseScreenServices:
    """Formatting and table operations supplied by the Streamlit entrypoint."""

    input_status_labels: Mapping[str, str]
    format_datetime: Callable[[str], str]
    render_calendar_table: Callable[..., None]


def response_status_rows(
    participants: list[Participant],
    *,
    services: ResponseScreenServices,
) -> list[dict[str, object]]:
    return [
        {
            "名前": participant.name,
            "入力状況": services.input_status_labels.get(
                participant.input_status,
                participant.input_status,
            ),
            "日程作成に使用": (
                "代理入力"
                if participant.response_source == "manager"
                else "本人の入力"
            ),
            "登録種別": (
                "管理者登録"
                if participant.registered_by == "admin"
                else "参加者追加"
            ),
            "承認": "承認済み" if participant.approved else "承認待ち",
            "有効": "対象" if participant.active else "対象外",
            "班": str(participant.group_number),
            "期": participant.cohort,
            "文理": participant.humanities_or_science,
            "科類・学部学科": display_department(
                participant.department,
                participant.department_detail,
            ),
            "属性変更": (
                "管理者確認待ち"
                if participant.attributes_changed_by_participant
                else ""
            ),
            "対面可コマ数": len(participant.availability),
            "Zoomなら可コマ数": len(participant.zoom_availability),
            "合計可コマ数": len(
                set(participant.availability) | set(participant.zoom_availability)
            ),
            "最終更新": services.format_datetime(
                participant.updated_at or participant.submitted_at
            ),
        }
        for participant in participants
    ]


def render_response_list(
    config: Config,
    participants: list[Participant],
    *,
    status_only: bool,
    services: ResponseScreenServices,
) -> None:
    frame = pd.DataFrame(response_status_rows(participants, services=services))
    status_filter = st.multiselect(
        "入力状況で絞り込み",
        list(services.input_status_labels.values()),
        default=list(services.input_status_labels.values()),
        key=(
            f"response_status_filter_{config.project_id}_"
            f"{'status' if status_only else 'content'}"
        ),
    )
    filtered = frame[frame["入力状況"].isin(status_filter)]
    if status_only:
        filtered = filtered[
            ["名前", "入力状況", "日程作成に使用", "最終更新"]
        ]
    else:
        filtered = filtered.drop(
            columns=["登録種別", "承認", "有効", "属性変更"],
            errors="ignore",
        )
    st.dataframe(filtered, hide_index=True, width="stretch")


def render_response_calendar(
    config: Config,
    participants: list[Participant],
    *,
    services: ResponseScreenServices,
) -> None:
    st.caption(
        "各日付・時限に、参加可能と回答した参加者を表示します。"
        "Zoomのみ参加可能な場合は、名前に（Zoom）が付きます。"
    )
    calendar_range = st.segmented_control(
        "表示範囲",
        ["週ごと", "期間全体"],
        default="週ごと",
        key=f"availability_calendar_range_{config.project_id}",
    )
    if calendar_range == "週ごと":
        for week_title, week_frame in availability_calendar_frames(
            config,
            participants,
        ):
            st.markdown(f"##### {week_title}")
            services.render_calendar_table(week_frame, config=config)
    else:
        services.render_calendar_table(
            availability_full_calendar(config, participants),
            config=config,
        )


def render_response_reminder(participants: list[Participant]) -> None:
    reminder_names = [
        participant.name
        for participant in participants
        if participant.active
        and participant.approved
        and participant.input_status != "submitted"
    ]
    reminder_text = (
        "練習会の日程調整が未提出です。入力をお願いします。\n対象: "
        + "、".join(reminder_names)
        if reminder_names
        else "全員提出済みです。"
    )
    st.markdown("##### 連絡用メッセージ")
    st.caption("右上のコピーアイコンからコピーできます。")
    st.code(reminder_text, language=None)
