"""Candidate calendar conversion and validation without UI or storage effects."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any

import pandas as pd

from schedule_adjustment_tool.domain.evaluation_config import EVALUATION_SCORE_VERSION
from schedule_adjustment_tool.domain.models import (
    Config,
    eligible_dates,
    Participant,
    now_iso,
    participant_name_identity_key,
    practice_dates,
)
from schedule_adjustment_tool.domain.schedule_model import (
    ScheduleModelError,
    schedule_policy_issues,
)


def _cell_text(value: object) -> str:
    return "" if pd.isna(value) else str(value).strip()


def calendar_dates_for_editing(
    config: Config,
    sessions: list[dict],
) -> list[date]:
    """Keep existing rows visible while excluding dates for new sessions."""

    dates = set(eligible_dates(config))
    for session in sessions:
        try:
            dates.add(date.fromisoformat(str(session.get("date", ""))))
        except (TypeError, ValueError):
            continue
    return sorted(dates)


def _refresh_candidate_evaluation(
    candidate: dict,
    config: Config,
    participants: list[Participant],
) -> None:
    # Keep the solver import lazy so opening an editor does not initialize it.
    from schedule_adjustment_tool.domain.scheduler import refresh_candidate_evaluation

    refresh_candidate_evaluation(
        candidate,
        config,
        participants,
        evaluation_config=config,
    )


def _schedule_member_names(value: object) -> list[str]:
    text = _cell_text(value).replace(",", "、").replace("，", "、")
    return [name.strip() for name in text.split("、") if name.strip()]


def schedule_from_editor(
    base_schedule: dict | None,
    edited_rows: pd.DataFrame,
    participants: list[Participant],
) -> tuple[dict | None, list[str]]:
    participant_id_by_name = {
        participant_name_identity_key(participant.name): participant.id
        for participant in participants
    }
    participant_name_by_id = {
        participant.id: participant.name for participant in participants
    }
    base_sessions = list((base_schedule or {}).get("sessions", []))
    sessions: list[dict] = []
    errors: list[str] = []
    for display_index, row in edited_rows.iterrows():
        if not any(
            _cell_text(row.get(field, ""))
            for field in ("日付", "参加者", "大学生役", "高校生役", "session_id")
        ):
            continue
        try:
            base_index_text = _cell_text(row.get("_base_index", ""))
            base_index = int(float(base_index_text)) if base_index_text else -1
            template = (
                deepcopy(base_sessions[base_index])
                if 0 <= base_index < len(base_sessions)
                else {}
            )
            period = int(float(row.get("時限", 0)))
            group_index = int(float(row.get("組", 1)))
        except (TypeError, ValueError, IndexError):
            errors.append(f"編集表の{display_index + 1}行目の時限・組が不正です。")
            continue
        university_names = _schedule_member_names(row.get("大学生役", ""))
        high_school_names = _schedule_member_names(row.get("高校生役", ""))
        unassigned_names = _schedule_member_names(row.get("参加者", ""))
        unknown_names = [
            name
            for name in unassigned_names + university_names + high_school_names
            if participant_name_identity_key(name) not in participant_id_by_name
        ]
        if unknown_names:
            errors.append(
                f"編集表の{display_index + 1}行目に未登録の参加者がいます: "
                + "、".join(unknown_names)
            )
            continue
        university_ids = [
            participant_id_by_name[participant_name_identity_key(name)]
            for name in university_names
        ]
        high_school_ids = [
            participant_id_by_name[participant_name_identity_key(name)]
            for name in high_school_names
        ]
        unassigned_ids = [
            participant_id_by_name[participant_name_identity_key(name)]
            for name in unassigned_names
        ]
        participant_member_ids = list(
            dict.fromkeys(unassigned_ids + university_ids + high_school_ids)
        )
        if len(set(participant_member_ids)) != len(
            unassigned_ids + university_ids + high_school_ids
        ):
            errors.append(
                f"編集表の{display_index + 1}行目で参加者が重複しています。"
            )
            continue
        template.update(
            {
                "date": _cell_text(row.get("日付", "")),
                "period": period,
                "group_index": group_index,
                "meeting_mode": (
                    "zoom" if _cell_text(row.get("開催形式")) == "Zoom" else "in_person"
                ),
                "university_role_member_ids": university_ids,
                "high_school_role_member_ids": high_school_ids,
                "participant_member_ids": participant_member_ids,
                "university_role_members": [
                    participant_name_by_id[participant_id]
                    for participant_id in university_ids
                ],
                "high_school_role_members": [
                    participant_name_by_id[participant_id]
                    for participant_id in high_school_ids
                ],
            }
        )
        for display_role, fallback_ids in (
            ("university", university_ids),
            ("high_school", high_school_ids),
        ):
            display_field = f"_display_{display_role}_role_member_ids"
            if display_field in row.index:
                display_ids = [
                    str(participant_id)
                    for participant_id in (row.get(display_field) or [])
                    if str(participant_id) in participant_name_by_id
                ]
                template[f"display_{display_role}_role_member_ids"] = list(
                    dict.fromkeys(display_ids)
                )
                template[f"display_{display_role}_role_members"] = [
                    participant_name_by_id[participant_id]
                    for participant_id in template[
                        f"display_{display_role}_role_member_ids"
                    ]
                ]
            elif not template.get(f"display_{display_role}_role_member_ids"):
                template[f"display_{display_role}_role_member_ids"] = list(
                    fallback_ids
                )
                template[f"display_{display_role}_role_members"] = [
                    participant_name_by_id[participant_id]
                    for participant_id in fallback_ids
                ]
        for editor_only_field in (
            "lock_session",
            "lock_session_wide",
            "lock_meeting_mode",
            "locked_university_role_member_ids",
            "locked_high_school_role_member_ids",
            "role_locked_university_role_member_ids",
            "role_locked_high_school_role_member_ids",
        ):
            template.pop(editor_only_field, None)
        session_id = _cell_text(row.get("session_id", ""))
        if session_id:
            template["session_id"] = session_id
        else:
            template.pop("session_id", None)
        sessions.append(template)
    if errors:
        return None, errors
    if not sessions:
        return None, ["日程を1行以上入力してください。"]
    participant_counts: dict[str, dict[str, int | str]] = {}
    for session in sessions:
        for role, ids_field in (
            ("university_count", "university_role_member_ids"),
            ("high_school_count", "high_school_role_member_ids"),
        ):
            for participant_id in session[ids_field]:
                summary = participant_counts.setdefault(
                    participant_id,
                    {
                        "participant_id": participant_id,
                        "name": participant_name_by_id[participant_id],
                        "university_count": 0,
                        "high_school_count": 0,
                        "total_count": 0,
                        "extra_count": 0,
                    },
                )
                summary[role] = int(summary[role]) + 1
                summary["total_count"] = int(summary["total_count"]) + 1
        for participant_id in session.get("participant_member_ids", []):
            summary = participant_counts.setdefault(
                participant_id,
                {
                    "participant_id": participant_id,
                    "name": participant_name_by_id[participant_id],
                    "university_count": 0,
                    "high_school_count": 0,
                    "total_count": 0,
                    "extra_count": 0,
                },
            )
            if participant_id not in session["university_role_member_ids"] and participant_id not in session[
                "high_school_role_member_ids"
            ]:
                summary["total_count"] = int(summary["total_count"]) + 1
    schedule = deepcopy(base_schedule or {})
    schedule.pop("schedule_revision", None)
    schedule["sessions"] = sessions
    schedule["participant_summary"] = list(participant_counts.values())
    schedule["metrics"] = {
        "is_strict_candidate": True,
        "is_manually_maintained": True,
        "unmet_participant_count": 0,
        "required_shortfall_total": 0,
        "over_limit_total": 0,
        "mandatory_violation_score": 0,
        "session_role_shortfall_total": 0,
        "total_extra_count": 0,
        "number_of_sessions": len(sessions),
        "number_of_time_slots": len(
            {(session["date"], session["period"]) for session in sessions}
        ),
        "spread_score": 0,
        "participant_schedule_score": 0,
        "zoom_session_count": sum(
            session["meeting_mode"] == "zoom" for session in sessions
        ),
        "priority_violation_count": 0,
        "priority_unset_issue_count": 0,
        "priority_configured_violation_count": 0,
        "university_group_match_count": 0,
        "university_group_evaluated_count": 0,
        "field_match_count": 0,
        "field_evaluated_count": 0,
        "evaluation_penalties": {},
        "evaluation_score_version": EVALUATION_SCORE_VERSION,
        "evaluation_score": 100.0,
        "evaluation_priority_penalties": {},
        "evaluation_priority_item_counts": {},
        "evaluation_enabled_item_count": 0,
    }
    return schedule, []


def _safe_schedule_member_ids(
    session: dict,
    *,
    ids_field: str,
    names_field: str,
    participants: list[Participant],
) -> list[str]:
    participant_by_id = {
        participant.id: participant for participant in participants
    }
    participant_id_by_name = {
        participant_name_identity_key(participant.name): participant.id
        for participant in participants
    }
    selected_ids: list[str] = []
    for participant_id in map(str, session.get(ids_field, [])):
        if participant_id in participant_by_id and participant_id not in selected_ids:
            selected_ids.append(participant_id)
    for participant_name in map(str, session.get(names_field, [])):
        participant_id = participant_id_by_name.get(
            participant_name_identity_key(participant_name)
        )
        if participant_id and participant_id not in selected_ids:
            selected_ids.append(participant_id)
    return selected_ids


def schedule_calendar_initial_sessions(
    schedule: dict | None,
    participants: list[Participant],
    *,
    lock_sessions: bool = False,
    lock_meeting_modes: bool = False,
    lock_members: bool = False,
    reset_roles: bool = False,
) -> list[dict]:
    participant_name_by_id = {
        participant.id: participant.name for participant in participants
    }
    initial_sessions: list[dict] = []
    for session in (schedule or {}).get("sessions", []):
        copied = deepcopy(session)
        university_ids = _safe_schedule_member_ids(
            copied,
            ids_field="university_role_member_ids",
            names_field="university_role_members",
            participants=participants,
        )
        high_school_ids = _safe_schedule_member_ids(
            copied,
            ids_field="high_school_role_member_ids",
            names_field="high_school_role_members",
            participants=participants,
        )
        display_university_ids = _safe_schedule_member_ids(
            copied,
            ids_field="display_university_role_member_ids",
            names_field="display_university_role_members",
            participants=participants,
        ) or list(university_ids)
        display_high_school_ids = _safe_schedule_member_ids(
            copied,
            ids_field="display_high_school_role_member_ids",
            names_field="display_high_school_role_members",
            participants=participants,
        ) or list(high_school_ids)
        selected_ids = [
            participant_id
            for participant_id in map(
                str, copied.get("participant_member_ids", [])
            )
            if participant_id in participant_name_by_id
        ]
        selected_ids = list(
            dict.fromkeys(
                selected_ids
                + university_ids
                + high_school_ids
                + display_university_ids
                + display_high_school_ids
            )
        )
        copied["participant_member_ids"] = selected_ids
        copied["display_university_role_member_ids"] = display_university_ids
        copied["display_high_school_role_member_ids"] = display_high_school_ids
        copied["display_university_role_members"] = [
            participant_name_by_id[participant_id]
            for participant_id in display_university_ids
        ]
        copied["display_high_school_role_members"] = [
            participant_name_by_id[participant_id]
            for participant_id in display_high_school_ids
        ]
        editable_university_ids = [] if reset_roles else university_ids
        editable_high_school_ids = [] if reset_roles else high_school_ids
        copied["university_role_member_ids"] = editable_university_ids
        copied["high_school_role_member_ids"] = editable_high_school_ids
        copied["university_role_members"] = [
            participant_name_by_id[participant_id]
            for participant_id in editable_university_ids
        ]
        copied["high_school_role_members"] = [
            participant_name_by_id[participant_id]
            for participant_id in editable_high_school_ids
        ]
        copied["lock_session"] = bool(
            copied.get("lock_session", lock_sessions)
        )
        copied["lock_session_wide"] = bool(
            copied.get("lock_session_wide", lock_sessions)
        )
        copied["lock_meeting_mode"] = bool(
            copied.get("lock_meeting_mode", lock_meeting_modes)
        )
        if reset_roles:
            copied["locked_participant_member_ids"] = list(
                dict.fromkeys(
                    map(
                        str,
                        copied.get(
                            "locked_participant_member_ids",
                            selected_ids if lock_members else [],
                        ),
                    )
                )
            )
            copied["locked_participant_member_ids"] = [
                participant_id
                for participant_id in copied["locked_participant_member_ids"]
                if participant_id in selected_ids
            ]
            copied["locked_university_role_member_ids"] = []
            copied["locked_high_school_role_member_ids"] = []
        else:
            copied["locked_university_role_member_ids"] = [
                participant_id
                for participant_id in copied.get(
                    "locked_university_role_member_ids",
                    university_ids if lock_members else [],
                )
                if participant_id in university_ids
            ]
            copied["locked_high_school_role_member_ids"] = [
                participant_id
                for participant_id in copied.get(
                    "locked_high_school_role_member_ids",
                    high_school_ids if lock_members else [],
                )
                if participant_id in high_school_ids
            ]
        copied["role_locked_university_role_member_ids"] = [
            participant_id
            for participant_id in copied.get(
                "role_locked_university_role_member_ids",
                [],
            )
            if participant_id in editable_university_ids
        ]
        copied["role_locked_high_school_role_member_ids"] = [
            participant_id
            for participant_id in copied.get(
                "role_locked_high_school_role_member_ids",
                [],
            )
            if participant_id in editable_high_school_ids
        ]
        if reset_roles:
            copied["role_locked_university_role_member_ids"] = []
            copied["role_locked_high_school_role_member_ids"] = []
        else:
            copied["locked_university_role_member_ids"] = list(
                dict.fromkeys(
                    [
                        *copied["locked_university_role_member_ids"],
                        *copied[
                            "role_locked_university_role_member_ids"
                        ],
                    ]
                )
            )
            copied["locked_high_school_role_member_ids"] = list(
                dict.fromkeys(
                    [
                        *copied["locked_high_school_role_member_ids"],
                        *copied[
                            "role_locked_high_school_role_member_ids"
                        ],
                    ]
                )
            )
        initial_sessions.append(copied)
    return initial_sessions


def calendar_required_total_count(
    participant: Participant,
    config: Config,
) -> int:
    if participant.is_role_unspecified or participant.uses_legacy_total_requirement(
        config
    ):
        return participant.total_requirement(config)
    return (
        participant.university_requirement(config)
        + participant.high_school_requirement(config)
    )


def calendar_participation_limit(
    participant: Participant,
    config: Config,
) -> int:
    """Return the warning baseline used when manually adding participants."""

    return participant.participation_limit(config)


def calendar_required_role_counts(
    participant: Participant,
    config: Config,
) -> dict[str, int] | None:
    if participant.is_role_unspecified or participant.uses_legacy_total_requirement(
        config
    ):
        return None
    return {
        "university": participant.university_requirement(config),
        "high_school": participant.high_school_requirement(config),
    }


def assignment_locks_from_calendar_sessions(
    edited_sessions: list[dict],
    participants: list[Participant],
) -> tuple[list[dict], list[str]]:
    participant_ids = {participant.id for participant in participants}
    locks: list[dict] = []
    errors: list[str] = []
    seen_keys: set[tuple[str, int, int]] = set()
    locked_participant_ids_by_slot: dict[tuple[str, int], set[str]] = {}
    for index, session in enumerate(edited_sessions, start=1):
        university_ids = [
            str(participant_id)
            for participant_id in session.get(
                "university_role_member_ids",
                [],
            )
        ]
        high_school_ids = [
            str(participant_id)
            for participant_id in session.get(
                "high_school_role_member_ids",
                [],
            )
        ]
        participant_ids_in_session = [
            str(participant_id)
            for participant_id in session.get("participant_member_ids", [])
        ]
        participant_selection_mode = "participant_member_ids" in session
        participant_ids_in_session = list(
            dict.fromkeys(
                participant_ids_in_session + university_ids + high_school_ids
            )
        )
        unassigned_ids = [
            participant_id
            for participant_id in participant_ids_in_session
            if participant_id not in set(university_ids + high_school_ids)
        ]
        locked_university_ids = [
            str(participant_id)
            for participant_id in session.get(
                "locked_university_role_member_ids",
                [],
            )
        ]
        locked_high_school_ids = [
            str(participant_id)
            for participant_id in session.get(
                "locked_high_school_role_member_ids",
                [],
            )
        ]
        role_locked_university_ids = [
            str(participant_id)
            for participant_id in session.get(
                "role_locked_university_role_member_ids",
                [],
            )
        ]
        role_locked_high_school_ids = [
            str(participant_id)
            for participant_id in session.get(
                "role_locked_high_school_role_member_ids",
                [],
            )
        ]
        if participant_selection_mode:
            role_locked_university_ids = list(
                dict.fromkeys(university_ids)
            )
            role_locked_high_school_ids = list(
                dict.fromkeys(high_school_ids)
            )
        generic_locked_ids = [
            str(participant_id)
            for participant_id in session.get(
                "locked_participant_member_ids", []
            )
        ]
        lock_session = bool(session.get("lock_session"))
        lock_meeting_mode = bool(session.get("lock_meeting_mode"))
        raw_session_wide_lock = session.get("lock_session_wide")
        if isinstance(raw_session_wide_lock, bool):
            lock_session_wide = raw_session_wide_lock
        else:
            all_member_ids = set(participant_ids_in_session)
            all_locked_ids = set(locked_university_ids) | set(
                locked_high_school_ids
            ) | set(generic_locked_ids)
            lock_session_wide = bool(
                lock_session and all_member_ids.issubset(all_locked_ids)
            )
        if not (
            lock_session_wide
            or lock_meeting_mode
            or locked_university_ids
            or locked_high_school_ids
            or role_locked_university_ids
            or role_locked_high_school_ids
            or participant_selection_mode
        ):
            continue
        try:
            day_text = date.fromisoformat(str(session.get("date", ""))).isoformat()
            period = int(session.get("period", 0))
            group_index = int(session.get("group_index", 0))
            if period <= 0 or group_index <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(
                f"部分固定の{index}回目の日付・時限・組番号が不正です。"
            )
            continue
        key = (day_text, period, group_index)
        if key in seen_keys:
            errors.append(
                f"部分固定の{index}回目は同じ日時・組と重複しています。"
            )
            continue
        seen_keys.add(key)
        if not (
            set(locked_university_ids) | set(locked_high_school_ids)
        ).issubset(set(university_ids) | set(high_school_ids)):
            errors.append(
                f"部分固定の{index}回目で、配置から外した参加者が"
                "固定状態に残っています。"
            )
            continue
        if not set(role_locked_university_ids).issubset(
            set(university_ids)
        ) or not set(role_locked_high_school_ids).issubset(
            set(high_school_ids)
        ):
            errors.append(
                f"部分固定の{index}回目で、現在の役割から外した"
                "参加者が役割指定に残っています。"
            )
            continue
        locked_ids = (
            set(participant_ids_in_session)
            if lock_session_wide
            else (
                set(locked_university_ids)
                | set(locked_high_school_ids)
                | set(generic_locked_ids)
                | set(role_locked_university_ids)
                | set(role_locked_high_school_ids)
                | (
                    set(participant_ids_in_session)
                    if participant_selection_mode
                    else set()
                )
            )
        )
        unknown_ids = locked_ids - participant_ids
        if unknown_ids:
            errors.append(
                f"部分固定の{index}回目に未登録の参加者IDが"
                "含まれています。"
            )
            continue
        slot_key = (day_text, period)
        duplicate_ids = locked_ids & locked_participant_ids_by_slot.setdefault(
            slot_key,
            set(),
        )
        if duplicate_ids:
            errors.append(
                f"部分固定の{index}回目で、同じ参加者が同一コマの"
                "複数組に固定されています。"
            )
            continue
        locked_participant_ids_by_slot[slot_key].update(locked_ids)
        lock = {
            "date": day_text,
            "period": period,
            "group_index": group_index,
            "session_id": str(session.get("session_id", "")),
            "lock_session": lock_session_wide,
            "participant_ids": list(
                dict.fromkeys(
                    [
                        *(
                            participant_ids_in_session
                            if participant_selection_mode
                            else []
                        ),
                        *generic_locked_ids,
                        *locked_university_ids,
                        *locked_high_school_ids,
                        *role_locked_university_ids,
                        *role_locked_high_school_ids,
                        *(
                            university_ids + high_school_ids
                            if lock_session_wide
                            else []
                        ),
                    ]
                )
            ),
        }
        if role_locked_university_ids:
            lock["role_locked_university_participant_ids"] = list(
                dict.fromkeys(role_locked_university_ids)
            )
        if role_locked_high_school_ids:
            lock["role_locked_high_school_participant_ids"] = list(
                dict.fromkeys(role_locked_high_school_ids)
            )
        if lock_meeting_mode:
            lock["meeting_mode"] = (
                "zoom"
                if session.get("meeting_mode") == "zoom"
                else "in_person"
            )
        locks.append(lock)
    return locks, errors


def schedule_from_calendar_sessions(
    base_schedule: dict | None,
    edited_sessions: list[dict],
    participants: list[Participant],
) -> tuple[dict | None, list[str]]:
    participant_name_by_id = {
        participant.id: participant.name for participant in participants
    }
    base_sessions = list((base_schedule or {}).get("sessions", []))
    base_index_by_session_id = {
        str(session.get("session_id", "")): index
        for index, session in enumerate(base_sessions)
        if str(session.get("session_id", ""))
    }
    rows: list[dict[str, object]] = []
    errors: list[str] = []
    for index, session in enumerate(edited_sessions, start=1):
        university_ids = [
            str(participant_id)
            for participant_id in session.get(
                "university_role_member_ids", []
            )
        ]
        high_school_ids = [
            str(participant_id)
            for participant_id in session.get(
                "high_school_role_member_ids", []
            )
        ]
        raw_participant_ids = [
            str(participant_id)
            for participant_id in session.get("participant_member_ids", [])
        ]
        role_member_ids = university_ids + high_school_ids
        participant_ids_in_session = list(
            dict.fromkeys(raw_participant_ids + role_member_ids)
        )
        unassigned_ids = [
            participant_id
            for participant_id in raw_participant_ids
            if participant_id not in set(role_member_ids)
        ]
        unknown_ids = [
            participant_id
            for participant_id in (
                participant_ids_in_session + university_ids + high_school_ids
            )
            if participant_id not in participant_name_by_id
        ]
        if unknown_ids:
            errors.append(
                f"カレンダーの{index}回目に未登録の参加者IDが含まれています。"
            )
            continue
        if len(set(raw_participant_ids)) != len(raw_participant_ids):
            errors.append(
                f"カレンダーの{index}回目で参加者が重複しています。"
            )
            continue
        if len(set(university_ids)) != len(university_ids) or len(
            set(high_school_ids)
        ) != len(high_school_ids):
            errors.append(
                f"カレンダーの{index}回目で役割内の参加者が重複しています。"
            )
            continue
        if set(university_ids).intersection(high_school_ids):
            errors.append(
                f"カレンダーの{index}回目で参加者が重複しています。"
                "同じ参加者を両方の役割に配置できません。"
            )
            continue
        session_id = str(session.get("session_id", "")).strip()
        rows.append(
            {
                "_base_index": base_index_by_session_id.get(session_id, ""),
                "session_id": session_id,
                "日付": str(session.get("date", "")),
                "時限": session.get("period", 0),
                "組": session.get("group_index", 1),
                "開催形式": (
                    "Zoom"
                    if session.get("meeting_mode") == "zoom"
                    else "対面"
                ),
                "参加者": "、".join(
                    participant_name_by_id[participant_id]
                    for participant_id in unassigned_ids
                ),
                "大学生役": "、".join(
                    participant_name_by_id[participant_id]
                    for participant_id in university_ids
                ),
                "高校生役": "、".join(
                    participant_name_by_id[participant_id]
                    for participant_id in high_school_ids
                ),
                "_display_university_role_member_ids": list(
                    session.get(
                        "display_university_role_member_ids",
                        university_ids,
                    )
                ),
                "_display_high_school_role_member_ids": list(
                    session.get(
                        "display_high_school_role_member_ids",
                        high_school_ids,
                    )
                ),
            }
        )
    if errors:
        return None, errors
    return schedule_from_editor(
        base_schedule,
        pd.DataFrame(rows),
        participants,
    )


def candidate_from_calendar_sessions(
    base_candidate: dict | None,
    edited_sessions: list[dict],
    config: Config,
    participants: list[Participant],
    *,
    origin: dict[str, object],
    allow_solver_completion: bool = False,
) -> tuple[dict | None, list[str], list[str]]:
    candidate, errors = schedule_from_calendar_sessions(
        base_candidate,
        edited_sessions,
        participants,
    )
    if candidate is None or errors:
        return None, errors, []
    excluded_dates = set(config.excluded_dates)
    base_session_dates = {
        str(session.get("session_id", "")): str(session.get("date", ""))
        for session in (base_candidate or {}).get("sessions", [])
        if str(session.get("session_id", ""))
    }
    base_session_slots = {
        (
            str(session.get("date", "")),
            int(session.get("period", 0)),
            int(session.get("group_index", 0)),
        )
        for session in (base_candidate or {}).get("sessions", [])
        if str(session.get("date", ""))
    }
    excluded_date_errors = [
        "除外日に新しい組を追加できません。"
        f"（{session.get('date', '')} {session.get('period', '')}限）"
        for session in candidate.get("sessions", [])
        if str(session.get("date", "")) in excluded_dates
        and not (
            base_session_dates.get(str(session.get("session_id", "")))
            == str(session.get("date", ""))
            or (
                str(session.get("date", "")),
                int(session.get("period", 0)),
                int(session.get("group_index", 0)),
            )
            in base_session_slots
        )
    ]
    if excluded_date_errors:
        return None, list(dict.fromkeys(excluded_date_errors)), []
    try:
        policy_issues = schedule_policy_issues(
            candidate,
            config,
            participants,
            allow_solver_completion=allow_solver_completion,
        )
    except ScheduleModelError as error:
        return None, [str(error)], []
    candidate["origin"] = {
        **origin,
        "created_at": now_iso(),
    }
    candidate.setdefault("metrics", {})["is_manually_maintained"] = True
    _refresh_candidate_evaluation(
        candidate,
        config,
        participants,
    )
    return candidate, [], policy_issues
