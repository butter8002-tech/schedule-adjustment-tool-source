from __future__ import annotations

from datetime import date
from typing import Any

from schedule_adjustment_tool.ui.calendar_views import (
    availability_calendar_frames,
    availability_full_calendar,
    candidate_calendar_frames,
    candidate_full_calendar,
)
from schedule_adjustment_tool.domain.japanese_holidays import is_japanese_holiday
from schedule_adjustment_tool.domain.models import (
    Config,
    Participant,
    ROLE_DISPLAY_COLORS,
    ROLE_DISPLAY_LABELS,
    WEEKDAY_LABELS,
    eligible_dates,
    make_slot_key,
)
from schedule_adjustment_tool.exports.xlsx_writer import (
    excel_cell_text as _excel_cell_text,
    workbook_bytes as _workbook_bytes,
)


def _session_rows(candidate: dict[str, Any]) -> list[list[Any]]:
    sessions = candidate.get("sessions", [])
    counts_by_slot: dict[tuple[str, int], int] = {}
    for session in sessions:
        key = (session["date"], int(session["period"]))
        counts_by_slot[key] = counts_by_slot.get(key, 0) + 1
    rows = [["回", "日付", "時限", "開催形式", "組", "大学生役", "高校生役"]]
    for index, session in enumerate(sessions, start=1):
        day = session["date"]
        weekday = WEEKDAY_LABELS[date.fromisoformat(day).weekday()]
        key = (day, int(session["period"]))
        rows.append(
            [
                index,
                f"{day}（{weekday}）",
                f"{session['period']}限",
                "Zoom" if session.get("meeting_mode") == "zoom" else "対面",
                f"組{session['group_index']}" if counts_by_slot[key] > 1 else "",
                "、".join(session["university_role_members"]),
                "、".join(session["high_school_role_members"]),
            ]
        )
    return rows


def _schedule_sheet(
    candidate: dict[str, Any], role_display_mode: str, name: str
) -> tuple[str, list[list[Any]], list[float], dict[tuple[int, int], int], list[str]]:
    rows = _session_rows(candidate)
    styles: dict[tuple[int, int], int] = {}
    if role_display_mode == ROLE_DISPLAY_COLORS:
        for row in range(2, len(rows) + 1):
            styles[(row, 6)] = 2
            styles[(row, 7)] = 3
    return name, rows, [7, 20, 9, 12, 9, 34, 34], styles, []


def _calendar_sheet(
    name: str,
    frame: Any,
    days: list[date],
) -> tuple[str, list[list[Any]], list[float], dict[tuple[int, int], int], list[str]]:
    rows = [list(frame.columns)]
    rows.extend(
        [
            [_excel_cell_text(value) for value in row]
            for row in frame.fillna("").values.tolist()
        ]
    )
    styles: dict[tuple[int, int], int] = {}
    for row_index, day in enumerate(days, start=2):
        for column_index in range(1, len(rows[0]) + 1):
            styles[(row_index, column_index)] = 6
        if day.weekday() == 6 or is_japanese_holiday(day):
            styles[(row_index, 1)] = 8
        elif day.weekday() == 5:
            styles[(row_index, 1)] = 7
    return name[:31], rows, [16] + [30] * (len(rows[0]) - 1), styles, []


def _calendar_sheets(
    config: Config,
    full_frame: Any,
    weekly_frames: list[tuple[str, Any]],
) -> list[
    tuple[str, list[list[Any]], list[float], dict[tuple[int, int], int], list[str]]
]:
    target_days = eligible_dates(config)
    sheets = [_calendar_sheet("期間全体カレンダー", full_frame, target_days)]
    day_offset = 0
    for week_index, (_title, frame) in enumerate(weekly_frames, start=1):
        week_days = target_days[day_offset : day_offset + len(frame)]
        day_offset += len(frame)
        sheets.append(_calendar_sheet(f"週{week_index}カレンダー", frame, week_days))
    return sheets


