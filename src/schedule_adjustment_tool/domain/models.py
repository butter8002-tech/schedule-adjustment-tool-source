from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4

from schedule_adjustment_tool.domain.app_config import (
    DEFAULT_MAX_TARGET_PERIOD_DAYS as APP_DEFAULT_MAX_TARGET_PERIOD_DAYS,
    MAX_MAX_TARGET_PERIOD_DAYS,
    configured_max_target_period_days,
    deadline_has_passed,
    local_today,
    normalize_deadline,
)
from schedule_adjustment_tool.domain.evaluation_config import normalize_evaluation_settings
from schedule_adjustment_tool.domain.amendments import (
    DEFAULT_DM_TEMPLATE_PREFIX,
    DEFAULT_DM_TEMPLATE_SUFFIX,
)
from schedule_adjustment_tool.domain.participant_attributes import normalize_department

WEEKDAY_LABELS = {
    0: "月",
    1: "火",
    2: "水",
    3: "木",
    4: "金",
    5: "土",
    6: "日",
}

# ``遊撃`` is a real participant group.  ``役割指定なし`` is a practice-session
# setting, not another group value; the label remains as a compatibility alias
# for data written by the previous UI revision.
ROLE_UNSPECIFIED_GROUP = "役割指定なし"
LEGACY_SUPPORT_GROUP = "遊撃"
SUPPORT_GROUP = LEGACY_SUPPORT_GROUP
GROUP_FIELD_OPTIONS = ["文系", "理系", "文理混合"]
PARTICIPATION_MODE_ROLE_BASED = "role_based"
PARTICIPATION_MODE_TOTAL_ONCE = "total_once"
CANDIDATE_SEARCH_MODE_AUTO = "auto"
CANDIDATE_SEARCH_MODE_STRICT_ONLY = "strict_only"
CANDIDATE_SEARCH_MODE_RELAXED_ONLY = "relaxed_only"
CANDIDATE_SEARCH_MODES = {
    CANDIDATE_SEARCH_MODE_AUTO,
    CANDIDATE_SEARCH_MODE_STRICT_ONLY,
    CANDIDATE_SEARCH_MODE_RELAXED_ONLY,
}
ROLE_DISPLAY_LABELS = "labels"
ROLE_DISPLAY_COLORS = "colors"
ROLE_DISPLAY_MODES = {ROLE_DISPLAY_LABELS, ROLE_DISPLAY_COLORS}
PARTICIPATION_MODES = {
    PARTICIPATION_MODE_ROLE_BASED,
    PARTICIPATION_MODE_TOTAL_ONCE,
}
ABSOLUTE_MAX_TARGET_PERIOD_DAYS = MAX_MAX_TARGET_PERIOD_DAYS
DEFAULT_MAX_TARGET_PERIOD_DAYS = APP_DEFAULT_MAX_TARGET_PERIOD_DAYS


def participant_name_identity_key(name: object) -> str:
    return "".join(str(name).split()).casefold()


