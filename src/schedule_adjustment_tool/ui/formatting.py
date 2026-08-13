"""Shared date and deadline presentation helpers."""

from datetime import date

from schedule_adjustment_tool.domain.app_config import normalize_deadline
from schedule_adjustment_tool.domain.models import WEEKDAY_LABELS


def format_date_with_weekday(value: str | date) -> str:
    day = date.fromisoformat(value) if isinstance(value, str) else value
    return f"{day.isoformat()}（{WEEKDAY_LABELS[day.weekday()]}）"


def format_datetime_with_weekday(value: str) -> str:
    if not value:
        return ""
    moment = normalize_deadline(value)
    if moment is None:
        return value
    return (
        f"{moment.date().isoformat()}（{WEEKDAY_LABELS[moment.weekday()]}）"
        f" {moment.strftime('%H:%M')}"
    )
