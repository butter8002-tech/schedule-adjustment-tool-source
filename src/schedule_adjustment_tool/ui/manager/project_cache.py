"""Cached project state and deadline handling for manager screens."""

from __future__ import annotations

from copy import deepcopy

import streamlit as st

from schedule_adjustment_tool.domain.app_config import deadline_has_passed
from schedule_adjustment_tool.domain.models import Config, Participant
from schedule_adjustment_tool.storage import (
    StorageConflictError,
    load_candidates_with_version,
    load_confirmed_candidate,
    load_config,
    load_manager_project_overview,
    load_participants,
    load_project_data,
    save_config_fields,
)
from schedule_adjustment_tool.ui.manager.app_cache import (
    clear_prepared_exports,
    load_project_list_cached,
    status_message,
)


PROJECT_LIST_CACHE_KEY = "project_list_cache"
PROJECT_DATA_CACHE_KEY = "project_data_cache"
PROJECT_CONFIG_CACHE_KEY = "project_config_cache"
PROJECT_CANDIDATES_CACHE_KEY = "project_candidates_cache"
PROJECT_CANDIDATE_VERSIONS_CACHE_KEY = "project_candidate_versions_cache"
PROJECT_PARTICIPANTS_CACHE_KEY = "project_participants_cache"
PROJECT_OVERVIEW_CACHE_KEY = "project_overview_cache"
PROJECT_CONFIRMED_CACHE_KEY = "project_confirmed_cache"
PROJECT_CONFIG_DRAFTS_KEY = "project_config_drafts"
PROJECT_CONFIG_DRAFT_VERSIONS_KEY = "project_config_draft_versions"
AUTO_CLOSED_DEADLINES_KEY = "manager_auto_closed_deadlines"


def run_with_status(message: str, operation, *args, **kwargs):
    with st.spinner(message, show_time=True):
        return operation(*args, **kwargs)


def refresh_project_list_cache() -> list[dict]:
    return load_project_list_cached(force=True)


def update_cached_project_summary(project_id: str, config: Config) -> None:
    projects = st.session_state.get(PROJECT_LIST_CACHE_KEY)
    if not isinstance(projects, list):
        return
    for project in projects:
        if str(project.get("id")) == project_id:
            project["title"] = config.title
            project["status"] = config.status
            break


def project_cache() -> dict[str, dict]:
    return st.session_state.setdefault(PROJECT_DATA_CACHE_KEY, {})


def config_cache() -> dict[str, Config]:
    return st.session_state.setdefault(PROJECT_CONFIG_CACHE_KEY, {})


def candidates_cache() -> dict[str, list[dict]]:
    return st.session_state.setdefault(PROJECT_CANDIDATES_CACHE_KEY, {})


def candidate_versions_cache() -> dict[str, int]:
    return st.session_state.setdefault(PROJECT_CANDIDATE_VERSIONS_CACHE_KEY, {})


def overview_cache() -> dict[str, dict]:
    return st.session_state.setdefault(PROJECT_OVERVIEW_CACHE_KEY, {})


def confirmed_cache() -> dict[str, dict | None]:
    return st.session_state.setdefault(PROJECT_CONFIRMED_CACHE_KEY, {})


def load_manager_project_overview_cached(
    project_id: str,
    *,
    force: bool = False,
) -> dict:
    cache = overview_cache()
    if force or project_id not in cache:
        with status_message("企画概要を読み込んでいます..."):
            cache[project_id] = load_manager_project_overview(project_id)
        config_cache()[project_id] = cache[project_id]["config"]
        candidate_versions_cache()[project_id] = int(
            cache[project_id].get("candidate_version", 0)
        )
    return cache[project_id]


def load_project_data_cached(project_id: str, *, force: bool = False) -> dict:
    cache = project_cache()
    if force or project_id not in cache:
        with status_message("企画データを読み込んでいます..."):
            cache[project_id] = load_project_data(project_id)
        config_cache()[project_id] = cache[project_id]["config"]
        candidates_cache()[project_id] = deepcopy(cache[project_id]["candidates"])
        candidate_versions_cache()[project_id] = int(
            cache[project_id].get("candidates_version", 0)
        )
        participant_cache()[project_id] = deepcopy(cache[project_id]["participants"])
        confirmed_cache()[project_id] = deepcopy(cache[project_id]["confirmed"])
    return cache[project_id]


