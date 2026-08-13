from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from schedule_adjustment_tool.domain.models import (
    Participant,
    WEEKDAY_LABELS,
)


_COMPONENT_PATH = Path(__file__).parent / "frontend"
_schedule_calendar_editor = components.declare_component(
    "schedule_calendar_editor_v3",
    path=str(_COMPONENT_PATH),
)


def _editor_state_id(
    days: list[date],
    periods: list[int],
    sessions: list[dict[str, Any]],
    participants: list[Participant],
    participant_required_counts: dict[str, int],
    participant_role_required_counts: dict[str, dict[str, int] | None],
    *,
    max_groups_per_slot: int,
    university_role_size: int,
    high_school_role_size: int,
    show_optimization_controls: bool,
    show_date_lock_controls: bool,
    excluded_dates: list[str] | set[str] | None,
) -> str:
    payload = {
        "days": [day.isoformat() for day in days],
        "periods": [int(period) for period in periods],
        "sessions": sessions,
        "participants": [
            {
                "id": participant.id,
                "name": participant.name,
                "active": participant.active,
                "approved": participant.approved,
                "availability": participant.availability,
                "zoom_availability": participant.zoom_availability,
                "required_total_count": max(
                    0,
                    int(participant_required_counts.get(participant.id, 0)),
                ),
                "required_role_counts": participant_role_required_counts.get(
                    participant.id
                ),
            }
            for participant in participants
        ],
        "max_groups_per_slot": int(max_groups_per_slot),
        "university_role_size": int(university_role_size),
        "high_school_role_size": int(high_school_role_size),
        "show_optimization_controls": bool(show_optimization_controls),
        "show_date_lock_controls": bool(show_date_lock_controls),
        "excluded_dates": sorted(
            str(value) for value in (excluded_dates or [])
        ),
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def schedule_calendar_editor(
    days: list[date],
    *,
    periods: list[int],
    sessions: list[dict[str, Any]],
    participants: list[Participant],
    participant_required_counts: dict[str, int],
    participant_role_required_counts: dict[str, dict[str, int] | None],
    max_groups_per_slot: int,
    university_role_size: int,
    high_school_role_size: int,
    key: str,
    show_optimization_controls: bool = True,
    show_date_lock_controls: bool = True,
    excluded_dates: list[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    excluded_date_set = {str(value) for value in (excluded_dates or [])}
    server_state_id = _editor_state_id(
        days,
        periods,
        sessions,
        participants,
        participant_required_counts,
        participant_role_required_counts,
        max_groups_per_slot=max_groups_per_slot,
        university_role_size=university_role_size,
        high_school_role_size=high_school_role_size,
        show_optimization_controls=show_optimization_controls,
        show_date_lock_controls=show_date_lock_controls,
        excluded_dates=excluded_date_set,
    )
    state_key = f"{key}_value"
    default_value = {
        "sessions": deepcopy(sessions),
        "server_state_id": server_state_id,
    }
    current_value = st.session_state.get(state_key, default_value)
    if (
        not isinstance(current_value, dict)
        or current_value.get("server_state_id") != server_state_id
        or not isinstance(current_value.get("sessions"), list)
    ):
        current_value = default_value

    component_value: dict[str, Any] | None = _schedule_calendar_editor(
        days=[
            {
                "iso": day.isoformat(),
                "label": (
                    f"{day.isoformat()}（{WEEKDAY_LABELS[day.weekday()]}）"
                ),
                "day_type": (
                    "saturday"
                    if day.weekday() == 5
                    else "sunday"
                    if day.weekday() == 6
                    else ""
                ),
                "excluded": day.isoformat() in excluded_date_set,
            }
            for day in days
        ],
        periods=[int(period) for period in periods],
        initial_sessions=deepcopy(sessions),
        sessions=deepcopy(current_value["sessions"]),
        participants=[
            {
                "id": participant.id,
                "name": participant.name,
                "active": participant.active,
                "approved": participant.approved,
                "input_status": participant.input_status,
                "availability": list(participant.availability),
                "zoom_availability": list(participant.zoom_availability),
                "required_total_count": max(
                    0,
                    int(participant_required_counts.get(participant.id, 0)),
                ),
                "required_role_counts": participant_role_required_counts.get(
                    participant.id
                ),
            }
            for participant in participants
        ],
        max_groups_per_slot=max(1, int(max_groups_per_slot)),
        university_role_size=max(1, int(university_role_size)),
        high_school_role_size=max(1, int(high_school_role_size)),
        server_state_id=server_state_id,
        storage_key=key,
        show_optimization_controls=bool(show_optimization_controls),
        show_date_lock_controls=bool(show_date_lock_controls),
        excluded_dates=sorted(excluded_date_set),
        key=key,
        default=current_value,
    )
    if (
        isinstance(component_value, dict)
        and isinstance(component_value.get("sessions"), list)
    ):
        component_value["server_state_id"] = server_state_id
        st.session_state[state_key] = component_value
        current_value = component_value
    return deepcopy(current_value["sessions"])
