from __future__ import annotations

from collections.abc import Iterable

import streamlit as st

from schedule_adjustment_tool.ui.manager.routes import normalize_route_id

ROUTE_STATE_PREFIX = "manager_ui_route"
DIRTY_STEPS_PREFIX = "manager_ui_dirty_steps"
REVIEW_STEPS_PREFIX = "manager_ui_review_steps"
STEP_SELECTOR_PREFIX = "manager_ui_step_selector"
PROGRESS_PREFIX = "manager_ui_progress_v1"


def _project_state_key(prefix: str, project_id: str) -> str:
    return f"{prefix}_{project_id}"


def manager_route_key(project_id: str) -> str:
    return _project_state_key(ROUTE_STATE_PREFIX, project_id)


def manager_step_selector_key(project_id: str) -> str:
    return _project_state_key(STEP_SELECTOR_PREFIX, project_id)


def manager_subscreen_selector_key(project_id: str, step_id: str) -> str:
    return f"manager_ui_subscreen_{project_id}_{step_id}"


def _dirty_steps_key(project_id: str) -> str:
    return _project_state_key(DIRTY_STEPS_PREFIX, project_id)


def _review_steps_key(project_id: str) -> str:
    return _project_state_key(REVIEW_STEPS_PREFIX, project_id)


def _progress_key(project_id: str) -> str:
    return _project_state_key(PROGRESS_PREFIX, project_id)


def _progress(project_id: str) -> dict[str, object]:
    value = st.session_state.get(_progress_key(project_id))
    if not isinstance(value, dict):
        value = {}
    value.setdefault("started", set())
    value.setdefault("completed", set())
    value.setdefault("status_overrides", {})
    return value


def _normalise_step_set(value: object) -> set[str]:
    if isinstance(value, (set, list, tuple)):
        return {str(item) for item in value}
    return set()


def manager_started_steps(project_id: str) -> set[str]:
    return _normalise_step_set(_progress(project_id).get("started"))


def manager_completed_steps(project_id: str) -> set[str]:
    return _normalise_step_set(_progress(project_id).get("completed"))


def manager_status_overrides(project_id: str) -> dict[str, str]:
    value = _progress(project_id).get("status_overrides")
    if not isinstance(value, dict):
        return {}
    return {str(step_id): str(status) for step_id, status in value.items()}


def mark_manager_step_started(project_id: str, step_id: str) -> None:
    progress = _progress(project_id)
    started = manager_started_steps(project_id)
    started.add(step_id)
    completed = manager_completed_steps(project_id)
    completed.discard(step_id)
    progress["started"] = started
    progress["completed"] = completed
    st.session_state[_progress_key(project_id)] = progress


def mark_manager_step_completed(project_id: str, step_id: str) -> None:
    progress = _progress(project_id)
    started = manager_started_steps(project_id)
    started.add(step_id)
    completed = manager_completed_steps(project_id)
    completed.add(step_id)
    progress["started"] = started
    progress["completed"] = completed
    st.session_state[_progress_key(project_id)] = progress


def invalidate_manager_steps(
    project_id: str,
    step_ids: Iterable[str],
) -> None:
    progress = _progress(project_id)
    invalidated = {str(step_id) for step_id in step_ids}
    completed = manager_completed_steps(project_id)
    completed.difference_update(invalidated)
    progress["completed"] = completed
    st.session_state[_progress_key(project_id)] = progress


def set_manager_status_overrides(
    project_id: str,
    overrides: dict[str, str],
) -> None:
    progress = _progress(project_id)
    progress["status_overrides"] = {
        str(step_id): str(status) for step_id, status in overrides.items()
    }
    st.session_state[_progress_key(project_id)] = progress


def manager_dirty_steps(project_id: str) -> set[str]:
    value = st.session_state.get(_dirty_steps_key(project_id), set())
    return set(value) if isinstance(value, (set, list, tuple)) else set()


def manager_review_steps(project_id: str) -> set[str]:
    value = st.session_state.get(_review_steps_key(project_id), set())
    return set(value) if isinstance(value, (set, list, tuple)) else set()


def mark_manager_step_dirty(project_id: str, step_id: str) -> None:
    dirty = manager_dirty_steps(project_id)
    dirty.add(step_id)
    st.session_state[_dirty_steps_key(project_id)] = dirty


def mark_manager_step_saved(
    project_id: str,
    step_id: str,
    *,
    downstream_review: Iterable[str] = (),
) -> None:
    dirty = manager_dirty_steps(project_id)
    dirty.discard(step_id)
    st.session_state[_dirty_steps_key(project_id)] = dirty
    review = manager_review_steps(project_id)
    review.update(downstream_review)
    st.session_state[_review_steps_key(project_id)] = review


def clear_manager_step_review(project_id: str, step_id: str) -> None:
    review = manager_review_steps(project_id)
    review.discard(step_id)
    st.session_state[_review_steps_key(project_id)] = review


def set_manager_route(project_id: str, route_id: str) -> None:
    st.session_state[manager_route_key(project_id)] = normalize_route_id(route_id)
