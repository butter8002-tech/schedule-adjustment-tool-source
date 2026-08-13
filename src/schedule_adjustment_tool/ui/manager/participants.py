"""Manager UI renderers for project participant administration.

This module owns the new manager UI's participant roster, membership, and
individual-condition screens.  Database writes remain in ``storage``; the
small service object contains only app-level cache, workflow, and dialog
coordination so this module does not import the Streamlit entrypoint.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.models import (
    Config,
    LEGACY_SUPPORT_GROUP,
    Participant,
    now_iso,
    participant_name_identity_key,
)
from schedule_adjustment_tool.domain.participant_attributes import (
    DEPARTMENT_OPTIONS,
    display_department,
)
from schedule_adjustment_tool.storage import (
    StorageConflictError,
    StorageError,
    add_participants,
    save_participant_admin_fields_bulk,
)
from schedule_adjustment_tool.ui.manager.app_cache import (
    render_project_operation_feedback,
    set_project_operation_feedback,
)


@dataclass(frozen=True)
class ParticipantManagementServices:
    """Entry-point operations needed to coordinate participant UI updates."""

    max_text_length: int
    max_description_length: int
    load_common_participants: Callable[[], list[dict[str, object]]]
    load_system_settings: Callable[[], dict[str, object]]
    set_cached_participants: Callable[[str, list[Participant]], None]
    clear_common_participants_cache: Callable[[], None]
    clear_candidate_state: Callable[[str], None]
    mark_step_started: Callable[[str, str], None]
    status_message: Callable[[str], AbstractContextManager[Any]]
    render_participant_deletion_tools: Callable[[str, list[Participant]], None]
    confirm_membership_change: Callable[[str, list[dict[str, object]]], None]
    confirm_individual_condition_change: Callable[..., None]
    save_participant_updates: Callable[[str, list[Participant]], None]
    candidate_count: Callable[[str], int]


def _cell_text(value: object) -> str:
    return "" if pd.isna(value) else str(value).strip()


def cohort_from_label(value: object) -> int | None:
    label = _cell_text(value)
    if not label:
        return None
    number = label.removesuffix("期")
    if not number.isdigit() or int(number) < 1:
        raise ValueError("期の値が不正です。")
    return int(number)


def normalize_bulk_participant_name(value: object) -> str:
    """Remove whitespace from a newly entered participant name."""

    return "".join(str(value).split())


def cell_bool(value: object, default: bool = False) -> bool:
    return default if pd.isna(value) else bool(value)


def nonnegative_int_from_cell(value: object, label: str) -> int:
    text = _cell_text(value)
    if not text:
        raise ValueError(f"{label}を入力してください。")
    numeric = float(text)
    if not numeric.is_integer() or numeric < 0:
        raise ValueError(f"{label}は0以上の整数にしてください。")
    return int(numeric)


def participant_membership_signature(
    participant: Participant,
) -> tuple[object, ...]:
    return (
        participant.practice_role_unspecified,
        participant.name,
        participant.active,
        participant.approved,
        str(participant.group_number),
        participant.cohort,
        participant.humanities_or_science,
        participant.department,
        participant.department_detail,
        participant.attributes_changed_by_participant,
        participant.attributes_changed_at,
    )


def participant_individual_condition_signature(
    participant: Participant,
) -> tuple[object, ...]:
    return (
        participant.practice_role_unspecified,
        participant.practice_participation_count,
        participant.required_university_count,
        participant.required_high_school_count,
        participant.total_extra_limit,
        participant.notes,
    )


def participant_individual_candidate_signature(
    participant: Participant,
) -> tuple[object, ...]:
    return (
        participant.practice_role_unspecified,
        (
            participant.practice_participation_count
            if participant.practice_role_unspecified
            else None
        ),
        participant.required_university_count,
        participant.required_high_school_count,
        participant.total_extra_limit,
    )


def classify_bulk_participant_names(
    names: list[str],
    common_profiles: list[dict[str, object]],
    project_participants: list[Participant],
) -> dict[str, list[str]]:
    project_names = {
        participant_name_identity_key(participant.name)
        for participant in project_participants
    }
    common_names = {
        participant_name_identity_key(_cell_text(profile.get("name", "")))
        for profile in common_profiles
    }
    result: dict[str, list[str]] = {
        "creatable": [],
        "existing_project": [],
        "existing_common": [],
        "duplicate_input": [],
    }
    seen_input_names: set[str] = set()
    for name in names:
        identity = participant_name_identity_key(name)
        if identity in seen_input_names:
            result["duplicate_input"].append(name)
            continue
        seen_input_names.add(identity)
        if identity in project_names:
            result["existing_project"].append(name)
        elif identity in common_names:
            result["existing_common"].append(name)
        else:
            result["creatable"].append(name)
    return result


def common_participant_cohort_label(profile: dict[str, object]) -> str:
    cohort = profile.get("cohort")
    return f"{int(cohort)}期" if str(cohort or "").isdigit() else "期未登録"


def filter_common_participants(
    profiles: list[dict[str, object]],
    *,
    cohort_label: str,
    search_text: str,
) -> list[dict[str, object]]:
    search_key = participant_name_identity_key(search_text)
    return [
        profile
        for profile in profiles
        if (
            cohort_label == "すべて"
            or common_participant_cohort_label(profile) == cohort_label
        )
        and (
            not search_key
            or search_key
            in participant_name_identity_key(_cell_text(profile.get("name", "")))
        )
    ]


def participant_from_common_profile(
    profile: dict[str, object],
    registered_by: str = "admin",
) -> Participant:
    participant = Participant.create(
        _cell_text(profile.get("name", "")), registered_by
    )
    participant.id = _cell_text(profile.get("id", participant.id)) or participant.id
    participant.user_id = _cell_text(profile.get("user_id", ""))
    cohort = profile.get("cohort")
    participant.cohort = int(cohort) if str(cohort or "").isdigit() else None
    participant.humanities_or_science = _cell_text(
        profile.get("humanities_or_science", "")
    )
    participant.department = _cell_text(profile.get("department", ""))
    participant.department_detail = _cell_text(
        profile.get("department_detail", "")
    )
    return participant


def participant_choice_sort_key(participant: Participant) -> tuple[int, int, int, str]:
    field_order = {"文系": 0, "理系": 1, "その他": 2}
    has_cohort = participant.cohort is not None
    field = participant.humanities_or_science
    has_field = bool(field)
    return (
        0 if has_cohort else 1,
        int(participant.cohort or 9999),
        field_order.get(field, 99) if has_field else 100,
        participant.name,
    )


def render_common_participant_addition_table(
    project_id: str,
    participants: list[Participant],
    *,
    late_addition: bool = False,
    services: ParticipantManagementServices,
) -> None:
    common_profiles = [
        profile
        for profile in services.load_common_participants()
        if str(profile.get("id", ""))
        not in {participant.id for participant in participants}
    ]
    common_profiles.sort(
        key=lambda profile: participant_choice_sort_key(
            participant_from_common_profile(profile, "admin")
        )
    )
    if not common_profiles:
        st.info("この企画へ追加できる共通名簿の参加者はいません。")
        return

    st.caption(
        "企画に追加したい参加者は追加予定にチェックしてください。"
        "企画への保存は最後のボタンを押した時だけ行います。"
    )
    common_by_id = {str(profile["id"]): profile for profile in common_profiles}
    available_common_ids = set(common_by_id)
    selection_key = f"focused_common_participant_selection_{project_id}"
    selected_common_ids = {
        str(participant_id)
        for participant_id in st.session_state.get(selection_key, [])
        if str(participant_id) in available_common_ids
    }
    cohort_labels = sorted(
        {
            common_participant_cohort_label(profile)
            for profile in common_profiles
            if common_participant_cohort_label(profile) != "期未登録"
        },
        key=lambda label: int(label.removesuffix("期")),
    )
    if any(
        common_participant_cohort_label(profile) == "期未登録"
        for profile in common_profiles
    ):
        cohort_labels.append("期未登録")

    filter_columns = st.columns([3, 5])
    search_text = filter_columns[0].text_input(
        "共通名簿を検索",
        placeholder="名前の一部を入力",
        key=f"focused_common_participant_search_{project_id}",
    )
    selected_cohort = filter_columns[1].segmented_control(
        "期区分",
        ["すべて", *cohort_labels],
        default="すべて",
        key=f"focused_common_participant_cohort_{project_id}",
    )
    filtered_profiles = filter_common_participants(
        common_profiles,
        cohort_label=str(selected_cohort or "すべて"),
        search_text=search_text,
    )
    if filtered_profiles:
        visible_rows = []
        for profile in filtered_profiles:
            participant = participant_from_common_profile(profile, "admin")
            visible_rows.append(
                {
                    "ID": participant.id,
                    "追加予定": participant.id in selected_common_ids,
                    "名前": participant.name,
                    "期": common_participant_cohort_label(profile),
                    "文理": participant.humanities_or_science or "未登録",
                    "学部・学科": (
                        display_department(
                            participant.department,
                            participant.department_detail,
                        )
                        or "未登録"
                    ),
                }
            )
        filter_token = hashlib.sha256(
            (
                f"{selected_cohort}\x1f"
                f"{participant_name_identity_key(search_text)}"
            ).encode("utf-8")
        ).hexdigest()[:12]
        with st.form(
            f"focused_select_common_participants_{project_id}_{filter_token}"
        ):
            edited_profiles = st.data_editor(
                pd.DataFrame(visible_rows),
                hide_index=True,
                width="stretch",
                height=min(620, 36 * (len(visible_rows) + 1)),
                disabled=["ID", "名前", "期", "文理", "学部・学科"],
                column_config={
                    "ID": None,
                    "追加予定": st.column_config.CheckboxColumn(
                        "追加予定",
                        help="チェックした人を追加予定リストへ反映します。",
                    ),
                },
                key=(
                    f"focused_common_participant_table_{project_id}_"
                    f"{filter_token}"
                ),
            )
            selection_clicked = st.form_submit_button(
                "表示中の選択を追加予定へ反映"
            )
        if selection_clicked:
            visible_ids = {str(row["ID"]) for row in visible_rows}
            selected_common_ids.difference_update(visible_ids)
            selected_common_ids.update(
                str(row["ID"])
                for _, row in edited_profiles.iterrows()
                if bool(row.get("追加予定", False))
            )
            st.session_state[selection_key] = sorted(selected_common_ids)
            st.rerun()
    else:
        st.info("この条件に一致する共通名簿の参加者はいません。")

    selected_profiles = [
        common_by_id[participant_id]
        for participant_id in sorted(selected_common_ids)
        if participant_id in common_by_id
    ]
    st.caption(f"追加予定: {len(selected_profiles)}人")
    if selected_profiles:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "名前": _cell_text(profile.get("name", "")),
                        "期": common_participant_cohort_label(profile),
                    }
                    for profile in selected_profiles
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    action_columns = st.columns(2)
    late_addition_confirmed = True
    if late_addition:
        st.warning(
            "回答締切後または日程確定後の追加です。追加する参加者は"
            "「承認済み・日調対象外」で登録され、既存の日程には入りません。"
        )
        late_addition_confirmed = st.checkbox(
            "日調対象外として追加することを確認しました",
            key=f"confirm_late_common_participant_addition_{project_id}",
        )
    add_clicked = action_columns[0].button(
        f"追加予定{len(selected_profiles)}人を企画に追加",
        type="primary",
        disabled=not selected_profiles or not late_addition_confirmed,
        key=f"focused_add_common_participants_{project_id}",
    )
    clear_clicked = action_columns[1].button(
        "追加予定を空にする",
        disabled=not selected_profiles,
        key=f"focused_clear_common_participants_{project_id}",
    )
    render_project_operation_feedback(project_id, "roster_common_add")
    if clear_clicked:
        st.session_state[selection_key] = []
        st.rerun()
    if not add_clicked:
        return

    selected_participants = [
        participant_from_common_profile(profile, "admin")
        for profile in selected_profiles
    ]
    if late_addition:
        for participant in selected_participants:
            participant.approved = True
            participant.active = False
    updated_participants = [*participants, *selected_participants]
    normalized_names = [
        participant_name_identity_key(participant.name)
        for participant in updated_participants
    ]
    if len(normalized_names) != len(set(normalized_names)):
        st.warning("同じ名前の参加者が含まれるため追加できません。")
        return
    with services.status_message("共通名簿から参加者を追加しています..."):
        add_participants(project_id, selected_participants)
        services.set_cached_participants(project_id, updated_participants)
        if not late_addition:
            services.clear_candidate_state(project_id)
    services.mark_step_started(project_id, "participants")
    st.session_state[selection_key] = []
    set_project_operation_feedback(
        project_id,
        f"{len(selected_participants)}人を共通名簿から追加しました。",
        operation_key="roster_common_add",
    )
    st.rerun()


def render_participant_roster(
    project_id: str,
    config: Config,
    participants: list[Participant],
    *,
    services: ParticipantManagementServices,
) -> None:
    late_addition = config.status in {"closed", "confirmed"}
    st.caption(
        "新しい参加者を追加するか、共通名簿からこの企画へ追加します。"
    )
    with st.form(
        f"focused_bulk_add_participants_{project_id}",
        clear_on_submit=True,
    ):
        pasted_names = st.text_area(
            "名前を1行1名で入力",
            height=180,
            max_chars=services.max_description_length,
            placeholder="山田太郎\n佐藤花子",
        )
        late_addition_confirmed = True
        if late_addition:
            st.warning(
                "回答締切後または日程確定後の追加です。追加する参加者は"
                "「承認済み・日調対象外」で登録され、既存の日程には入りません。"
            )
            late_addition_confirmed = st.checkbox(
                "日調対象外として追加することを確認しました"
            )
        bulk_add_clicked = st.form_submit_button(
            "参加者を新規登録・追加",
            type="primary",
            disabled=not late_addition_confirmed,
        )
    render_project_operation_feedback(project_id, "roster_new_add")
    if bulk_add_clicked:
        names = [
            normalize_bulk_participant_name(line)
            for line in pasted_names.splitlines()
            if line.strip()
        ]
        if not names:
            st.warning("追加する参加者名を入力してください。")
        elif any(len(name) > services.max_text_length for name in names):
            st.warning(
                f"参加者名は{services.max_text_length}文字以内にしてください。"
            )
        else:
            common_participants = services.load_common_participants()
            classified = classify_bulk_participant_names(
                names,
                common_participants,
                participants,
            )
            creatable_names = classified["creatable"]
            if not creatable_names:
                st.info(
                    "新しく追加できる参加者はいません。"
                    "共通名簿にいる参加者は下から追加してください。"
                )
            else:
                with services.status_message("参加者を追加しています..."):
                    new_participants = [
                        Participant.create(name, "admin")
                        for name in creatable_names
                    ]
                    if late_addition:
                        for participant in new_participants:
                            participant.approved = True
                            participant.active = False
                    add_participants(project_id, new_participants)
                    services.set_cached_participants(
                        project_id,
                        [*participants, *new_participants],
                    )
                    services.clear_common_participants_cache()
                    if not late_addition:
                        services.clear_candidate_state(project_id)
                services.mark_step_started(project_id, "participants")
                set_project_operation_feedback(
                    project_id,
                    f"{len(new_participants)}人の参加者を追加しました。",
                    operation_key="roster_new_add",
                )
                st.rerun()

    st.divider()
    st.markdown("##### 共通名簿から追加")
    render_common_participant_addition_table(
        project_id,
        participants,
        late_addition=late_addition,
        services=services,
    )


def render_participant_membership(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None = None,
    *,
    services: ParticipantManagementServices,
) -> None:
    if not participants:
        st.info("参加者はまだ登録されていません。")
        return
    st.caption(
        "日調対象、班、期、文理、所属を設定します。"
        "候補対象を外しても名簿からは削除されません。"
    )
    sorted_participants = sorted(participants, key=participant_choice_sort_key)
    group_options = [
        str(number) for number in range(1, config.group_count + 1)
    ] + [LEGACY_SUPPORT_GROUP]
    cohort_options = [
        "",
        *[
            f"{cohort}期"
            for cohort in services.load_system_settings()["active_cohorts"]
        ],
    ]
    rows = [
        {
            "ID": participant.id,
            "名前": participant.name,
            "日調対象": participant.active,
            "班": (
                LEGACY_SUPPORT_GROUP
                if participant.is_support
                else str(participant.group_number)
            ),
            "期": (
                f"{participant.cohort}期"
                if participant.cohort is not None
                else ""
            ),
            "文理": participant.humanities_or_science,
            "学部": participant.department,
            "学科・コース等": participant.department_detail,
        }
        for participant in sorted_participants
    ]
    with st.form(f"focused_participant_membership_{project_id}"):
        edited = st.data_editor(
            pd.DataFrame(rows),
            hide_index=True,
            width="stretch",
            height=min(620, 36 * (len(rows) + 1)),
            disabled=["ID"],
            column_config={
                "ID": None,
                "名前": st.column_config.TextColumn("名前", required=True),
                "日調対象": st.column_config.CheckboxColumn("日調対象"),
                "班": st.column_config.SelectboxColumn(
                    "班",
                    options=group_options,
                    required=True,
                ),
                "期": st.column_config.SelectboxColumn("期", options=cohort_options),
                "文理": st.column_config.SelectboxColumn(
                    "文理",
                    options=["", "文系", "理系", "その他"],
                ),
                "学部": st.column_config.SelectboxColumn(
                    "学部",
                    options=DEPARTMENT_OPTIONS,
                ),
                "学科・コース等": st.column_config.TextColumn("学科・コース等"),
            },
            key=f"focused_participant_membership_table_{project_id}",
        )
        save_clicked = st.form_submit_button("参加者設定を保存", type="primary")
    render_project_operation_feedback(project_id, "participant_membership")

    st.divider()
    with st.expander("参加者を削除"):
        services.render_participant_deletion_tools(project_id, participants)

    if not save_clicked:
        return

    services.mark_step_started(project_id, "participants")
    participant_by_id = {participant.id: participant for participant in participants}
    normalized_names = [
        participant_name_identity_key(_cell_text(row.get("名前", "")))
        for _, row in edited.iterrows()
    ]
    if any(not name for name in normalized_names):
        st.error("名前を空欄にはできません。")
        return
    if len(normalized_names) != len(set(normalized_names)):
        st.error("参加者名が重複しています。")
        return

    updated_participants: list[Participant] = []
    try:
        for _, row in edited.iterrows():
            original_participant = participant_by_id[str(row["ID"])]
            participant = Participant.from_dict(original_participant.to_dict())
            participant.name = _cell_text(row["名前"])
            participant.active = cell_bool(row["日調対象"])
            participant.group_number = _cell_text(row["班"])
            participant.cohort = cohort_from_label(row["期"])
            participant.humanities_or_science = _cell_text(row["文理"])
            participant.department = _cell_text(row["学部"])
            participant.department_detail = (
                _cell_text(row["学科・コース等"])
                if participant.department
                else ""
            )
            if participant.is_support and not original_participant.is_support:
                participant.practice_role_unspecified = True
            elif original_participant.is_support and not participant.is_support:
                participant.practice_role_unspecified = False
            participant.attributes_changed_by_participant = False
            participant.attributes_changed_at = ""
            original_role_unspecified = (
                original_participant.is_support
                or original_participant.is_practice_role_unspecified
            )
            if (
                original_role_unspecified
                and not participant.is_practice_role_unspecified
            ):
                university_count = int(
                    original_participant.required_university_count or 0
                )
                high_school_count = int(
                    original_participant.required_high_school_count or 0
                )
                if university_count == 0 and high_school_count == 0:
                    university_count = int(config.required_university_count)
                    high_school_count = int(config.required_high_school_count)
                participant.required_university_count = university_count
                participant.required_high_school_count = high_school_count
                participant.practice_participation_count = (
                    university_count + high_school_count
                )
            if (
                participant_membership_signature(participant)
                == participant_membership_signature(original_participant)
            ):
                continue
            participant.updated_at = now_iso()
            updated_participants.append(participant)
        if not updated_participants:
            st.info("変更はありません。")
            return
        if confirmed or services.candidate_count(project_id):
            services.confirm_membership_change(
                project_id,
                [participant.to_dict() for participant in updated_participants],
                published_schedule_exists=bool(confirmed),
            )
            return
        with services.status_message("参加者設定を保存しています..."):
            services.save_participant_updates(project_id, updated_participants)
    except (StorageError, StorageConflictError, ValueError) as error:
        st.error(str(error))
        return
    set_project_operation_feedback(
        project_id,
        "参加者設定を保存しました。",
        operation_key="participant_membership",
    )
    st.rerun()


def render_participant_individual_conditions(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None = None,
    *,
    services: ParticipantManagementServices,
) -> None:
    target_participants = [
        participant
        for participant in sorted(participants, key=participant_choice_sort_key)
        if participant.active
    ]
    if not target_participants:
        st.info("日調対象の参加者がいません。")
        return
    cohort_labels = sorted(
        {
            (
                f"{participant.cohort}期"
                if participant.cohort is not None
                else "期未設定"
            )
            for participant in target_participants
        },
        key=lambda value: (
            value == "期未設定",
            int(value.removesuffix("期"))
            if value.removesuffix("期").isdigit()
            else 9999,
        ),
    )
    filter_columns = st.columns([3, 5])
    participant_search_text = filter_columns[0].text_input(
        "参加者を検索",
        placeholder="名前の一部を入力",
        key=f"individual_condition_search_{project_id}",
    )
    selected_cohort = filter_columns[1].segmented_control(
        "期区分",
        ["すべて", *cohort_labels],
        default="すべて",
        key=f"individual_condition_cohort_filter_{project_id}",
    )
    normalized_search = participant_name_identity_key(participant_search_text)
    filtered_participants = [
        participant
        for participant in target_participants
        if (
            selected_cohort == "すべて"
            or (
                f"{participant.cohort}期"
                if participant.cohort is not None
                else "期未設定"
            )
            == selected_cohort
        )
        and (
            not normalized_search
            or normalized_search in participant_name_identity_key(participant.name)
        )
    ]
    if not filtered_participants:
        st.info("この条件に一致する参加者はいません。")
        return

    st.caption(
        "役割を指定しない場合は「練習会参加数」を使用します。"
        "役割を指定する場合は大学生役・高校生役の必要回数と"
        "超過上限を使用します。"
    )
    rows = [
        {
            "ID": participant.id,
            "名前": participant.name,
            "期": (
                f"{participant.cohort}期"
                if participant.cohort is not None
                else "期未設定"
            ),
            "班": str(participant.group_number),
            "役割指定なし": participant.practice_role_unspecified,
            "練習会参加数": (
                participant.practice_participation_count
                if participant.practice_participation_count is not None
                else (
                    config.required_university_count
                    + config.required_high_school_count
                )
            ),
            "大学生役の必要回数": (
                0
                if participant.practice_role_unspecified
                else participant.university_requirement(config)
            ),
            "高校生役の必要回数": (
                0
                if participant.practice_role_unspecified
                else participant.high_school_requirement(config)
            ),
            "合計参加の超過上限": participant.extra_limit(config),
            "メモ": participant.notes,
        }
        for participant in filtered_participants
    ]
    table_key_suffix = hashlib.sha256(
        f"{selected_cohort}\x1f{normalized_search}".encode("utf-8")
    ).hexdigest()[:10]
    with st.form(f"focused_individual_conditions_{project_id}_{table_key_suffix}"):
        edited = st.data_editor(
            pd.DataFrame(rows),
            hide_index=True,
            width="stretch",
            height=min(680, 36 * (len(rows) + 1)),
            disabled=["ID", "名前", "期", "班"],
            column_config={
                "ID": None,
                "名前": st.column_config.TextColumn("名前", width="medium"),
                "期": st.column_config.TextColumn("期", width="small"),
                "班": st.column_config.TextColumn("班", width="small"),
                "役割指定なし": st.column_config.CheckboxColumn(
                    "役割指定なし",
                    help=(
                        "選択すると役割別回数ではなく"
                        "練習会参加数を使用します。"
                    ),
                ),
                "練習会参加数": st.column_config.NumberColumn(
                    "練習会参加数",
                    min_value=0,
                    max_value=20,
                    step=1,
                    format="%d",
                    required=True,
                ),
                "大学生役の必要回数": st.column_config.NumberColumn(
                    "大学生役",
                    min_value=0,
                    max_value=20,
                    step=1,
                    format="%d",
                    required=True,
                ),
                "高校生役の必要回数": st.column_config.NumberColumn(
                    "高校生役",
                    min_value=0,
                    max_value=20,
                    step=1,
                    format="%d",
                    required=True,
                ),
                "合計参加の超過上限": st.column_config.NumberColumn(
                    "超過上限",
                    min_value=0,
                    max_value=20,
                    step=1,
                    format="%d",
                    required=True,
                ),
                "メモ": st.column_config.TextColumn(
                    "メモ",
                    width="large",
                    max_chars=services.max_description_length,
                ),
            },
            key=(
                f"focused_individual_condition_table_{project_id}_"
                f"{table_key_suffix}"
            ),
        )
        save_clicked = st.form_submit_button(
            "個別条件を保存",
            type="primary",
        )
    render_project_operation_feedback(project_id, "individual_conditions")
    if not save_clicked:
        return

    participant_by_id = {
        participant.id: participant for participant in target_participants
    }
    updated_participants: list[Participant] = []
    candidate_affecting_change = False
    try:
        for _, row in edited.iterrows():
            participant = Participant.from_dict(
                participant_by_id[_cell_text(row["ID"])].to_dict()
            )
            was_role_unspecified = participant.practice_role_unspecified
            role_unspecified = cell_bool(row["役割指定なし"])
            participant.practice_role_unspecified = role_unspecified
            if role_unspecified:
                participant.practice_participation_count = nonnegative_int_from_cell(
                    row["練習会参加数"],
                    f"{participant.name}さんの練習会参加数",
                )
                participant.required_university_count = 0
                participant.required_high_school_count = 0
                # None means use the current global default. Retain it when
                # saving a role-unspecified row instead of replacing it with 0.
                participant.total_extra_limit = participant_by_id[
                    _cell_text(row["ID"])
                ].total_extra_limit
            else:
                university_count = nonnegative_int_from_cell(
                    row["大学生役の必要回数"],
                    f"{participant.name}さんの大学生役の必要回数",
                )
                high_school_count = nonnegative_int_from_cell(
                    row["高校生役の必要回数"],
                    f"{participant.name}さんの高校生役の必要回数",
                )
                if (
                    was_role_unspecified
                    and university_count == 0
                    and high_school_count == 0
                ):
                    university_count = int(config.required_university_count)
                    high_school_count = int(config.required_high_school_count)
                participant.required_university_count = university_count
                participant.required_high_school_count = high_school_count
                participant.practice_participation_count = (
                    university_count + high_school_count
                )
                participant.total_extra_limit = nonnegative_int_from_cell(
                    row["合計参加の超過上限"],
                    f"{participant.name}さんの合計参加の超過上限",
                )
            participant.notes = _cell_text(row["メモ"])
            if (
                participant_individual_condition_signature(participant)
                == participant_individual_condition_signature(
                    participant_by_id[_cell_text(row["ID"])]
                )
            ):
                continue
            if (
                participant_individual_candidate_signature(participant)
                != participant_individual_candidate_signature(
                    participant_by_id[_cell_text(row["ID"])]
                )
            ):
                candidate_affecting_change = True
            participant.updated_at = now_iso()
            updated_participants.append(participant)

        if not updated_participants:
            st.info("変更はありません。")
            return
        if candidate_affecting_change and (
            confirmed or services.candidate_count(project_id)
        ):
            services.confirm_individual_condition_change(
                project_id,
                [participant.to_dict() for participant in updated_participants],
                change_label="個別条件",
                workflow_step_id="conditions",
                published_schedule_exists=bool(confirmed),
            )
            return
        with services.status_message("個別条件を保存しています..."):
            if candidate_affecting_change:
                services.save_participant_updates(
                    project_id,
                    updated_participants,
                )
            else:
                save_participant_admin_fields_bulk(
                    project_id,
                    updated_participants,
                )
            updated_by_id = {
                participant.id: participant for participant in updated_participants
            }
            services.set_cached_participants(
                project_id,
                [
                    updated_by_id.get(participant.id, participant)
                    for participant in participants
                ],
            )
    except (StorageError, StorageConflictError, ValueError) as error:
        st.error(str(error))
        return
    set_project_operation_feedback(
        project_id,
        f"{len(updated_participants)}人の個別条件を保存しました。",
        operation_key="individual_conditions",
    )
    services.mark_step_started(project_id, "conditions")
    st.rerun()
