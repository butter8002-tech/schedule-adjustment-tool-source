"""Account issuance, password reset, and project roster deletion controls.

This module owns the screens and storage calls for manager-created participant
accounts.  Candidate invalidation remains an injected operation because it
also coordinates the manager workflow state held by the application shell.
"""

from __future__ import annotations

import secrets
import string
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.auth import (
    hash_password,
    invalidate_bootstrap_admin_cache,
)
from schedule_adjustment_tool.domain.models import Participant, now_iso
from schedule_adjustment_tool.domain.password_secrets import (
    password_secret_key_configured,
)
from schedule_adjustment_tool.storage import (
    StorageError,
    bulk_create_participant_users,
    bulk_delete_users,
    bulk_update_user_passwords,
    delete_participants,
    save_participant_admin_fields_bulk,
)
from schedule_adjustment_tool.ui.manager.app_cache import (
    clear_audit_logs_cache,
    clear_common_participants_cache,
    load_memberships_cached,
    load_users_cached,
    refresh_memberships_cache,
    refresh_users_cache,
    status_message,
)
from schedule_adjustment_tool.ui.manager.project_cache import (
    load_project_participants_cached,
    refresh_project_data_cache,
    set_cached_participants,
)
from schedule_adjustment_tool.ui.csv_exports import dataframe_csv_bytes


INDIVIDUAL_PARTICIPANT_ACCOUNTS_KEY = "individual_participant_accounts"
BULK_PASSWORD_RESETS_KEY = "bulk_password_resets"


@dataclass(frozen=True)
class AccountManagementServices:
    """Application-owned coordination needed after roster deletion."""

    clear_candidate_state: Callable[[str], None]


def generate_participant_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "-".join(
        "".join(secrets.choice(alphabet) for _ in range(6)) for _ in range(3)
    )


def individual_participant_account_exports() -> dict[str, list[dict[str, str]]]:
    return st.session_state.setdefault(INDIVIDUAL_PARTICIPANT_ACCOUNTS_KEY, {})


def bulk_password_reset_exports() -> list[dict[str, str]]:
    return st.session_state.setdefault(BULK_PASSWORD_RESETS_KEY, [])


def next_simple_participant_username(existing_usernames: set[str], index: int) -> str:
    candidate_number = max(1, index)
    while True:
        username = f"participant{candidate_number:03d}"
        if username.casefold() not in existing_usernames:
            existing_usernames.add(username.casefold())
            return username
        candidate_number += 1


def participant_ids_linked_to_accounts(
    project_id: str,
    participants: list[Participant],
    memberships: list[dict],
) -> set[str]:
    del project_id
    participant_ids = {participant.id for participant in participants}
    return {
        str(item.get("participant_id"))
        for item in memberships
        if item.get("role") == "participant"
        and item.get("participant_id")
        and str(item.get("participant_id")) in participant_ids
    }


def participant_account_rows(
    project_id: str,
    participants: list[Participant],
    memberships: list[dict],
) -> list[dict[str, str]]:
    del project_id
    participant_by_id = {participant.id: participant for participant in participants}
    rows: list[dict[str, str]] = []
    for item in memberships:
        if item.get("role") != "participant" or not item.get("participant_id"):
            continue
        participant = participant_by_id.get(str(item["participant_id"]))
        if not participant:
            continue
        password = str(item.get("password_plain") or "")
        username = str(item.get("username") or "")
        password_source = str(item.get("password_source") or "未記録")
        password_updated_at = str(item.get("password_updated_at") or "")
        rows.append(
            {
                "参加者": participant.name,
                "参加者ID": participant.id,
                "アカウント名": username,
                "パスワード": password,
                "DM用コピー": (
                    f"アカウント：{username}\nパスワード：{password}"
                    if password
                    else f"アカウント：{username}\nパスワード：（未保存）"
                ),
                "最終パスワード発行元": password_source,
                "最終パスワード更新日時": password_updated_at,
            }
        )
    return sorted(rows, key=lambda row: row["参加者"].casefold())