@dataclass
class Config:
    schema_version: int = 10
    project_id: str = field(default_factory=lambda: uuid4().hex)
    title: str = "ワークショップ練習会"
    description: str = ""
    project_access_password_hash: str = ""
    status: str = "collecting"
    start_date: str = field(
        default_factory=lambda: (local_today() + timedelta(days=10)).isoformat()
    )
    end_date: str = field(
        default_factory=lambda: (local_today() + timedelta(days=24)).isoformat()
    )
    response_deadline: str = field(
        default_factory=lambda: datetime.combine(
            local_today() + timedelta(days=7),
            time(23, 59),
        ).isoformat(timespec="minutes")
    )
    excluded_dates: list[str] = field(default_factory=list)
    performance_date: str = ""
    performance_avoid_days: int = 0
    avoided_periods: list[int] = field(default_factory=list)
    role_display_mode: str = ROLE_DISPLAY_LABELS
    allow_edits_after_deadline: bool = False
    enabled_weekdays: list[int] = field(
        default_factory=lambda: [0, 1, 2, 3, 4, 5, 6]
    )
    enabled_periods: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 5, 6])
    university_role_size: int = 2
    high_school_role_size: int = 2
    participation_requirement_mode: str = PARTICIPATION_MODE_ROLE_BASED
    required_total_count: int = 1
    required_university_count: int = 1
    required_high_school_count: int = 1
    total_extra_limit: int = 0
    max_groups_per_slot: int = 1
    max_candidates: int = 1
    max_sessions_per_person_per_day: int = 2
    avoid_consecutive_periods: bool = False
    search_timeout_seconds: int = 120
    candidate_search_mode: str = CANDIDATE_SEARCH_MODE_AUTO
    random_seed: int = 42
    amendment_candidate_count: int = 3
    amendment_max_non_requester_changes: int = 8
    group_count: int = 1
    support_participation_limit: int = 3
    group_field_assignments: dict[str, str] = field(
        default_factory=lambda: {"1": "文理混合"}
    )
    evaluation_settings: dict[str, dict[str, Any]] = field(
        default_factory=normalize_evaluation_settings
    )
    amendment_dm_template_prefix: str = DEFAULT_DM_TEMPLATE_PREFIX
    amendment_dm_template_suffix: str = DEFAULT_DM_TEMPLATE_SUFFIX

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        defaults = asdict(cls())
        defaults.update({key: value for key, value in data.items() if key in defaults})
        config = cls(**defaults)
        config.evaluation_settings = normalize_evaluation_settings(
            data.get("evaluation_settings")
        )
        if config.role_display_mode not in ROLE_DISPLAY_MODES:
            config.role_display_mode = ROLE_DISPLAY_LABELS
        if config.candidate_search_mode not in CANDIDATE_SEARCH_MODES:
            config.candidate_search_mode = CANDIDATE_SEARCH_MODE_AUTO
        config.avoided_periods = sorted(
            {
                int(period)
                for period in data.get("avoided_periods", [])
                if str(period).isdigit() and 1 <= int(period) <= 6
            }
        )
        normalized_excluded_dates: set[str] = set()
        for value in data.get("excluded_dates", []) or []:
            try:
                normalized_excluded_dates.add(date.fromisoformat(str(value)).isoformat())
            except (TypeError, ValueError):
                continue
        config.excluded_dates = sorted(normalized_excluded_dates)
        config.group_field_assignments = {
            str(group_number): (
                config.group_field_assignments.get(str(group_number), "文理混合")
                if config.group_field_assignments.get(str(group_number), "文理混合")
                in GROUP_FIELD_OPTIONS
                else "文理混合"
            )
            for group_number in range(1, config.group_count + 1)
        }
        return config

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.title.strip():
            errors.append("企画名を入力してください。")
        try:
            start = date.fromisoformat(self.start_date)
            end = date.fromisoformat(self.end_date)
            if start > end:
                errors.append("開始日は終了日以前にしてください。")
            else:
                target_days = (end - start).days + 1
                max_target_days = configured_max_target_period_days()
                if target_days > max_target_days:
                    errors.append(
                        f"対象期間は{max_target_days}日以内にしてください。"
                    )
        except ValueError:
            errors.append("開始日または終了日の形式が不正です。")
        if not self.enabled_weekdays:
            errors.append("対象曜日を1つ以上選択してください。")
        if not self.enabled_periods:
            errors.append("対象時限を1つ以上選択してください。")
        positive_fields = {
            "大学生役人数": self.university_role_size,
            "高校生役人数": self.high_school_role_size,
            "1コマあたり最大組数": self.max_groups_per_slot,
            "候補表示数": self.max_candidates,
        }
        for label, value in positive_fields.items():
            if value < 1:
                errors.append(f"{label}は1以上にしてください。")
        if self.required_total_count < 0:
            errors.append("合計の練習会参加数は0以上にしてください。")
        if self.required_university_count < 0 or self.required_high_school_count < 0:
            errors.append("規定回数は0以上にしてください。")
        if self.participation_requirement_mode not in PARTICIPATION_MODES:
            errors.append("参加回数ルールが不正です。")
        if self.total_extra_limit < 0:
            errors.append("合計参加回数の超過上限は0以上にしてください。")
        if self.max_sessions_per_person_per_day < 1:
            errors.append("1人あたり1日の参加上限は1以上にしてください。")
        if self.search_timeout_seconds < 1:
            errors.append("探索時間上限は1秒以上にしてください。")
        if self.candidate_search_mode not in CANDIDATE_SEARCH_MODES:
            errors.append("候補探索モードが不正です。")
        if not 1 <= self.amendment_candidate_count <= 3:
            errors.append("改訂案の探索数は1〜3にしてください。")
        if self.amendment_max_non_requester_changes < 0:
            errors.append(
                "改訂時の依頼者以外の日時変更上限は0以上にしてください。"
            )
        if self.group_count < 1:
            errors.append("班の数は1以上にしてください。")
        if self.support_participation_limit < 0:
            errors.append("旧形式の役割指定なし上限は0以上にしてください。")
        for group_number in range(1, self.group_count + 1):
            if self.group_field_assignments.get(str(group_number)) not in GROUP_FIELD_OPTIONS:
                errors.append(f"{group_number}班の文理割り当てが不正です。")
        if self.status not in {"draft", "collecting", "closed", "confirmed"}:
            errors.append("企画状態が不正です。")
        if self.response_deadline:
            try:
                datetime.fromisoformat(self.response_deadline)
            except ValueError:
                errors.append("入力締切の形式が不正です。")
        if self.performance_date:
            try:
                date.fromisoformat(self.performance_date)
            except ValueError:
                errors.append("本番日の形式が不正です。")
        if self.performance_avoid_days < 0:
            errors.append("本番直前の回避日数は0以上にしてください。")
        if any(period not in range(1, 7) for period in self.avoided_periods):
            errors.append("避ける時限は1〜6限から選択してください。")
        if self.role_display_mode not in ROLE_DISPLAY_MODES:
            errors.append("役割の表示方式が不正です。")
        return errors

    def is_input_open(self, current: datetime | None = None) -> bool:
        if self.status not in {"draft", "collecting"}:
            return False
        if not self.response_deadline or self.allow_edits_after_deadline:
            return True
        try:
            deadline = normalize_deadline(self.response_deadline)
        except (TypeError, ValueError):
            return False
        if deadline is None:
            return False
        return not deadline_has_passed(self.response_deadline, current=current)


