"""Read-only mappings from stored domain values to UI presentation labels."""

from types import MappingProxyType
from typing import Mapping

from schedule_adjustment_tool.domain.models import (
    CANDIDATE_SEARCH_MODE_AUTO,
    CANDIDATE_SEARCH_MODE_RELAXED_ONLY,
    CANDIDATE_SEARCH_MODE_STRICT_ONLY,
    ROLE_DISPLAY_COLORS,
    ROLE_DISPLAY_LABELS,
)


STATUS_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "draft": "準備中",
        "collecting": "回答受付中",
        "closed": "回答締切",
        "confirmed": "日程確定",
    }
)
INPUT_STATUS_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "not_started": "未入力",
        "draft": "下書き",
        "submitted": "提出済み",
    }
)
CANDIDATE_SEARCH_MODE_LABELS: Mapping[str, str] = MappingProxyType(
    {
        CANDIDATE_SEARCH_MODE_AUTO: "自動（厳密→近似）",
        CANDIDATE_SEARCH_MODE_STRICT_ONLY: "厳密",
        CANDIDATE_SEARCH_MODE_RELAXED_ONLY: "近似",
    }
)
CANDIDATE_SEARCH_MODE_CHOICES = tuple(CANDIDATE_SEARCH_MODE_LABELS.items())

ROLE_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "university": "大学生役",
        "high_school": "高校生役",
    }
)
ROLE_DISPLAY_MODE_LABELS: Mapping[str, str] = MappingProxyType(
    {
        ROLE_DISPLAY_LABELS: "役割名を表示",
        ROLE_DISPLAY_COLORS: "色で区別",
    }
)
ROLE_DISPLAY_MODE_CHOICES = tuple(ROLE_DISPLAY_MODE_LABELS.values())