def generate_individual_participant_accounts(
    project_id: str,
    participants: list[Participant],
    *,
    account_source: str,
) -> list[dict[str, str]]:
    account_target_participants = [
        participant for participant in participants if participant.active
    ]
    users = load_users_cached()
    memberships = load_memberships_cached()
    existing_usernames = {str(user["username"]).casefold() for user in users}
    linked_participant_ids = participant_ids_linked_to_accounts(
        project_id,
        account_target_participants,
        memberships,
    )
    account_inputs: list[dict[str, str]] = []
    updated_participants = deepcopy(participants)
    participant_by_id = {
        participant.id: participant for participant in updated_participants
    }
    target_participants = [
        participant
        for participant in updated_participants
        if participant.active and participant.id not in linked_participant_ids
    ]
    for offset, participant in enumerate(target_participants, start=1):
        username = next_simple_participant_username(existing_usernames, offset)
        password = generate_participant_password()
        account_inputs.append(
            {
                "username": username,
                "password_hash": hash_password(password),
                "password_plain": password,
                "participant_id": participant.id,
                "participant_name": participant.name,
                "account_source": account_source,
                "password_source": account_source,
            }
        )
    created_accounts = (
        bulk_create_participant_users(project_id, account_inputs)
        if account_inputs
        else []
    )
    generated_rows: list[dict[str, str]] = []
    for account in created_accounts:
        participant_id = str(account["participant_id"])
        password = str(account["password_plain"])
        username = str(account["username"])
        participant_by_id[participant_id].user_id = str(account["user_id"])
        participant_by_id[participant_id].updated_at = now_iso()
        generated_rows.append(
            {
                "参加者": str(account["participant_name"]),
                "参加者ID": participant_id,
                "アカウント名": username,
                "パスワード": password,
                "DM用コピー": f"アカウント：{username}\nパスワード：{password}",
            }
        )
    if generated_rows:
        save_participant_admin_fields_bulk(project_id, updated_participants)
        set_cached_participants(project_id, updated_participants)
        individual_participant_account_exports()[project_id] = generated_rows
        refresh_users_cache()
        refresh_memberships_cache()
        clear_common_participants_cache()
        clear_audit_logs_cache()
    return generated_rows


def render_individual_participant_account_tools(
    project_id: str,
    participants: list[Participant],
    *,
    heading: str = "参加者個別アカウント一括発行",
    account_source: str = "スケジュール担当者",
) -> None:
    st.markdown(f"**{heading}**")
    target_participants = [
        participant for participant in participants if participant.active
    ]
    if not password_secret_key_configured():
        st.warning(
            "配布用パスワード暗号化鍵が未設定です。"
            " アカウント発行・再発行前に `SCHEDULE_PASSWORD_SECRET_KEY` を設定してください。"
        )
    memberships = load_memberships_cached()
    linked_participant_ids = participant_ids_linked_to_accounts(
        project_id,
        target_participants,
        memberships,
    )
    unlinked_count = sum(
        1
        for participant in target_participants
        if participant.id not in linked_participant_ids
    )
    st.caption(
        f"未紐付け日調対象者: {unlinked_count}人 / "
        f"日調対象者: {len(target_participants)}人。"
        " 登録済み参加者に紐付いたアカウントは、この参加者が参加する企画で共通利用します。"
    )
    linked_rows = participant_account_rows(
        project_id,
        target_participants,
        memberships,
    )
    if st.toggle(
        "現在の紐付けアカウント一覧を表示",
        value=False,
        key=f"show_linked_participant_accounts_{project_id}",
    ):
        if linked_rows:
            linked_frame = pd.DataFrame(linked_rows)
            st.dataframe(linked_frame, hide_index=True, width="stretch")
            st.download_button(
                "現在の紐付けアカウント一覧CSVをダウンロード",
                data=dataframe_csv_bytes(linked_frame),
                file_name=f"linked_participant_accounts_{project_id}.csv",
                mime="text/csv",
                key=f"download_linked_participant_accounts_{project_id}",
            )
        else:
            st.caption("現在、個別に紐付いた参加者アカウントはありません。")
    if st.button(
        "未紐付け参加者の個別アカウントを発行",
        type="primary",
        disabled=not unlinked_count,
        key=f"generate_individual_participant_accounts_{project_id}",
    ):
        try:
            with status_message("参加者個別アカウントを発行しています..."):
                generated = generate_individual_participant_accounts(
                    project_id,
                    participants,
                    account_source=account_source,
                )
            if generated:
                st.success(f"{len(generated)}人分の個別アカウントを発行しました。")
            else:
                st.info("新しく発行する対象者はいませんでした。")
            st.rerun()
        except (ValueError, StorageError) as error:
            st.warning(str(error))

    generated_rows = individual_participant_account_exports().get(project_id, [])
    if generated_rows:
        generated_frame = pd.DataFrame(generated_rows)
        st.dataframe(generated_frame, hide_index=True, width="stretch")
        st.download_button(
            "発行アカウント一覧CSVをダウンロード",
            data=dataframe_csv_bytes(generated_frame),
            file_name=f"participant_accounts_{project_id}.csv",
            mime="text/csv",
            key=f"download_individual_participant_accounts_{project_id}",
        )
        st.caption("パスワード一覧はこの発行結果として表示しています。")


def render_individual_participant_account_generator(projects: list[dict]) -> None:
    project_id = st.selectbox(
        "個別アカウントを発行する企画",
        [str(project["id"]) for project in projects],
        format_func=lambda value: next(
            str(project["title"])
            for project in projects
            if str(project["id"]) == value
        ),
        key="individual_participant_account_project",
    )
    participants = load_project_participants_cached(project_id)
    render_individual_participant_account_tools(
        project_id,
        participants,
        heading="選択企画の参加者個別アカウント",
        account_source="システム管理",
    )