def refresh_project_data_cache(project_id: str) -> dict:
    data = load_project_data_cached(project_id, force=True)
    config_cache()[project_id] = data["config"]
    candidates_cache()[project_id] = deepcopy(data["candidates"])
    candidate_versions_cache()[project_id] = int(
        data.get("candidates_version", 0)
    )
    participant_cache()[project_id] = deepcopy(data["participants"])
    confirmed_cache()[project_id] = deepcopy(data["confirmed"])
    overview_cache().pop(project_id, None)
    return data


def clear_project_data_cache(project_id: str | None = None) -> None:
    if project_id is None:
        st.session_state.pop(PROJECT_DATA_CACHE_KEY, None)
        st.session_state.pop(PROJECT_CONFIG_CACHE_KEY, None)
        st.session_state.pop(PROJECT_CANDIDATES_CACHE_KEY, None)
        st.session_state.pop(PROJECT_CANDIDATE_VERSIONS_CACHE_KEY, None)
        st.session_state.pop(PROJECT_PARTICIPANTS_CACHE_KEY, None)
        st.session_state.pop(PROJECT_OVERVIEW_CACHE_KEY, None)
        st.session_state.pop(PROJECT_CONFIRMED_CACHE_KEY, None)
        return
    project_cache().pop(project_id, None)
    config_cache().pop(project_id, None)
    candidates_cache().pop(project_id, None)
    candidate_versions_cache().pop(project_id, None)
    participant_cache().pop(project_id, None)
    overview_cache().pop(project_id, None)
    confirmed_cache().pop(project_id, None)


def load_project_config_cached(
    project_id: str, *, force: bool = False
) -> Config:
    full_project_cache = st.session_state.get(PROJECT_DATA_CACHE_KEY)
    if (
        not force
        and isinstance(full_project_cache, dict)
        and project_id in full_project_cache
    ):
        return full_project_cache[project_id]["config"]

    cache = config_cache()
    if force or project_id not in cache:
        cache[project_id] = run_with_status(
            "企画情報を読み込んでいます...",
            load_config,
            project_id,
        )
        if (
            isinstance(full_project_cache, dict)
            and project_id in full_project_cache
        ):
            full_project_cache[project_id]["config"] = cache[project_id]
    return cache[project_id]


def refresh_project_config_cache(project_id: str) -> Config:
    refreshed = load_config(project_id)
    config_cache()[project_id] = refreshed
    full_project_cache = st.session_state.get(PROJECT_DATA_CACHE_KEY)
    if (
        isinstance(full_project_cache, dict)
        and project_id in full_project_cache
    ):
        full_project_cache[project_id]["config"] = refreshed
    update_cached_project_summary(project_id, refreshed)
    if project_id in overview_cache():
        overview_cache()[project_id]["config"] = refreshed
        overview_cache()[project_id]["config_issue_count"] = len(
            refreshed.validate()
        )
    return refreshed


def participant_cache() -> dict[str, list[Participant]]:
    return st.session_state.setdefault(PROJECT_PARTICIPANTS_CACHE_KEY, {})


def load_project_participants_cached(
    project_id: str, *, force: bool = False
) -> list[Participant]:
    full_project_cache = st.session_state.get(PROJECT_DATA_CACHE_KEY)
    if (
        not force
        and isinstance(full_project_cache, dict)
        and project_id in full_project_cache
    ):
        return deepcopy(full_project_cache[project_id]["participants"])

    cache = participant_cache()
    if force or project_id not in cache:
        cache[project_id] = run_with_status(
            "参加者一覧を読み込んでいます...",
            load_participants,
            project_id,
        )
    return deepcopy(cache[project_id])


