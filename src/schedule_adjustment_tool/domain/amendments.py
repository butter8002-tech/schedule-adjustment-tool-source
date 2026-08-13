from __future__ import annotations

import json
from collections import defaultdict
from copy import deepcopy
from datetime import date
from typing import Any, Iterable

DEFAULT_DM_TEMPLATE_PREFIX = (
    "おつかれさまです！練習会の日程変更の相談です！\n"
    "今のところ、次の変更を考えています。"
)
DEFAULT_DM_TEMPLATE_SUFFIX = (
    "変更後の日程で参加できるものがあるか教えてください！"
)
AMENDMENT_DOCUMENT_SCHEMA_VERSION = 1
AMENDMENT_REPLY_STATUSES = {"unanswered", "possible", "impossible"}
AMENDMENT_DRAFT_SOURCES = {"amendment_proposal", "manual_amendment"}

_ROLE_FIELDS = (
    ("university", "university_role_member_ids"),
    ("high_school", "high_school_role_member_ids"),
)
_MODE_LABELS = {
    "in_person": "対面",
    "zoom": "Zoom",
}
_WEEKDAY_LABELS = {
    0: "月",
    1: "火",
    2: "水",
    3: "木",
    4: "金",
    5: "土",
    6: "日",
}


def empty_amendment_workspace() -> dict[str, Any]:
    return {
        "schema_version": AMENDMENT_DOCUMENT_SCHEMA_VERSION,
        "active_amendment_id": "",
        "amendments": [],
    }


