from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from typing import Any, Iterable

from ortools.sat.python import cp_model

from schedule_adjustment_tool.domain.amendments import (
    amendment_candidate_sort_key,
    amendment_movement_metrics,
    amendment_movement_signature,
)
from schedule_adjustment_tool.domain.evaluation_config import (
    EVALUATION_SCORE_VERSION,
    PRIORITY_LEVELS,
    PRIORITY_SCORE_FACTORS,
    aggregate_evaluation_score,
    clamp_evaluation_penalty,
    normalize_evaluation_settings,
)
from schedule_adjustment_tool.domain.models import (
    CANDIDATE_SEARCH_MODE_AUTO,
    CANDIDATE_SEARCH_MODE_RELAXED_ONLY,
    CANDIDATE_SEARCH_MODE_STRICT_ONLY,
    Config,
    Participant,
    eligible_dates,
    make_slot_key,
    parse_slot_key,
    practice_dates,
)

MEETING_MODE_IN_PERSON = "in_person"
MEETING_MODE_ZOOM = "zoom"
EVALUATION_OBJECTIVE_SCALE = 10_000
EVALUATION_MAX_SHARE_NUMERATOR = 3
EVALUATION_MEAN_SHARE_NUMERATOR = 2
EVALUATION_SHARE_DENOMINATOR = 5
ZOOM_PENALTY_EXEMPT_PERIODS = {6}
FIRST_CANDIDATE_TIME_SHARE = 0.8
MIN_SOLVER_SLICE_SECONDS = 0.05
AMENDMENT_SCOPED_REPAIR_TIME_SHARE = 0.2
AMENDMENT_SCOPED_REPAIR_MAX_SECONDS = 12.0


def _solver_time_slice(
    remaining_seconds: float,
    target_candidate_count: int,
    found_candidate_count: int,
) -> float:
    """Reserve most of the budget for finding a useful first candidate."""

    remaining_seconds = max(0.0, float(remaining_seconds))
    if remaining_seconds <= MIN_SOLVER_SLICE_SECONDS:
        return remaining_seconds
    if found_candidate_count == 0:
        if target_candidate_count <= 1:
            return remaining_seconds
        return min(
            remaining_seconds,
            max(
                MIN_SOLVER_SLICE_SECONDS,
                remaining_seconds * FIRST_CANDIDATE_TIME_SHARE,
            ),
        )
    remaining_targets = max(1, target_candidate_count - found_candidate_count)
    return min(
        remaining_seconds,
        max(MIN_SOLVER_SLICE_SECONDS, remaining_seconds / remaining_targets),
    )


def _is_zoom_penalty_exempt_period(period: int) -> bool:
    return period in ZOOM_PENALTY_EXEMPT_PERIODS


def _is_zoom_penalty_exempt_session(session: dict[str, Any]) -> bool:
    try:
        period = int(session.get("period", 0))
    except (TypeError, ValueError):
        return False
    return _is_zoom_penalty_exempt_period(period)


def _evaluation_solver_objective(
    model: cp_model.CpModel,
    evaluation_terms: dict[str, list[tuple[cp_model.IntVar, int]]],
    evaluation_settings: dict[str, dict[str, Any]],
) -> tuple[Any, int]:
    """Build the integer objective using the displayed score's aggregation.

    Each evaluation item is normalized to 0..10000 first.  A priority group
    then combines its worst item (60%) and its mean item (40%).  The solver
    minimizes the fixed priority-factor sum; the denominator used for the
    displayed score is constant for one configuration and is therefore not
    needed in the objective.
    """

    item_penalties_by_priority: dict[str, list[cp_model.IntVar]] = {
        priority: [] for priority in PRIORITY_LEVELS
    }
    for evaluation_id, terms in evaluation_terms.items():
        setting = evaluation_settings.get(evaluation_id, {})
        if not setting.get("enabled") or not terms:
            continue
        item_bound = sum(max(0, int(maximum)) for _, maximum in terms)
        if item_bound <= 0:
            continue
        raw_penalty = model.new_int_var(
            0,
            item_bound,
            f"evaluation_raw_penalty_{evaluation_id}",
        )
        model.add(raw_penalty == sum(variable for variable, _ in terms))
        normalized_penalty = model.new_int_var(
            0,
            EVALUATION_OBJECTIVE_SCALE,
            f"evaluation_normalized_penalty_{evaluation_id}",
        )
        model.add_division_equality(
            normalized_penalty,
            raw_penalty * EVALUATION_OBJECTIVE_SCALE,
            item_bound,
        )
        priority = str(setting.get("priority", "consider"))
        if priority in item_penalties_by_priority:
            item_penalties_by_priority[priority].append(normalized_penalty)

    priority_penalties: dict[str, cp_model.IntVar] = {}
    for priority, item_penalties in item_penalties_by_priority.items():
        if not item_penalties:
            continue
        item_count = len(item_penalties)
        worst_penalty = model.new_int_var(
            0,
            EVALUATION_OBJECTIVE_SCALE,
            f"evaluation_worst_penalty_{priority}",
        )
        model.add_max_equality(worst_penalty, item_penalties)
        group_numerator = (
            EVALUATION_MAX_SHARE_NUMERATOR * item_count * worst_penalty
            + EVALUATION_MEAN_SHARE_NUMERATOR * sum(item_penalties)
        )
        group_penalty = model.new_int_var(
            0,
            EVALUATION_OBJECTIVE_SCALE,
            f"evaluation_group_penalty_{priority}",
        )
        model.add_division_equality(
            group_penalty,
            group_numerator,
            EVALUATION_SHARE_DENOMINATOR * item_count,
        )
        priority_penalties[priority] = group_penalty

    evaluation_expression = sum(
        PRIORITY_SCORE_FACTORS[priority] * group_penalty
        for priority, group_penalty in priority_penalties.items()
    )
    evaluation_upper_bound = sum(
        PRIORITY_SCORE_FACTORS[priority] * EVALUATION_OBJECTIVE_SCALE
        for priority in priority_penalties
    )
    return evaluation_expression, evaluation_upper_bound


def scheduling_participants(participants: list[Participant]) -> list[Participant]:
    return [
        participant
        for participant in participants
        if participant.active and participant.approved
    ]


def _valid_slots(config: Config) -> list[str]:
    return [
        make_slot_key(day, period)
        for day in practice_dates(config)
        for period in config.enabled_periods
    ]


def _availability_map(
    config: Config,
    participants: list[Participant],
    *,
    allow_incomplete_groups: bool = False,
    blocked_slots_by_participant: dict[str, set[str]] | None = None,
) -> tuple[list[str], dict[str, set[str]], dict[str, list[str]]]:
    valid_slots = set(_valid_slots(config))
    blocked_slots_by_participant = blocked_slots_by_participant or {}
    availability = {
        participant.id: (
            (set(participant.availability) | set(participant.zoom_availability))
            & valid_slots
            - set(blocked_slots_by_participant.get(participant.id, set()))
        )
        for participant in participants
    }
    members_by_slot: dict[str, list[str]] = defaultdict(list)
    for participant in participants:
        for slot_key in availability[participant.id]:
            members_by_slot[slot_key].append(participant.id)
    required_people = config.university_role_size + config.high_school_role_size
    feasible_slots = sorted(
        slot_key
        for slot_key, member_ids in members_by_slot.items()
        if len(member_ids) >= (1 if allow_incomplete_groups else required_people)
    )
    return feasible_slots, availability, members_by_slot


