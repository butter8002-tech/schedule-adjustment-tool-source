from __future__ import annotations

from datetime import date, timedelta


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    day = date(year, month, 1)
    offset = (weekday - day.weekday()) % 7
    return day + timedelta(days=offset + (occurrence - 1) * 7)


def _vernal_equinox(year: int) -> int:
    return int(20.8431 + 0.242194 * (year - 1980) - (year - 1980) // 4)


def _autumnal_equinox(year: int) -> int:
    return int(23.2488 + 0.242194 * (year - 1980) - (year - 1980) // 4)


def japanese_holidays(year: int) -> set[date]:
    holidays = {
        date(year, 1, 1),
        _nth_weekday(year, 1, 0, 2),
        date(year, 2, 11),
        date(year, 2, 23),
        date(year, 3, _vernal_equinox(year)),
        date(year, 4, 29),
        date(year, 5, 3),
        date(year, 5, 4),
        date(year, 5, 5),
        _nth_weekday(year, 7, 0, 3),
        date(year, 8, 11),
        _nth_weekday(year, 9, 0, 3),
        date(year, 9, _autumnal_equinox(year)),
        _nth_weekday(year, 10, 0, 2),
        date(year, 11, 3),
        date(year, 11, 23),
    }
    # 日曜日の祝日は次の平日へ振替。
    substitute_candidates = sorted(day for day in holidays if day.weekday() == 6)
    for holiday in substitute_candidates:
        substitute = holiday + timedelta(days=1)
        while substitute in holidays:
            substitute += timedelta(days=1)
        holidays.add(substitute)

    # 祝日に挟まれた平日は国民の休日。
    current = date(year, 1, 2)
    end = date(year, 12, 30)
    while current <= end:
        if (
            current not in holidays
            and current - timedelta(days=1) in holidays
            and current + timedelta(days=1) in holidays
        ):
            holidays.add(current)
        current += timedelta(days=1)
    return holidays


def is_japanese_holiday(day: date) -> bool:
    return day in japanese_holidays(day.year)