def load_project_confirmed_cached(
    project_id: str,
    *,
    force: bool = False,
) -> dict | None:
    full_project_cache = st.session_state.get(PROJECT_DATA_CACHE_KEY)
    if (
        not force
        and isinstance(full_project_cache, dict)
        and project_id in full_project_cache
    ):
        return deepcopy(full_project_cache[project_id].get("confirmed"))
    cache = confirmed_cache()
    if force or project_id not in cache:
        cache[project_id] = run_with_status(
            "確定日程を読み込んでいます...",
            load_confirmed_candidate,
            project_id,
        )
    return deepcopy(cache[project_id])


def update_cached_config(
    project_id: str,
    updates: dict,
    *,
    storage_version: int | None = None,
) -> Config:
    current = load_project_config_cached(project_id)
    config = Config.from_dict({**current.to_dict(), **updates})
    config._storage_version = (
        getattr(current, "_storage_version", 0)
        if storage_version is None
        else storage_version
    )
    config_cache()[project_id] = config
    full_project_cache = st.session_state.get(PROJECT_DATA_CACHE_KEY)
    if isinstance(full_project_cache, dict) and project_id in full_project_cache:
        full_project_cache[project_id]["config"] = config
    if project_id in overview_cache():
        overview_cache()[project_id]["config"] = config
        overview_cache()[project_id]["config_issue_count"] = len(config.validate())
    update_cached_project_summary(project_id, config)
    return config


def set_cached_participants(
    project_id: str, participants: list[Participant]
) -> None:
    full_project_cache = st.session_state.get(PROJECT_DATA_CACHE_KEY)
    if isinstance(full_project_cache, dict) and project_id in full_project_cache:
        full_project_cache[project_id]["participants"] = participants
    participant_cache()[project_id] = deepcopy(participants)
    # Participant responses and approval flags feed the overview aggregates.
    overview_cache().pop(project_id, None)


def load_project_candidates_cached(
    project_id: str, *, force: bool = False
) -> list[dict]:
    full_project_cache = st.session_state.get(PROJECT_DATA_CACHE_KEY)
    if (
        not force
        and isinstance(full_project_cache, dict)
        and project_id in full_project_cache
    ):
        candidate_versions_cache()[project_id] = int(
            full_project_cache[project_id].get("candidates_version", 0)
        )
        return deepcopy(full_project_cache[project_id]["candidates"])

    cache = candidates_cache()
    if force or project_id not in cache:
        loaded_candidates, loaded_version = run_with_status(
            "候補データを読み込んでいます...",
            load_candidates_with_version,
            project_id,
        )
        cache[project_id] = loaded_candidates
        candidate_versions_cache()[project_id] = int(loaded_version)
    return deepcopy(cache[project_id])


def cached_candidates(project_id: str) -> list[dict]:
    return load_project_candidates_cached(project_id)


def set_cached_candidates(
    project_id: str,
    candidates: list[dict],
    *,
    version: int | None = None,
) -> None:
    full_project_cache = st.session_state.get(PROJECT_DATA_CACHE_KEY)
    if isinstance(full_project_cache, dict) and project_id in full_project_cache:
        full_project_cache[project_id]["candidates"] = candidates
        if version is not None:
            full_project_cache[project_id]["candidates_version"] = version
    candidates_cache()[project_id] = deepcopy(candidates)
    if version is not None:
        candidate_versions_cache()[project_id] = int(version)
    if project_id in overview_cache():
        overview_cache()[project_id]["candidate_count"] = len(candidates)
        overview_cache()[project_id]["candidate_warning_count"] = sum(
            not bool(candidate.get("metrics", {}).get("is_strict_candidate", True))
            for candidate in candidates
        )
        if version is not None:
            overview_cache()[project_id]["candidate_version"] = int(version)
    clear_prepared_exports(project_id)


def candidate_storage_version(project_id: str) -> int:
    versions = candidate_versions_cache()
    if project_id not in versions:
        loaded_candidates, loaded_version = run_with_status(
            "候補データを読み込んでいます...",
            load_candidates_with_version,
            project_id,
        )
        candidates_cache()[project_id] = loaded_candidates
        versions[project_id] = int(loaded_version)
    return int(versions[project_id])


