from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from typing import Any

from schedule_adjustment_tool.domain.models import (
    Config,
    Participant,
    eligible_dates,
    make_slot_key,
)


MEETING_MODES = {"in_person", "zoom"}
ROLE_FIELDS = (
    ("university", "university_role_member_ids", "university_role_members"),
    ("high_school", "high_school_role_member_ids", "high_school_role_members"),
)


class ScheduleModelError(ValueError):
    pass


def _stable_identifier(*parts: object) -> str:
    value = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _member_pairs(
    project_id: str,
    session_uid: str,
    role: str,
    member_ids: object,
    member_names: object,
) -> list[tuple[str, str]]:
    ids = [str(value).strip() for value in member_ids or []]
    names = [str(value).strip() for value in member_names or []]
    length = max(len(ids), len(names))
    pairs: list[tuple[str, str]] = []
    for index in range(length):
        participant_id = ids[index] if index < len(ids) else ""
        participant_name = names[index] if index < len(names) else ""
        if not participant_id:
            if not participant_name:
                raise ScheduleModelError("役割メンバーのIDと名前が空です。")
            participant_id = "unresolved-" + _stable_identifier(
                project_id,
                session_uid,
                role,
                index,
                participant_name,
            )
        pairs.append((participant_id, participant_name))
    if len({participant_id for participant_id, _ in pairs}) != len(pairs):
        raise ScheduleModelError("同じ役割に参加者IDが重複しています。")
    return pairs


def normalize_schedule(
    schedule: dict[str, Any],
    project_id: str,
) -> dict[str, Any]:
    """Return a revision-ready schedule without discarding unknown fields."""

    if not isinstance(schedule, dict):
        raise ScheduleModelError("確定日程がオブジェクト形式ではありません。")
    raw_sessions = schedule.get("sessions", [])
    if not isinstance(raw_sessions, list):
        raise ScheduleModelError("確定日程のsessionsが配列ではありません。")

    normalized = deepcopy(schedule)
    normalized_sessions: list[dict[str, Any]] = []
    occurrences: dict[tuple[str, int, int], int] = {}
    seen_session_uids: set[str] = set()
    for index, raw_session in enumerate(raw_sessions):
        if not isinstance(raw_session, dict):
            raise ScheduleModelError(f"sessions[{index}]がオブジェクトではありません。")
        day_text = str(raw_session.get("date", "")).strip()
        try:
            date.fromisoformat(day_text)
        except ValueError as error:
            raise ScheduleModelError(
                f"sessions[{index}]の日付形式が不正です。"
            ) from error
        try:
            period = int(raw_session.get("period", 0))
            group_index = int(raw_session.get("group_index", 1))
        except (TypeError, ValueError) as error:
            raise ScheduleModelError(
                f"sessions[{index}]の時限または組番号が不正です。"
            ) from error
        if period < 1 or period > 24:
            raise ScheduleModelError(f"sessions[{index}]の時限が範囲外です。")
        if group_index < 1:
            raise ScheduleModelError(f"sessions[{index}]の組番号が範囲外です。")
        meeting_mode = str(
            raw_session.get("meeting_mode", "in_person")
        ).strip()
        if meeting_mode not in MEETING_MODES:
            raise ScheduleModelError(f"sessions[{index}]の開催形式が不正です。")

        occurrence_key = (day_text, period, group_index)
        occurrence = occurrences.get(occurrence_key, 0)
        occurrences[occurrence_key] = occurrence + 1
        session_uid = str(
            raw_session.get("session_id")
            or raw_session.get("session_uid")
            or _stable_identifier(
                project_id,
                day_text,
                period,
                group_index,
                occurrence,
            )
        ).strip()[:120]
        if not session_uid or session_uid in seen_session_uids:
            raise ScheduleModelError(f"sessions[{index}]のsession_idが重複しています。")
        seen_session_uids.add(session_uid)

        session = deepcopy(raw_session)
        session.update(
            {
                "session_id": session_uid,
                "date": day_text,
                "period": period,
                "group_index": group_index,
                "meeting_mode": meeting_mode,
            }
        )
        all_member_ids: set[str] = set()
        for role, ids_field, names_field in ROLE_FIELDS:
            pairs = _member_pairs(
                project_id,
                session_uid,
                role,
                session.get(ids_field, []),
                session.get(names_field, []),
            )
            role_member_ids = [participant_id for participant_id, _ in pairs]
            overlap = all_member_ids.intersection(role_member_ids)
            if overlap:
                raise ScheduleModelError(
                    f"sessions[{index}]で同じ参加者が複数役割に割り当てられています。"
                )
            all_member_ids.update(role_member_ids)
            session[ids_field] = role_member_ids
            session[names_field] = [name for _, name in pairs]
        normalized_sessions.append(session)

    normalized["sessions"] = normalized_sessions
    return normalized


def schedule_metadata(schedule: dict[str, Any]) -> dict[str, Any]:
    metadata = deepcopy(schedule)
    metadata.pop("sessions", None)
    return metadata