def _participant_summary_sheet(
    config: Config,
    candidate: dict[str, Any],
    participants: list[Participant],
    role_display_mode: str,
) -> tuple[str, list[list[Any]], list[float], dict[tuple[int, int], int], list[str]]:
    participant_by_id = {
        str(participant.id): participant for participant in participants
    }
    participant_id_by_name = {
        participant.name: str(participant.id) for participant in participants
    }
    records: dict[str, dict[str, Any]] = {}

    def ensure_record(
        participant_id: str,
        name: str,
    ) -> dict[str, Any]:
        identity = participant_id or f"name:{name}"
        return records.setdefault(
            identity,
            {
                "participant_id": participant_id,
                "name": name,
                "university_count": 0,
                "high_school_count": 0,
                "summary_university_count": 0,
                "summary_high_school_count": 0,
                "assignments": [],
            },
        )

    for participant in participants:
        if participant.active:
            ensure_record(str(participant.id), participant.name)
    for summary in candidate.get("participant_summary", []):
        participant_id = str(summary.get("participant_id", ""))
        name = str(summary.get("name", "")).strip()
        if participant_id or name:
            record = ensure_record(participant_id, name)
            record["summary_university_count"] = int(
                summary.get("university_count", 0)
            )
            record["summary_high_school_count"] = int(
                summary.get("high_school_count", 0)
            )

    for session in candidate.get("sessions", []):
        day_text = str(session.get("date", ""))
        try:
            weekday = WEEKDAY_LABELS[date.fromisoformat(day_text).weekday()]
            day_label = f"{day_text}（{weekday}）"
        except ValueError:
            day_label = day_text
        slot_label = (
            f"{day_label} {int(session.get('period', 0))}限 "
            f"組{int(session.get('group_index', 1))}"
        )
        for role_label, ids_field, names_field, count_field in (
            (
                "大学生役",
                "university_role_member_ids",
                "university_role_members",
                "university_count",
            ),
            (
                "高校生役",
                "high_school_role_member_ids",
                "high_school_role_members",
                "high_school_count",
            ),
        ):
            member_ids = [
                str(value) for value in session.get(ids_field, [])
            ]
            member_names = [
                str(value) for value in session.get(names_field, [])
            ]
            member_count = max(len(member_ids), len(member_names))
            for member_index in range(member_count):
                participant_id = (
                    member_ids[member_index]
                    if member_index < len(member_ids)
                    else ""
                )
                name = (
                    member_names[member_index]
                    if member_index < len(member_names)
                    else (
                        participant_by_id[participant_id].name
                        if participant_id in participant_by_id
                        else ""
                    )
                )
                participant_id = (
                    participant_id
                    or participant_id_by_name.get(name, "")
                )
                record = ensure_record(participant_id, name)
                record[count_field] += 1
                record["assignments"].append(f"{slot_label} {role_label}")

    rows = [
        [
            "名前",
            "班",
            "大学生役",
            "高校生役",
            "合計",
            "規定数",
            "超過許容",
            "規定数超過",
            "参加上限",
            "上限超過",
            "担当日時・役割",
        ]
    ]
    ordered_records = sorted(
        records.values(),
        key=lambda record: (
            (
                participant_by_id[
                    str(record.get("participant_id", ""))
                ].is_role_unspecified
                if str(record.get("participant_id", ""))
                in participant_by_id
                else False
            ),
            str(record.get("name", "")),
        ),
    )
    for record in ordered_records:
        participant = participant_by_id.get(
            str(record.get("participant_id", ""))
        )
        has_session_assignments = bool(record.get("assignments"))
        university_count = int(
            record.get(
                (
                    "university_count"
                    if has_session_assignments
                    else "summary_university_count"
                ),
                0,
            )
        )
        high_school_count = int(
            record.get(
                (
                    "high_school_count"
                    if has_session_assignments
                    else "summary_high_school_count"
                ),
                0,
            )
        )
        total_count = university_count + high_school_count
        if participant is None:
            required_total: int | str = ""
            extra_limit: int | str = ""
            participation_limit: int | str = ""
            extra_count: int | str = ""
            over_limit_count: int | str = ""
            group_number: int | str = ""
        else:
            required_total = (
                participant.total_requirement(config)
                or participant.university_requirement(config)
                + participant.high_school_requirement(config)
            )
            extra_limit = (
                ""
                if participant.is_role_unspecified
                else participant.extra_limit(config)
            )
            participation_limit = participant.participation_limit(config)
            extra_count = (
                0
                if participant.is_role_unspecified
                else max(0, total_count - int(required_total))
            )
            over_limit_count = max(
                0,
                total_count - int(participation_limit),
            )
            group_number = participant.group_number
        rows.append(
            [
                record.get("name", ""),
                group_number,
                university_count,
                high_school_count,
                total_count,
                required_total,
                extra_limit,
                extra_count,
                participation_limit,
                over_limit_count,
                "\n".join(record.get("assignments", [])),
            ]
        )
    styles: dict[tuple[int, int], int] = {}
    for row_index in range(2, len(rows) + 1):
        styles[(row_index, 11)] = 6
        if role_display_mode == ROLE_DISPLAY_COLORS:
            styles[(row_index, 3)] = 2
            styles[(row_index, 4)] = 3
    return (
        "個人別サマリー",
        rows,
        [22, 10, 12, 12, 10, 10, 10, 12, 10, 10, 52],
        styles,
        [],
    )