def append_unique_candidates(
    existing_candidates: list[dict],
    new_candidates: list[dict],
    *,
    fingerprint,
    max_candidates: int,
) -> tuple[list[dict], list[dict], int, int]:
    merged = deepcopy(existing_candidates)
    known_fingerprints = {
        fingerprint(candidate) for candidate in existing_candidates
    }
    added: list[dict] = []
    duplicate_count = 0
    capacity_skipped_count = 0
    effective_limit = max(int(max_candidates), len(merged))
    for candidate in new_candidates:
        candidate_fingerprint = fingerprint(candidate)
        if candidate_fingerprint in known_fingerprints:
            duplicate_count += 1
            continue
        if len(merged) >= effective_limit:
            capacity_skipped_count += 1
            continue
        copied_candidate = deepcopy(candidate)
        merged.append(copied_candidate)
        added.append(copied_candidate)
        known_fingerprints.add(candidate_fingerprint)
    return merged, added, duplicate_count, capacity_skipped_count


def set_cached_confirmed(project_id: str, confirmed: dict | None) -> None:
    full_project_cache = st.session_state.get(PROJECT_DATA_CACHE_KEY)
    if isinstance(full_project_cache, dict) and project_id in full_project_cache:
        full_project_cache[project_id]["confirmed"] = confirmed
    confirmed_cache()[project_id] = deepcopy(confirmed)
    if project_id in overview_cache():
        overview_cache()[project_id]["confirmed"] = confirmed is not None
        overview_cache()[project_id]["confirmed_candidate_number"] = (
            confirmed.get("candidate_number", "-") if confirmed else "-"
        )


def config_drafts() -> dict[str, dict]:
    return st.session_state.setdefault(PROJECT_CONFIG_DRAFTS_KEY, {})


def config_draft_versions() -> dict[str, int]:
    return st.session_state.setdefault(PROJECT_CONFIG_DRAFT_VERSIONS_KEY, {})


def project_config_draft(project_id: str, config: Config) -> dict:
    drafts = config_drafts()
    source_versions = config_draft_versions()
    draft = drafts.get(project_id)
    current_version = int(getattr(config, "_storage_version", 0) or 0)
    if (
        not isinstance(draft, dict)
        or draft.get("project_id") != config.project_id
        or source_versions.get(project_id) != current_version
    ):
        draft = config.to_dict()
        drafts[project_id] = draft
        source_versions[project_id] = current_version
    return draft


def update_project_config_draft(
    project_id: str,
    updates: dict,
    *,
    source_version: int | None = None,
) -> None:
    draft = config_drafts().setdefault(project_id, {})
    draft.update(deepcopy(updates))
    if source_version is not None:
        config_draft_versions()[project_id] = int(source_version)


def basic_project_settings_locked(config: Config) -> bool:
    return config.status == "collecting"


def response_deadline_has_passed(config: Config) -> bool:
    if not config.response_deadline:
        return False
    return deadline_has_passed(config.response_deadline)


def close_expired_open_project(
    project_id: str,
    config: Config,
) -> Config:
    """Close only the currently opened project after its deadline."""

    if (
        config.status != "collecting"
        or not response_deadline_has_passed(config)
    ):
        return config
    deadline_key = f"{project_id}:{config.response_deadline}"
    closed_deadlines = st.session_state.setdefault(
        AUTO_CLOSED_DEADLINES_KEY,
        set(),
    )
    if deadline_key in closed_deadlines:
        return config
    try:
        storage_version = save_config_fields(
            project_id,
            {"status": "closed"},
            expected_version=getattr(config, "_storage_version", None),
        )
    except StorageConflictError:
        refreshed = refresh_project_config_cache(project_id)
        if refreshed.status != "collecting":
            closed_deadlines.add(deadline_key)
        return refreshed
    updated = update_cached_config(
        project_id,
        {"status": "closed"},
        storage_version=storage_version,
    )
    closed_deadlines.add(deadline_key)
    return updated