def render_bulk_account_password_reset(users: list[dict]) -> None:
    st.markdown("**パスワード一括再発行**")
    if not password_secret_key_configured():
        st.warning(
            "配布用パスワード暗号化鍵が未設定です。"
            " 再発行前に `SCHEDULE_PASSWORD_SECRET_KEY` を設定してください。"
        )
    selected_user_ids = st.multiselect(
        "再発行するアカウント",
        [str(user["id"]) for user in users],
        format_func=lambda value: next(
            str(user["username"])
            for user in users
            if str(user["id"]) == value
        ),
        key="bulk_reset_password_user_ids",
    )
    if st.button(
        "選択したアカウントのパスワードを一括再発行",
        type="primary",
        disabled=not selected_user_ids,
        key="bulk_reset_passwords",
    ):
        reset_rows: list[dict[str, str]] = []
        try:
            with status_message("パスワードを一括再発行しています..."):
                user_by_id = {str(user["id"]): user for user in users}
                password_updates: list[dict[str, str]] = []
                for user_id in selected_user_ids:
                    user = user_by_id[user_id]
                    password = generate_participant_password()
                    password_updates.append(
                        {
                            "user_id": user_id,
                            "password_hash": hash_password(password),
                            "password_plain": password,
                            "password_source": "システム管理",
                        }
                    )
                    username = str(user["username"])
                    reset_rows.append(
                        {
                            "アカウント名": username,
                            "パスワード": password,
                            "DM用コピー": (
                                f"アカウント：{username}\nパスワード：{password}"
                            ),
                        }
                    )
                bulk_update_user_passwords(
                    password_updates,
                    password_source="システム管理",
                )
                st.session_state[BULK_PASSWORD_RESETS_KEY] = reset_rows
                refresh_users_cache()
                refresh_memberships_cache()
                clear_audit_logs_cache()
            st.success(f"{len(reset_rows)}件のパスワードを再発行しました。")
            st.rerun()
        except (ValueError, StorageError) as error:
            st.warning(str(error))

    reset_rows = bulk_password_reset_exports()
    if reset_rows:
        reset_frame = pd.DataFrame(reset_rows)
        st.dataframe(reset_frame, hide_index=True, width="stretch")
        st.download_button(
            "再発行パスワード一覧CSVをダウンロード",
            data=dataframe_csv_bytes(reset_frame),
            file_name="reset_account_passwords.csv",
            mime="text/csv",
            key="download_bulk_reset_passwords",
        )


def render_bulk_account_deletion(users: list[dict]) -> None:
    st.markdown("**アカウント一括削除**")
    selected_user_ids = st.multiselect(
        "削除するアカウント",
        [str(user["id"]) for user in users],
        format_func=lambda value: next(
            str(user["username"])
            for user in users
            if str(user["id"]) == value
        ),
        key="bulk_delete_user_ids",
    )
    if st.button(
        "選択したアカウントを一括削除",
        type="primary",
        disabled=not selected_user_ids,
        key="bulk_delete_users",
    ):
        try:
            with status_message("アカウントを一括削除しています..."):
                deleted_count = bulk_delete_users(list(selected_user_ids))
                invalidate_bootstrap_admin_cache()
                individual_participant_account_exports().clear()
                refresh_users_cache()
                refresh_memberships_cache()
                clear_audit_logs_cache()
            st.success(f"{deleted_count}件のアカウントを削除しました。")
            st.rerun()
        except StorageError as error:
            st.warning(str(error))


def delete_project_participants_with_memberships(
    project_id: str,
    participant_ids: list[str],
    *,
    services: AccountManagementServices,
) -> None:
    delete_participants(project_id, participant_ids)
    refresh_project_data_cache(project_id)
    refresh_memberships_cache()
    individual_participant_account_exports().pop(project_id, None)
    clear_common_participants_cache()
    services.clear_candidate_state(project_id)
    clear_audit_logs_cache()


def render_participant_deletion_tools(
    project_id: str,
    participants: list[Participant],
    *,
    services: AccountManagementServices,
) -> None:
    st.markdown("**参加者削除**")
    selected_participant_ids = st.multiselect(
        "削除する参加者",
        [participant.id for participant in participants],
        format_func=lambda value: next(
            participant.name
            for participant in participants
            if participant.id == value
        ),
        key=f"delete_participant_ids_{project_id}",
    )
    st.caption("削除すると、この企画の名簿・回答・本人アカウント紐付けから外れます。")
    if st.button(
        "選択した参加者を削除",
        type="primary",
        disabled=not selected_participant_ids,
        key=f"delete_participants_{project_id}",
    ):
        try:
            with status_message("参加者を削除しています..."):
                delete_project_participants_with_memberships(
                    project_id,
                    list(selected_participant_ids),
                    services=services,
                )
            st.success(f"{len(selected_participant_ids)}人の参加者を削除しました。")
            st.rerun()
        except StorageError as error:
            st.warning(str(error))