def candidate_workbook(
    candidate: dict[str, Any],
    role_display_mode: str,
    config: Config | None = None,
    participants: list[Participant] | None = None,
) -> bytes:
    sheets = [_schedule_sheet(candidate, role_display_mode, "候補日程")]
    if config is not None:
        sheets.append(
            _participant_summary_sheet(
                config,
                candidate,
                participants or [],
                role_display_mode,
            )
        )
        sheets.append(
            _calendar_sheet(
                "期間全体カレンダー",
                candidate_full_calendar(
                    config,
                    candidate,
                    ROLE_DISPLAY_LABELS,
                ),
                eligible_dates(config),
            )
        )
    return _workbook_bytes(sheets)


def candidate_calendar_workbook(config: Config, candidate: dict[str, Any]) -> bytes:
    return _workbook_bytes(
        _calendar_sheets(
            config,
            candidate_full_calendar(config, candidate, ROLE_DISPLAY_LABELS),
            candidate_calendar_frames(config, candidate, ROLE_DISPLAY_LABELS),
        )
    )


def availability_calendar_workbook(
    config: Config, participants: list[Participant]
) -> bytes:
    return _workbook_bytes(
        _calendar_sheets(
            config,
            availability_full_calendar(config, participants),
            availability_calendar_frames(config, participants),
        )
    )


def confirmed_schedule_workbook(
    config: Config,
    candidate: dict[str, Any],
    participants: list[Participant],
    role_display_mode: str | None = None,
) -> bytes:
    info_rows = [
        ["確定練習会日程", "", "", ""],
        ["企画名", config.title],
        ["説明・連絡事項", config.description],
        ["調整期間", f"{config.start_date} ～ {config.end_date}"],
        ["本番日", config.performance_date or "未設定"],
        ["確定日時", candidate.get("confirmed_at", "")],
        ["参加者数", len([item for item in participants if item.active])],
    ]
    info_styles = {(1, 1): 5}
    info_styles.update({(row, 1): 4 for row in range(2, len(info_rows) + 1)})
    participant_rows = [["名前", "班", "期", "文理", "学部", "学科・コース等"]]
    participant_rows.extend(
        [
            participant.name,
            participant.group_number,
            participant.cohort or "",
            participant.humanities_or_science,
            participant.department,
            participant.department_detail,
        ]
        for participant in participants
        if participant.active
    )
    return _workbook_bytes(
        [
            ("企画情報", info_rows, [22, 70, 12, 12], info_styles, ["A1:D1"]),
            _schedule_sheet(
                candidate,
                role_display_mode or config.role_display_mode,
                "確定日程",
            ),
            (
                "参加者別",
                participant_rows,
                [22, 10, 8, 10, 22, 28],
                {},
                [],
            ),
            _participant_summary_sheet(
                config,
                candidate,
                participants,
                role_display_mode or config.role_display_mode,
            ),
        ]
        + _calendar_sheets(
            config,
            candidate_full_calendar(config, candidate, ROLE_DISPLAY_LABELS),
            candidate_calendar_frames(config, candidate, ROLE_DISPLAY_LABELS),
        )
    )