def _availability_sets(
    config: Config,
    participants: list[Participant],
    *,
    blocked_slots_by_participant: dict[str, set[str]] | None = None,
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    valid_slots = set(_valid_slots(config))
    blocked_slots_by_participant = blocked_slots_by_participant or {}
    in_person: dict[str, set[str]] = {}
    zoom_only: dict[str, set[str]] = {}
    combined: dict[str, set[str]] = {}
    for participant in participants:
        blocked = set(blocked_slots_by_participant.get(participant.id, set()))
        in_person_slots = (set(participant.availability) & valid_slots) - blocked
        zoom_slots = (
            (set(participant.zoom_availability) & valid_slots)
            - blocked
            - in_person_slots
        )
        in_person[participant.id] = in_person_slots
        zoom_only[participant.id] = zoom_slots
        combined[participant.id] = in_person_slots | zoom_slots
    return in_person, zoom_only, combined


def _unique_reasons(reasons: Iterable[str], *, limit: int = 10) -> list[str]:
    return list(dict.fromkeys(reason for reason in reasons if reason))[:limit]


def diagnose_infeasibility(
    config: Config,
    participants: list[Participant],
    *,
    blocked_slots_by_participant: dict[str, set[str]] | None = None,
) -> list[str]:
    reasons: list[str] = []
    target = scheduling_participants(participants)
    unapproved = [
        participant.name
        for participant in participants
        if participant.active and not participant.approved
    ]
    unsubmitted = [
        participant.name
        for participant in target
        if participant.input_status != "submitted"
    ]
    if unapproved:
        reasons.append("未承認の仮登録者がいます: " + ", ".join(unapproved))
    if unsubmitted:
        reasons.append("未提出または下書きの参加者がいます: " + ", ".join(unsubmitted))
    required_people = config.university_role_size + config.high_school_role_size
    if len(target) < required_people:
        reasons.append(
            f"1組に必要な{required_people}人に対し、有効な参加者は{len(target)}人です。"
        )
    feasible_slots, availability, members_by_slot = _availability_map(
        config,
        target,
        blocked_slots_by_participant=blocked_slots_by_participant,
    )
    if not feasible_slots:
        reasons.append("1組分の人数が同時にそろうコマがありません。")

    total_university_need = sum(
        participant.university_requirement(config) for participant in target
    )
    total_high_school_need = sum(
        participant.high_school_requirement(config) for participant in target
    )
    max_groups_capacity = len(feasible_slots) * config.max_groups_per_slot
    total_participation_need = sum(
        participant.total_requirement(config) for participant in target
    )
    if max_groups_capacity * config.university_role_size < total_university_need:
        reasons.append(
            "実行可能コマ全体の大学生役席数が、必要な担当回数に足りません。"
        )
    if max_groups_capacity * config.high_school_role_size < total_high_school_need:
        reasons.append(
            "実行可能コマ全体の高校生役席数が、必要な担当回数に足りません。"
        )
    if (
        max_groups_capacity * required_people
        < total_participation_need
    ):
        reasons.append(
            "実行可能コマ全体の参加枠が、必要な合計参加回数に足りません。"
        )

    for participant in target:
        requirement = (
            participant.total_requirement(config)
            or participant.university_requirement(config)
            + participant.high_school_requirement(config)
        )
        feasible_for_person = sum(
            participant.id in members_by_slot.get(slot_key, [])
            for slot_key in feasible_slots
        )
        if feasible_for_person < requirement:
            reasons.append(
                f"{participant.name}さんが参加できる実行可能コマは"
                f"{feasible_for_person}件で、必要参加回数{requirement}回に届きません。"
            )
        if requirement > 0 and not availability.get(participant.id):
            reasons.append(f"{participant.name}さんの参加可能コマがありません。")
    return _unique_reasons(reasons) or [
        "参加可能時間、必要回数、役割人数、1日上限、連続コマ禁止の組み合わせを確認してください。"
    ]


def _participation_limit_failure_reasons(
    config: Config,
    participants: list[Participant],
    *,
    blocked_slots_by_participant: dict[str, set[str]] | None = None,
) -> list[str]:
    """Return cap guidance only when the cap is a strong local blocker."""

    target = scheduling_participants(participants)
    _slots, _availability, members_by_slot = _availability_map(
        config,
        target,
        allow_incomplete_groups=True,
        blocked_slots_by_participant=blocked_slots_by_participant,
    )
    raw_pair_slots = [
        member_ids for member_ids in members_by_slot.values() if len(member_ids) >= 2
    ]
    if not raw_pair_slots:
        return []
    participant_by_id = {participant.id: participant for participant in target}
    if any(
        sum(
            participant_by_id[participant_id].participation_limit(config) >= 1
            for participant_id in member_ids
        )
        >= 2
        for member_ids in raw_pair_slots
    ):
        return []
    blocked_people = sorted(
        {
            (
                participant_by_id[participant_id].name,
                participant_by_id[participant_id].participation_limit(config),
            )
            for member_ids in raw_pair_slots
            for participant_id in member_ids
            if participant_by_id[participant_id].participation_limit(config) < 1
        }
    )
    if not blocked_people:
        return []
    details = "、".join(
        f"{name}さん（現在の上限{limit}回、少なくとも1回必要になる可能性）"
        for name, limit in blocked_people[:4]
    )
    return [
        "合計参加の超過上限が成立を妨げている可能性があります。"
        "上限を上げると候補を作成できる場合があります。",
        "個別条件または成立条件の超過上限を確認してください: " + details,
    ]


def _candidate_failure_reasons(
    config: Config,
    participants: list[Participant],
    *,
    headline: str,
    blocked_slots_by_participant: dict[str, set[str]] | None = None,
) -> list[str]:
    return _unique_reasons(
        [
            headline,
            *_participation_limit_failure_reasons(
                config,
                participants,
                blocked_slots_by_participant=blocked_slots_by_participant,
            ),
            *diagnose_infeasibility(
                config,
                participants,
                blocked_slots_by_participant=blocked_slots_by_participant,
            ),
        ]
    )


def _spread_score(session_slots: list[str], config: Config) -> float:
    dates = sorted(parse_slot_key(slot_key)[0] for slot_key in session_slots)
    unique_dates = sorted(set(dates))
    if len(unique_dates) <= 1:
        return 0.0
    total_period = max(1, (date.fromisoformat(config.end_date) - date.fromisoformat(config.start_date)).days)
    span = (unique_dates[-1] - unique_dates[0]).days
    gaps = [
        (right - left).days for left, right in zip(unique_dates, unique_dates[1:])
    ]
    mean_gap = sum(gaps) / len(gaps)
    variance = sum((gap - mean_gap) ** 2 for gap in gaps) / len(gaps)
    same_day_penalty = len(dates) - len(unique_dates)
    normalized_span = span / total_period * 60
    coverage = len(unique_dates) / len(dates) * 30
    uniformity = max(0.0, 10 - math.sqrt(variance))
    return round(max(0.0, normalized_span + coverage + uniformity - same_day_penalty * 3), 2)


def _participant_schedule_preference(
    sessions: list[dict[str, Any]], config: Config
) -> dict[str, float | int]:
    slots_by_participant: dict[str, list[tuple[date, int]]] = defaultdict(list)
    for session in sessions:
        session_date = date.fromisoformat(session["date"])
        period = int(session["period"])
        member_ids = (
            session.get("university_role_member_ids", [])
            + session.get("high_school_role_member_ids", [])
        )
        for participant_id in member_ids:
            slots_by_participant[participant_id].append((session_date, period))

    total_period_days = max(
        1,
        (
            date.fromisoformat(config.end_date)
            - date.fromisoformat(config.start_date)
        ).days,
    )
    total_score = 0.0
    same_day_consecutive_penalty = 0.0
    separate_days_penalty = 0.0
    consecutive_pairs = 0
    nonconsecutive_gap = 0
    multi_day_participants = 0
    for slots in slots_by_participant.values():
        periods_by_day: dict[date, list[int]] = defaultdict(list)
        for day, period in slots:
            periods_by_day[day].append(period)
        for periods in periods_by_day.values():
            periods.sort()
            for left, right in zip(periods, periods[1:]):
                if right - left == 1:
                    consecutive_pairs += 1
                    total_score += 12
                else:
                    gap = max(0, right - left - 1)
                    nonconsecutive_gap += gap
                    total_score -= gap * 5
                    same_day_consecutive_penalty += gap + 1
            if len(periods) >= 2:
                separate_days_penalty += len(periods) - 1

        unique_days = sorted(periods_by_day)
        if len(unique_days) >= 2:
            multi_day_participants += 1
            pairwise_gaps = [
                (right - left).days
                for index, left in enumerate(unique_days)
                for right in unique_days[index + 1 :]
            ]
            average_gap = sum(pairwise_gaps) / len(pairwise_gaps)
            total_score += average_gap / total_period_days * 30
            same_day_consecutive_penalty += len(unique_days) - 1
            separate_days_penalty += max(
                0.0, 1 - average_gap / total_period_days
            )

    participant_count = max(1, len(slots_by_participant))
    return {
        "participant_schedule_score": round(total_score / participant_count, 2),
        "consecutive_pair_count": consecutive_pairs,
        "nonconsecutive_same_day_gap": nonconsecutive_gap,
        "multi_day_participant_count": multi_day_participants,
        "same_day_consecutive_penalty": round(
            same_day_consecutive_penalty, 2
        ),
        "separate_days_penalty": round(separate_days_penalty, 2),
    }


def _cohort_evaluation(
    sessions: list[dict[str, Any]],
    participant_by_id: dict[str, Participant],
) -> dict[str, int]:
    known_cohorts = [
        participant.cohort
        for participant in participant_by_id.values()
        if not participant.is_role_unspecified and participant.cohort is not None
    ]
    if not known_cohorts:
        return {
            "latest_cohort": 0,
            "cohort_evaluated_session_count": 0,
            "cohort_latest_only_session_count": 0,
            "cohort_experienced_session_count": 0,
            "cohort_role_split_session_count": 0,
            "cohort_unset_issue_count": len(sessions),
        }
    latest_cohort = max(known_cohorts)
    evaluated = 0
    latest_only = 0
    experienced_sessions = 0
    role_split = 0
    unset = 0
    for session in sessions:
        university_members = [
            participant_by_id[participant_id]
            for participant_id in session.get("university_role_member_ids", [])
            if not participant_by_id[participant_id].is_role_unspecified
        ]
        high_school_members = [
            participant_by_id[participant_id]
            for participant_id in session.get("high_school_role_member_ids", [])
            if not participant_by_id[participant_id].is_role_unspecified
        ]
        regular_members = university_members + high_school_members
        if not regular_members:
            continue
        if any(participant.cohort is None for participant in regular_members):
            unset += 1
            continue
        evaluated += 1
        experienced_university = any(
            participant.cohort < latest_cohort
            for participant in university_members
        )
        experienced_high_school = any(
            participant.cohort < latest_cohort
            for participant in high_school_members
        )
        has_experienced = experienced_university or experienced_high_school
        experienced_sessions += int(has_experienced)
        latest_only += int(not has_experienced)
        role_split += int(experienced_university and experienced_high_school)
    return {
        "latest_cohort": latest_cohort,
        "cohort_evaluated_session_count": evaluated,
        "cohort_latest_only_session_count": latest_only,
        "cohort_experienced_session_count": experienced_sessions,
        "cohort_role_split_session_count": role_split,
        "cohort_unset_issue_count": unset,
    }


def _evaluation_metrics(
    sessions: list[dict[str, Any]],
    config: Config,
    participants: list[Participant],
    base_metrics: dict[str, Any],
) -> dict[str, Any]:
    settings = normalize_evaluation_settings(config.evaluation_settings)
    buffer_sessions = 0
    avoided_period_session_count = sum(
        int(session["period"]) in config.avoided_periods
        for session in sessions
    )
    closest_days = None
    if config.performance_date and config.performance_avoid_days > 0:
        performance_day = date.fromisoformat(config.performance_date)
        for session in sessions:
            session_day = date.fromisoformat(session["date"])
            days_before = (performance_day - session_day).days
            if days_before >= 0:
                closest_days = (
                    days_before
                    if closest_days is None
                    else min(closest_days, days_before)
                )
            if 1 <= days_before <= config.performance_avoid_days:
                buffer_sessions += 1

    cohort_metrics = _cohort_evaluation(
        sessions, {participant.id: participant for participant in participants}
    )
    evaluated_cohort = cohort_metrics["cohort_evaluated_session_count"]
    cohort_unset = cohort_metrics["cohort_unset_issue_count"]
    cohort_denominator = max(1, (evaluated_cohort + cohort_unset) * 3)
    cohort_penalty = round(
        (
            cohort_metrics["cohort_latest_only_session_count"] * 2
            + max(
                0,
                evaluated_cohort
                - cohort_metrics["cohort_role_split_session_count"],
            )
            + cohort_unset * 3
        )
        / cohort_denominator
        * 100,
        2,
    )
    participant_scale = max(1, len(participants))
    eligible_day_count = max(1, len(eligible_dates(config)))
    zoom_sessions = [
        session
        for session in sessions
        if session.get("meeting_mode") == MEETING_MODE_ZOOM
    ]
    penalty_target_zoom_sessions = [
        session
        for session in zoom_sessions
        if not _is_zoom_penalty_exempt_session(session)
    ]
    required_people = max(1, config.university_role_size + config.high_school_role_size)
    necessary_zoom_sessions = [
        session
        for session in penalty_target_zoom_sessions
        if int(session.get("zoom_only_member_count", 0)) * 2 >= required_people
    ]
    avoidable_zoom_session_count = (
        0
        if settings["zoom_meeting"]["policy"] == "ignore"
        else (
            len(penalty_target_zoom_sessions) - len(necessary_zoom_sessions)
            if settings["zoom_meeting"]["policy"] == "avoid_unless_needed"
            else len(penalty_target_zoom_sessions)
        )
    )

    penalties: dict[str, float] = {
        "performance_buffer": (
            buffer_sessions / max(1, len(sessions)) * 100
        ),
        "avoid_periods": (
            avoided_period_session_count / max(1, len(sessions)) * 100
        ),
        "zoom_meeting": avoidable_zoom_session_count / max(1, len(sessions)) * 100,
        "cohort_balance": cohort_penalty,
        "same_group": (
            base_metrics.get("priority_group_violation_count", 0)
            / max(1, base_metrics.get("university_group_evaluated_count", 0))
            * 100
        ),
        "field_match": (
            base_metrics.get("priority_field_violation_count", 0)
            / max(1, base_metrics.get("field_evaluated_count", 0))
            * 100
        ),
        "session_count": len(sessions) / participant_scale * 100,
        "participant_schedule": (
            min(
                100.0,
                base_metrics.get("same_day_consecutive_penalty", 0)
                / participant_scale
                * 100,
            )
            if settings["participant_schedule"]["policy"]
            == "same_day_consecutive"
            else min(
                100.0,
                base_metrics.get("separate_days_penalty", 0)
                / participant_scale
                * 100,
            )
        ),
        "overall_schedule": (
            max(0.0, 100 - float(base_metrics.get("spread_score", 0)))
            if settings["overall_schedule"]["policy"] == "spread"
            else (
                len({session["date"] for session in sessions})
                / eligible_day_count
                * 100
            )
        ),
    }
    penalties = {
        evaluation_id: round(clamp_evaluation_penalty(penalty), 2)
        for evaluation_id, penalty in penalties.items()
    }
    score_details = aggregate_evaluation_score(penalties, settings)
    return {
        "performance_buffer_session_count": buffer_sessions,
        "avoided_period_session_count": avoided_period_session_count,
        "zoom_session_count": len(zoom_sessions),
        "zoom_session_rate": round(len(zoom_sessions) / max(1, len(sessions)), 4),
        "zoom_penalty_exempt_session_count": (
            len(zoom_sessions) - len(penalty_target_zoom_sessions)
        ),
        "necessary_zoom_session_count": len(necessary_zoom_sessions),
        "avoidable_zoom_session_count": avoidable_zoom_session_count,
        "zoom_only_assignment_count": sum(
            int(session.get("zoom_only_member_count", 0)) for session in zoom_sessions
        ),
        "closest_session_days_before_performance": closest_days,
        **cohort_metrics,
        "cohort_penalty": cohort_penalty,
        "evaluation_penalties": penalties,
        "evaluation_score_version": EVALUATION_SCORE_VERSION,
        **score_details,
    }


def _session_assignment_evaluation(
    sessions: list[dict[str, Any]],
    config: Config,
    participant_by_id: dict[str, Participant],
) -> dict[str, int | bool]:
    group_match_count = 0
    group_evaluated_count = 0
    field_match_count = 0
    field_evaluated_count = 0
    unset_issue_count = 0
    configured_violation_count = 0

    for session in sessions:
        university_members = [
            participant_by_id[participant_id]
            for participant_id in session.get("university_role_member_ids", [])
        ]
        high_school_members = [
            participant_by_id[participant_id]
            for participant_id in session.get("high_school_role_member_ids", [])
        ]
        regular_university_members = [
            participant
            for participant in university_members
            if not participant.is_role_unspecified
        ]
        regular_groups = {
            str(participant.group_number)
            for participant in regular_university_members
        }
        invalid_group_members = [
            participant
            for participant in regular_university_members
            if not isinstance(participant.group_number, int)
            or not 1 <= participant.group_number <= config.group_count
        ]

        group_match: bool | None = None
        group_status = "対象外"
        if config.university_role_size >= 2:
            group_evaluated_count += 1
            if invalid_group_members:
                group_status = "属性未設定"
                unset_issue_count += 1
            else:
                group_match = len(regular_groups) <= 1
                group_match_count += int(group_match)
                group_status = "適合" if group_match else "不一致"
                configured_violation_count += int(not group_match)

        field_match: bool | None = None
        field_status = "対象外"
        if regular_groups:
            field_evaluated_count += 1
            required_fields = {
                config.group_field_assignments.get(group_number, "文理混合")
                for group_number in regular_groups
            }
            requires_specific_field = any(
                required_field != "文理混合"
                for required_field in required_fields
            )
            has_unset_field = requires_specific_field and any(
                participant.humanities_or_science not in {"文系", "理系"}
                for participant in high_school_members
            )
            if invalid_group_members or has_unset_field:
                field_status = "属性未設定"
                unset_issue_count += 1
            else:
                field_match = all(
                    required_field == "文理混合"
                    or all(
                        participant.humanities_or_science == required_field
                        for participant in high_school_members
                    )
                    for required_field in required_fields
                )
                field_match_count += int(field_match)
                field_status = "適合" if field_match else "不一致"
                configured_violation_count += int(not field_match)

        session["university_group_match"] = group_match
        session["field_match"] = field_match
        session["university_group_status"] = group_status
        session["field_status"] = field_status
        session["university_regular_groups"] = sorted(regular_groups)
        session["assigned_group_fields"] = sorted(
            {
                config.group_field_assignments.get(group_number, "文理混合")
                for group_number in regular_groups
            }
        )

    group_violation_count = sum(
        session.get("university_group_status") in {"不一致", "属性未設定"}
        for session in sessions
    )
    field_violation_count = sum(
        session.get("field_status") in {"不一致", "属性未設定"}
        for session in sessions
    )
    return {
        "university_group_match_count": group_match_count,
        "university_group_evaluated_count": group_evaluated_count,
        "university_group_all_matched": group_violation_count == 0,
        "field_match_count": field_match_count,
        "field_evaluated_count": field_evaluated_count,
        "field_all_matched": field_violation_count == 0,
        "priority_violation_count": group_violation_count + field_violation_count,
        "priority_group_violation_count": group_violation_count,
        "priority_field_violation_count": field_violation_count,
        "priority_unset_issue_count": unset_issue_count,
        "priority_configured_violation_count": configured_violation_count,
    }


def candidate_fingerprint(candidate: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        sorted(
            (
                session["date"],
                int(session["period"]),
                int(session["group_index"]),
                session.get("meeting_mode", MEETING_MODE_IN_PERSON),
                tuple(sorted(session.get("university_role_member_ids", []))),
                tuple(sorted(session.get("high_school_role_member_ids", []))),
            )
            for session in candidate.get("sessions", [])
        )
    )


def candidate_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    metrics = candidate["metrics"]
    try:
        evaluation_score = float(metrics.get("evaluation_score", 0.0))
    except (TypeError, ValueError):
        evaluation_score = 0.0
    return (
        not metrics.get("is_strict_candidate", True),
        metrics.get(
            "mandatory_violation_total",
            metrics.get("mandatory_violation_score", 0),
        ),
        metrics.get("over_limit_total", 0),
        metrics.get("total_extra_count", 0),
        -evaluation_score,
        not metrics.get("evaluation_optimality_proven", False),
        not metrics.get("extra_minimum_proven", False),
        not metrics.get("violation_minimum_proven", False),
        metrics.get("required_shortfall_total", 0),
        metrics["unmet_participant_count"],
    )


def evaluation_config_snapshot(config: Config) -> dict[str, Any]:
    """Return the evaluation inputs stored with a candidate."""

    return {
        "evaluation_settings": normalize_evaluation_settings(
            config.evaluation_settings
        ),
        "performance_date": str(config.performance_date),
        "performance_avoid_days": int(config.performance_avoid_days),
        "avoided_periods": sorted(
            int(period) for period in config.avoided_periods
        ),
    }


def candidate_has_evaluation_config(candidate: dict[str, Any]) -> bool:
    snapshot = candidate.get("evaluation_config")
    return isinstance(snapshot, dict) and isinstance(
        snapshot.get("evaluation_settings"),
        dict,
    )


def _evaluation_config_for_candidate(
    candidate: dict[str, Any],
    config: Config,
) -> Config:
    snapshot = candidate.get("evaluation_config")
    if not isinstance(snapshot, dict):
        return config
    updates = {
        key: snapshot[key]
        for key in (
            "evaluation_settings",
            "performance_date",
            "performance_avoid_days",
            "avoided_periods",
        )
        if key in snapshot
    }
    if not updates:
        return config
    return Config.from_dict({**config.to_dict(), **updates})


def refresh_candidate_evaluation(
    candidate: dict[str, Any],
    config: Config,
    participants: list[Participant],
    *,
    evaluation_config: Config | None = None,
) -> dict[str, Any]:
    score_config = evaluation_config or _evaluation_config_for_candidate(
        candidate,
        config,
    )
    target = scheduling_participants(participants)
    # A published or saved schedule can still contain a participant who is now
    # outside the scheduling target. Keep their current attributes available
    # for display-only re-evaluation while requirements continue to use target.
    participant_by_id = {
        participant.id: participant for participant in participants
    }
    sessions = candidate.get("sessions", [])
    totals: Counter[str] = Counter()
    university_counts: Counter[str] = Counter()
    high_school_counts: Counter[str] = Counter()
    groups_per_slot: Counter[str] = Counter()
    session_role_shortfall_total = 0
    for session in sessions:
        university_ids = list(
            session.get("university_role_member_ids", [])
        )
        high_school_ids = list(
            session.get("high_school_role_member_ids", [])
        )
        university_counts.update(university_ids)
        high_school_counts.update(high_school_ids)
        totals.update(
            list(
                dict.fromkeys(
                    [
                        *session.get("participant_member_ids", []),
                        *university_ids,
                        *high_school_ids,
                    ]
                )
            )
        )
        slot_key = make_slot_key(session["date"], int(session["period"]))
        groups_per_slot[slot_key] += 1
        university_shortfall = max(
            0,
            score_config.university_role_size - len(university_ids),
        )
        high_school_shortfall = max(
            0,
            score_config.high_school_role_size - len(high_school_ids),
        )
        session["university_role_shortfall"] = university_shortfall
        session["high_school_role_shortfall"] = high_school_shortfall
        session_role_shortfall_total += (
            university_shortfall + high_school_shortfall
        )
    total_extra = 0
    unmet_participants = 0
    required_shortfall_total = 0
    over_limit_total = 0
    participant_summary: list[dict[str, Any]] = []
    for participant in target:
        university_required = participant.university_requirement(score_config)
        high_school_required = participant.high_school_requirement(score_config)
        total_required = participant.total_requirement(score_config)
        required_total = (
            total_required
            if total_required
            else university_required + high_school_required
        )
        university_shortfall = max(
            0,
            university_required - university_counts[participant.id],
        )
        high_school_shortfall = max(
            0,
            high_school_required - high_school_counts[participant.id],
        )
        total_shortfall = max(
            0,
            total_required - totals[participant.id],
        )
        required_shortfall = (
            university_shortfall + high_school_shortfall + total_shortfall
        )
        over_limit = max(
            0,
            totals[participant.id]
            - participant.participation_limit(score_config),
        )
        extra = (
            0
            if participant.is_role_unspecified
            else max(0, totals[participant.id] - required_total)
        )
        unmet_participants += int(required_shortfall > 0)
        required_shortfall_total += required_shortfall
        over_limit_total += over_limit
        total_extra += extra
        participant_summary.append(
            {
                "participant_id": participant.id,
                "name": participant.name,
                "university_count": university_counts[participant.id],
                "high_school_count": high_school_counts[participant.id],
                "total_count": totals[participant.id],
                "extra_count": extra,
                "is_support": participant.is_support,
                "is_role_unspecified": participant.is_role_unspecified,
                "university_shortfall": university_shortfall,
                "high_school_shortfall": high_school_shortfall,
                "total_shortfall": total_shortfall,
                "over_limit_count": over_limit,
            }
        )
    session_slots = [
        make_slot_key(session["date"], int(session["period"]))
        for session in sessions
    ]
    assignment_metrics = _session_assignment_evaluation(
        sessions, score_config, participant_by_id
    )
    preference_metrics = _participant_schedule_preference(
        sessions,
        score_config,
    )
    base_metrics = {
        "spread_score": _spread_score(session_slots, score_config),
        **assignment_metrics,
        **preference_metrics,
    }
    candidate["participant_summary"] = participant_summary
    candidate.setdefault("metrics", {}).update(
        {
            "is_strict_candidate": (
                required_shortfall_total == 0
                and over_limit_total == 0
                and session_role_shortfall_total == 0
            ),
            "unmet_participant_count": unmet_participants,
            "required_shortfall_total": required_shortfall_total,
            "over_limit_total": over_limit_total,
            "mandatory_violation_total": (
                required_shortfall_total + session_role_shortfall_total
            ),
            "mandatory_violation_score": (
                required_shortfall_total * 100
                + session_role_shortfall_total * 100
                + over_limit_total * 20
            ),
            "session_role_shortfall_total": session_role_shortfall_total,
            "total_extra_count": total_extra,
            "number_of_sessions": len(sessions),
            "number_of_time_slots": len(set(session_slots)),
            "max_groups_used_in_same_slot": max(
                groups_per_slot.values(),
                default=0,
            ),
            **base_metrics,
            **_evaluation_metrics(
                sessions,
                score_config,
                target,
                base_metrics,
            ),
        }
    )
    candidate["evaluation_config"] = evaluation_config_snapshot(score_config)
    return candidate


def _candidate_from_solution(
    solver: cp_model.CpSolver,
    config: Config,
    participants: list[Participant],
    feasible_slots: list[str],
    active_vars: dict[tuple[str, int], cp_model.IntVar],
    zoom_vars: dict[tuple[str, int], cp_model.IntVar],
    university_vars: dict[tuple[str, int, str], cp_model.IntVar],
    high_school_vars: dict[tuple[str, int, str], cp_model.IntVar],
) -> dict[str, Any]:
    participant_by_id = {participant.id: participant for participant in participants}
    zoom_only_slots = {
        participant.id: set(participant.zoom_availability) - set(participant.availability)
        for participant in participants
    }
    sessions: list[dict[str, Any]] = []
    totals: Counter[str] = Counter()
    university_counts: Counter[str] = Counter()
    high_school_counts: Counter[str] = Counter()
    groups_per_slot: Counter[str] = Counter()
    session_role_shortfall_total = 0

    for slot_key in feasible_slots:
        day, period = parse_slot_key(slot_key)
        for group_index in range(config.max_groups_per_slot):
            if not solver.boolean_value(active_vars[(slot_key, group_index)]):
                continue
            university_ids = sorted(
                participant.id
                for participant in participants
                if (slot_key, group_index, participant.id) in university_vars
                and solver.boolean_value(
                    university_vars[(slot_key, group_index, participant.id)]
                )
            )
            high_school_ids = sorted(
                participant.id
                for participant in participants
                if (slot_key, group_index, participant.id) in high_school_vars
                and solver.boolean_value(
                    high_school_vars[(slot_key, group_index, participant.id)]
                )
            )
            totals.update(university_ids + high_school_ids)
            university_counts.update(university_ids)
            high_school_counts.update(high_school_ids)
            groups_per_slot[slot_key] += 1
            university_role_shortfall = max(
                0, config.university_role_size - len(university_ids)
            )
            high_school_role_shortfall = max(
                0, config.high_school_role_size - len(high_school_ids)
            )
            meeting_mode = (
                MEETING_MODE_ZOOM
                if solver.boolean_value(zoom_vars[(slot_key, group_index)])
                else MEETING_MODE_IN_PERSON
            )
            zoom_only_member_ids = sorted(
                participant_id
                for participant_id in university_ids + high_school_ids
                if slot_key in zoom_only_slots.get(participant_id, set())
            )
            session_role_shortfall_total += (
                university_role_shortfall + high_school_role_shortfall
            )
            sessions.append(
                {
                    "date": day.isoformat(),
                    "period": period,
                    "group_index": group_index + 1,
                    "meeting_mode": meeting_mode,
                    "university_role_member_ids": university_ids,
                    "high_school_role_member_ids": high_school_ids,
                    "university_role_members": [
                        participant_by_id[item].name for item in university_ids
                    ],
                    "high_school_role_members": [
                        participant_by_id[item].name for item in high_school_ids
                    ],
                    "university_role_shortfall": university_role_shortfall,
                    "high_school_role_shortfall": high_school_role_shortfall,
                    "zoom_only_member_ids": zoom_only_member_ids,
                    "zoom_only_member_count": len(zoom_only_member_ids),
                }
            )

    total_extra = 0
    unmet_participants = 0
    required_shortfall_total = 0
    over_limit_total = 0
    participant_summary: list[dict[str, Any]] = []
    for participant in participants:
        university_required = participant.university_requirement(config)
        high_school_required = participant.high_school_requirement(config)
        total_required = participant.total_requirement(config)
        required_total = (
            total_required
            if total_required
            else university_required + high_school_required
        )
        university_shortfall = max(
            0, university_required - university_counts[participant.id]
        )
        high_school_shortfall = max(
            0, high_school_required - high_school_counts[participant.id]
        )
        total_shortfall = max(0, total_required - totals[participant.id])
        required_shortfall = (
            university_shortfall + high_school_shortfall + total_shortfall
        )
        over_limit = max(
            0,
            totals[participant.id] - participant.participation_limit(config),
        )
        unmet_participants += int(required_shortfall > 0)
        required_shortfall_total += required_shortfall
        over_limit_total += over_limit
        extra = (
            0
            if participant.is_role_unspecified
            else max(0, totals[participant.id] - required_total)
        )
        total_extra += extra
        participant_summary.append(
            {
                "participant_id": participant.id,
                "name": participant.name,
                "university_count": university_counts[participant.id],
                "high_school_count": high_school_counts[participant.id],
                "total_count": totals[participant.id],
                "extra_count": extra,
                "is_support": participant.is_support,
                "is_role_unspecified": participant.is_role_unspecified,
                "university_shortfall": university_shortfall,
                "high_school_shortfall": high_school_shortfall,
                "total_shortfall": total_shortfall,
                "over_limit_count": over_limit,
            }
        )
    session_slots = [
        make_slot_key(session["date"], session["period"]) for session in sessions
    ]
    preference_metrics = _participant_schedule_preference(sessions, config)
    assignment_metrics = _session_assignment_evaluation(
        sessions, config, participant_by_id
    )
    base_metrics = {
        "spread_score": _spread_score(session_slots, config),
        **assignment_metrics,
        **preference_metrics,
    }
    evaluation_metrics = _evaluation_metrics(
        sessions, config, participants, base_metrics
    )
    return {
        "sessions": sessions,
        "participant_summary": participant_summary,
        "evaluation_config": evaluation_config_snapshot(config),
        "metrics": {
            "is_strict_candidate": (
                required_shortfall_total == 0
                and over_limit_total == 0
                and session_role_shortfall_total == 0
            ),
            "unmet_participant_count": unmet_participants,
            "required_shortfall_total": required_shortfall_total,
            "over_limit_total": over_limit_total,
            "mandatory_violation_total": (
                required_shortfall_total + session_role_shortfall_total
            ),
            "mandatory_violation_score": (
                required_shortfall_total * 100
                + session_role_shortfall_total * 100
                + over_limit_total * 20
            ),
            "session_role_shortfall_total": session_role_shortfall_total,
            "total_extra_count": total_extra,
            "number_of_sessions": len(sessions),
            "number_of_time_slots": len(set(session_slots)),
            "spread_score": base_metrics["spread_score"],
            "max_groups_used_in_same_slot": max(groups_per_slot.values(), default=0),
            **assignment_metrics,
            **preference_metrics,
            **evaluation_metrics,
        },
    }


def _validate_candidate(
    candidate: dict[str, Any],
    config: Config,
    participants: list[Participant],
    *,
    require_all_mandatory_conditions: bool = True,
) -> bool:
    participant_by_id = {participant.id: participant for participant in participants}
    university_counts: Counter[str] = Counter()
    high_school_counts: Counter[str] = Counter()
    total_counts: Counter[str] = Counter()
    groups_per_slot: Counter[str] = Counter()
    used_in_slot: dict[str, set[str]] = defaultdict(set)
    used_periods_by_day: dict[tuple[str, str], list[int]] = defaultdict(list)
    for session in candidate.get("sessions", []):
        slot_key = make_slot_key(session["date"], int(session["period"]))
        meeting_mode = session.get("meeting_mode", MEETING_MODE_IN_PERSON)
        university = set(session.get("university_role_member_ids", []))
        high_school = set(session.get("high_school_role_member_ids", []))
        if require_all_mandatory_conditions:
            if len(university) != config.university_role_size:
                return False
            if len(high_school) != config.high_school_role_size:
                return False
        elif (
            len(university) > config.university_role_size
            or len(high_school) > config.high_school_role_size
            or not university
            or not high_school
        ):
            return False
        if university & high_school:
            return False
        members = university | high_school
        if members & used_in_slot[slot_key]:
            return False
        for participant_id in members:
            participant = participant_by_id.get(participant_id)
            if participant is None:
                return False
            can_attend = slot_key in participant.availability or (
                meeting_mode == MEETING_MODE_ZOOM
                and slot_key in participant.zoom_availability
            )
            if not can_attend:
                return False
            used_periods_by_day[(participant_id, session["date"])].append(
                int(session["period"])
            )
        used_in_slot[slot_key].update(members)
        groups_per_slot[slot_key] += 1
        university_counts.update(university)
        high_school_counts.update(high_school)
        total_counts.update(members)
    if any(value > config.max_groups_per_slot for value in groups_per_slot.values()):
        return False
    for participant in participants:
        university_required = participant.university_requirement(config)
        high_school_required = participant.high_school_requirement(config)
        total_required = participant.total_requirement(config)
        if require_all_mandatory_conditions:
            if university_counts[participant.id] < university_required:
                return False
            if high_school_counts[participant.id] < high_school_required:
                return False
            if total_counts[participant.id] < total_required:
                return False
        if total_counts[participant.id] > participant.participation_limit(config):
            return False
    for periods in used_periods_by_day.values():
        if len(periods) > config.max_sessions_per_person_per_day:
            return False
        if config.avoid_consecutive_periods:
            sorted_periods = sorted(periods)
            if any(right - left == 1 for left, right in zip(sorted_periods, sorted_periods[1:])):
                return False
    return True


def generate_candidates(
    config: Config,
    participants: list[Participant],
    timeout_seconds: float | None = None,
    max_attempts: int | None = None,
    random_seed: int | None = None,
    candidate_limit: int | None = None,
    excluded_candidates: list[dict[str, Any]] | None = None,
    blocked_slots_by_participant: dict[str, set[str]] | None = None,
    fixed_sessions: list[dict[str, Any]] | None = None,
    assignment_locks: list[dict[str, Any]] | None = None,
    amendment_base_schedule: dict[str, Any] | None = None,
    amendment_requester_id: str = "",
    amendment_requester_ids: set[str] | None = None,
    amendment_fixed_participant_ids: set[str] | None = None,
    amendment_distinct_movements: bool = False,
    amendment_search_stage: str = "",
    amendment_incumbent_candidate: dict[str, Any] | None = None,
    allow_relaxed_fallback: bool = True,
    relaxed: bool = False,
    _deadline: float | None = None,
    _search_started: float | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    del max_attempts
    search_mode = getattr(
        config,
        "candidate_search_mode",
        CANDIDATE_SEARCH_MODE_AUTO,
    )
    if not relaxed:
        if search_mode == CANDIDATE_SEARCH_MODE_RELAXED_ONLY:
            relaxed = True
            allow_relaxed_fallback = False
        elif search_mode == CANDIDATE_SEARCH_MODE_STRICT_ONLY:
            allow_relaxed_fallback = False
    total_timeout = max(
        MIN_SOLVER_SLICE_SECONDS,
        float(timeout_seconds or config.search_timeout_seconds),
    )
    search_started = _search_started or time.monotonic()
    deadline = _deadline or (search_started + total_timeout)
    normalized_amendment_requester_ids = {
        str(value) for value in (amendment_requester_ids or set())
    }
    if amendment_requester_id:
        normalized_amendment_requester_ids.add(
            str(amendment_requester_id)
        )
    errors = config.validate()
    if errors:
        return [], errors
    target = scheduling_participants(participants)
    if not target:
        return [], ["承認済みで有効な参加者が0人です。"]
    if any(participant.input_status != "submitted" for participant in target):
        return [], diagnose_infeasibility(
            config,
            participants,
            blocked_slots_by_participant=blocked_slots_by_participant,
        )

    feasible_slots, availability, members_by_slot = _availability_map(
        config,
        target,
        allow_incomplete_groups=relaxed,
        blocked_slots_by_participant=blocked_slots_by_participant,
    )
    in_person_availability, zoom_only_availability, availability = _availability_sets(
        config,
        target,
        blocked_slots_by_participant=blocked_slots_by_participant,
    )
    if not feasible_slots:
        if allow_relaxed_fallback and not relaxed:
            relaxed_candidates, _ = generate_candidates(
                config,
                participants,
                timeout_seconds=timeout_seconds,
                random_seed=random_seed,
                candidate_limit=candidate_limit,
                excluded_candidates=excluded_candidates,
                blocked_slots_by_participant=blocked_slots_by_participant,
                fixed_sessions=fixed_sessions,
                assignment_locks=assignment_locks,
                amendment_base_schedule=amendment_base_schedule,
                amendment_requester_id=amendment_requester_id,
                amendment_requester_ids=normalized_amendment_requester_ids,
                amendment_fixed_participant_ids=amendment_fixed_participant_ids,
                amendment_distinct_movements=amendment_distinct_movements,
                amendment_search_stage=amendment_search_stage,
                amendment_incumbent_candidate=amendment_incumbent_candidate,
                allow_relaxed_fallback=False,
                relaxed=True,
                _deadline=deadline,
                _search_started=search_started,
            )
            if relaxed_candidates:
                return relaxed_candidates, _candidate_failure_reasons(
                    config,
                    participants,
                    headline=(
                        "必須条件を満たす候補が存在しないことを確認したため、"
                        "近似候補を表示しています。"
                    ),
                    blocked_slots_by_participant=blocked_slots_by_participant,
                )
            return [], _candidate_failure_reasons(
                config,
                participants,
                headline=(
                    "必須条件を満たす候補が存在せず、ハード制約を守る"
                    "近似候補も見つかりませんでした。"
                ),
                blocked_slots_by_participant=blocked_slots_by_participant,
            )
        return [], _candidate_failure_reasons(
            config,
            participants,
            headline=(
                "現在のハード制約を守る近似候補が存在しません。"
                if relaxed
                else "必須条件を満たす候補が存在しません。"
            ),
            blocked_slots_by_participant=blocked_slots_by_participant,
        )

    model = cp_model.CpModel()
    evaluation_settings = normalize_evaluation_settings(
        config.evaluation_settings
    )
    if amendment_base_schedule is not None:
        evaluation_settings = {
            evaluation_id: {
                **setting,
                "enabled": False,
            }
            for evaluation_id, setting in evaluation_settings.items()
        }
    known_cohorts = [
        participant.cohort
        for participant in target
        if not participant.is_role_unspecified and participant.cohort is not None
    ]
    latest_cohort = max(known_cohorts) if known_cohorts else None
    experienced_participant_ids = {
        participant.id
        for participant in target
        if (
            not participant.is_role_unspecified
            and participant.cohort is not None
            and latest_cohort is not None
            and participant.cohort < latest_cohort
        )
    }
    active_vars: dict[tuple[str, int], cp_model.IntVar] = {}
    zoom_vars: dict[tuple[str, int], cp_model.IntVar] = {}
    university_vars: dict[tuple[str, int, str], cp_model.IntVar] = {}
    high_school_vars: dict[tuple[str, int, str], cp_model.IntVar] = {}
    evaluation_terms: dict[str, list[tuple[cp_model.IntVar, int]]] = defaultdict(
        list
    )
    mandatory_violation_vars: list[cp_model.IntVar] = []
    default_extra_terms: list[tuple[cp_model.IntVar, int]] = []

    for slot_key in feasible_slots:
        _slot_day, slot_period = parse_slot_key(slot_key)
        member_ids = members_by_slot[slot_key]
        for group_index in range(config.max_groups_per_slot):
            active = model.new_bool_var(f"active_{slot_key}_{group_index}")
            is_zoom = model.new_bool_var(f"zoom_{slot_key}_{group_index}")
            active_vars[(slot_key, group_index)] = active
            zoom_vars[(slot_key, group_index)] = is_zoom
            model.add(is_zoom <= active)
            university_group_vars = []
            high_school_group_vars = []
            zoom_only_group_vars = []
            field_pair_mismatches: list[cp_model.IntVar] = []
            for participant_id in member_ids:
                university = model.new_bool_var(
                    f"u_{participant_id}_{slot_key}_{group_index}"
                )
                high_school = model.new_bool_var(
                    f"h_{participant_id}_{slot_key}_{group_index}"
                )
                university_vars[(slot_key, group_index, participant_id)] = university
                high_school_vars[(slot_key, group_index, participant_id)] = high_school
                model.add(university + high_school <= 1)
                if slot_key in zoom_only_availability.get(participant_id, set()):
                    model.add(university <= is_zoom)
                    model.add(high_school <= is_zoom)
                    zoom_only_group_vars.extend([university, high_school])
                university_group_vars.append(university)
                high_school_group_vars.append(high_school)
            if relaxed:
                university_group_shortfall = model.new_int_var(
                    0,
                    config.university_role_size,
                    f"u_group_shortfall_{slot_key}_{group_index}",
                )
                high_school_group_shortfall = model.new_int_var(
                    0,
                    config.high_school_role_size,
                    f"h_group_shortfall_{slot_key}_{group_index}",
                )
                model.add(
                    sum(university_group_vars) + university_group_shortfall
                    == config.university_role_size * active
                )
                model.add(
                    sum(high_school_group_vars) + high_school_group_shortfall
                    == config.high_school_role_size * active
                )
                # A relaxed session may be smaller than the configured role
                # sizes, but neither role may be empty.
                model.add(sum(university_group_vars) >= active)
                model.add(sum(high_school_group_vars) >= active)
                mandatory_violation_vars.extend(
                    [university_group_shortfall, high_school_group_shortfall]
                )
            else:
                model.add(
                    sum(university_group_vars)
                    == config.university_role_size * active
                )
                model.add(
                    sum(high_school_group_vars)
                    == config.high_school_role_size * active
                )
            cohort_setting = evaluation_settings.get("cohort_balance", {})
            if cohort_setting.get("enabled") and experienced_participant_ids:
                experienced_university_vars = [
                    university_vars[(slot_key, group_index, participant_id)]
                    for participant_id in member_ids
                    if participant_id in experienced_participant_ids
                ]
                experienced_high_school_vars = [
                    high_school_vars[(slot_key, group_index, participant_id)]
                    for participant_id in member_ids
                    if participant_id in experienced_participant_ids
                ]
                has_experienced_university = model.new_bool_var(
                    f"experienced_u_{slot_key}_{group_index}"
                )
                has_experienced_high_school = model.new_bool_var(
                    f"experienced_h_{slot_key}_{group_index}"
                )
                if experienced_university_vars:
                    model.add(
                        sum(experienced_university_vars)
                        >= has_experienced_university
                    )
                    model.add(
                        sum(experienced_university_vars)
                        <= config.university_role_size
                        * has_experienced_university
                    )
                else:
                    model.add(has_experienced_university == 0)
                if experienced_high_school_vars:
                    model.add(
                        sum(experienced_high_school_vars)
                        >= has_experienced_high_school
                    )
                    model.add(
                        sum(experienced_high_school_vars)
                        <= config.high_school_role_size
                        * has_experienced_high_school
                    )
                else:
                    model.add(has_experienced_high_school == 0)
                has_any_experienced = model.new_bool_var(
                    f"experienced_any_{slot_key}_{group_index}"
                )
                model.add(has_any_experienced >= has_experienced_university)
                model.add(has_any_experienced >= has_experienced_high_school)
                model.add(
                    has_any_experienced
                    <= has_experienced_university + has_experienced_high_school
                )
                both_roles_experienced = model.new_bool_var(
                    f"experienced_both_{slot_key}_{group_index}"
                )
                model.add(both_roles_experienced <= has_experienced_university)
                model.add(both_roles_experienced <= has_experienced_high_school)
                model.add(
                    both_roles_experienced
                    >= has_experienced_university
                    + has_experienced_high_school
                    - 1
                )
                latest_only = model.new_bool_var(
                    f"latest_only_{slot_key}_{group_index}"
                )
                missing_role_split = model.new_bool_var(
                    f"experienced_split_missing_{slot_key}_{group_index}"
                )
                model.add(latest_only == active - has_any_experienced)
                model.add(missing_role_split == active - both_roles_experienced)
                evaluation_terms["cohort_balance"].extend(
                    [
                        (latest_only, 1),
                        (latest_only, 1),
                        (missing_role_split, 1),
                    ]
                )
            regular_group_presence: list[cp_model.IntVar] = []
            if evaluation_settings.get("same_group", {}).get("enabled"):
                for group_number in range(1, config.group_count + 1):
                    group_university_vars = [
                        university_vars[
                            (
                                slot_key,
                                group_index,
                                participant_id,
                            )
                        ]
                        for participant_id in member_ids
                        if participant_id in {
                            participant.id
                            for participant in target
                            if participant.group_number == group_number
                        }
                    ]
                    if not group_university_vars:
                        continue
                    presence = model.new_bool_var(
                        f"group_{group_number}_{slot_key}_{group_index}"
                    )
                    model.add(sum(group_university_vars) >= presence)
                    model.add(
                        sum(group_university_vars)
                        <= config.university_role_size * presence
                    )
                    regular_group_presence.append(presence)

                if len(regular_group_presence) >= 2:
                    group_violation = model.new_bool_var(
                        f"group_mismatch_{slot_key}_{group_index}"
                    )
                    model.add(
                        sum(regular_group_presence)
                        <= 1 + config.group_count * group_violation
                    )
                    evaluation_terms["same_group"].append(
                        (group_violation, 1)
                    )

            if evaluation_settings.get("field_match", {}).get("enabled"):
                for participant_id in member_ids:
                    participant = next(
                        item
                        for item in target
                        if item.id == participant_id
                    )
                    if participant.is_role_unspecified:
                        continue
                    assigned_field = config.group_field_assignments.get(
                        str(participant.group_number),
                        "文理混合",
                    )
                    if assigned_field == "文理混合":
                        continue
                    university = university_vars[
                        (slot_key, group_index, participant_id)
                    ]
                    for high_school_id in member_ids:
                        high_school_participant = next(
                            item
                            for item in target
                            if item.id == high_school_id
                        )
                        if (
                            high_school_participant.humanities_or_science
                            == assigned_field
                        ):
                            continue
                        high_school = high_school_vars[
                            (
                                slot_key,
                                group_index,
                                high_school_id,
                            )
                        ]
                        mismatch = model.new_bool_var(
                            f"field_mismatch_{participant_id}_"
                            f"{high_school_id}_{slot_key}_{group_index}"
                        )
                        model.add(
                            mismatch >= university + high_school - 1
                        )
                        field_pair_mismatches.append(mismatch)
                if field_pair_mismatches:
                    field_violation = model.new_bool_var(
                        f"field_mismatch_{slot_key}_{group_index}"
                    )
                    for mismatch in field_pair_mismatches:
                        model.add(field_violation >= mismatch)
                    evaluation_terms["field_match"].append(
                        (field_violation, 1)
                    )
            if group_index:
                model.add(
                    active_vars[(slot_key, group_index - 1)] >= active
                )
            zoom_setting = evaluation_settings.get("zoom_meeting", {})
            if (
                zoom_setting.get("enabled")
                and zoom_setting.get("policy") != "ignore"
                and not _is_zoom_penalty_exempt_period(slot_period)
            ):
                if zoom_setting.get("policy") == "avoid_unless_needed":
                    required_people = (
                        config.university_role_size + config.high_school_role_size
                    )
                    sufficient_zoom_only = model.new_bool_var(
                        f"zoom_sufficient_{slot_key}_{group_index}"
                    )
                    zoom_only_count = sum(zoom_only_group_vars)
                    model.add(
                        zoom_only_count * 2 >= required_people
                    ).only_enforce_if(sufficient_zoom_only)
                    model.add(
                        zoom_only_count * 2 <= required_people - 1
                    ).only_enforce_if(sufficient_zoom_only.Not())
                    avoidable_zoom = model.new_bool_var(
                        f"avoidable_zoom_{slot_key}_{group_index}"
                    )
                    model.add(avoidable_zoom <= is_zoom)
                    model.add(avoidable_zoom <= sufficient_zoom_only.Not())
                    model.add(avoidable_zoom >= is_zoom - sufficient_zoom_only)
                    evaluation_terms["zoom_meeting"].append((avoidable_zoom, 1))
                else:
                    evaluation_terms["zoom_meeting"].append((is_zoom, 1))

    amendment_assignment_presence_vars: dict[
        tuple[str, str], cp_model.IntVar
    ] = {}
    for participant in target:
        university_for_person = []
        high_school_for_person = []
        total_for_person = []
        by_day: dict[str, list[cp_model.IntVar]] = defaultdict(list)
        by_slot: dict[str, list[cp_model.IntVar]] = defaultdict(list)
        for slot_key in feasible_slots:
            if slot_key not in availability[participant.id]:
                continue
            day, _ = parse_slot_key(slot_key)
            for group_index in range(config.max_groups_per_slot):
                key = (slot_key, group_index, participant.id)
                if key not in university_vars:
                    continue
                university = university_vars[key]
                high_school = high_school_vars[key]
                university_for_person.append(university)
                high_school_for_person.append(high_school)
                total_for_person.extend([university, high_school])
                by_day[day.isoformat()].extend([university, high_school])
                by_slot[slot_key].extend([university, high_school])
        for slot_key, slot_vars in by_slot.items():
            model.add(sum(slot_vars) <= 1)
            if amendment_base_schedule is not None:
                assigned = model.new_bool_var(
                    f"amendment_assigned_{participant.id}_{slot_key}"
                )
                model.add(assigned == sum(slot_vars))
                amendment_assignment_presence_vars[
                    (participant.id, slot_key)
                ] = assigned
        for day_vars in by_day.values():
            model.add(sum(day_vars) <= config.max_sessions_per_person_per_day)
        participant_schedule_setting = evaluation_settings.get(
            "participant_schedule", {}
        )
        if participant_schedule_setting.get("enabled"):
            for day_text, day_vars in by_day.items():
                if participant_schedule_setting.get("policy") == "separate_days":
                    extra_same_day = model.new_int_var(
                        0,
                        max(0, config.max_sessions_per_person_per_day - 1),
                        f"same_day_extra_{participant.id}_{day_text}",
                    )
                    model.add(extra_same_day >= sum(day_vars) - 1)
                    evaluation_terms["participant_schedule"].append(
                        (
                            extra_same_day,
                            max(0, config.max_sessions_per_person_per_day - 1),
                        )
                    )
                else:
                    day_used = model.new_bool_var(
                        f"participant_day_used_{participant.id}_{day_text}"
                    )
                    model.add(sum(day_vars) >= day_used)
                    model.add(sum(day_vars) <= len(day_vars) * day_used)
                    evaluation_terms["participant_schedule"].append(
                        (day_used, 1)
                    )
        if config.avoid_consecutive_periods:
            slots_by_day: dict[str, dict[int, list[cp_model.IntVar]]] = defaultdict(
                lambda: defaultdict(list)
            )
            for slot_key, slot_vars in by_slot.items():
                day, period = parse_slot_key(slot_key)
                slots_by_day[day.isoformat()][period].extend(slot_vars)
            for period_map in slots_by_day.values():
                for period in sorted(period_map):
                    if period + 1 in period_map:
                        model.add(
                            sum(period_map[period] + period_map[period + 1]) <= 1
                        )
        university_required = participant.university_requirement(config)
        high_school_required = participant.high_school_requirement(config)
        total_required = participant.total_requirement(config)
        required_total = (
            total_required
            if total_required
            else university_required + high_school_required
        )
        if not participant.is_role_unspecified:
            maximum_extra = max(0, len(total_for_person) - required_total)
            extra_participation = model.new_int_var(
                0,
                maximum_extra,
                f"default_extra_{participant.id}",
            )
            model.add(
                extra_participation
                >= sum(total_for_person) - required_total
            )
            default_extra_terms.append(
                (extra_participation, maximum_extra)
            )
        if relaxed:
            university_shortfall = model.new_int_var(
                0, university_required, f"u_shortfall_{participant.id}"
            )
            high_school_shortfall = model.new_int_var(
                0, high_school_required, f"h_shortfall_{participant.id}"
            )
            model.add(
                sum(university_for_person) + university_shortfall
                >= university_required
            )
            model.add(
                sum(high_school_for_person) + high_school_shortfall
                >= high_school_required
            )
            total_shortfall = model.new_int_var(
                0, total_required, f"total_shortfall_{participant.id}"
            )
            model.add(
                sum(total_for_person) + total_shortfall >= total_required
            )
            model.add(
                sum(total_for_person)
                <= participant.participation_limit(config)
            )
            mandatory_violation_vars.extend(
                [
                    university_shortfall,
                    high_school_shortfall,
                    total_shortfall,
                ]
            )
        else:
            model.add(sum(university_for_person) >= university_required)
            model.add(sum(high_school_for_person) >= high_school_required)
            model.add(sum(total_for_person) >= total_required)
            model.add(
                sum(total_for_person)
                <= participant.participation_limit(config)
            )
    amendment_non_requester_changed_vars: list[cp_model.IntVar] = []
    amendment_non_requester_difference_vars: list[cp_model.IntVar] = []
    amendment_requester_difference_vars: list[cp_model.IntVar] = []
    amendment_non_requester_difference_constant = 0
    amendment_requester_difference_constant = 0
    if amendment_base_schedule is not None:
        base_slots_by_participant: dict[str, set[str]] = defaultdict(set)
        for session in amendment_base_schedule.get("sessions", []):
            try:
                slot_key = make_slot_key(
                    str(session.get("date", "")),
                    int(session.get("period", 0)),
                )
            except (TypeError, ValueError):
                continue
            for participant_id in (
                list(session.get("university_role_member_ids", []))
                + list(session.get("high_school_role_member_ids", []))
            ):
                base_slots_by_participant[str(participant_id)].add(slot_key)

        fixed_amendment_ids = {
            str(value) for value in (amendment_fixed_participant_ids or set())
        }
        amendment_constraint_errors: list[str] = []
        for participant in target:
            participant_id = participant.id
            base_slots = base_slots_by_participant.get(participant_id, set())
            candidate_presence = {
                slot_key: variable
                for (target_id, slot_key), variable
                in amendment_assignment_presence_vars.items()
                if target_id == participant_id
            }
            unavailable_base_slots = base_slots - set(candidate_presence)
            if (
                participant_id in fixed_amendment_ids
                and unavailable_base_slots
            ):
                amendment_constraint_errors.append(
                    f"{participant.name}さんの現在日時を固定できません。"
                )
                continue

            difference_vars: list[cp_model.IntVar] = []
            for slot_key, assigned in candidate_presence.items():
                if slot_key in base_slots:
                    difference = model.new_bool_var(
                        f"amendment_removed_{participant_id}_{slot_key}"
                    )
                    model.add(difference + assigned == 1)
                else:
                    difference = assigned
                difference_vars.append(difference)
                if participant_id in fixed_amendment_ids:
                    model.add(assigned == int(slot_key in base_slots))

            missing_difference_count = len(unavailable_base_slots)
            if participant_id in normalized_amendment_requester_ids:
                amendment_requester_difference_vars.extend(difference_vars)
                amendment_requester_difference_constant += (
                    missing_difference_count
                )
                continue

            amendment_non_requester_difference_vars.extend(difference_vars)
            amendment_non_requester_difference_constant += (
                missing_difference_count
            )
            changed = model.new_bool_var(
                f"amendment_changed_{participant_id}"
            )
            if missing_difference_count:
                model.add(changed == 1)
            elif difference_vars:
                for difference in difference_vars:
                    model.add(changed >= difference)
                model.add(changed <= sum(difference_vars))
            else:
                model.add(changed == 0)
            amendment_non_requester_changed_vars.append(changed)
        if amendment_constraint_errors:
            return [], list(dict.fromkeys(amendment_constraint_errors))[:10]

    fixed_session_ids_by_key: dict[tuple[str, int], str] = {}
    fixed_session_errors: list[str] = []
    seen_fixed_keys: set[tuple[str, int]] = set()
    target_ids = {participant.id for participant in target}
    for fixed_index, fixed_session in enumerate(fixed_sessions or [], start=1):
        try:
            slot_key = make_slot_key(
                str(fixed_session.get("date", "")),
                int(fixed_session.get("period", 0)),
            )
            group_index = int(fixed_session.get("group_index", 1)) - 1
        except (TypeError, ValueError):
            fixed_session_errors.append(
                f"固定日程{fixed_index}の日付・時限・組番号が不正です。"
            )
            continue
        fixed_key = (slot_key, group_index)
        if fixed_key in seen_fixed_keys:
            fixed_session_errors.append(
                f"固定日程{fixed_index}の日時・組が重複しています。"
            )
            continue
        seen_fixed_keys.add(fixed_key)
        active = active_vars.get(fixed_key)
        zoom = zoom_vars.get(fixed_key)
        if active is None or zoom is None:
            fixed_session_errors.append(
                f"固定日程{fixed_index}は現在の対象期間・時限・組数では利用できません。"
            )
            continue
        university_ids = {
            str(value)
            for value in fixed_session.get("university_role_member_ids", [])
        }
        high_school_ids = {
            str(value)
            for value in fixed_session.get("high_school_role_member_ids", [])
        }
        unknown_ids = (university_ids | high_school_ids) - target_ids
        if unknown_ids:
            fixed_session_errors.append(
                f"固定日程{fixed_index}に現在の日調対象外の参加者が含まれています。"
            )
            continue
        if university_ids & high_school_ids:
            fixed_session_errors.append(
                f"固定日程{fixed_index}で同じ参加者が複数役割に割り当てられています。"
            )
            continue
        if len(university_ids) != config.university_role_size or len(
            high_school_ids
        ) != config.high_school_role_size:
            fixed_session_errors.append(
                f"固定日程{fixed_index}の役割人数が現在の設定と一致しません。"
            )
            continue
        meeting_mode = str(
            fixed_session.get("meeting_mode", MEETING_MODE_IN_PERSON)
        )
        if meeting_mode not in {MEETING_MODE_IN_PERSON, MEETING_MODE_ZOOM}:
            fixed_session_errors.append(
                f"固定日程{fixed_index}の開催形式が不正です。"
            )
            continue
        model.add(active == 1)
        model.add(zoom == int(meeting_mode == MEETING_MODE_ZOOM))
        for participant in target:
            key = (slot_key, group_index, participant.id)
            university = university_vars.get(key)
            high_school = high_school_vars.get(key)
            if participant.id in university_ids and university is None:
                fixed_session_errors.append(
                    f"固定日程{fixed_index}で{participant.name}さんを大学生役に固定できません。"
                )
                continue
            if participant.id in high_school_ids and high_school is None:
                fixed_session_errors.append(
                    f"固定日程{fixed_index}で{participant.name}さんを高校生役に固定できません。"
                )
                continue
            if university is not None:
                model.add(university == int(participant.id in university_ids))
            if high_school is not None:
                model.add(high_school == int(participant.id in high_school_ids))
        session_id = str(
            fixed_session.get("session_id")
            or fixed_session.get("session_uid")
            or ""
        )
        if session_id:
            fixed_session_ids_by_key[fixed_key] = session_id
    if fixed_session_errors:
        return [], list(dict.fromkeys(fixed_session_errors))[:10]

    assignment_lock_errors: list[str] = []
    seen_assignment_lock_keys: set[tuple[str, int]] = set()
    locked_participant_ids_by_slot: dict[str, set[str]] = defaultdict(set)
    locked_university_ids_by_slot: dict[str, set[str]] = defaultdict(set)
    locked_high_school_ids_by_slot: dict[str, set[str]] = defaultdict(set)
    assignment_lock_count = 0
    for lock_index, assignment_lock in enumerate(
        assignment_locks or [],
        start=1,
    ):
        try:
            slot_key = make_slot_key(
                str(assignment_lock.get("date", "")),
                int(assignment_lock.get("period", 0)),
            )
            group_index = int(assignment_lock.get("group_index", 1)) - 1
        except (TypeError, ValueError):
            assignment_lock_errors.append(
                f"部分固定{lock_index}の日付・時限・組番号が不正です。"
            )
            continue
        lock_key = (slot_key, group_index)
        if lock_key in seen_assignment_lock_keys:
            assignment_lock_errors.append(
                f"部分固定{lock_index}の日時・組が重複しています。"
            )
            continue
        seen_assignment_lock_keys.add(lock_key)
        active = active_vars.get(lock_key)
        zoom = zoom_vars.get(lock_key)
        if active is None or zoom is None:
            assignment_lock_errors.append(
                f"部分固定{lock_index}は現在の対象期間・時限・組数では"
                "利用できません。"
            )
            continue
        university_ids = {
            str(value)
            for value in assignment_lock.get(
                "university_role_member_ids",
                assignment_lock.get(
                    "locked_university_role_member_ids",
                    [],
                ),
            )
        }
        high_school_ids = {
            str(value)
            for value in assignment_lock.get(
                "high_school_role_member_ids",
                assignment_lock.get(
                    "locked_high_school_role_member_ids",
                    [],
                ),
            )
        }
        role_locked_university_ids = {
            str(value)
            for value in assignment_lock.get(
                "role_locked_university_participant_ids",
                [],
            )
        }
        role_locked_high_school_ids = {
            str(value)
            for value in assignment_lock.get(
                "role_locked_high_school_participant_ids",
                [],
            )
        }
        participant_ids = {
            str(value)
            for value in assignment_lock.get(
                "participant_ids",
                assignment_lock.get("locked_participant_ids", []),
            )
        }
        date_only_ids = (
            participant_ids
            - university_ids
            - high_school_ids
            - role_locked_university_ids
            - role_locked_high_school_ids
        )
        all_locked_ids = (
            university_ids
            | high_school_ids
            | role_locked_university_ids
            | role_locked_high_school_ids
            | date_only_ids
        )
        lock_session = bool(
            assignment_lock.get(
                "lock_session",
                not participant_ids,
            )
        )
        unknown_ids = all_locked_ids - target_ids
        if unknown_ids:
            assignment_lock_errors.append(
                f"部分固定{lock_index}に現在の日調対象外の参加者が"
                "含まれています。"
            )
            continue
        all_university_ids = (
            university_ids | role_locked_university_ids
        )
        all_high_school_ids = (
            high_school_ids | role_locked_high_school_ids
        )
        if all_university_ids & all_high_school_ids:
            assignment_lock_errors.append(
                f"部分固定{lock_index}で同じ参加者が複数役割に"
                "割り当てられています。"
            )
            continue
        if (
            len(
                locked_university_ids_by_slot[slot_key]
                | all_university_ids
            )
            > config.university_role_size * config.max_groups_per_slot
            or len(
                locked_high_school_ids_by_slot[slot_key]
                | all_high_school_ids
            )
            > config.high_school_role_size * config.max_groups_per_slot
        ):
            assignment_lock_errors.append(
                f"部分固定{lock_index}で、この日時に役割固定する人数が"
                "同時開催できる役割人数を超えています。"
            )
            continue
        if len(university_ids) > config.university_role_size or len(
            high_school_ids
        ) > config.high_school_role_size:
            assignment_lock_errors.append(
                f"部分固定{lock_index}の固定人数が現在の役割人数を"
                "超えています。"
            )
            continue
        duplicate_in_slot = (
            all_locked_ids & locked_participant_ids_by_slot[slot_key]
        )
        if duplicate_in_slot:
            assignment_lock_errors.append(
                f"部分固定{lock_index}で同じ参加者が同一コマの"
                "複数組に固定されています。"
            )
            continue
        if (
            len(
                locked_participant_ids_by_slot[slot_key]
                | all_locked_ids
            )
            > (
                config.university_role_size
                + config.high_school_role_size
            )
            * config.max_groups_per_slot
        ):
            assignment_lock_errors.append(
                f"部分固定{lock_index}で、この日時に固定する参加者数が"
                "同時開催できる人数を超えています。"
            )
            continue
        meeting_mode_value = assignment_lock.get("meeting_mode")
        meeting_mode = (
            str(meeting_mode_value)
            if meeting_mode_value not in {None, ""}
            else ""
        )
        if meeting_mode and meeting_mode not in {
            MEETING_MODE_IN_PERSON,
            MEETING_MODE_ZOOM,
        }:
            assignment_lock_errors.append(
                f"部分固定{lock_index}の開催形式が不正です。"
            )
            continue
        if meeting_mode == MEETING_MODE_IN_PERSON and any(
            slot_key not in in_person_availability.get(participant_id, set())
            for participant_id in university_ids | high_school_ids
        ):
            assignment_lock_errors.append(
                f"部分固定{lock_index}には対面参加できない参加者が"
                "含まれています。"
            )
            continue
        missing_university_ids = {
            participant_id
            for participant_id in university_ids
            if (
                slot_key,
                group_index,
                participant_id,
            )
            not in university_vars
        }
        missing_high_school_ids = {
            participant_id
            for participant_id in high_school_ids
            if (
                slot_key,
                group_index,
                participant_id,
            )
            not in high_school_vars
        }
        if missing_university_ids:
            assignment_lock_errors.append(
                f"部分固定{lock_index}の参加者を大学生役に固定できません。"
            )
            continue
        if missing_high_school_ids:
            assignment_lock_errors.append(
                f"部分固定{lock_index}の参加者を高校生役に固定できません。"
            )
            continue
        missing_role_locked_university_ids = {
            participant_id
            for participant_id in role_locked_university_ids
            if not any(
                (
                    slot_key,
                    candidate_group_index,
                    participant_id,
                )
                in university_vars
                for candidate_group_index in range(
                    config.max_groups_per_slot
                )
            )
        }
        missing_role_locked_high_school_ids = {
            participant_id
            for participant_id in role_locked_high_school_ids
            if not any(
                (
                    slot_key,
                    candidate_group_index,
                    participant_id,
                )
                in high_school_vars
                for candidate_group_index in range(
                    config.max_groups_per_slot
                )
            )
        }
        if missing_role_locked_university_ids:
            assignment_lock_errors.append(
                f"部分固定{lock_index}の参加者をこの日時の大学生役に"
                "固定できません。"
            )
            continue
        if missing_role_locked_high_school_ids:
            assignment_lock_errors.append(
                f"部分固定{lock_index}の参加者をこの日時の高校生役に"
                "固定できません。"
            )
            continue
        missing_date_only_ids = {
            participant_id
            for participant_id in date_only_ids
            if not any(
                (
                    slot_key,
                    candidate_group_index,
                    participant_id,
                )
                in university_vars
                for candidate_group_index in range(
                    config.max_groups_per_slot
                )
            )
        }
        if missing_date_only_ids:
            assignment_lock_errors.append(
                f"部分固定{lock_index}の参加者をこの日時に固定できません。"
            )
            continue
        group_is_anchored = bool(
            lock_session
            or meeting_mode
            or university_ids
            or high_school_ids
        )
        if group_is_anchored:
            model.add(active == 1)
        if meeting_mode:
            model.add(zoom == int(meeting_mode == MEETING_MODE_ZOOM))
        for participant_id in university_ids:
            model.add(
                university_vars[(slot_key, group_index, participant_id)] == 1
            )
        for participant_id in high_school_ids:
            model.add(
                high_school_vars[(slot_key, group_index, participant_id)] == 1
            )
        for participant_id in role_locked_university_ids:
            model.add(
                sum(
                    university_vars[
                        (
                            slot_key,
                            candidate_group_index,
                            participant_id,
                        )
                    ]
                    for candidate_group_index in range(
                        config.max_groups_per_slot
                    )
                )
                == 1
            )
        for participant_id in role_locked_high_school_ids:
            model.add(
                sum(
                    high_school_vars[
                        (
                            slot_key,
                            candidate_group_index,
                            participant_id,
                        )
                    ]
                    for candidate_group_index in range(
                        config.max_groups_per_slot
                    )
                )
                == 1
            )
        for participant_id in date_only_ids:
            date_assignment_vars: list[cp_model.IntVar] = []
            for candidate_group_index in range(
                config.max_groups_per_slot
            ):
                key = (
                    slot_key,
                    candidate_group_index,
                    participant_id,
                )
                university = university_vars.get(key)
                high_school = high_school_vars.get(key)
                if university is not None and high_school is not None:
                    date_assignment_vars.extend(
                        [university, high_school]
                    )
            model.add(sum(date_assignment_vars) == 1)
        locked_participant_ids_by_slot[slot_key].update(
            all_locked_ids
        )
        locked_university_ids_by_slot[slot_key].update(
            all_university_ids
        )
        locked_high_school_ids_by_slot[slot_key].update(
            all_high_school_ids
        )
        assignment_lock_count += len(all_locked_ids)
        session_id = str(
            assignment_lock.get("session_id")
            or assignment_lock.get("session_uid")
            or ""
        )
        if session_id and group_is_anchored:
            fixed_session_ids_by_key.setdefault(lock_key, session_id)
    if assignment_lock_errors:
        return [], list(dict.fromkeys(assignment_lock_errors))[:10]

    active_list = list(active_vars.values())
    evaluation_terms["session_count"].extend(
        (active, 1) for active in active_list
    )
    overall_schedule_setting = evaluation_settings.get("overall_schedule", {})
    if overall_schedule_setting.get("enabled") and active_list:
        active_by_day: dict[str, list[cp_model.IntVar]] = defaultdict(list)
        for (slot_key, _group_index), active in active_vars.items():
            session_day, _period = parse_slot_key(slot_key)
            active_by_day[session_day.isoformat()].append(active)
        schedule_day_used: dict[str, cp_model.IntVar] = {}
        for day_text, day_vars in active_by_day.items():
            day_used = model.new_bool_var(f"schedule_day_used_{day_text}")
            model.add(sum(day_vars) >= day_used)
            model.add(sum(day_vars) <= len(day_vars) * day_used)
            schedule_day_used[day_text] = day_used
            if overall_schedule_setting.get("policy") == "concentrate":
                evaluation_terms["overall_schedule"].append((day_used, 1))
            else:
                extra_same_day = model.new_int_var(
                    0,
                    max(0, len(day_vars) - 1),
                    f"schedule_same_day_extra_{day_text}",
                )
                model.add(extra_same_day >= sum(day_vars) - day_used)
                evaluation_terms["overall_schedule"].append(
                    (extra_same_day, max(0, len(day_vars) - 1))
                )
        if (
            overall_schedule_setting.get("policy") == "spread"
            and len(schedule_day_used) >= 2
        ):
            start_day = min(date.fromisoformat(day) for day in schedule_day_used)
            day_offsets = {
                day_text: (date.fromisoformat(day_text) - start_day).days
                for day_text in schedule_day_used
            }
            last_offset = max(day_offsets.values())
            if last_offset > 0:
                any_session = model.new_bool_var("schedule_has_session")
                model.add(sum(active_list) >= any_session)
                model.add(sum(active_list) <= len(active_list) * any_session)
                latest_used = model.new_int_var(
                    0, last_offset, "schedule_latest_day"
                )
                reverse_latest_used = model.new_int_var(
                    0, last_offset, "schedule_reverse_latest_day"
                )
                model.add_max_equality(
                    latest_used,
                    [
                        day_offsets[day_text] * day_used
                        for day_text, day_used in schedule_day_used.items()
                    ],
                )
                model.add_max_equality(
                    reverse_latest_used,
                    [
                        (last_offset - day_offsets[day_text]) * day_used
                        for day_text, day_used in schedule_day_used.items()
                    ],
                )
                span_shortfall = model.new_int_var(
                    0, last_offset, "schedule_span_shortfall"
                )
                model.add(
                    span_shortfall
                    == 2 * last_offset * any_session
                    - latest_used
                    - reverse_latest_used
                )
                evaluation_terms["overall_schedule"].append(
                    (span_shortfall, last_offset)
                )
    if config.performance_date and config.performance_avoid_days > 0:
        performance_day = date.fromisoformat(config.performance_date)
        for (slot_key, _), active in active_vars.items():
            session_day, _ = parse_slot_key(slot_key)
            days_before = (performance_day - session_day).days
            if 1 <= days_before <= config.performance_avoid_days:
                evaluation_terms["performance_buffer"].append((active, 1))
    for (slot_key, _), active in active_vars.items():
        _, period = parse_slot_key(slot_key)
        if period in config.avoided_periods:
            evaluation_terms["avoid_periods"].append((active, 1))
    if relaxed and active_list:
        model.add(sum(active_list) >= 1)

    evaluation_expression, evaluation_upper_bound = _evaluation_solver_objective(
        model,
        evaluation_terms,
        evaluation_settings,
    )
    default_extra_expression = sum(
        variable for variable, _maximum in default_extra_terms
    )
    default_extra_upper_bound = sum(
        maximum for _variable, maximum in default_extra_terms
    )
    default_extra_weight = evaluation_upper_bound + 1
    relaxed_violation_expression = sum(mandatory_violation_vars)
    normal_selection_objective = (
        default_extra_weight * default_extra_expression
        + evaluation_expression
    )
    amendment_objective_strategy = ""
    if relaxed:
        mandatory_weight = (
            default_extra_upper_bound * default_extra_weight
            + evaluation_upper_bound
            + 1
        )
        model.minimize(
            mandatory_weight * relaxed_violation_expression
            + default_extra_weight * default_extra_expression
            + evaluation_expression
        )
    elif amendment_base_schedule is not None:
        requester_difference_expression = (
            sum(amendment_requester_difference_vars)
            + amendment_requester_difference_constant
        )
        requester_difference_upper_bound = (
            len(amendment_requester_difference_vars)
            + amendment_requester_difference_constant
        )
        non_requester_difference_expression = (
            sum(amendment_non_requester_difference_vars)
            + amendment_non_requester_difference_constant
        )
        non_requester_difference_upper_bound = (
            len(amendment_non_requester_difference_vars)
            + amendment_non_requester_difference_constant
        )
        requester_difference_weight = 1
        non_requester_difference_weight = (
            requester_difference_upper_bound
            + 1
        )
        non_requester_changed_weight = (
            non_requester_difference_upper_bound
            * non_requester_difference_weight
            + requester_difference_upper_bound
            + 1
        )
        movement_objective = (
            non_requester_changed_weight
            * sum(amendment_non_requester_changed_vars)
            + non_requester_difference_weight
            * non_requester_difference_expression
            + requester_difference_weight
            * requester_difference_expression
        )
        model.minimize(movement_objective)
        incumbent_objective_bound: int | None = None
        if (
            amendment_incumbent_candidate is not None
            and _validate_candidate(
                amendment_incumbent_candidate,
                config,
                target,
            )
        ):
            incumbent_metrics = amendment_movement_metrics(
                amendment_base_schedule,
                amendment_incumbent_candidate,
                normalized_amendment_requester_ids,
            )
            incumbent_objective_bound = (
                non_requester_changed_weight
                * int(
                    incumbent_metrics[
                        "amendment_non_requester_changed_count"
                    ]
                )
                + non_requester_difference_weight
                * int(
                    incumbent_metrics[
                        "amendment_non_requester_slot_deviation"
                    ]
                )
                + requester_difference_weight
                * int(
                    incumbent_metrics[
                        "amendment_requester_slot_deviation"
                    ]
                )
            )
            model.add(movement_objective <= incumbent_objective_bound)
            for session in amendment_incumbent_candidate.get(
                "sessions",
                [],
            ):
                try:
                    slot_key = make_slot_key(
                        str(session.get("date", "")),
                        int(session.get("period", 0)),
                    )
                    group_index = int(
                        session.get("group_index", 1)
                    ) - 1
                except (TypeError, ValueError):
                    continue
                session_key = (slot_key, group_index)
                active = active_vars.get(session_key)
                if active is not None:
                    model.add_hint(active, 1)
                zoom = zoom_vars.get(session_key)
                if zoom is not None:
                    model.add_hint(
                        zoom,
                        int(
                            session.get("meeting_mode")
                            == MEETING_MODE_ZOOM
                        ),
                    )
                for participant_id in session.get(
                    "university_role_member_ids",
                    [],
                ):
                    variable = university_vars.get(
                        (slot_key, group_index, str(participant_id))
                    )
                    if variable is not None:
                        model.add_hint(variable, 1)
                for participant_id in session.get(
                    "high_school_role_member_ids",
                    [],
                ):
                    variable = high_school_vars.get(
                        (slot_key, group_index, str(participant_id))
                    )
                    if variable is not None:
                        model.add_hint(variable, 1)
        validation_error = model.validate().strip()
        if validation_error:
            return [], [
                "改訂探索の内部モデルを作成できませんでした。"
                f"詳細: {validation_error.splitlines()[0]}"
            ]
        amendment_objective_strategy = "movement_only"
    else:
        model.minimize(normal_selection_objective)
    for excluded_candidate in excluded_candidates or []:
        excluded_vars: list[cp_model.IntVar] = []
        for session in excluded_candidate.get("sessions", []):
            slot_key = make_slot_key(session["date"], int(session["period"]))
            group_index = int(session["group_index"]) - 1
            active = active_vars.get((slot_key, group_index))
            if active is not None:
                excluded_vars.append(active)
            if session.get("meeting_mode") == MEETING_MODE_ZOOM:
                zoom = zoom_vars.get((slot_key, group_index))
                if zoom is not None:
                    excluded_vars.append(zoom)
            for participant_id in session.get("university_role_member_ids", []):
                variable = university_vars.get(
                    (slot_key, group_index, participant_id)
                )
                if variable is not None:
                    excluded_vars.append(variable)
            for participant_id in session.get("high_school_role_member_ids", []):
                variable = high_school_vars.get(
                    (slot_key, group_index, participant_id)
                )
                if variable is not None:
                    excluded_vars.append(variable)
        if excluded_vars:
            minimum_changes = min(2, len(excluded_vars))
            model.add(
                sum(excluded_vars) <= len(excluded_vars) - minimum_changes
            )

    candidates: list[dict[str, Any]] = []
    target_candidate_count = candidate_limit or config.max_candidates
    seed = config.random_seed if random_seed is None else random_seed
    last_solver_status = "NOT_STARTED"
    amendment_lexicographic_statuses: dict[str, str] = {}

    def candidate_from_solver(
        solver: cp_model.CpSolver,
        solver_status: str,
        *,
        search_phase: str = "",
        violation_minimum_proven: bool = False,
        extra_minimum_proven: bool = False,
        evaluation_optimality_proven: bool = False,
    ) -> dict[str, Any]:
        candidate = _candidate_from_solution(
            solver,
            config,
            target,
            feasible_slots,
            active_vars,
            zoom_vars,
            university_vars,
            high_school_vars,
        )
        objective_value = float(solver.objective_value)
        best_bound = float(solver.best_objective_bound)
        candidate["metrics"].update(
            {
                "solver_status": solver_status,
                "solver_objective_value": round(objective_value, 4),
                "solver_best_objective_bound": round(best_bound, 4),
                "solver_relative_gap": round(
                    abs(objective_value - best_bound)
                    / max(1.0, abs(objective_value)),
                    6,
                ),
                "solver_wall_time_seconds": round(
                    float(solver.wall_time),
                    4,
                ),
                "fixed_session_count": len(fixed_sessions or []),
                "locked_session_count": len(assignment_locks or []),
                "assignment_lock_count": assignment_lock_count,
            }
        )
        if amendment_base_schedule is None:
            candidate["metrics"].update(
                {
                    "search_phase": search_phase,
                    "violation_minimum_proven": violation_minimum_proven,
                    "extra_minimum_proven": extra_minimum_proven,
                    "evaluation_optimality_proven": (
                        evaluation_optimality_proven
                    ),
                }
            )
        if amendment_base_schedule is not None:
            movement_signature = amendment_movement_signature(
                amendment_base_schedule,
                candidate,
            )
            candidate["metrics"].update(
                {
                    **amendment_movement_metrics(
                        amendment_base_schedule,
                        candidate,
                        normalized_amendment_requester_ids,
                    ),
                    "amendment_movement_signature": movement_signature,
                    "amendment_search_stage": amendment_search_stage,
                    "amendment_lexicographic_statuses": dict(
                        amendment_lexicographic_statuses
                    ),
                    "amendment_objective_strategy": (
                        amendment_objective_strategy
                    ),
                    "amendment_primary_minimum_proven": (
                        solver_status == "OPTIMAL"
                    ),
                    "amendment_search_phase": (
                        "change_and_slot_minimization"
                    ),
                    "amendment_incumbent_objective_bound": (
                        incumbent_objective_bound
                    ),
                }
            )
        for session in candidate.get("sessions", []):
            candidate_key = (
                make_slot_key(session["date"], int(session["period"])),
                int(session["group_index"]) - 1,
            )
            if candidate_key in fixed_session_ids_by_key:
                session["session_id"] = fixed_session_ids_by_key[
                    candidate_key
                ]
        return candidate

    decision_variables = (
        list(active_vars.values())
        + list(zoom_vars.values())
        + list(university_vars.values())
        + list(high_school_vars.values())
    )
    last_feasible_solver: cp_model.CpSolver | None = None
    violation_minimum_proven = False
    extra_minimum_proven = False
    evaluation_optimality_proven = False
    evaluation_proven_fingerprint: tuple[Any, ...] | None = None
    search_limit_reached = False

    def solve_phase(
        search_phase: str,
        *,
        stop_after_first_solution: bool = False,
        use_full_remaining_time: bool = True,
    ) -> tuple[cp_model.CpSolverStatus | None, cp_model.CpSolver | None]:
        nonlocal last_solver_status, last_feasible_solver
        nonlocal evaluation_proven_fingerprint
        nonlocal search_limit_reached
        remaining = deadline - time.monotonic()
        if remaining <= MIN_SOLVER_SLICE_SECONDS:
            last_solver_status = "TIME_LIMIT"
            return None, None
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = (
            remaining
            if use_full_remaining_time
            else _solver_time_slice(
                remaining,
                target_candidate_count,
                len(candidates),
            )
        )
        if amendment_base_schedule is None:
            solver.parameters.max_deterministic_time = max(2.0, total_timeout)
        solver.parameters.random_seed = seed + len(candidates)
        solver.parameters.randomize_search = True
        solver.parameters.stop_after_first_solution = stop_after_first_solution
        # Cloud/localとも停止時間を予測しやすくし、モデル検証・保存・再描画を
        # 含む実測なしにワーカー数だけを増やさない。
        solver.parameters.num_search_workers = 1
        status = solver.solve(model)
        last_solver_status = solver.status_name(status)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return status, solver
        if status == cp_model.FEASIBLE and not stop_after_first_solution:
            search_limit_reached = True
        last_feasible_solver = solver
        phase_violation_proven = violation_minimum_proven or (
            search_phase == "relaxed_violation_minimization"
            and status == cp_model.OPTIMAL
        )
        phase_extra_proven = extra_minimum_proven or (
            search_phase in {
                "strict_evaluation_improvement",
                "relaxed_extra_minimization",
            }
            and status == cp_model.OPTIMAL
        )
        phase_evaluation_proven = evaluation_optimality_proven or (
            search_phase in {
                "strict_evaluation_improvement",
                "relaxed_evaluation_improvement",
            }
            and status == cp_model.OPTIMAL
        )
        candidate = candidate_from_solver(
            solver,
            last_solver_status,
            search_phase=search_phase,
            violation_minimum_proven=phase_violation_proven,
            extra_minimum_proven=phase_extra_proven,
            evaluation_optimality_proven=phase_evaluation_proven,
        )
        if _validate_candidate(
            candidate,
            config,
            target,
            require_all_mandatory_conditions=not relaxed,
        ):
            candidates.append(candidate)
            if phase_evaluation_proven:
                evaluation_proven_fingerprint = candidate_fingerprint(candidate)
        return status, solver

    def exclude_solver_solution(solver: cp_model.CpSolver | None) -> bool:
        if solver is None:
            return False
        selected = [
            variable
            for variable in decision_variables
            if solver.boolean_value(variable)
        ]
        if not selected:
            return False
        minimum_changes = min(2, len(selected))
        model.add(sum(selected) <= len(selected) - minimum_changes)
        if amendment_distinct_movements and amendment_assignment_presence_vars:
            selected_presence: list[cp_model.IntVar] = []
            unselected_presence: list[cp_model.IntVar] = []
            for variable in amendment_assignment_presence_vars.values():
                if solver.boolean_value(variable):
                    selected_presence.append(variable)
                else:
                    unselected_presence.append(variable)
            model.add(
                sum(selected_presence) - sum(unselected_presence)
                <= len(selected_presence) - 1
            )
        return True

    if amendment_base_schedule is not None:
        while (
            len(candidates) < target_candidate_count
            and last_solver_status
            not in {"MODEL_INVALID", "INFEASIBLE", "UNKNOWN", "TIME_LIMIT"}
        ):
            status, solver = solve_phase(
                "amendment_change_minimization",
                use_full_remaining_time=not candidates,
            )
            if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                break
            if not exclude_solver_solution(solver):
                break
    elif relaxed:
        # Exact lexicographic optimization: never trade a larger mandatory
        # violation for fewer extras or a better evaluation score.
        model.clear_objective()
        model.minimize(relaxed_violation_expression)
        status, solver = solve_phase("relaxed_violation_minimization")
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) and solver is not None:
            violation_minimum_proven = status == cp_model.OPTIMAL
            if violation_minimum_proven:
                minimum_violation = int(
                    round(solver.value(relaxed_violation_expression))
                )
                model.add(relaxed_violation_expression == minimum_violation)
                model.clear_objective()
                model.minimize(default_extra_expression)
                status, solver = solve_phase("relaxed_extra_minimization")
                if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) and solver is not None:
                    extra_minimum_proven = status == cp_model.OPTIMAL
                    if extra_minimum_proven:
                        minimum_extra = int(
                            round(solver.value(default_extra_expression))
                        )
                        model.add(default_extra_expression == minimum_extra)
                        model.clear_objective()
                        model.minimize(evaluation_expression)
                        status, solver = solve_phase(
                            "relaxed_evaluation_improvement"
                        )
                        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                            evaluation_optimality_proven = status == cp_model.OPTIMAL
        if candidates and deadline - time.monotonic() > MIN_SOLVER_SLICE_SECONDS:
            exclude_solver_solution(last_feasible_solver)
    else:
        # First secure any strict candidate without evaluation terms.  The
        # second solve may improve it, but can never erase the secured result.
        model.clear_objective()
        status, solver = solve_phase(
            "strict_candidate_discovery",
            stop_after_first_solution=True,
        )
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            model.clear_objective()
            model.minimize(normal_selection_objective)
            status, solver = solve_phase("strict_evaluation_improvement")
            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                extra_minimum_proven = status == cp_model.OPTIMAL
                evaluation_optimality_proven = status == cp_model.OPTIMAL
        if candidates and deadline - time.monotonic() > MIN_SOLVER_SLICE_SECONDS:
            model.clear_objective()
            model.minimize(normal_selection_objective)
            exclude_solver_solution(last_feasible_solver)

    # Use only the shared remaining budget to look for distinct alternatives.
    while (
        amendment_base_schedule is None
        and len({candidate_fingerprint(item) for item in candidates})
        < target_candidate_count
        and last_solver_status
        not in {"MODEL_INVALID", "INFEASIBLE", "UNKNOWN", "TIME_LIMIT"}
        and deadline - time.monotonic() > MIN_SOLVER_SLICE_SECONDS
    ):
        status, solver = solve_phase(
            (
                "relaxed_candidate_enumeration"
                if relaxed
                else "strict_candidate_enumeration"
            ),
            use_full_remaining_time=False,
        )
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break
        if not exclude_solver_solution(solver):
            break

    unique_candidates: dict[tuple[Any, ...], dict[str, Any]] = {}
    for candidate in candidates:
        fingerprint = candidate_fingerprint(candidate)
        current = unique_candidates.get(fingerprint)
        if current is None or candidate_sort_key(candidate) < candidate_sort_key(current):
            unique_candidates[fingerprint] = candidate
    candidates = list(unique_candidates.values())

    if amendment_base_schedule is not None:
        candidates.sort(
            key=lambda candidate: (
                *amendment_candidate_sort_key(candidate),
                *candidate_sort_key(candidate),
            )
        )
    else:
        candidates.sort(key=candidate_sort_key)
    if (
        candidates
        and evaluation_proven_fingerprint != candidate_fingerprint(candidates[0])
    ):
        # The displayed 100-point score is the operator-facing authority.  Be
        # conservative if the CP-SAT integer surrogate proves a different
        # member of the retained pool than the best displayed score.
        for candidate in candidates:
            candidate["metrics"]["evaluation_optimality_proven"] = False
    candidates = candidates[:target_candidate_count]
    if last_solver_status in {"UNKNOWN", "TIME_LIMIT"} or search_limit_reached:
        termination_status = last_solver_status
        if last_solver_status == "FEASIBLE":
            termination_status = "TIME_LIMIT"
    elif len(candidates) >= target_candidate_count:
        termination_status = "TARGET_REACHED"
    elif last_solver_status == "INFEASIBLE" and candidates:
        termination_status = "EXHAUSTED"
    else:
        termination_status = last_solver_status
    search_elapsed = round(time.monotonic() - search_started, 4)
    for candidate in candidates:
        candidate["metrics"].update(
            {
                "search_termination_status": termination_status,
                "search_requested_candidate_count": target_candidate_count,
                "search_returned_candidate_count": len(candidates),
                "search_elapsed_seconds": search_elapsed,
            }
        )
    if not candidates:
        if (
            allow_relaxed_fallback
            and not relaxed
            and last_solver_status == "INFEASIBLE"
        ):
            relaxed_candidates, _ = generate_candidates(
                config,
                participants,
                timeout_seconds=timeout_seconds,
                random_seed=random_seed,
                candidate_limit=candidate_limit,
                excluded_candidates=excluded_candidates,
                blocked_slots_by_participant=blocked_slots_by_participant,
                fixed_sessions=fixed_sessions,
                assignment_locks=assignment_locks,
                amendment_base_schedule=amendment_base_schedule,
                amendment_requester_id=amendment_requester_id,
                amendment_requester_ids=normalized_amendment_requester_ids,
                amendment_fixed_participant_ids=amendment_fixed_participant_ids,
                amendment_distinct_movements=amendment_distinct_movements,
                amendment_search_stage=amendment_search_stage,
                amendment_incumbent_candidate=amendment_incumbent_candidate,
                allow_relaxed_fallback=False,
                relaxed=True,
                _deadline=deadline,
                _search_started=search_started,
            )
            if relaxed_candidates:
                return relaxed_candidates, _candidate_failure_reasons(
                    config,
                    participants,
                    headline=(
                        "必須条件を満たす候補が存在しないことを確認したため、"
                        "近似候補を表示しています。"
                    ),
                    blocked_slots_by_participant=blocked_slots_by_participant,
                )
            return [], _candidate_failure_reasons(
                config,
                participants,
                headline=(
                    "必須条件を満たす候補が存在せず、ハード制約を守る"
                    "近似候補も見つかりませんでした。"
                ),
                blocked_slots_by_participant=blocked_slots_by_participant,
            )
        if last_solver_status in {"UNKNOWN", "TIME_LIMIT", "NOT_STARTED"}:
            search_label = "近似候補" if relaxed else "厳密候補"
            return [], [
                f"最大探索時間まで探索しましたが、{search_label}を"
                "見つけられませんでした。"
                "条件矛盾が確定したわけではありません。"
                "参加可能回答を確認するか、完全手動調整を利用してください。"
            ]
        if last_solver_status == "MODEL_INVALID":
            search_subject = (
                "改訂探索" if amendment_base_schedule is not None else "候補探索"
            )
            validation_error = model.validate().strip()
            if validation_error:
                return [], [
                    f"{search_subject}の内部モデルを作成できませんでした。"
                    f"詳細: {validation_error.splitlines()[0]}"
                ]
            return [], [
                f"{search_subject}の最適化モデルを実行できませんでした。"
                "探索設定を変えずに再実行しても続く場合は、"
                "設定内容とログを確認してください。"
            ]
        if (
            amendment_base_schedule is not None
            and last_solver_status == "INFEASIBLE"
        ):
            return [], [
                "対象期間の未使用日時を含む変更最小化モデルで、"
                "現在の必須条件を満たす改訂案がないことを"
                "確認しました。参加可能回答や規定回数を見直すか、"
                "完全手動調整を利用してください。"
            ]
        return [], _candidate_failure_reasons(
            config,
            participants,
            headline=(
                "現在のハード制約を守る近似候補が存在しないことを"
                "確認しました。"
                if relaxed
                else "必須条件を満たす候補が存在しません。"
            ),
            blocked_slots_by_participant=blocked_slots_by_participant,
        )
    return candidates, []


