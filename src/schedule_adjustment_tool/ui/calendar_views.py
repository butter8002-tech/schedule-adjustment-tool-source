from __future__ import annotations

import html
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Callable

import pandas as pd

from schedule_adjustment_tool.domain.japanese_holidays import is_japanese_holiday
from schedule_adjustment_tool.domain.models import (
    Config,
    Participant,
    ROLE_DISPLAY_COLORS,
    WEEKDAY_LABELS,
    eligible_dates,
    make_slot_key,
)
from schedule_adjustment_tool.ui.design_tokens import (
    CALENDAR_HOLIDAY_BACKGROUND,
    CALENDAR_SATURDAY_BACKGROUND,
    HOLIDAY_FOREGROUND,
    SATURDAY_FOREGROUND,
    UNIVERSITY_ROLE,
    HIGH_SCHOOL_ROLE,
    ZOOM_BACKGROUND,
    ZOOM_BORDER,
    ZOOM_FOREGROUND,
)
from schedule_adjustment_tool.ui.presentation import ROLE_LABELS

MEETING_CHIP_HTML = (
    "<span class='meeting-chip' "
    "style='display:inline-block;padding:0.08rem 0.42rem;"
    f"border-radius:999px;background:{ZOOM_BACKGROUND};"
    f"color:{ZOOM_FOREGROUND};border:1px solid {ZOOM_BORDER};"
    "font-size:0.78rem;"
    "font-weight:700;line-height:1.35;margin-bottom:0.18rem;'>"
    "zoom</span>"
)
UNIVERSITY_ROLE_STYLE = f"color:{UNIVERSITY_ROLE};"
HIGH_SCHOOL_ROLE_STYLE = f"color:{HIGH_SCHOOL_ROLE};"


def role_display_legend_html(role_display_mode: str) -> str:
    if role_display_mode != ROLE_DISPLAY_COLORS:
        return ""
    return (
        "<div style='display:flex;gap:0.8rem;align-items:center;"
        "font-size:0.82rem;margin:0.2rem 0 0.55rem;'>"
        f"<span style='color:{UNIVERSITY_ROLE};'>● {ROLE_LABELS['university']}</span>"
        f"<span style='color:{HIGH_SCHOOL_ROLE};'>● {ROLE_LABELS['high_school']}</span>"
        "</div>"
    )


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _week_groups(config: Config) -> list[list[date]]:
    target_dates = set(eligible_dates(config))
    if not target_dates:
        return []
    first_week = _week_start(min(target_dates))
    last_week = _week_start(max(target_dates))
    weeks: list[list[date]] = []
    current = first_week
    while current <= last_week:
        week = [
            current + timedelta(days=offset)
            for offset in range(7)
            if current + timedelta(days=offset) in target_dates
        ]
        if week:
            weeks.append(week)
        current += timedelta(days=7)
    return weeks