def normalize_amendment_workspace(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return empty_amendment_workspace()
    normalized = empty_amendment_workspace()
    normalized["active_amendment_id"] = str(
        value.get("active_amendment_id", "")
    )
    amendments = value.get("amendments", [])
    if isinstance(amendments, list):
        normalized["amendments"] = []
        for item in amendments:
            if not isinstance(item, dict) or not str(item.get("id", "")):
                continue
            amendment = deepcopy(item)
            requests = amendment_requests(amendment)
            if requests:
                amendment["requests"] = requests
                amendment["requester_ids"] = list(
                    dict.fromkeys(
                        str(request["requester_id"]) for request in requests
                    )
                )
                amendment["unavailable_slots_by_participant"] = (
                    amendment_unavailable_slots_by_participant(amendment)
                )
            normalized["amendments"].append(amendment)
    if "_storage_version" in value:
        try:
            normalized["_storage_version"] = int(value["_storage_version"])
        except (TypeError, ValueError):
            normalized["_storage_version"] = 0
    return normalized


def active_amendment(workspace: object) -> dict[str, Any] | None:
    normalized = (
        workspace
        if isinstance(workspace, dict)
        and isinstance(workspace.get("amendments"), list)
        else normalize_amendment_workspace(workspace)
    )
    active_id = str(normalized.get("active_amendment_id", ""))
    return next(
        (
            item
            for item in normalized["amendments"]
            if str(item.get("id", "")) == active_id
            and str(item.get("status", "draft")) == "draft"
        ),
        None,
    )


def amendment_requests(amendment: object) -> list[dict[str, Any]]:
    if not isinstance(amendment, dict):
        return []
    raw_requests = amendment.get("requests", [])
    requests: list[dict[str, Any]] = []
    if isinstance(raw_requests, list):
        for index, raw_request in enumerate(raw_requests, start=1):
            if not isinstance(raw_request, dict):
                continue
            requester_id = str(
                raw_request.get(
                    "requester_id",
                    raw_request.get("participant_id", ""),
                )
            )
            if not requester_id:
                continue
            unavailable_slots = sorted(
                {
                    str(value)
                    for value in raw_request.get("unavailable_slots", [])
                    if str(value)
                }
            )
            if not unavailable_slots:
                continue
            request = deepcopy(raw_request)
            request["id"] = str(
                request.get("id")
                or f"request-{index}-{requester_id}"
            )
            request["requester_id"] = requester_id
            request["requester_name"] = str(
                request.get(
                    "requester_name",
                    request.get("participant_name", requester_id),
                )
            )
            request["unavailable_slots"] = unavailable_slots
            request["reason"] = str(request.get("reason", ""))
            requests.append(request)
    if requests:
        return requests

    requester_id = str(
        amendment.get("requester_id", amendment.get("participant_id", ""))
    )
    unavailable_slots = sorted(
        {
            str(value)
            for value in amendment.get("unavailable_slots", [])
            if str(value)
        }
    )
    if not requester_id or not unavailable_slots:
        return []
    return [
        {
            "id": f"legacy-{requester_id}",
            "requester_id": requester_id,
            "requester_name": str(
                amendment.get(
                    "requester_name",
                    amendment.get("participant_name", requester_id),
                )
            ),
            "unavailable_slots": unavailable_slots,
            "reason": str(amendment.get("reason", "")),
            "created_at": str(amendment.get("created_at", "")),
            "created_by": str(amendment.get("created_by", "")),
        }
    ]


def amendment_unavailable_slots_by_participant(
    amendment: object,
) -> dict[str, list[str]]:
    grouped: dict[str, set[str]] = defaultdict(set)
    for request in amendment_requests(amendment):
        grouped[str(request["requester_id"])].update(
            map(str, request.get("unavailable_slots", []))
        )
    return {
        participant_id: sorted(slots)
        for participant_id, slots in grouped.items()
    }


def amendment_requester_ids(amendment: object) -> set[str]:
    return set(amendment_unavailable_slots_by_participant(amendment))


def option_key(day_text: str, period: int, meeting_mode: str) -> str:
    return f"{day_text}#{int(period)}#{meeting_mode}"


def parse_option_key(value: str) -> tuple[str, int, str]:
    day_text, period_text, meeting_mode = str(value).rsplit("#", 2)
    return day_text, int(period_text), meeting_mode


def format_dm_option(value: str) -> str:
    day_text, period, meeting_mode = parse_option_key(value)
    day = date.fromisoformat(day_text)
    mode_label = _MODE_LABELS.get(meeting_mode, meeting_mode)
    return (
        f"{day.month}月{day.day}日（{_WEEKDAY_LABELS[day.weekday()]}）"
        f"{period}限・{mode_label}"
    )


def schedule_assignments(
    schedule: dict[str, Any] | None,
) -> dict[str, dict[str, set[str]]]:
    assignments: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for session in (schedule or {}).get("sessions", []):
        if not isinstance(session, dict):
            continue
        try:
            key = option_key(
                str(session["date"]),
                int(session["period"]),
                str(session.get("meeting_mode", "in_person")),
            )
        except (KeyError, TypeError, ValueError):
            continue
        for role, ids_field in _ROLE_FIELDS:
            for participant_id in session.get(ids_field, []):
                assignments[str(participant_id)][key].add(role)
    return {
        participant_id: {
            key: set(roles) for key, roles in options.items()
        }
        for participant_id, options in assignments.items()
    }


def participant_schedule_changes(
    base_schedule: dict[str, Any],
    proposal: dict[str, Any],
    participant_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    base = schedule_assignments(base_schedule)
    revised = schedule_assignments(proposal)
    names = participant_names or {}
    result: list[dict[str, Any]] = []
    for participant_id in sorted(set(base) | set(revised)):
        before = base.get(participant_id, {})
        after = revised.get(participant_id, {})
        before_keys = set(before)
        after_keys = set(after)
        removed = sorted(before_keys - after_keys)
        added = sorted(after_keys - before_keys)
        role_changes = [
            {
                "option_key": key,
                "before_roles": sorted(before[key]),
                "after_roles": sorted(after[key]),
            }
            for key in sorted(before_keys & after_keys)
            if before[key] != after[key]
        ]
        if not removed and not added and not role_changes:
            continue
        result.append(
            {
                "participant_id": participant_id,
                "participant_name": names.get(participant_id, participant_id),
                "current_options": removed,
                "proposed_options": added,
                "role_changes": role_changes,
                "dm_required": bool(removed or added),
            }
        )
    return result


def amendment_movement_signature(
    base_schedule: dict[str, Any],
    proposal: dict[str, Any],
) -> str:
    base = schedule_assignments(base_schedule)
    revised = schedule_assignments(proposal)
    changed = {
        participant_id: sorted(revised.get(participant_id, {}))
        for participant_id in sorted(set(base) | set(revised))
        if set(base.get(participant_id, {}))
        != set(revised.get(participant_id, {}))
    }
    return json.dumps(
        changed,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def amendment_movement_metrics(
    base_schedule: dict[str, Any],
    proposal: dict[str, Any],
    requester_ids: str | Iterable[str],
) -> dict[str, int]:
    normalized_requester_ids = (
        {str(requester_ids)}
        if isinstance(requester_ids, str)
        else {str(value) for value in requester_ids}
    )
    base = schedule_assignments(base_schedule)
    revised = schedule_assignments(proposal)
    non_requester_changed = 0
    non_requester_deviation = 0
    requester_deviation = 0
    for participant_id in set(base) | set(revised):
        before = set(base.get(participant_id, {}))
        after = set(revised.get(participant_id, {}))
        deviation = len(before.symmetric_difference(after))
        if participant_id in normalized_requester_ids:
            requester_deviation += deviation
        elif deviation:
            non_requester_changed += 1
            non_requester_deviation += deviation
    return {
        "amendment_non_requester_changed_count": non_requester_changed,
        "amendment_non_requester_slot_deviation": non_requester_deviation,
        "amendment_requester_slot_deviation": requester_deviation,
    }


def amendment_candidate_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    metrics = candidate.get("metrics", {})
    return (
        int(metrics.get("amendment_non_requester_changed_count", 0)),
        int(metrics.get("amendment_non_requester_slot_deviation", 0)),
        int(metrics.get("amendment_requester_slot_deviation", 0)),
        str(metrics.get("amendment_movement_signature", "")),
    )


def build_dm_text(
    *,
    prefix: str,
    current_options: Iterable[str],
    proposed_options: Iterable[str],
    suffix: str,
) -> str:
    current_labels = [format_dm_option(value) for value in current_options]
    proposed_labels = [format_dm_option(value) for value in proposed_options]
    lines: list[str] = []
    if str(prefix).strip():
        lines.append(str(prefix).strip())
    lines.append(f"現在：{'／'.join(current_labels) if current_labels else 'なし'}")
    lines.append(
        f"変更後：{'／'.join(proposed_labels) if proposed_labels else 'なし'}"
    )
    if str(suffix).strip():
        lines.append(str(suffix).strip())
    return "\n".join(lines)


def build_dm_messages(
    base_schedule: dict[str, Any],
    proposals: list[dict[str, Any]],
    participant_names: dict[str, str],
    *,
    prefix: str = DEFAULT_DM_TEMPLATE_PREFIX,
    suffix: str = DEFAULT_DM_TEMPLATE_SUFFIX,
) -> list[dict[str, Any]]:
    aggregated: dict[str, dict[str, Any]] = {}
    for proposal in proposals:
        revision_id = str(
            proposal.get("schedule_revision", {}).get("id", "")
        )
        for change in participant_schedule_changes(
            base_schedule,
            proposal,
            participant_names,
        ):
            if not change["dm_required"]:
                continue
            participant_id = str(change["participant_id"])
            item = aggregated.setdefault(
                participant_id,
                {
                    "participant_id": participant_id,
                    "participant_name": change["participant_name"],
                    "current_options": set(),
                    "proposed_options": set(),
                    "proposal_revision_ids_by_option": defaultdict(set),
                },
            )
            item["current_options"].update(change["current_options"])
            item["proposed_options"].update(change["proposed_options"])
            for proposed_option in change["proposed_options"]:
                if revision_id:
                    item["proposal_revision_ids_by_option"][
                        proposed_option
                    ].add(revision_id)

    messages: list[dict[str, Any]] = []
    for participant_id, item in sorted(
        aggregated.items(),
        key=lambda pair: (str(pair[1]["participant_name"]), pair[0]),
    ):
        current_options = sorted(item["current_options"])
        proposed_options = sorted(item["proposed_options"])
        messages.append(
            {
                "participant_id": participant_id,
                "participant_name": item["participant_name"],
                "current_options": current_options,
                "proposed_options": proposed_options,
                "proposal_revision_ids_by_option": {
                    option: sorted(revision_ids)
                    for option, revision_ids in item[
                        "proposal_revision_ids_by_option"
                    ].items()
                },
                "message": build_dm_text(
                    prefix=prefix,
                    current_options=current_options,
                    proposed_options=proposed_options,
                    suffix=suffix,
                ),
            }
        )
    return messages
