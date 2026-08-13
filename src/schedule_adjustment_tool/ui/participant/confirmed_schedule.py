"""Read-only confirmed-schedule screen for a participant."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.models import (
    Config,
    Participant,
    ROLE_DISPLAY_COLORS,
    ROLE_DISPLAY_LABELS,
    eligible_dates,
    make_slot_key,
)
from schedule_adjustment_tool.ui.calendar_views import (
    calendar_table_html,
    candidate_calendar_frames,
    candidate_full_calendar,
    role_display_legend_html,
)
from schedule_adjustment_tool.ui.presentation import ROLE_DISPLAY_MODE_CHOICES
from schedule_adjustment_tool.ui.design_tokens import (
    HIGH_SCHOOL_ROLE,
    UNIVERSITY_ROLE,
)


ROLE_DISPLAY_LABEL_OPTION, ROLE_DISPLAY_COLOR_OPTION = ROLE_DISPLAY_MODE_CHOICES


@dataclass(frozen=True)
class ConfirmedScheduleServices:
    """Storage and formatting operations supplied by the app entrypoint."""

    load_confirmed_candidate: Callable[[str], dict[str, Any] | None]
    format_datetime: Callable[[str], str]
    format_date: Callable[[str], str]


def participant_in_session(
    session: dict[str, Any], participant: Participant
) -> bool:
    """Match both current member IDs and pre-ID legacy member names."""

    member_ids = {
        *map(str, session.get("university_role_member_ids", [])),
        *map(str, session.get("high_school_role_member_ids", [])),
    }
    if participant.id in member_ids:
        return True
    member_names = {
        *map(str, session.get("university_role_members", [])),
        *map(str, session.get("high_school_role_members", [])),
    }
    return participant.name in member_names


def _participant_schedule_rows(
    sessions: list[dict[str, Any]],
    participant: Participant,
    *,
    format_date: Callable[[str], str],
) -> tuple[list[dict[str, object]], set[str]]:
    rows: list[dict[str, object]] = []
    highlighted_slots: set[str] = set()
    for index, session in enumerate(sessions, start=1):
        is_self = participant_in_session(session, participant)
        slot_key = make_slot_key(session["date"], int(session["period"]))
        rows.append(
            {
                "番号": index,
                "日付": format_date(session["date"]),
                "時限": f"{session['period']}限",
                "開催形式": (
                    "Zoom" if session.get("meeting_mode") == "zoom" else "対面"
                ),
                "大学生役": "、".join(
                    session.get("university_role_members", [])
                ),
                "高校生役": "、".join(
                    session.get("high_school_role_members", [])
                ),
                "自分の予定": "参加" if is_self else "",
            }
        )
        if is_self:
            highlighted_slots.add(slot_key)
    return rows, highlighted_slots


def _highlight_participant_row(row: pd.Series) -> list[str]:
    if row.get("自分の予定") == "参加":
        return [
            "background-color: rgba(255, 224, 130, 0.45); font-weight: 700"
        ] * len(row)
    return [""] * len(row)


def render_confirmed_schedule(
    project_id: str,
    config: Config,
    participant: Participant,
    *,
    services: ConfirmedScheduleServices,
) -> None:
    """Render one participant's view of the active published schedule."""

    st.header("確定日程")
    confirmed = services.load_confirmed_candidate(project_id)
    if not confirmed:
        st.info("確定済みのスケジュールはありません。")
        return
    st.success(
        f"公開版v{max(1, int(confirmed.get('publication_number', 1)))} / "
        "公開日時: "
        f"{services.format_datetime(confirmed.get('confirmed_at', ''))}"
    )
    sessions = confirmed.get("sessions", [])
    if not sessions:
        st.info("表示できる日程がありません。")
        return

    list_rows, highlighted_slots = _participant_schedule_rows(
        sessions,
        participant,
        format_date=services.format_date,
    )
    selected_display = st.segmented_control(
        "役割の見分け方",
        [ROLE_DISPLAY_LABEL_OPTION, ROLE_DISPLAY_COLOR_OPTION],
        default=(
            ROLE_DISPLAY_COLOR_OPTION
            if config.role_display_mode == ROLE_DISPLAY_COLORS
            else ROLE_DISPLAY_LABEL_OPTION
        ),
        key=f"participant_confirmed_role_display_{project_id}",
    )
    role_display_mode = (
        ROLE_DISPLAY_COLORS
        if selected_display == ROLE_DISPLAY_COLOR_OPTION
        else ROLE_DISPLAY_LABELS
    )
    if role_display_mode == ROLE_DISPLAY_COLORS:
        st.markdown(
            role_display_legend_html(role_display_mode),
            unsafe_allow_html=True,
        )

    selected_view = st.segmented_control(
        "表示形式",
        ["カレンダー表示", "一覧表示"],
        default="カレンダー表示",
        key=f"participant_confirmed_view_{project_id}",
    )
    if selected_view == "一覧表示":
        styled_list = pd.DataFrame(list_rows).style.apply(
            _highlight_participant_row,
            axis=1,
        )
        if role_display_mode == ROLE_DISPLAY_COLORS:
            styled_list = styled_list.set_properties(
                subset=["大学生役"], **{"color": UNIVERSITY_ROLE}
            ).set_properties(
                subset=["高校生役"], **{"color": HIGH_SCHOOL_ROLE}
            )
        st.dataframe(styled_list, hide_index=True, width="stretch")
        return

    if not eligible_dates(config):
        st.info("カレンダー表示できる対象日がありません。")
        return
    calendar_range = st.segmented_control(
        "カレンダー範囲",
        ["週ごと", "期間全体"],
        default="週ごと",
        key=f"participant_confirmed_calendar_range_{project_id}",
    )
    if calendar_range == "週ごと":
        for week_title, week_frame in candidate_calendar_frames(
            config, confirmed, role_display_mode
        ):
            st.markdown(f"##### {week_title}")
            st.markdown(
                calendar_table_html(
                    week_frame,
                    config=config,
                    allow_cell_html=True,
                    highlighted_slots=highlighted_slots,
                ),
                unsafe_allow_html=True,
            )
        return
    st.markdown(
        calendar_table_html(
            candidate_full_calendar(config, confirmed, role_display_mode),
            config=config,
            allow_cell_html=True,
            highlighted_slots=highlighted_slots,
        ),
        unsafe_allow_html=True,
    )
