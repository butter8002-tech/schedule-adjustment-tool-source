"""Validation and change detection for the system common-participant table."""

from __future__ import annotations

import pandas as pd

from schedule_adjustment_tool.domain.app_config import load_app_settings
from schedule_adjustment_tool.domain.models import participant_name_identity_key
from schedule_adjustment_tool.domain.participant_attributes import (
    DEPARTMENT_OPTIONS,
    department_detail_options,
)


APP_SETTINGS = load_app_settings()


def common_participant_table_updates(
    profiles: list[dict[str, object]],
    edited_frame: pd.DataFrame,
) -> tuple[list[dict[str, object]], list[str]]:
    """Validate an edited common roster and return only changed rows."""

    profile_by_id = {
        _cell_text(profile.get("id", "")): profile
        for profile in profiles
    }
    edited_by_id: dict[str, dict[str, object]] = {}
    errors: list[str] = []
    for _, row in edited_frame.iterrows():
        participant_id = _cell_text(row.get("ID", ""))
        if participant_id not in profile_by_id:
            errors.append("一覧にない参加者が含まれています。再読み込みしてください。")
            continue
        name = _cell_text(row.get("名前", ""))
        if not name:
            errors.append("参加者名を空欄にはできません。")
        elif len(name) > APP_SETTINGS.max_text_length:
            errors.append(f"{name[:20]}…の名前が入力可能な長さを超えています。")
        cohort_label = _cell_text(row.get("期", ""))
        if cohort_label in {"", "未登録", "期未登録"}:
            cohort: int | None = None
        elif cohort_label.endswith("期") and cohort_label.removesuffix("期").isdigit():
            cohort = int(cohort_label.removesuffix("期"))
        else:
            errors.append(f"{name or '参加者'}の期が不正です。")
            cohort = None
        field = _cell_text(row.get("文理", ""))
        if field not in {"", "文系", "理系", "その他"}:
            errors.append(f"{name or '参加者'}の文理が不正です。")
        department = _cell_text(row.get("科類・学部", ""))
        if department not in DEPARTMENT_OPTIONS:
            errors.append(f"{name or '参加者'}の科類・学部が不正です。")
        department_detail = _cell_text(row.get("学科・類・専修", ""))
        if not department:
            department_detail = ""
        elif (
            department != "その他"
            and department_detail
            and department_detail not in department_detail_options(department)
        ):
            errors.append(
                f"{name or '参加者'}の学科・類・専修が"
                "選択した科類・学部と一致しません。"
            )
        edited_by_id[participant_id] = {
            "participant_id": participant_id,
            "name": name,
            "cohort": cohort,
            "humanities_or_science": field,
            "department": department,
            "department_detail": department_detail,
            "expected_updated_at": _cell_text(
                profile_by_id[participant_id].get("updated_at", "")
            ),
        }

    desired_names = {
        participant_id: _cell_text(profile.get("name", ""))
        for participant_id, profile in profile_by_id.items()
    }
    desired_names.update(
        {
            participant_id: _cell_text(update.get("name", ""))
            for participant_id, update in edited_by_id.items()
        }
    )
    ids_by_name: dict[str, list[str]] = {}
    for participant_id, name in desired_names.items():
        ids_by_name.setdefault(participant_name_identity_key(name), []).append(
            participant_id
        )
    if any(len(ids) > 1 for ids in ids_by_name.values()):
        errors.append("同じ名前の参加者を複数登録することはできません。")

    updates: list[dict[str, object]] = []
    for participant_id, update in edited_by_id.items():
        profile = profile_by_id[participant_id]
        current_cohort = (
            int(profile["cohort"])
            if str(profile.get("cohort") or "").isdigit()
            else None
        )
        if (
            _cell_text(update["name"]) != _cell_text(profile.get("name", ""))
            or update["cohort"] != current_cohort
            or _cell_text(update["humanities_or_science"])
            != _cell_text(profile.get("humanities_or_science", ""))
            or _cell_text(update["department"])
            != _cell_text(profile.get("department", ""))
            or _cell_text(update["department_detail"])
            != _cell_text(profile.get("department_detail", ""))
        ):
            updates.append(update)
    return updates, list(dict.fromkeys(errors))


def _cell_text(value: object) -> str:
    return "" if pd.isna(value) else str(value).strip()