def input_status_workbook(
    config: Config,
    participants: list[Participant],
) -> bytes:
    rows = [
        [
            "名前",
            "日調対象",
            "入力状況",
            "日程作成に使用",
            "対面可コマ数",
            "Zoomなら可コマ数",
            "提出日時",
            "最終更新",
        ]
    ]
    rows.extend(
        [
            participant.name,
            participant.active,
            participant.input_status,
            (
                "代理入力"
                if participant.response_source == "manager"
                else "本人の入力"
            ),
            len(participant.availability),
            len(participant.zoom_availability),
            participant.submitted_at,
            participant.updated_at,
        ]
        for participant in participants
    )
    response_rows = [["名前", "日付", "時限", "対面", "Zoom"]]
    for participant in participants:
        in_person_slots = set(participant.availability)
        zoom_slots = set(participant.zoom_availability)
        for day in eligible_dates(config):
            for period in config.enabled_periods:
                slot_key = make_slot_key(day, period)
                if slot_key not in in_person_slots and slot_key not in zoom_slots:
                    continue
                response_rows.append(
                    [
                        participant.name,
                        day.isoformat(),
                        f"{period}限",
                        "○" if slot_key in in_person_slots else "",
                        "○" if slot_key in zoom_slots else "",
                    ]
                )
    return _workbook_bytes(
        [
            (
                "入力状況",
                rows,
                [24, 12, 14, 16, 16, 18, 24, 24],
                {},
                [],
            ),
            (
                "回答内容",
                response_rows,
                [22, 16, 10, 10, 10],
                {},
                [],
            ),
        ]
        + _calendar_sheets(
            config,
            availability_full_calendar(config, participants),
            availability_calendar_frames(config, participants),
        )
    )


def candidates_workbook(
    config: Config,
    candidates: list[dict[str, Any]],
    participants: list[Participant] | None = None,
) -> bytes:
    summary_rows = [
        [
            "候補",
            "総合適合度（/100・100が最良）",
            "必須条件",
            "開催組数",
            "規定数超過（延べ回数）",
        ]
    ]
    sheets: list[
        tuple[
            str,
            list[list[Any]],
            list[float],
            dict[tuple[int, int], int],
            list[str],
        ]
    ] = []
    for index, candidate in enumerate(candidates, start=1):
        metrics = candidate.get("metrics", {})
        summary_rows.append(
            [
                index,
                float(metrics.get("evaluation_score", 0)),
                (
                    "満足"
                    if metrics.get("is_strict_candidate", False)
                    else "要確認"
                ),
                int(
                    metrics.get(
                        "number_of_sessions",
                        len(candidate.get("sessions", [])),
                    )
                ),
                int(metrics.get("total_extra_count", 0)),
            ]
        )
        sheets.append(
            _schedule_sheet(
                candidate,
                config.role_display_mode,
                f"候補{index}",
            )
        )
        personal_summary = _participant_summary_sheet(
            config,
            candidate,
            participants or [],
            config.role_display_mode,
        )
        sheets.append(
            (
                f"候補{index}個人別"[:31],
                personal_summary[1],
                personal_summary[2],
                personal_summary[3],
                personal_summary[4],
            )
        )
        calendar_sheet = _calendar_sheet(
            f"候補{index}期間全体カレンダー",
            candidate_full_calendar(
                config,
                candidate,
                ROLE_DISPLAY_LABELS,
            ),
            eligible_dates(config),
        )
        sheets.append(calendar_sheet)
    return _workbook_bytes(
        [
            (
                "候補一覧",
                summary_rows,
                [10, 18, 14, 12, 24],
                {},
                [],
            ),
            *sheets,
        ]
    )


def project_data_workbook(
    config: Config, participants: list[Participant]
) -> bytes:
    settings_rows = [["項目", "値"]]
    settings_rows.extend(
        [key, str(value)] for key, value in config.to_dict().items()
    )
    roster_rows = [
        [
            "名前",
            "対象",
            "承認",
            "班",
            "期",
            "文理",
            "学部",
            "学科・コース等",
            "入力状況",
            "対面可日時",
            "Zoomなら可日時",
        ]
    ]
    roster_rows.extend(
        [
            participant.name,
            participant.active,
            participant.approved,
            participant.group_number,
            participant.cohort or "",
            participant.humanities_or_science,
            participant.department,
            participant.department_detail,
            participant.input_status,
            "、".join(participant.availability),
            "、".join(participant.zoom_availability),
        ]
        for participant in participants
    )
    return _workbook_bytes(
        [
            ("企画情報", settings_rows, [32, 90], {}, []),
            (
                "参加者",
                roster_rows,
                [22, 9, 9, 9, 8, 10, 22, 28, 12, 70, 70],
                {},
                [],
            ),
        ]
    )