def schedule_policy_issues(
    schedule: dict[str, Any],
    config: Config,
    participants: list[Participant],
    *,
    allow_solver_completion: bool = False,
) -> list[str]:
    """Explain differences from the current scheduling rules.

    External schedules may intentionally differ from collected availability or
    project settings, so these are explicit override warnings rather than
    storage-level rejection rules. Structural errors still raise
    ``ScheduleModelError`` through ``normalize_schedule``.
    """

    normalized = normalize_schedule(schedule, config.project_id)
    valid_dates = {day.isoformat() for day in eligible_dates(config)}
    excluded_dates = set(config.excluded_dates)
    enabled_periods = set(config.enabled_periods)
    participant_by_id = {
        participant.id: participant for participant in participants
    }
    target_ids = {
        participant.id
        for participant in participants
        if participant.active and participant.approved
    }
    university_counts: Counter[str] = Counter()
    high_school_counts: Counter[str] = Counter()
    total_counts: Counter[str] = Counter()
    sessions_per_slot: Counter[str] = Counter()
    assigned_in_slot: dict[str, set[str]] = defaultdict(set)
    periods_by_participant_day: dict[tuple[str, str], list[int]] = defaultdict(
        list
    )
    seen_group_slots: set[tuple[str, int]] = set()
    issues: list[str] = []

    for session_index, session in enumerate(normalized["sessions"], start=1):
        day_text = str(session["date"])
        period = int(session["period"])
        group_index = int(session["group_index"])
        slot_key = make_slot_key(day_text, period)
        meeting_mode = str(session["meeting_mode"])
        university_ids = list(session["university_role_member_ids"])
        high_school_ids = list(session["high_school_role_member_ids"])
        member_ids = university_ids + high_school_ids

        if day_text not in valid_dates:
            issues.append(f"{session_index}回目の日付が企画の対象期間・曜日外です。")
        elif day_text in excluded_dates:
            issues.append(
                f"{session_index}回目の日付は除外日に設定されています。"
                "既存内容を残したまま確認してください。"
            )
        if period not in enabled_periods:
            issues.append(f"{session_index}回目の時限が企画の対象時限外です。")
        if not 1 <= group_index <= config.max_groups_per_slot:
            issues.append(f"{session_index}回目の組番号が同時開催上限外です。")
        group_slot = (slot_key, group_index)
        if group_slot in seen_group_slots:
            issues.append(f"{session_index}回目の日時・組が別の回と重複しています。")
        seen_group_slots.add(group_slot)
        if (
            not allow_solver_completion
            and len(university_ids) != config.university_role_size
        ):
            issues.append(f"{session_index}回目の大学生役人数が設定と異なります。")
        if (
            not allow_solver_completion
            and len(high_school_ids) != config.high_school_role_size
        ):
            issues.append(f"{session_index}回目の高校生役人数が設定と異なります。")

        duplicate_in_slot = assigned_in_slot[slot_key].intersection(member_ids)
        if duplicate_in_slot:
            duplicate_names = [
                participant_by_id[participant_id].name
                for participant_id in sorted(duplicate_in_slot)
                if participant_id in participant_by_id
            ]
            issues.append(
                f"{session_index}回目で同じ時限に重複参加しています: "
                + "、".join(duplicate_names or sorted(duplicate_in_slot))
            )
        assigned_in_slot[slot_key].update(member_ids)
        sessions_per_slot[slot_key] += 1

        for participant_id in member_ids:
            participant = participant_by_id.get(participant_id)
            if participant is None:
                issues.append(
                    f"{session_index}回目に未登録の参加者IDが含まれています。"
                )
                continue
            if participant_id not in target_ids:
                issues.append(
                    f"{session_index}回目の{participant.name}さんは現在の日調対象外です。"
                )
            can_attend = slot_key in participant.availability or (
                meeting_mode == "zoom"
                and slot_key in participant.zoom_availability
            )
            if not can_attend:
                issues.append(
                    f"{session_index}回目は{participant.name}さんの参加可能回答外です。"
                )
            total_counts[participant_id] += 1
            periods_by_participant_day[(participant_id, day_text)].append(period)
        university_counts.update(university_ids)
        high_school_counts.update(high_school_ids)

    if any(
        count > config.max_groups_per_slot for count in sessions_per_slot.values()
    ):
        issues.append("同じ時限の開催数が1コマあたり最大組数を超えています。")

    for (participant_id, _day_text), periods in periods_by_participant_day.items():
        participant = participant_by_id.get(participant_id)
        if participant is None:
            continue
        if len(periods) > config.max_sessions_per_person_per_day:
            issues.append(
                f"{participant.name}さんの1日あたり参加数が上限を超えています。"
            )
        if config.avoid_consecutive_periods:
            ordered = sorted(periods)
            if any(
                right - left == 1
                for left, right in zip(ordered, ordered[1:])
            ):
                issues.append(f"{participant.name}さんに連続時限があります。")

    for participant in participants:
        if participant.id not in target_ids:
            continue
        if (
            not allow_solver_completion
            and university_counts[participant.id]
            < participant.university_requirement(config)
        ):
            issues.append(f"{participant.name}さんの大学生役回数が不足しています。")
        if (
            not allow_solver_completion
            and high_school_counts[participant.id]
            < participant.high_school_requirement(config)
        ):
            issues.append(f"{participant.name}さんの高校生役回数が不足しています。")
        if (
            not allow_solver_completion
            and total_counts[participant.id] < participant.total_requirement(config)
        ):
            issues.append(f"{participant.name}さんの合計参加回数が不足しています。")
        if total_counts[participant.id] > participant.participation_limit(config):
            baseline = participant.participation_limit(config)
            overage = total_counts[participant.id] - baseline
            issues.append(
                f"{participant.name}さんの参加回数が基準値を超えています。"
                f"現在{total_counts[participant.id]}回、基準値{baseline}回、"
                f"超過{overage}回です。"
            )

    return list(dict.fromkeys(issues))
