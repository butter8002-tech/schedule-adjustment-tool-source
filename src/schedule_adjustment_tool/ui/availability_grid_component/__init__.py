from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from schedule_adjustment_tool.domain.japanese_holidays import is_japanese_holiday
from schedule_adjustment_tool.domain.models import WEEKDAY_LABELS, make_slot_key


_COMPONENT_PATH = Path(__file__).parent / "frontend"
_availability_grid = components.declare_component(
    "availability_grid_v4",
    path=str(_COMPONENT_PATH),
)


def _format_date_with_weekday(value: date) -> str:
    return f"{value.isoformat()}（{WEEKDAY_LABELS[value.weekday()]}）"


def _date_cell_type(value: date) -> str:
    if is_japanese_holiday(value):
        return "holiday"
    if value.weekday() == 5:
        return "saturday"
    if value.weekday() == 6:
        return "sunday"
    return ""


def _server_state_id(
    days: list[date],
    periods: list[int],
    availability: set[str],
    zoom_availability: set[str],
    gap_days: set[date],
) -> str:
    payload = {
        "days": [day.isoformat() for day in days],
        "periods": [int(period) for period in periods],
        "availability": sorted(map(str, availability)),
        "zoom_availability": sorted(map(str, zoom_availability)),
        "gap_days": sorted(day.isoformat() for day in gap_days),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def availability_grid(
    days: list[date],
    *,
    periods: list[int],
    availability: set[str],
    zoom_availability: set[str],
    key: str,
    disabled: bool,
    active_project_days: set[date] | None = None,
    project_titles_by_day: dict[date, list[str]] | None = None,
    active_project_titles_by_day: dict[date, list[str]] | None = None,
    gap_days: set[date] | None = None,
    show_actions: bool = True,
    action_buttons: list[dict[str, Any]] | None = None,
) -> tuple[set[str], set[str], str]:
    return _availability_grid_value(
        days,
        periods=periods,
        availability=availability,
        zoom_availability=zoom_availability,
        key=key,
        disabled=disabled,
        active_project_days=active_project_days,
        project_titles_by_day=project_titles_by_day,
        active_project_titles_by_day=active_project_titles_by_day,
        gap_days=gap_days,
        show_actions=show_actions,
        action_buttons=action_buttons,
        render_mode="grid",
        storage_key=key,
    )


def availability_grid_actions(
    days: list[date],
    *,
    periods: list[int],
    availability: set[str],
    zoom_availability: set[str],
    key: str,
    storage_key: str,
    disabled: bool,
    action_buttons: list[dict[str, Any]],
    gap_days: set[date] | None = None,
) -> tuple[set[str], set[str], str]:
    return _availability_grid_value(
        days,
        periods=periods,
        availability=availability,
        zoom_availability=zoom_availability,
        key=key,
        disabled=disabled,
        gap_days=gap_days,
        show_actions=False,
        action_buttons=action_buttons,
        render_mode="actions",
        storage_key=storage_key,
    )


def _availability_grid_value(
    days: list[date],
    *,
    periods: list[int],
    availability: set[str],
    zoom_availability: set[str],
    key: str,
    disabled: bool,
    active_project_days: set[date] | None = None,
    project_titles_by_day: dict[date, list[str]] | None = None,
    active_project_titles_by_day: dict[date, list[str]] | None = None,
    gap_days: set[date] | None = None,
    show_actions: bool = True,
    action_buttons: list[dict[str, Any]] | None = None,
    render_mode: str,
    storage_key: str,
) -> tuple[set[str], set[str], str]:
    highlight_active_project_days = active_project_days is not None
    active_project_days = active_project_days or set()
    highlight_active_project_titles = active_project_titles_by_day is not None
    project_titles_by_day = project_titles_by_day or {}
    active_project_titles_by_day = active_project_titles_by_day or {}
    gap_days = gap_days or set()
    state_key = f"{key}_value"
    server_state_id = _server_state_id(
        days, periods, availability, zoom_availability, gap_days
    )
    default_value = {
        "availability": sorted(availability),
        "zoom_availability": sorted(zoom_availability),
        "zoom_days": {},
        "zoom_initialized_days": {},
        "server_state_id": server_state_id,
    }
    current_value = st.session_state.get(state_key, default_value)
    if not isinstance(current_value, dict):
        current_value = default_value
    elif current_value.get("server_state_id") != server_state_id:
        current_value = default_value

    render_availability_source = current_value.get(
        "state_availability", current_value.get("availability", [])
    )
    render_zoom_availability_source = current_value.get(
        "state_zoom_availability", current_value.get("zoom_availability", [])
    )
    current_availability = {str(slot_key) for slot_key in render_availability_source}
    current_zoom_availability = {
        str(slot_key) for slot_key in render_zoom_availability_source
    }
    raw_zoom_days = current_value.get("zoom_days", {})
    current_zoom_days = raw_zoom_days if isinstance(raw_zoom_days, dict) else {}
    raw_zoom_initialized_days = current_value.get("zoom_initialized_days", {})
    current_zoom_initialized_days = (
        raw_zoom_initialized_days
        if isinstance(raw_zoom_initialized_days, dict)
        else {}
    )
    zoom_days = {
        day.isoformat(): bool(
            current_zoom_days.get(day.isoformat())
            or any(
                make_slot_key(day, period) in current_zoom_availability
                for period in periods
            )
        )
        for day in days
    }
    component_value: dict[str, Any] | None = _availability_grid(
        days=[
            {
                "iso": day.isoformat(),
                "label": _format_date_with_weekday(day),
                "active": (
                    day in active_project_days
                    if highlight_active_project_days
                    else True
                ),
                "project_titles": project_titles_by_day.get(day, []),
                "active_project_titles": (
                    active_project_titles_by_day.get(day, [])
                    if highlight_active_project_titles
                    else None
                ),
                "day_type": _date_cell_type(day),
                "gap": day in gap_days,
            }
            for day in days
        ],
        periods=periods,
        availability=sorted(current_availability),
        zoom_availability=sorted(current_zoom_availability),
        zoom_days=zoom_days,
        zoom_initialized_days={
            day.isoformat(): bool(
                current_zoom_initialized_days.get(day.isoformat())
                or zoom_days.get(day.isoformat())
            )
            for day in days
        },
        disabled=disabled,
        show_actions=show_actions,
        action_buttons=action_buttons or [],
        render_mode=render_mode,
        storage_key=storage_key,
        server_state_id=server_state_id,
        key=key,
        default=current_value,
    )
    if isinstance(component_value, dict):
        visible_slot_keys = {
            make_slot_key(day, period)
            for day in days
            for period in periods
        }
        preserved_availability = {
            str(slot_key)
            for slot_key in availability
            if str(slot_key) not in visible_slot_keys
        }
        preserved_zoom_availability = {
            str(slot_key)
            for slot_key in zoom_availability
            if str(slot_key) not in visible_slot_keys
        }
        component_value["availability"] = sorted(
            {
                str(slot_key)
                for slot_key in component_value.get("availability", [])
            }
            | preserved_availability
        )
        component_value["zoom_availability"] = sorted(
            {
                str(slot_key)
                for slot_key in component_value.get("zoom_availability", [])
            }
            | preserved_zoom_availability
        )
        st.session_state[state_key] = component_value
    else:
        component_value = current_value

    selected_availability = {
        str(slot_key) for slot_key in component_value.get("availability", [])
    }
    selected_zoom_availability = {
        str(slot_key) for slot_key in component_value.get("zoom_availability", [])
    }
    action = str(component_value.get("action", ""))
    nonce = str(component_value.get("nonce", ""))
    handled_nonce_key = f"{key}_handled_nonce"
    if not nonce or st.session_state.get(handled_nonce_key) == nonce:
        action = ""
    elif action:
        st.session_state[handled_nonce_key] = nonce
    return selected_availability, selected_zoom_availability, action
