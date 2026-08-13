from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

PRIORITY_LEVELS = ["highest", "priority", "consider"]
PRIORITY_LABELS = {
    "highest": "最優先",
    "priority": "優先",
    "consider": "考慮",
}
PRIORITY_SCORE_FACTORS = {
    "highest": 4,
    "priority": 2,
    "consider": 1,
}
EVALUATION_SCORE_VERSION = 1
EVALUATION_MAX_PENALTY_SHARE = 0.6
EVALUATION_MEAN_PENALTY_SHARE = 0.4

EVALUATION_DEFINITIONS: dict[str, dict[str, Any]] = {
    "performance_buffer": {
        "label": "本番直前期間の回避",
        "description": "本番直前の指定日数内にある練習会を避けます。",
        "policies": {"avoid": "直前期間を避ける"},
    },
    "avoid_periods": {
        "label": "避ける時限",
        "description": "指定した時限に練習会を置く候補を低く評価します。",
        "policies": {"avoid": "指定時限を避ける"},
    },
    "zoom_meeting": {
        "label": "Zoom開催",
        "description": "6限以外のZoom開催を避けつつ、Zoomなら可の参加者が多い組は許容します。",
        "policies": {
            "avoid": "Zoom開催を避ける",
            "avoid_unless_needed": "Zoomなら可が多い組は許容",
            "ignore": "評価対象外",
        },
    },
    "cohort_balance": {
        "label": "期による経験バランス",
        "description": "最新期だけの組を避け、経験者を各役割へ分散します。",
        "policies": {"experienced_split": "経験者を組・役割に分散"},
    },
    "same_group": {
        "label": "大学生役の同班編成",
        "description": "大学生役が複数の場合、役割指定なし以外を同じ班にそろえます。",
        "policies": {"match": "同じ班を優先"},
    },
    "field_match": {
        "label": "班と高校生役の文理対応",
        "description": "通常班に設定した文理と高校生役の文理を対応させます。",
        "policies": {"match": "文理一致を優先"},
    },
    "session_count": {
        "label": "開催組数",
        "description": "必要条件を満たす練習会組数を少なくします。",
        "policies": {"minimize": "少なくする"},
    },
    "participant_schedule": {
        "label": "個人の複数回参加配置",
        "description": "同じ参加者が複数回参加する場合の配置方針です。",
        "policies": {
            "same_day_consecutive": "同日連続コマを優先",
            "separate_days": "別日に分散を優先",
        },
    },
    "overall_schedule": {
        "label": "練習会全体の日程配置",
        "description": "参加者を横断した開催日の配置方針です。",
        "policies": {
            "spread": "期間内に分散",
            "concentrate": "開催日を集約",
        },
    },
}

# Keep the evaluation controls and the candidate-detail breakdown in the
# order used by the manager's evaluation review.
EVALUATION_DISPLAY_ORDER = (
    "same_group",
    "field_match",
    "zoom_meeting",
    "session_count",
    "participant_schedule",
    "overall_schedule",
    "cohort_balance",
    "performance_buffer",
    "avoid_periods",
)


def default_evaluation_settings() -> dict[str, dict[str, Any]]:
    return {
        "performance_buffer": {
            "enabled": False,
            "policy": "avoid",
            "priority": "priority",
        },
        "avoid_periods": {
            "enabled": False,
            "policy": "avoid",
            "priority": "priority",
        },
        "zoom_meeting": {
            "enabled": True,
            "policy": "avoid_unless_needed",
            "priority": "priority",
        },
        "cohort_balance": {
            "enabled": False,
            "policy": "experienced_split",
            "priority": "priority",
        },
        "same_group": {
            "enabled": False,
            "policy": "match",
            "priority": "priority",
        },
        "field_match": {
            "enabled": False,
            "policy": "match",
            "priority": "priority",
        },
        "session_count": {
            "enabled": True,
            "policy": "minimize",
            "priority": "priority",
        },
        "participant_schedule": {
            "enabled": True,
            "policy": "same_day_consecutive",
            "priority": "priority",
        },
        "overall_schedule": {
            "enabled": True,
            "policy": "spread",
            "priority": "priority",
        },
    }


def clamp_evaluation_penalty(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed):
        return 100.0 if parsed > 0 else 0.0
    return max(0.0, min(100.0, parsed))


def aggregate_evaluation_score(
    penalties: dict[str, object],
    settings: object = None,
) -> dict[str, Any]:
    """Combine enabled evaluation penalties into one 0-100 score.

    Individual penalties use 0 for the best result and 100 for the worst
    normalized result.  Within one priority, the worst item keeps most of its
    influence while the mean preserves the effect of several moderate issues.
    Priority factors are fixed system semantics, not user-editable weights.
    """

    normalized_settings = normalize_evaluation_settings(settings)
    priority_penalties: dict[str, float] = {}
    priority_item_counts: dict[str, int] = {}
    for priority in PRIORITY_LEVELS:
        values = [
            clamp_evaluation_penalty(penalties.get(evaluation_id, 0.0))
            for evaluation_id, setting in normalized_settings.items()
            if setting["enabled"] and setting["priority"] == priority
        ]
        if not values:
            continue
        priority_penalties[priority] = round(
            EVALUATION_MAX_PENALTY_SHARE * max(values)
            + EVALUATION_MEAN_PENALTY_SHARE * (sum(values) / len(values)),
            4,
        )
        priority_item_counts[priority] = len(values)

    active_factor_total = sum(
        PRIORITY_SCORE_FACTORS[priority]
        for priority in priority_penalties
    )
    if active_factor_total <= 0:
        return {
            "evaluation_score": 100.0,
            "evaluation_priority_penalties": {},
            "evaluation_priority_item_counts": {},
            "evaluation_enabled_item_count": 0,
        }

    weighted_penalty = sum(
        PRIORITY_SCORE_FACTORS[priority] * priority_penalty
        for priority, priority_penalty in priority_penalties.items()
    ) / active_factor_total
    return {
        "evaluation_score": round(
            100.0 - max(0.0, min(100.0, weighted_penalty)),
            2,
        ),
        "evaluation_priority_penalties": priority_penalties,
        "evaluation_priority_item_counts": priority_item_counts,
        "evaluation_enabled_item_count": sum(priority_item_counts.values()),
    }


def normalize_evaluation_settings(
    value: object = None,
) -> dict[str, dict[str, Any]]:
    normalized = deepcopy(default_evaluation_settings())
    if not isinstance(value, dict):
        return normalized
    for evaluation_id, definition in EVALUATION_DEFINITIONS.items():
        raw = value.get(evaluation_id)
        if not isinstance(raw, dict):
            continue
        policy = str(raw.get("policy", normalized[evaluation_id]["policy"]))
        priority = str(
            raw.get("priority", normalized[evaluation_id]["priority"])
        )
        normalized[evaluation_id] = {
            "enabled": bool(raw.get("enabled", normalized[evaluation_id]["enabled"])),
            "policy": (
                policy
                if policy in definition["policies"]
                else normalized[evaluation_id]["policy"]
            ),
            "priority": (
                priority
                if priority in PRIORITY_LEVELS
                else normalized[evaluation_id]["priority"]
            ),
        }
    return normalized