def _calendar_frame(
    config: Config,
    days: list[date],
    cell_value: Callable[[str], str],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for day in days:
        row: dict[str, Any] = {
            "日付": f"{day.strftime('%m/%d')}（{WEEKDAY_LABELS[day.weekday()]}）"
        }
        for period in config.enabled_periods:
            row[f"{period}限"] = cell_value(make_slot_key(day, period))
        rows.append(row)
    return pd.DataFrame(rows)


def availability_calendar_frames(
    config: Config, participants: list[Participant]
) -> list[tuple[str, pd.DataFrame]]:
    names_by_slot: dict[str, list[str]] = defaultdict(list)
    for participant in participants:
        if participant.active:
            for slot_key in participant.availability:
                names_by_slot[slot_key].append(participant.name)
            in_person_slots = set(participant.availability)
            for slot_key in participant.zoom_availability:
                if slot_key not in in_person_slots:
                    names_by_slot[slot_key].append(f"{participant.name}（Zoom）")

    return [
        (
            f"{week[0].strftime('%Y/%m/%d')}（{WEEKDAY_LABELS[week[0].weekday()]}）週",
            _calendar_frame(
                config,
                week,
                lambda slot_key: "\n".join(
                    sorted(names_by_slot.get(slot_key, []))
                ),
            ),
        )
        for week in _week_groups(config)
    ]


def availability_full_calendar(
    config: Config, participants: list[Participant]
) -> pd.DataFrame:
    names_by_slot: dict[str, list[str]] = defaultdict(list)
    for participant in participants:
        if participant.active:
            for slot_key in participant.availability:
                names_by_slot[slot_key].append(participant.name)
            in_person_slots = set(participant.availability)
            for slot_key in participant.zoom_availability:
                if slot_key not in in_person_slots:
                    names_by_slot[slot_key].append(f"{participant.name}（Zoom）")
    return _calendar_frame(
        config,
        eligible_dates(config),
        lambda slot_key: "\n".join(sorted(names_by_slot.get(slot_key, []))),
    )


def date_cell_style(value: object) -> str:
    text = str(value)
    try:
        day = date.fromisoformat(text[:10])
    except ValueError:
        try:
            day = datetime.strptime(text[:5], "%m/%d").replace(
                year=date.today().year
            ).date()
        except ValueError:
            return ""
    if day.weekday() == 6 or is_japanese_holiday(day):
        return (
            f"background-color: {CALENDAR_HOLIDAY_BACKGROUND}; "
            f"color: {HOLIDAY_FOREGROUND};"
        )
    if day.weekday() == 5:
        return (
            f"background-color: {CALENDAR_SATURDAY_BACKGROUND}; "
            f"color: {SATURDAY_FOREGROUND};"
        )
    return ""


def _calendar_display_date(value: object, config: Config) -> date | None:
    text = str(value)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    try:
        month, day_number = map(int, text[:5].split("/"))
        year = date.fromisoformat(config.start_date).year
        if month < date.fromisoformat(config.start_date).month:
            year += 1
        return date(year, month, day_number)
    except (ValueError, TypeError):
        return None


def calendar_table_html(
    frame: pd.DataFrame,
    *,
    config: Config,
    allow_cell_html: bool = False,
    highlighted_slots: set[str] | None = None,
) -> str:
    highlighted_slots = highlighted_slots or set()
    date_column_width_rem = 8.5
    period_column_width_rem = 9.0
    period_column_count = max(0, len(frame.columns) - 1)
    min_table_width_rem = (
        date_column_width_rem + period_column_width_rem * period_column_count
    )
    colgroup_columns = []
    for column in frame.columns:
        width_rem = (
            date_column_width_rem
            if column == "日付"
            else period_column_width_rem
        )
        colgroup_columns.append(f"<col style='width:{width_rem}rem;'>")
    colgroup = f"<colgroup>{''.join(colgroup_columns)}</colgroup>"
    headers = "".join(
        f"<th>{html.escape(str(column))}</th>" for column in frame.columns
    )
    body_rows = []
    for _, row in frame.iterrows():
        row_day = _calendar_display_date(row.get("日付", ""), config)
        cells = []
        for column in frame.columns:
            value = str(row[column]) if not pd.isna(row[column]) else ""
            classes = []
            styles = []
            if column == "日付" and row_day:
                if row_day.weekday() == 6 or is_japanese_holiday(row_day):
                    classes.append("holiday")
                elif row_day.weekday() == 5:
                    classes.append("saturday")
            elif row_day and column.endswith("限"):
                try:
                    period = int(column.removesuffix("限"))
                except ValueError:
                    period = 0
                if period and make_slot_key(row_day, period) in highlighted_slots:
                    styles.append(
                        "background-color: rgba(255, 224, 130, 0.55); "
                        "font-weight: 700;"
                    )
            if allow_cell_html and column != "日付":
                rendered = value.replace("\n", "<br>")
            else:
                rendered = html.escape(value).replace("\n", "<br>")
            class_attribute = (
                f" class='{' '.join(classes)}'" if classes else ""
            )
            style_attribute = (
                f" style='{html.escape(' '.join(styles), quote=True)}'"
                if styles
                else ""
            )
            cells.append(f"<td{class_attribute}{style_attribute}>{rendered}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    return (
        "<div class='schedule-calendar-wrapper'>"
        f"<table class='schedule-calendar' style='width:{min_table_width_rem}rem;'>"
        f"{colgroup}"
        f"<thead><tr>{headers}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
        "</div>"
    )


def style_calendar_dates(frame: pd.DataFrame) -> pd.io.formats.style.Styler:
    return frame.style.map(date_cell_style, subset=["日付"])


def _candidate_text_by_slot(
    candidate: dict[str, Any], role_display_mode: str
) -> dict[str, list[str]]:
    text_by_slot: dict[str, list[str]] = defaultdict(list)
    sessions_by_slot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for session in candidate.get("sessions", []):
        slot_key = make_slot_key(session["date"], int(session["period"]))
        sessions_by_slot[slot_key].append(session)
    for slot_key, sessions in sessions_by_slot.items():
        show_group = len(sessions) > 1
        for session in sessions:
            group_name = f"組{session['group_index']}"
            group_label = (
                f"{html.escape(group_name)}\n"
                if show_group
                else ""
            )
            meeting_label = (
                f"{MEETING_CHIP_HTML}\n"
                if session.get("meeting_mode") == "zoom"
                else ""
            )
            university = html.escape("、".join(session["university_role_members"]))
            high_school = html.escape("、".join(session["high_school_role_members"]))
            if role_display_mode == ROLE_DISPLAY_COLORS:
                text_by_slot[slot_key].append(
                    f"{group_label}"
                    f"{meeting_label}"
                    f"<span class='university-role' "
                    f"style='{UNIVERSITY_ROLE_STYLE}'>{university}</span>\n"
                    f"<span class='high-school-role' "
                    f"style='{HIGH_SCHOOL_ROLE_STYLE}'>{high_school}</span>"
                )
            else:
                text_by_slot[slot_key].append(
                    f"{group_label}{meeting_label}"
                    f"{ROLE_LABELS['university']}: {university}\n"
                    f"{ROLE_LABELS['high_school']}: {high_school}"
                )
    return text_by_slot


def candidate_calendar_frames(
    config: Config,
    candidate: dict[str, Any],
    role_display_mode: str | None = None,
) -> list[tuple[str, pd.DataFrame]]:
    text_by_slot = _candidate_text_by_slot(
        candidate, role_display_mode or config.role_display_mode
    )
    return [
        (
            f"{week[0].strftime('%Y/%m/%d')}（{WEEKDAY_LABELS[week[0].weekday()]}）週",
            _calendar_frame(
                config,
                week,
                lambda slot_key: "\n\n".join(text_by_slot.get(slot_key, [])),
            ),
        )
        for week in _week_groups(config)
    ]


def candidate_full_calendar(
    config: Config,
    candidate: dict[str, Any],
    role_display_mode: str | None = None,
) -> pd.DataFrame:
    text_by_slot = _candidate_text_by_slot(
        candidate, role_display_mode or config.role_display_mode
    )
    return _calendar_frame(
        config,
        eligible_dates(config),
        lambda slot_key: "\n\n".join(text_by_slot.get(slot_key, [])),
    )
