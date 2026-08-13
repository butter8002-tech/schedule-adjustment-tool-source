"""Evaluation-profile helpers for comparing saved candidates."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any

import streamlit as st

from schedule_adjustment_tool.domain.evaluation_config import (
    EVALUATION_DEFINITIONS,
    EVALUATION_DISPLAY_ORDER,
    PRIORITY_LABELS,
    normalize_evaluation_settings,
)
from schedule_adjustment_tool.domain.models import Config, Participant


LEGACY_EVALUATION_PROFILE_KEY = "__legacy_evaluation_profile__"
LEGACY_EVALUATION_SOURCE = "legacy_current"


@dataclass(frozen=True)
class EvaluationProfile:
    """One evaluation-settings snapshot that can be selected for comparison."""

    key: str
    label: str
    snapshot: dict[str, Any]
    is_current: bool = False


def evaluation_snapshot_from_config(config: Config) -> dict[str, Any]:
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


def evaluation_snapshot_from_candidate(
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    if candidate.get("evaluation_config_source") == LEGACY_EVALUATION_SOURCE:
        return None
    snapshot = candidate.get("evaluation_config")
    if not isinstance(snapshot, dict):
        return None
    if not isinstance(snapshot.get("evaluation_settings"), dict):
        return None
    try:
        performance_avoid_days = int(snapshot.get("performance_avoid_days", 0))
    except (TypeError, ValueError):
        performance_avoid_days = 0
    avoided_periods: list[int] = []
    for period in snapshot.get("avoided_periods", []):
        try:
            avoided_periods.append(int(period))
        except (TypeError, ValueError):
            continue
    return {
        "evaluation_settings": normalize_evaluation_settings(
            snapshot["evaluation_settings"]
        ),
        "performance_date": str(snapshot.get("performance_date", "")),
        "performance_avoid_days": performance_avoid_days,
        "avoided_periods": sorted(set(avoided_periods)),
    }


def evaluation_profile_key(snapshot: dict[str, Any]) -> str:
    serialized = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()[:16]


def candidate_evaluation_profile_key(candidate: dict[str, Any]) -> str:
    snapshot = evaluation_snapshot_from_candidate(candidate)
    if snapshot is None:
        return LEGACY_EVALUATION_PROFILE_KEY
    return evaluation_profile_key(snapshot)


def build_evaluation_profiles(
    candidates: list[dict[str, Any]],
    config: Config,
) -> list[EvaluationProfile]:
    current_snapshot = evaluation_snapshot_from_config(config)
    current_key = evaluation_profile_key(current_snapshot)
    snapshots: list[tuple[str, dict[str, Any]]] = [(current_key, current_snapshot)]
    known_keys = {current_key}
    for candidate in candidates:
        snapshot = evaluation_snapshot_from_candidate(candidate)
        if snapshot is None:
            continue
        key = evaluation_profile_key(snapshot)
        if key in known_keys:
            continue
        known_keys.add(key)
        snapshots.append((key, snapshot))

    profiles: list[EvaluationProfile] = []
    other_index = 0
    for key, snapshot in snapshots:
        if key == current_key:
            profiles.append(
                EvaluationProfile(
                    key=key,
                    label="現在の評価条件",
                    snapshot=snapshot,
                    is_current=True,
                )
            )
            continue
        suffix = (
            chr(ord("A") + other_index)
            if other_index < 26
            else str(other_index + 1)
        )
        other_index += 1
        profiles.append(
            EvaluationProfile(
                key=key,
                label=f"保存時の評価条件{suffix}",
                snapshot=snapshot,
            )
        )
    return profiles


def profile_label_for_candidate(
    candidate: dict[str, Any],
    profiles: list[EvaluationProfile],
) -> str:
    key = candidate_evaluation_profile_key(candidate)
    if key == LEGACY_EVALUATION_PROFILE_KEY:
        return "評価条件不明（現在設定で再評価）"
    for profile in profiles:
        if profile.key == key:
            return profile.label
    return "保存時の評価条件"


def _evaluation_setting_label(
    evaluation_id: str,
    setting: dict[str, Any],
) -> str:
    if not bool(setting.get("enabled")) or setting.get("policy") == "ignore":
        return "評価対象外"
    policies = EVALUATION_DEFINITIONS[evaluation_id]["policies"]
    policy = str(setting.get("policy", ""))
    policy_label = str(policies.get(policy, policy))
    priority_label = PRIORITY_LABELS.get(
        str(setting.get("priority", "")),
        str(setting.get("priority", "")),
    )
    return f"{policy_label}（{priority_label}）"


def _periods_label(periods: object) -> str:
    if not isinstance(periods, list) or not periods:
        return "なし"
    return "、".join(f"{int(period)}限" for period in periods)


def _evaluation_profile_differences(
    profile: EvaluationProfile,
    current_snapshot: dict[str, Any],
) -> list[str]:
    current_settings = normalize_evaluation_settings(
        current_snapshot["evaluation_settings"]
    )
    profile_settings = normalize_evaluation_settings(
        profile.snapshot["evaluation_settings"]
    )
    differences: list[str] = []
    for evaluation_id in EVALUATION_DISPLAY_ORDER:
        current_value = _evaluation_setting_label(
            evaluation_id,
            current_settings[evaluation_id],
        )
        profile_value = _evaluation_setting_label(
            evaluation_id,
            profile_settings[evaluation_id],
        )
        if current_value != profile_value:
            label = str(EVALUATION_DEFINITIONS[evaluation_id]["label"])
            differences.append(
                f"{label}: 現在「{current_value}」→「{profile_value}」"
            )

    current_date = str(current_snapshot.get("performance_date", "")) or "未設定"
    profile_date = str(profile.snapshot.get("performance_date", "")) or "未設定"
    if current_date != profile_date:
        differences.append(
            f"本番日: 現在「{current_date}」→「{profile_date}」"
        )
    current_avoid_days = int(current_snapshot.get("performance_avoid_days", 0))
    profile_avoid_days = int(profile.snapshot.get("performance_avoid_days", 0))
    if current_avoid_days != profile_avoid_days:
        differences.append(
            "本番直前の回避日数: "
            f"現在「{current_avoid_days}日」→「{profile_avoid_days}日」"
        )
    current_periods = _periods_label(current_snapshot.get("avoided_periods", []))
    profile_periods = _periods_label(profile.snapshot.get("avoided_periods", []))
    if current_periods != profile_periods:
        differences.append(
            f"避ける時限: 現在「{current_periods}」→「{profile_periods}」"
        )
    return differences


def render_evaluation_profile_summary(
    profiles: list[EvaluationProfile],
    config: Config,
) -> None:
    other_profiles = [profile for profile in profiles if not profile.is_current]
    if not other_profiles:
        return
    current_snapshot = evaluation_snapshot_from_config(config)
    profile_names = "、".join(profile.label for profile in other_profiles)
    with st.expander(f"{profile_names}と現在の評価条件の違い"):
        st.caption("保存時の評価条件ごとの差分を簡潔に表示しています。")
        st.table(
            [
                {
                    "評価条件": profile.label,
                    "現在との違い": "、".join(
                        _evaluation_profile_differences(
                            profile,
                            current_snapshot,
                        )
                    ),
                }
                for profile in other_profiles
            ]
        )


def render_evaluation_profile_selector(
    project_id: str,
    candidates: list[dict[str, Any]],
    config: Config,
    *,
    key_suffix: str,
) -> tuple[EvaluationProfile, list[EvaluationProfile]]:
    profiles = build_evaluation_profiles(candidates, config)
    if len(profiles) == 1:
        selected = profiles[0]
        st.caption(f"表示中の評価条件: {selected.label}")
        st.caption("適合度は、表示中の評価条件で計算しています。")
        return selected, profiles

    selected_label = st.selectbox(
        "表示する評価条件",
        [profile.label for profile in profiles],
        key=f"candidate_evaluation_profile_{project_id}_{key_suffix}",
    )
    selected = next(
        profile for profile in profiles if profile.label == selected_label
    )
    st.caption("適合度は、選択中の評価条件で計算しています。")
    render_evaluation_profile_summary(profiles, config)
    return selected, profiles


def evaluate_candidates_for_profile(
    project_id: str,
    candidates: list[dict[str, Any]],
    config: Config,
    participants: list[Participant],
    profile: EvaluationProfile,
    *,
    candidate_version: int | None = None,
) -> list[dict[str, Any]]:
    """Re-score candidates in memory without changing the stored candidates."""

    cache = st.session_state.setdefault(
        "candidate_evaluation_profile_cache",
        {},
    )
    cache_payload = {
        "project_id": project_id,
        "candidate_version": candidate_version,
        "candidates": candidates,
        "config": config.to_dict(),
        "participants": [participant.to_dict() for participant in participants],
        "profile": profile.key,
    }
    serialized = json.dumps(
        cache_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    cache_key = hashlib.sha256(serialized).hexdigest()[:20]
    if cache_key in cache:
        return deepcopy(cache[cache_key])

    if all(
        candidate_evaluation_profile_key(candidate) == profile.key
        for candidate in candidates
    ):
        return deepcopy(candidates)

    from schedule_adjustment_tool.domain.scheduler import (
        refresh_candidate_evaluation,
    )

    evaluation_config = Config.from_dict(
        {**config.to_dict(), **profile.snapshot}
    )
    evaluated = []
    for candidate in candidates:
        evaluated.append(
            refresh_candidate_evaluation(
                deepcopy(candidate),
                config,
                participants,
                evaluation_config=evaluation_config,
            )
        )
    cache[cache_key] = evaluated
    while len(cache) > 32:
        cache.pop(next(iter(cache)))
    return deepcopy(evaluated)


def evaluation_context(
    candidate: dict[str, Any],
    selected_profile: EvaluationProfile,
    profiles: list[EvaluationProfile],
) -> dict[str, object]:
    stored_key = candidate_evaluation_profile_key(candidate)
    return {
        "selected_label": selected_profile.label,
        "stored_label": profile_label_for_candidate(candidate, profiles),
        "recalculated": stored_key != selected_profile.key,
    }