def participant_response_editable(
    config: Config,
    current: datetime | None = None,
) -> bool:
    """Return whether a participant response may be saved for this project."""

    if config.status == "collecting":
        return config.is_input_open(current)
    if config.status == "closed":
        return bool(config.allow_edits_after_deadline)
    return False


@dataclass
class Participant:
    id: str
    name: str
    humanities_or_science: str = ""
    department: str = ""
    department_detail: str = ""
    cohort: int | None = None
    group_number: int | str = 1
    practice_role_unspecified: bool = False
    registered_by: str = "admin"
    availability: list[str] = field(default_factory=list)
    zoom_availability: list[str] = field(default_factory=list)
    submitted_at: str = ""
    input_status: str = "not_started"
    approved: bool = True
    active: bool = True
    required_university_count: int | None = None
    required_high_school_count: int | None = None
    total_extra_limit: int | None = None
    support_requested_count: int | None = None
    support_desired_count: int | None = None
    practice_participation_count: int | None = None
    notes: str = ""
    updated_at: str = ""
    attributes_changed_by_participant: bool = False
    attributes_changed_at: str = ""
    user_id: str = ""
    storage_version: int = 0
    storage_project_id: str = ""
    participant_response: dict[str, Any] = field(default_factory=dict)
    manager_response: dict[str, Any] = field(default_factory=dict)
    response_source: str = "participant"

    @classmethod
    def create(cls, name: str, registered_by: str = "admin") -> "Participant":
        return cls(
            id=uuid4().hex,
            name=name.strip(),
            registered_by=registered_by,
            approved=registered_by == "admin",
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Participant":
        department, department_detail = normalize_department(
            str(data.get("department", "")),
            str(data.get("department_detail", "")),
        )
        raw_group_number = data.get("group_number")
        legacy_role_unspecified = (
            str(raw_group_number).strip() == ROLE_UNSPECIFIED_GROUP
        )
        normalized_group_number = _normalize_group(raw_group_number)
        role_unspecified_value = (
            bool(data.get("practice_role_unspecified"))
            if "practice_role_unspecified" in data
            else (
                legacy_role_unspecified
                or normalized_group_number == LEGACY_SUPPORT_GROUP
            )
        )
        return cls(
            id=str(data.get("id") or uuid4().hex),
            name=str(data.get("name", "")).strip(),
            humanities_or_science=str(data.get("humanities_or_science", "")),
            department=department,
            department_detail=department_detail,
            cohort=_optional_positive_int(data.get("cohort")),
            group_number=normalized_group_number,
            practice_role_unspecified=role_unspecified_value,
            registered_by=(
                "participant" if data.get("registered_by") == "participant" else "admin"
            ),
            availability=sorted(set(map(str, data.get("availability", [])))),
            zoom_availability=sorted(
                set(map(str, data.get("zoom_availability", [])))
                - set(map(str, data.get("availability", [])))
            ),
            submitted_at=str(data.get("submitted_at", "")),
            input_status=str(
                data.get(
                    "input_status",
                    "submitted" if data.get("submitted_at") else "not_started",
                )
            ),
            approved=bool(
                data.get("approved", data.get("registered_by") != "participant")
            ),
            active=bool(data.get("active", True)),
            required_university_count=_optional_nonnegative_int(
                data.get("required_university_count")
            ),
            required_high_school_count=_optional_nonnegative_int(
                data.get("required_high_school_count")
            ),
            total_extra_limit=_optional_nonnegative_int(data.get("total_extra_limit")),
            support_requested_count=_optional_nonnegative_int(
                data.get(
                    "support_requested_count",
                    data.get("support_desired_count"),
                )
            ),
            support_desired_count=_optional_nonnegative_int(
                data.get("support_desired_count")
            ),
            practice_participation_count=_optional_nonnegative_int(
                data.get("practice_participation_count")
            ),
            notes=str(data.get("notes", "")),
            updated_at=str(data.get("updated_at", data.get("submitted_at", ""))),
            attributes_changed_by_participant=bool(
                data.get("attributes_changed_by_participant", False)
            ),
            attributes_changed_at=str(data.get("attributes_changed_at", "")),
            user_id=str(data.get("user_id", "")),
            storage_version=(
                _optional_nonnegative_int(data.get("storage_version")) or 0
            ),
            storage_project_id=str(data.get("storage_project_id", "")),
            participant_response=(
                dict(data.get("participant_response", {}))
                if isinstance(data.get("participant_response"), dict)
                else {}
            ),
            manager_response=(
                dict(data.get("manager_response", {}))
                if isinstance(data.get("manager_response"), dict)
                else {}
            ),
            response_source=(
                "manager"
                if data.get("response_source") == "manager"
                else "participant"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def university_requirement(self, config: Config) -> int:
        if self.is_role_unspecified or self.uses_legacy_total_requirement(config):
            return 0
        return (
            config.required_university_count
            if self.required_university_count is None
            else self.required_university_count
        )

    def high_school_requirement(self, config: Config) -> int:
        if self.is_role_unspecified or self.uses_legacy_total_requirement(config):
            return 0
        return (
            config.required_high_school_count
            if self.required_high_school_count is None
            else self.required_high_school_count
        )

    def extra_limit(self, config: Config) -> int:
        return (
            config.total_extra_limit
            if self.total_extra_limit is None
            else self.total_extra_limit
        )

    @property
    def is_support(self) -> bool:
        return self.group_number == LEGACY_SUPPORT_GROUP

    @property
    def is_practice_role_unspecified(self) -> bool:
        # ``from_dict`` supplies the historical default for support-group
        # records that do not contain this field.  Once the field exists, the
        # saved checkbox is authoritative so a support participant can be
        # explicitly switched to role-based requirements.
        return bool(self.practice_role_unspecified)

    @property
    def is_role_unspecified(self) -> bool:
        return self.is_practice_role_unspecified

    def role_unspecified_default_requirement(self, config: Config) -> int:
        if config.participation_requirement_mode == PARTICIPATION_MODE_TOTAL_ONCE:
            return config.required_total_count
        return config.required_university_count + config.required_high_school_count

    def uses_legacy_total_requirement(self, config: Config) -> bool:
        """Preserve unsynchronized total-mode records written before version 2."""

        return (
            config.participation_requirement_mode == PARTICIPATION_MODE_TOTAL_ONCE
            and self.practice_participation_count is None
        )

    def total_requirement(self, config: Config) -> int:
        if self.is_practice_role_unspecified:
            if self.practice_participation_count is not None:
                return self.practice_participation_count
            return self.role_unspecified_default_requirement(config)
        if self.uses_legacy_total_requirement(config):
            return config.required_total_count
        return 0

    def participation_limit(self, config: Config) -> int:
        if self.is_practice_role_unspecified:
            return self.total_requirement(config)
        if self.uses_legacy_total_requirement(config):
            return config.required_total_count
        return (
            self.university_requirement(config)
            + self.high_school_requirement(config)
            + self.extra_limit(config)
        )


def _optional_nonnegative_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
        return parsed if parsed >= 1 else None
    except (TypeError, ValueError):
        return None


def _normalize_group(value: Any) -> int | str:
    normalized = str(value).strip()
    if normalized == ROLE_UNSPECIFIED_GROUP:
        return 1
    if normalized == LEGACY_SUPPORT_GROUP:
        return normalized
    return _positive_int(value, 1)


def make_slot_key(day: str | date, period: int) -> str:
    day_text = day.isoformat() if isinstance(day, date) else day
    return f"{day_text}#{period}"


def parse_slot_key(slot_key: str) -> tuple[date, int]:
    day_text, period_text = slot_key.rsplit("#", 1)
    return date.fromisoformat(day_text), int(period_text)


def eligible_dates(config: Config) -> list[date]:
    start = date.fromisoformat(config.start_date)
    end = date.fromisoformat(config.end_date)
    result: list[date] = []
    current = start
    while current <= end:
        if current.weekday() in config.enabled_weekdays:
            result.append(current)
        current = date.fromordinal(current.toordinal() + 1)
    return result


def practice_dates(config: Config) -> list[date]:
    """Return dates available for newly created practice sessions.

    The response-collection range remains represented by ``eligible_dates``.
    Excluded dates only affect newly generated or manually added sessions.
    """

    excluded = set(config.excluded_dates)
    return [
        day
        for day in eligible_dates(config)
        if day.isoformat() not in excluded
    ]


def format_slot(slot_key: str) -> str:
    day, period = parse_slot_key(slot_key)
    return f"{day.isoformat()} ({WEEKDAY_LABELS[day.weekday()]}) {period}限"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