def _direct_swap_amendment_candidates(
    config: Config,
    participants: list[Participant],
    base_schedule: dict[str, Any],
    requested_slots_by_participant: dict[str, set[str]],
    blocked_slots_by_participant: dict[str, set[str]],
    candidate_limit: int,
) -> list[dict[str, Any]]:
    """Return strict one-person swaps for the common single-request case."""

    if (
        len(requested_slots_by_participant) != 1
        or candidate_limit < 1
    ):
        return []
    requester_id, requested_slots = next(
        iter(requested_slots_by_participant.items())
    )
    target = scheduling_participants(participants)
    participant_by_id = {
        participant.id: participant for participant in target
    }
    requester = participant_by_id.get(requester_id)
    if requester is None:
        return []

    source_matches: list[tuple[int, str]] = []
    for session_index, session in enumerate(
        base_schedule.get("sessions", [])
    ):
        try:
            slot_key = make_slot_key(
                str(session["date"]),
                int(session["period"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if slot_key not in requested_slots:
            continue
        for role_field in (
            "university_role_member_ids",
            "high_school_role_member_ids",
        ):
            if requester_id in set(map(str, session.get(role_field, []))):
                source_matches.append((session_index, role_field))
    if len(source_matches) != 1:
        return []

    source_index, source_role_field = source_matches[0]
    source_session = base_schedule["sessions"][source_index]
    source_slot = make_slot_key(
        str(source_session["date"]),
        int(source_session["period"]),
    )
    source_mode = str(
        source_session.get("meeting_mode", MEETING_MODE_IN_PERSON)
    )
    source_member_ids = {
        *map(
            str,
            source_session.get("university_role_member_ids", []),
        ),
        *map(
            str,
            source_session.get("high_school_role_member_ids", []),
        ),
    }

    def can_attend(
        participant: Participant,
        slot_key: str,
        meeting_mode: str,
    ) -> bool:
        if slot_key in blocked_slots_by_participant.get(
            participant.id,
            set(),
        ):
            return False
        return (
            slot_key in set(participant.availability)
            or (
                meeting_mode == MEETING_MODE_ZOOM
                and slot_key in set(participant.zoom_availability)
            )
        )

    def respects_all_blocks(candidate: dict[str, Any]) -> bool:
        for session in candidate.get("sessions", []):
            slot_key = make_slot_key(
                str(session["date"]),
                int(session["period"]),
            )
            member_ids = {
                *map(
                    str,
                    session.get("university_role_member_ids", []),
                ),
                *map(
                    str,
                    session.get("high_school_role_member_ids", []),
                ),
            }
            if any(
                slot_key
                in blocked_slots_by_participant.get(
                    participant_id,
                    set(),
                )
                for participant_id in member_ids
            ):
                return False
        return True

    candidates_by_fingerprint: dict[
        tuple[Any, ...],
        dict[str, Any],
    ] = {}
    for target_index, target_session in enumerate(
        base_schedule.get("sessions", [])
    ):
        if target_index == source_index:
            continue
        try:
            target_slot = make_slot_key(
                str(target_session["date"]),
                int(target_session["period"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if target_slot == source_slot:
            continue
        target_mode = str(
            target_session.get(
                "meeting_mode",
                MEETING_MODE_IN_PERSON,
            )
        )
        target_member_ids = {
            *map(
                str,
                target_session.get(
                    "university_role_member_ids",
                    [],
                ),
            ),
            *map(
                str,
                target_session.get(
                    "high_school_role_member_ids",
                    [],
                ),
            ),
        }
        if requester_id in target_member_ids or not can_attend(
            requester,
            target_slot,
            target_mode,
        ):
            continue
        for target_role_field in (
            "university_role_member_ids",
            "high_school_role_member_ids",
        ):
            for raw_partner_id in target_session.get(
                target_role_field,
                [],
            ):
                partner_id = str(raw_partner_id)
                partner = participant_by_id.get(partner_id)
                if (
                    partner is None
                    or partner_id in source_member_ids
                    or not can_attend(
                        partner,
                        source_slot,
                        source_mode,
                    )
                ):
                    continue
                candidate = {
                    "sessions": deepcopy(
                        base_schedule.get("sessions", [])
                    ),
                    "participant_summary": [],
                    "metrics": {},
                }
                candidate_source = candidate["sessions"][source_index]
                candidate_target = candidate["sessions"][target_index]
                candidate_source[source_role_field] = [
                    (
                        partner_id
                        if str(participant_id) == requester_id
                        else str(participant_id)
                    )
                    for participant_id in candidate_source.get(
                        source_role_field,
                        [],
                    )
                ]
                candidate_target[target_role_field] = [
                    (
                        requester_id
                        if str(participant_id) == partner_id
                        else str(participant_id)
                    )
                    for participant_id in candidate_target.get(
                        target_role_field,
                        [],
                    )
                ]
                for changed_session in (
                    candidate_source,
                    candidate_target,
                ):
                    slot_key = make_slot_key(
                        str(changed_session["date"]),
                        int(changed_session["period"]),
                    )
                    member_ids: list[str] = []
                    for ids_field, names_field in (
                        (
                            "university_role_member_ids",
                            "university_role_members",
                        ),
                        (
                            "high_school_role_member_ids",
                            "high_school_role_members",
                        ),
                    ):
                        normalized_ids = list(
                            map(
                                str,
                                changed_session.get(ids_field, []),
                            )
                        )
                        changed_session[ids_field] = normalized_ids
                        changed_session[names_field] = [
                            participant_by_id[participant_id].name
                            for participant_id in normalized_ids
                        ]
                        member_ids.extend(normalized_ids)
                    zoom_only_member_ids = sorted(
                        participant_id
                        for participant_id in member_ids
                        if (
                            slot_key
                            not in set(
                                participant_by_id[
                                    participant_id
                                ].availability
                            )
                            and slot_key
                            in set(
                                participant_by_id[
                                    participant_id
                                ].zoom_availability
                            )
                        )
                    )
                    changed_session["zoom_only_member_ids"] = (
                        zoom_only_member_ids
                    )
                    changed_session["zoom_only_member_count"] = len(
                        zoom_only_member_ids
                    )
                if not respects_all_blocks(candidate):
                    continue
                refresh_candidate_evaluation(
                    candidate,
                    config,
                    target,
                    evaluation_config=config,
                )
                if not _validate_candidate(candidate, config, target):
                    continue
                candidate["metrics"].update(
                    {
                        **amendment_movement_metrics(
                            base_schedule,
                            candidate,
                            {requester_id},
                        ),
                        "amendment_movement_signature": (
                            amendment_movement_signature(
                                base_schedule,
                                candidate,
                            )
                        ),
                        "amendment_search_stage": "direct_swap",
                        "amendment_objective_strategy": "direct_swap",
                        "amendment_lexicographic_statuses": {},
                        "solver_status": "DIRECT_SWAP",
                        "amendment_direct_swap_role_change_count": (
                            0
                            if source_role_field == target_role_field
                            else 2
                        ),
                    }
                )
                candidates_by_fingerprint.setdefault(
                    candidate_fingerprint(candidate),
                    candidate,
                )

    return sorted(
        candidates_by_fingerprint.values(),
        key=lambda candidate: (
            *amendment_candidate_sort_key(candidate),
            int(
                candidate.get("metrics", {}).get(
                    "amendment_direct_swap_role_change_count",
                    0,
                )
            ),
            *candidate_sort_key(candidate),
        ),
    )[:candidate_limit]


def _scheduled_seat_repair_candidates(
    config: Config,
    participants: list[Participant],
    base_schedule: dict[str, Any],
    requester_ids: set[str],
    blocked_slots_by_participant: dict[str, set[str]],
    candidate_limit: int,
    timeout_seconds: float,
    random_seed: int,
    movable_session_indexes: set[int] | None = None,
    search_stage: str = "seat_repair",
    objective_strategy: str = "seat_repair",
) -> list[dict[str, Any]]:
    """Repair assignments inside published sessions without rebuilding dates."""

    if candidate_limit < 1 or timeout_seconds <= 0:
        return []
    target = scheduling_participants(participants)
    participant_by_id = {
        participant.id: participant for participant in target
    }
    target_ids = set(participant_by_id)
    sessions = base_schedule.get("sessions", [])
    if not sessions:
        return []

    seat_records: list[tuple[int, str, int, str, str]] = []
    base_participant_by_seat: dict[int, str] = {}
    base_slots_by_participant: dict[str, set[str]] = defaultdict(set)
    base_total_by_participant: Counter[str] = Counter()
    session_slots: dict[int, str] = {}
    session_modes: dict[int, str] = {}
    for session_index, session in enumerate(sessions):
        try:
            slot_key = make_slot_key(
                str(session["date"]),
                int(session["period"]),
            )
        except (KeyError, TypeError, ValueError):
            return []
        session_slots[session_index] = slot_key
        session_modes[session_index] = str(
            session.get("meeting_mode", MEETING_MODE_IN_PERSON)
        )
        for role_field, expected_size in (
            (
                "university_role_member_ids",
                config.university_role_size,
            ),
            (
                "high_school_role_member_ids",
                config.high_school_role_size,
            ),
        ):
            member_ids = list(map(str, session.get(role_field, [])))
            if (
                len(member_ids) != expected_size
                or len(member_ids) != len(set(member_ids))
                or not set(member_ids) <= target_ids
            ):
                return []
            for role_index, participant_id in enumerate(member_ids):
                seat_index = len(seat_records)
                seat_records.append(
                    (
                        session_index,
                        role_field,
                        role_index,
                        slot_key,
                        session_modes[session_index],
                    )
                )
                base_participant_by_seat[seat_index] = participant_id
                base_slots_by_participant[participant_id].add(slot_key)
                base_total_by_participant[participant_id] += 1
    if not seat_records:
        return []
    normalized_movable_indexes = (
        None
        if movable_session_indexes is None
        else {
            int(index)
            for index in movable_session_indexes
            if 0 <= int(index) < len(sessions)
        }
    )
    if (
        movable_session_indexes is not None
        and not normalized_movable_indexes
    ):
        return []
    movable_participant_ids = (
        target_ids
        if normalized_movable_indexes is None
        else {
            participant_id
            for seat_index, participant_id
            in base_participant_by_seat.items()
            if seat_records[seat_index][0]
            in normalized_movable_indexes
        }
    )

    in_person_by_participant = {
        participant.id: set(participant.availability)
        for participant in target
    }
    zoom_by_participant = {
        participant.id: set(participant.zoom_availability)
        for participant in target
    }
    model = cp_model.CpModel()
    assignment_vars: dict[tuple[int, str], cp_model.IntVar] = {}
    vars_by_seat: dict[int, list[cp_model.IntVar]] = defaultdict(list)
    vars_by_participant: dict[str, list[cp_model.IntVar]] = defaultdict(
        list
    )
    university_vars_by_participant: dict[
        str,
        list[cp_model.IntVar],
    ] = defaultdict(list)
    high_school_vars_by_participant: dict[
        str,
        list[cp_model.IntVar],
    ] = defaultdict(list)
    vars_by_participant_slot: dict[
        tuple[str, str],
        list[cp_model.IntVar],
    ] = defaultdict(list)
    vars_by_participant_day: dict[
        tuple[str, str],
        list[cp_model.IntVar],
    ] = defaultdict(list)
    vars_by_participant_day_period: dict[
        tuple[str, str, int],
        list[cp_model.IntVar],
    ] = defaultdict(list)

    for seat_index, (
        session_index,
        role_field,
        _role_index,
        slot_key,
        meeting_mode,
    ) in enumerate(seat_records):
        day, period = parse_slot_key(slot_key)
        if (
            normalized_movable_indexes is not None
            and session_index not in normalized_movable_indexes
        ):
            seat_participants = [
                participant_by_id[
                    base_participant_by_seat[seat_index]
                ]
            ]
        else:
            seat_participants = [
                participant
                for participant in target
                if participant.id in movable_participant_ids
            ]
        for participant in seat_participants:
            participant_id = participant.id
            if slot_key in blocked_slots_by_participant.get(
                participant_id,
                set(),
            ):
                continue
            if (
                slot_key not in in_person_by_participant[participant_id]
                and not (
                    meeting_mode == MEETING_MODE_ZOOM
                    and slot_key
                    in zoom_by_participant[participant_id]
                )
            ):
                continue
            variable = model.new_bool_var(
                f"seat_{seat_index}_{participant_id}"
            )
            assignment_vars[(seat_index, participant_id)] = variable
            vars_by_seat[seat_index].append(variable)
            vars_by_participant[participant_id].append(variable)
            vars_by_participant_slot[
                (participant_id, slot_key)
            ].append(variable)
            vars_by_participant_day[
                (participant_id, day.isoformat())
            ].append(variable)
            vars_by_participant_day_period[
                (participant_id, day.isoformat(), period)
            ].append(variable)
            if role_field == "university_role_member_ids":
                university_vars_by_participant[participant_id].append(
                    variable
                )
            else:
                high_school_vars_by_participant[participant_id].append(
                    variable
                )
        if not vars_by_seat[seat_index]:
            return []
        model.add(sum(vars_by_seat[seat_index]) == 1)

    presence_vars: dict[tuple[str, str], cp_model.IntVar] = {}
    for participant in target:
        participant_id = participant.id
        participant_vars = vars_by_participant.get(participant_id, [])
        base_total = base_total_by_participant.get(participant_id, 0)
        model.add(sum(participant_vars) == base_total)
        model.add(
            sum(university_vars_by_participant.get(participant_id, []))
            >= participant.university_requirement(config)
        )
        model.add(
            sum(high_school_vars_by_participant.get(participant_id, []))
            >= participant.high_school_requirement(config)
        )
        model.add(
            sum(participant_vars) >= participant.total_requirement(config)
        )
        model.add(
            sum(participant_vars)
            <= participant.participation_limit(config)
        )
        for (
            target_id,
            slot_key,
        ), slot_vars in vars_by_participant_slot.items():
            if target_id != participant_id:
                continue
            model.add(sum(slot_vars) <= 1)
            presence = model.new_bool_var(
                f"presence_{participant_id}_{slot_key}"
            )
            model.add(presence == sum(slot_vars))
            presence_vars[(participant_id, slot_key)] = presence
        for (
            target_id,
            _day_text,
        ), day_vars in vars_by_participant_day.items():
            if target_id == participant_id:
                model.add(
                    sum(day_vars)
                    <= config.max_sessions_per_person_per_day
                )
        if config.avoid_consecutive_periods:
            day_periods: dict[str, set[int]] = defaultdict(set)
            for (
                target_id,
                day_text,
                period,
            ) in vars_by_participant_day_period:
                if target_id == participant_id:
                    day_periods[day_text].add(period)
            for day_text, periods in day_periods.items():
                for period in periods:
                    if period + 1 not in periods:
                        continue
                    model.add(
                        sum(
                            vars_by_participant_day_period.get(
                                (participant_id, day_text, period),
                                [],
                            )
                        )
                        + sum(
                            vars_by_participant_day_period.get(
                                (
                                    participant_id,
                                    day_text,
                                    period + 1,
                                ),
                                [],
                            )
                        )
                        <= 1
                    )

    non_requester_changed_vars: list[cp_model.IntVar] = []
    non_requester_difference_terms: list[cp_model.LinearExpr] = []
    requester_difference_terms: list[cp_model.LinearExpr] = []
    non_requester_difference_constant = 0
    requester_difference_constant = 0
    all_schedule_slots = set(session_slots.values())
    for participant in target:
        participant_id = participant.id
        base_slots = base_slots_by_participant.get(participant_id, set())
        difference_terms: list[cp_model.LinearExpr] = []
        difference_constant = 0
        for slot_key in all_schedule_slots | base_slots:
            presence = presence_vars.get((participant_id, slot_key))
            if slot_key in base_slots:
                if presence is None:
                    difference_constant += 1
                else:
                    difference_terms.append(1 - presence)
            elif presence is not None:
                difference_terms.append(presence)
        if participant_id in requester_ids:
            requester_difference_terms.extend(difference_terms)
            requester_difference_constant += difference_constant
            continue
        non_requester_difference_terms.extend(difference_terms)
        non_requester_difference_constant += difference_constant
        changed = model.new_bool_var(
            f"seat_repair_changed_{participant_id}"
        )
        if difference_constant:
            model.add(changed == 1)
        elif difference_terms:
            for difference in difference_terms:
                model.add(changed >= difference)
            model.add(changed <= sum(difference_terms))
        else:
            model.add(changed == 0)
        non_requester_changed_vars.append(changed)

    requester_difference_upper = (
        len(requester_difference_terms) + requester_difference_constant
    )
    non_requester_difference_weight = (
        requester_difference_upper + 1
    )
    model.minimize(
        non_requester_difference_weight
        * (
            sum(non_requester_difference_terms)
            + non_requester_difference_constant
        )
        + (
            sum(requester_difference_terms)
            + requester_difference_constant
        )
    )
    maximum_change_limit = min(
        max(
            0,
            int(config.amendment_max_non_requester_changes),
        ),
        len(non_requester_changed_vars),
    )
    change_limit_literals: dict[int, cp_model.IntVar] = {}
    for change_limit in range(maximum_change_limit + 1):
        literal = model.new_bool_var(
            f"seat_repair_change_limit_{change_limit}"
        )
        model.add(
            sum(non_requester_changed_vars) <= change_limit
        ).only_enforce_if(literal)
        change_limit_literals[change_limit] = literal
    validation_error = model.validate().strip()
    if validation_error:
        return []

    for seat_index, participant_id in base_participant_by_seat.items():
        variable = assignment_vars.get((seat_index, participant_id))
        if variable is not None:
            model.add_hint(variable, 1)

    deadline = time.monotonic() + timeout_seconds
    candidates_by_signature: dict[str, dict[str, Any]] = {}
    search_statuses: dict[str, str] = {}

    def candidate_from_solver(
        solver: cp_model.CpSolver,
        status: cp_model.CpSolverStatus,
        change_limit: int,
        primary_minimum_proven: bool,
        search_phase: str,
    ) -> dict[str, Any] | None:
        candidate = {
            "sessions": deepcopy(sessions),
            "participant_summary": [],
            "metrics": {},
        }
        for session_index, session in enumerate(candidate["sessions"]):
            for role_field, names_field in (
                (
                    "university_role_member_ids",
                    "university_role_members",
                ),
                (
                    "high_school_role_member_ids",
                    "high_school_role_members",
                ),
            ):
                assigned_ids = [
                    participant_id
                    for seat_index, (
                        target_session_index,
                        target_role_field,
                        _role_index,
                        _slot_key,
                        _meeting_mode,
                    ) in enumerate(seat_records)
                    if (
                        target_session_index == session_index
                        and target_role_field == role_field
                    )
                    for participant_id in target_ids
                    if (
                        variable := assignment_vars.get(
                            (seat_index, participant_id)
                        )
                    )
                    is not None
                    and solver.boolean_value(variable)
                ]
                session[role_field] = assigned_ids
                session[names_field] = [
                    participant_by_id[participant_id].name
                    for participant_id in assigned_ids
                ]
            slot_key = session_slots[session_index]
            member_ids = [
                *map(
                    str,
                    session.get("university_role_member_ids", []),
                ),
                *map(
                    str,
                    session.get("high_school_role_member_ids", []),
                ),
            ]
            zoom_only_member_ids = sorted(
                participant_id
                for participant_id in member_ids
                if (
                    slot_key
                    not in in_person_by_participant[participant_id]
                    and slot_key
                    in zoom_by_participant[participant_id]
                )
            )
            session["zoom_only_member_ids"] = zoom_only_member_ids
            session["zoom_only_member_count"] = len(
                zoom_only_member_ids
            )
        refresh_candidate_evaluation(
            candidate,
            config,
            target,
            evaluation_config=config,
        )
        if not _validate_candidate(candidate, config, target):
            return None
        movement_signature = amendment_movement_signature(
            base_schedule,
            candidate,
        )
        objective_value = float(solver.objective_value)
        best_bound = float(solver.best_objective_bound)
        changed_count = sum(
            int(solver.boolean_value(variable))
            for variable in non_requester_changed_vars
        )
        candidate["metrics"].update(
            {
                **amendment_movement_metrics(
                    base_schedule,
                    candidate,
                    requester_ids,
                ),
                "amendment_movement_signature": movement_signature,
                "amendment_search_stage": search_stage,
                "amendment_objective_strategy": objective_strategy,
                "amendment_lexicographic_statuses": {},
                "solver_status": solver.status_name(status),
                "solver_objective_value": round(
                    objective_value,
                    4,
                ),
                "solver_best_objective_bound": round(best_bound, 4),
                "solver_relative_gap": round(
                    abs(objective_value - best_bound)
                    / max(1.0, abs(objective_value)),
                    6,
                ),
                "solver_wall_time_seconds": round(
                    float(solver.wall_time),
                    4,
                ),
                "amendment_changed_count_limit": change_limit,
                "amendment_changed_count_actual": changed_count,
                "amendment_changed_count_upper_bound": (
                    maximum_change_limit
                ),
                "amendment_primary_minimum_proven": (
                    primary_minimum_proven
                ),
                "amendment_search_phase": search_phase,
                "amendment_change_limit_statuses": dict(
                    search_statuses
                ),
            }
        )
        return candidate

    def exclude_movement(
        solver: cp_model.CpSolver,
    ) -> None:
        selected_presence: list[cp_model.IntVar] = []
        unselected_presence: list[cp_model.IntVar] = []
        for variable in presence_vars.values():
            if solver.boolean_value(variable):
                selected_presence.append(variable)
            else:
                unselected_presence.append(variable)
        model.add(
            sum(selected_presence) - sum(unselected_presence)
            <= len(selected_presence) - 1
        )

    selected_change_limit: int | None = None
    primary_minimum_proven = True
    fallback_candidate: dict[str, Any] | None = None
    for change_limit in range(maximum_change_limit + 1):
        remaining = deadline - time.monotonic()
        if remaining <= MIN_SOLVER_SLICE_SECONDS:
            break
        remaining_limits = maximum_change_limit - change_limit + 1
        feasibility_budget = (
            remaining
            if remaining_limits == 1
            else min(
                remaining,
                max(
                    MIN_SOLVER_SLICE_SECONDS,
                    remaining / (remaining_limits + 1),
                ),
            )
        )
        model.clear_assumptions()
        model.add_assumption(change_limit_literals[change_limit])
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = feasibility_budget
        solver.parameters.stop_after_first_solution = True
        solver.parameters.random_seed = random_seed + change_limit
        solver.parameters.randomize_search = True
        solver.parameters.num_search_workers = 1
        status = solver.solve(model)
        status_name = solver.status_name(status)
        search_statuses[str(change_limit)] = status_name
        if status == cp_model.INFEASIBLE:
            continue
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            selected_change_limit = change_limit
            fallback_candidate = candidate_from_solver(
                solver,
                status,
                change_limit,
                primary_minimum_proven,
                "feasibility",
            )
            break
        primary_minimum_proven = False
    if selected_change_limit is None:
        return []
    if fallback_candidate is not None:
        fallback_signature = str(
            fallback_candidate["metrics"][
                "amendment_movement_signature"
            ]
        )
        candidates_by_signature[fallback_signature] = (
            fallback_candidate
    )

    selected_limit_literal = change_limit_literals[
        selected_change_limit
    ]
    optimization_complete = False
    optimization_attempt_count = 0
    optimized_candidate_count = 0
    while (
        not optimization_complete
        and deadline - time.monotonic() > MIN_SOLVER_SLICE_SECONDS
    ):
        remaining = deadline - time.monotonic()
        model.clear_assumptions()
        model.add_assumption(selected_limit_literal)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = _solver_time_slice(
            remaining,
            candidate_limit,
            optimized_candidate_count,
        )
        solver.parameters.random_seed = (
            random_seed + optimization_attempt_count + 1
        )
        solver.parameters.randomize_search = True
        solver.parameters.num_search_workers = 1
        status = solver.solve(model)
        optimization_attempt_count += 1
        search_statuses[
            f"lexicographic_{optimization_attempt_count}"
        ] = solver.status_name(status)
        if status == cp_model.UNKNOWN:
            continue
        if status == cp_model.INFEASIBLE:
            optimization_complete = True
            break
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break
        candidate = candidate_from_solver(
            solver,
            status,
            selected_change_limit,
            primary_minimum_proven,
            "slot_minimization",
        )
        if candidate is None:
            break
        optimized_candidate_count += 1
        movement_signature = str(
            candidate["metrics"]["amendment_movement_signature"]
        )
        candidates_by_signature[movement_signature] = candidate
        exclude_movement(solver)
        if (
            status == cp_model.OPTIMAL
            and optimized_candidate_count >= candidate_limit
        ):
            optimization_complete = True

    return sorted(
        candidates_by_signature.values(),
        key=lambda candidate: (
            *amendment_candidate_sort_key(candidate),
            *candidate_sort_key(candidate),
        ),
    )[:candidate_limit]


def _scoped_session_repair_candidates(
    config: Config,
    participants: list[Participant],
    base_schedule: dict[str, Any],
    requested_slots_by_participant: dict[str, set[str]],
    blocked_slots_by_participant: dict[str, set[str]],
    candidate_limit: int,
    timeout_seconds: float,
    random_seed: int,
) -> list[dict[str, Any]]:
    """Rebuild affected sessions with at most one unrelated session."""

    if candidate_limit < 1 or timeout_seconds <= 0:
        return []
    sessions = base_schedule.get("sessions", [])
    if not sessions:
        return []
    requester_ids = set(requested_slots_by_participant)
    affected_indexes: set[int] = set()
    session_slots: dict[int, str] = {}
    session_member_ids: dict[int, set[str]] = {}
    for session_index, session in enumerate(sessions):
        try:
            slot_key = make_slot_key(
                str(session["date"]),
                int(session["period"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        member_ids = {
            *map(
                str,
                session.get("university_role_member_ids", []),
            ),
            *map(
                str,
                session.get("high_school_role_member_ids", []),
            ),
        }
        session_slots[session_index] = slot_key
        session_member_ids[session_index] = member_ids
        if any(
            requester_id in member_ids
            and slot_key
            in requested_slots_by_participant.get(
                requester_id,
                set(),
            )
            for requester_id in requester_ids
        ):
            affected_indexes.add(session_index)
    if not affected_indexes:
        return []

    participant_by_id = {
        participant.id: participant
        for participant in scheduling_participants(participants)
    }

    def can_attend(participant_id: str, slot_key: str) -> bool:
        participant = participant_by_id.get(participant_id)
        if participant is None:
            return False
        if slot_key in blocked_slots_by_participant.get(
            participant_id,
            set(),
        ):
            return False
        return (
            slot_key in set(participant.availability)
            or slot_key in set(participant.zoom_availability)
        )

    affected_slots = {
        session_slots[index]
        for index in affected_indexes
        if index in session_slots
    }

    def bridge_score(session_index: int) -> tuple[int, int, int]:
        slot_key = session_slots.get(session_index, "")
        member_ids = session_member_ids.get(session_index, set())
        requester_bridges = sum(
            can_attend(requester_id, slot_key)
            for requester_id in requester_ids
        )
        return_bridges = sum(
            can_attend(participant_id, affected_slot)
            for participant_id in member_ids
            for affected_slot in affected_slots
        )
        cross_options = sum(
            can_attend(participant_id, candidate_slot)
            for participant_id in (
                set().union(
                    *(
                        session_member_ids.get(index, set())
                        for index in affected_indexes
                    )
                )
                | member_ids
            )
            for candidate_slot in affected_slots | {slot_key}
        )
        return requester_bridges, return_bridges, cross_options

    unrelated_indexes = sorted(
        (
            index
            for index in session_slots
            if index not in affected_indexes
        ),
        key=lambda index: (
            *(-value for value in bridge_score(index)),
            index,
        ),
    )
    neighborhoods = [set(affected_indexes)]
    neighborhoods.extend(
        affected_indexes | {extra_index}
        for extra_index in unrelated_indexes
    )

    deadline = time.monotonic() + timeout_seconds
    candidates_by_signature: dict[str, dict[str, Any]] = {}
    for neighborhood_index, movable_indexes in enumerate(
        neighborhoods
    ):
        remaining = deadline - time.monotonic()
        if remaining <= MIN_SOLVER_SLICE_SECONDS:
            break
        remaining_neighborhoods = len(neighborhoods) - neighborhood_index
        neighborhood_budget = min(
            remaining,
            max(
                MIN_SOLVER_SLICE_SECONDS,
                remaining / max(1, remaining_neighborhoods),
            ),
        )
        candidates = _scheduled_seat_repair_candidates(
            config,
            participants,
            base_schedule,
            requester_ids,
            blocked_slots_by_participant,
            candidate_limit,
            neighborhood_budget,
            random_seed + neighborhood_index,
            movable_session_indexes=movable_indexes,
            search_stage="scoped_session_repair",
            objective_strategy="scoped_session_repair",
        )
        for candidate in candidates:
            metrics = candidate.setdefault("metrics", {})
            metrics["amendment_scope_session_count"] = len(
                movable_indexes
            )
            metrics["amendment_added_unaffected_session_count"] = (
                len(movable_indexes - affected_indexes)
            )
            signature = str(
                metrics.get(
                    "amendment_movement_signature",
                    amendment_movement_signature(
                        base_schedule,
                        candidate,
                    ),
                )
            )
            current = candidates_by_signature.get(signature)
            if current is None or amendment_candidate_sort_key(
                candidate
            ) < amendment_candidate_sort_key(current):
                candidates_by_signature[signature] = candidate

    return sorted(
        candidates_by_signature.values(),
        key=lambda candidate: (
            *amendment_candidate_sort_key(candidate),
            *candidate_sort_key(candidate),
        ),
    )[:candidate_limit]


def _affected_session_move_candidates(
    config: Config,
    participants: list[Participant],
    base_schedule: dict[str, Any],
    requested_slots_by_participant: dict[str, set[str]],
    blocked_slots_by_participant: dict[str, set[str]],
    candidate_limit: int,
) -> list[dict[str, Any]]:
    """Move one disrupted published session as a unit to a common free slot."""

    if (
        len(requested_slots_by_participant) != 1
        or candidate_limit < 1
    ):
        return []
    requester_id, requested_slots = next(
        iter(requested_slots_by_participant.items())
    )
    target = scheduling_participants(participants)
    participant_by_id = {
        participant.id: participant for participant in target
    }
    source_indexes: list[int] = []
    for session_index, session in enumerate(
        base_schedule.get("sessions", [])
    ):
        try:
            slot_key = make_slot_key(
                str(session["date"]),
                int(session["period"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        member_ids = {
            *map(
                str,
                session.get("university_role_member_ids", []),
            ),
            *map(
                str,
                session.get("high_school_role_member_ids", []),
            ),
        }
        if requester_id in member_ids and slot_key in requested_slots:
            source_indexes.append(session_index)
    if len(source_indexes) != 1:
        return []

    source_index = source_indexes[0]
    source = base_schedule["sessions"][source_index]
    source_slot = make_slot_key(
        str(source["date"]),
        int(source["period"]),
    )
    source_mode = str(
        source.get("meeting_mode", MEETING_MODE_IN_PERSON)
    )
    member_ids = [
        *map(
            str,
            source.get("university_role_member_ids", []),
        ),
        *map(
            str,
            source.get("high_school_role_member_ids", []),
        ),
    ]
    if (
        not member_ids
        or len(member_ids) != len(set(member_ids))
        or any(
            participant_id not in participant_by_id
            for participant_id in member_ids
        )
    ):
        return []

    eligible_slot_keys = {
        make_slot_key(day, period)
        for day in practice_dates(config)
        for period in config.enabled_periods
    }
    common_slots = set(eligible_slot_keys)
    for participant_id in member_ids:
        participant = participant_by_id[participant_id]
        if source_mode == MEETING_MODE_ZOOM:
            possible = (
                set(participant.availability)
                | set(participant.zoom_availability)
            )
        else:
            possible = set(participant.availability)
        possible -= blocked_slots_by_participant.get(
            participant_id,
            set(),
        )
        common_slots &= possible
    common_slots.discard(source_slot)
    if not common_slots:
        return []

    occupied_groups_by_slot: dict[str, set[int]] = defaultdict(set)
    for session_index, session in enumerate(
        base_schedule.get("sessions", [])
    ):
        if session_index == source_index:
            continue
        slot_key = make_slot_key(
            str(session["date"]),
            int(session["period"]),
        )
        occupied_groups_by_slot[slot_key].add(
            int(session.get("group_index", 1))
        )

    candidates_by_signature: dict[str, dict[str, Any]] = {}
    for target_slot in sorted(common_slots):
        occupied_groups = occupied_groups_by_slot.get(
            target_slot,
            set(),
        )
        target_group = next(
            (
                group_index
                for group_index in range(
                    1,
                    config.max_groups_per_slot + 1,
                )
                if group_index not in occupied_groups
            ),
            None,
        )
        if target_group is None:
            continue
        target_day, target_period = parse_slot_key(target_slot)
        candidate = {
            "sessions": deepcopy(
                base_schedule.get("sessions", [])
            ),
            "participant_summary": [],
            "metrics": {},
        }
        moved_session = candidate["sessions"][source_index]
        moved_session["date"] = target_day.isoformat()
        moved_session["period"] = target_period
        moved_session["group_index"] = target_group
        zoom_only_member_ids = sorted(
            participant_id
            for participant_id in member_ids
            if (
                target_slot
                not in set(
                    participant_by_id[participant_id].availability
                )
                and target_slot
                in set(
                    participant_by_id[
                        participant_id
                    ].zoom_availability
                )
            )
        )
        moved_session["zoom_only_member_ids"] = zoom_only_member_ids
        moved_session["zoom_only_member_count"] = len(
            zoom_only_member_ids
        )
        refresh_candidate_evaluation(
            candidate,
            config,
            target,
            evaluation_config=config,
        )
        if not _validate_candidate(candidate, config, target):
            continue
        movement_signature = amendment_movement_signature(
            base_schedule,
            candidate,
        )
        candidate["metrics"].update(
            {
                **amendment_movement_metrics(
                    base_schedule,
                    candidate,
                    {requester_id},
                ),
                "amendment_movement_signature": movement_signature,
                "amendment_search_stage": "session_move",
                "amendment_objective_strategy": "session_move",
                "amendment_lexicographic_statuses": {},
                "solver_status": "SESSION_MOVE",
            }
        )
        candidates_by_signature.setdefault(
            movement_signature,
            candidate,
        )

    return sorted(
        candidates_by_signature.values(),
        key=lambda candidate: (
            *amendment_candidate_sort_key(candidate),
            *candidate_sort_key(candidate),
        ),
    )[:candidate_limit]


def generate_amendment_candidates(
    config: Config,
    participants: list[Participant],
    base_schedule: dict[str, Any],
    requester_ids: str | Iterable[str],
    unavailable_slots: list[str] | dict[str, list[str]],
    *,
    timeout_seconds: float | None = None,
    random_seed: int | None = None,
    candidate_limit: int = 3,
    blocked_slots_by_participant: dict[str, set[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Find movement-distinct amendment proposals within one total time budget."""

    target = scheduling_participants(participants)
    target_ids = {participant.id for participant in target}
    normalized_requester_ids = (
        {str(requester_ids)}
        if isinstance(requester_ids, str)
        else {str(value) for value in requester_ids}
    )
    missing_requesters = normalized_requester_ids - target_ids
    if missing_requesters:
        return [], ["変更依頼者が現在の日調対象に含まれていません。"]
    if isinstance(unavailable_slots, dict):
        requested_slots_by_participant = {
            str(participant_id): {
                str(value) for value in slots
            }
            for participant_id, slots in unavailable_slots.items()
            if str(participant_id) in normalized_requester_ids
        }
    elif len(normalized_requester_ids) == 1:
        requested_slots_by_participant = {
            next(iter(normalized_requester_ids)): {
                str(value) for value in unavailable_slots
            }
        }
    else:
        return [], [
            "複数の変更依頼者について、不可能コマを参加者別に指定してください。"
        ]
    requested_slots_by_participant = {
        participant_id: slots
        for participant_id, slots in requested_slots_by_participant.items()
        if slots
    }
    if not requested_slots_by_participant:
        return [], ["不可能になったコマが登録されていません。"]
    normalized_requester_ids = set(requested_slots_by_participant)

    total_timeout = max(
        MIN_SOLVER_SLICE_SECONDS,
        float(timeout_seconds or config.search_timeout_seconds),
    )
    search_started = time.monotonic()
    deadline = search_started + total_timeout
    effective_blocked = {
        str(participant_id): set(slots)
        for participant_id, slots in (
            blocked_slots_by_participant or {}
        ).items()
    }
    for participant_id, requested_slots in (
        requested_slots_by_participant.items()
    ):
        effective_blocked.setdefault(participant_id, set()).update(
            requested_slots
        )

    result_by_signature: dict[str, dict[str, Any]] = {}
    requested_count = max(1, min(3, int(candidate_limit)))
    maximum_non_requester_changes = max(
        0,
        int(config.amendment_max_non_requester_changes),
    )

    def remember_candidate(candidate: dict[str, Any]) -> None:
        metrics = candidate.get("metrics", {})
        if (
            int(
                metrics.get(
                    "amendment_non_requester_changed_count",
                    0,
                )
            )
            > maximum_non_requester_changes
        ):
            return
        signature = str(
            metrics.get(
                "amendment_movement_signature",
                amendment_movement_signature(base_schedule, candidate),
            )
        )
        current = result_by_signature.get(signature)
        if current is None:
            result_by_signature[signature] = candidate
            return
        current_metrics = current.get("metrics", {})
        current_rank = (
            int(
                current_metrics.get("amendment_objective_strategy")
                == "seat_repair"
            ),
            int(
                current_metrics.get(
                    "amendment_primary_minimum_proven",
                    False,
                )
            ),
            int(current_metrics.get("solver_status") == "OPTIMAL"),
        )
        candidate_rank = (
            int(
                metrics.get("amendment_objective_strategy")
                == "seat_repair"
            ),
            int(
                metrics.get(
                    "amendment_primary_minimum_proven",
                    False,
                )
            ),
            int(metrics.get("solver_status") == "OPTIMAL"),
        )
        if candidate_rank >= current_rank:
            result_by_signature[signature] = candidate

    direct_swap_candidates = _direct_swap_amendment_candidates(
        config,
        participants,
        base_schedule,
        requested_slots_by_participant,
        effective_blocked,
        requested_count,
    )
    for candidate in direct_swap_candidates:
        remember_candidate(candidate)

    session_move_candidates = _affected_session_move_candidates(
        config,
        participants,
        base_schedule,
        requested_slots_by_participant,
        effective_blocked,
        requested_count,
    )
    for candidate in session_move_candidates:
        remember_candidate(candidate)

    remaining_before_scoped_repair = deadline - time.monotonic()
    if remaining_before_scoped_repair > 0:
        scoped_repair_budget = min(
            remaining_before_scoped_repair,
            AMENDMENT_SCOPED_REPAIR_MAX_SECONDS,
            max(
                MIN_SOLVER_SLICE_SECONDS,
                total_timeout
                * AMENDMENT_SCOPED_REPAIR_TIME_SHARE,
            ),
        )
        scoped_repair_candidates = (
            _scoped_session_repair_candidates(
                config,
                participants,
                base_schedule,
                requested_slots_by_participant,
                effective_blocked,
                requested_count,
                scoped_repair_budget,
                (
                    config.random_seed
                    if random_seed is None
                    else random_seed
                ),
            )
        )
        for candidate in scoped_repair_candidates:
            remember_candidate(candidate)

    remaining_before_repair = deadline - time.monotonic()
    if remaining_before_repair > 0:
        seat_repair_candidates = _scheduled_seat_repair_candidates(
            config,
            participants,
            base_schedule,
            normalized_requester_ids,
            effective_blocked,
            requested_count,
            remaining_before_repair,
            (
                config.random_seed
                if random_seed is None
                else random_seed
            ),
        )
        for candidate in seat_repair_candidates:
            remember_candidate(candidate)

    candidates = sorted(
        result_by_signature.values(),
        key=lambda candidate: (
            *amendment_candidate_sort_key(candidate),
            *candidate_sort_key(candidate),
        ),
    )[:requested_count]
    elapsed = round(time.monotonic() - search_started, 4)
    for candidate in candidates:
        metrics = candidate.setdefault("metrics", {})
        objective_strategy = str(
            metrics.get("amendment_objective_strategy", "")
        )
        movement_minimum_proven = (
            objective_strategy == "seat_repair"
            and metrics.get(
                "amendment_primary_minimum_proven",
                False,
            )
            and metrics.get("solver_status") == "OPTIMAL"
        )
        metrics.update(
            {
                "amendment_requested_candidate_count": requested_count,
                "amendment_returned_candidate_count": len(candidates),
                "amendment_total_search_elapsed_seconds": elapsed,
                "amendment_optimization_incomplete": (
                    not movement_minimum_proven
                ),
                "amendment_minimum_scope": (
                    "published_sessions"
                    if movement_minimum_proven
                    else "best_found"
                ),
            }
        )
    if candidates:
        reasons = []
        if any(
            candidate.get("metrics", {}).get(
                "amendment_optimization_incomplete",
                False,
            )
            for candidate in candidates
        ):
            reasons.append(
                "実行可能な改訂案を確認できましたが、"
                "変更が最小であることの確認は時間内に"
                "完了しませんでした。"
            )
        if len(candidates) < requested_count:
            reasons.append(
                f"変更の異なる改訂案は時間内に{len(candidates)}案見つかりました。"
            )
        return candidates, reasons
    return [], [
        "変更依頼者以外の日時変更を"
        f"{maximum_non_requester_changes}人以内に抑えた"
        "自動改訂案は、探索時間内に見つかりませんでした。"
        "変更人数上限を増やすか、探索時間を延長するか、"
        "完全手動調整を利用してください。"
    ]
