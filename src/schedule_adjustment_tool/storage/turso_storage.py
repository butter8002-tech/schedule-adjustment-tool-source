from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from uuid import uuid4

import libsql

from schedule_adjustment_tool.domain.app_config import (
    DEFAULT_DELETED_RETENTION_DAYS,
    MAX_RETENTION_DAYS,
    bounded_int,
    configured_audit_retention_days,
    configured_backup_limit,
    configured_deleted_retention_days,
)
from schedule_adjustment_tool.domain.models import (
    Config,
    LEGACY_SUPPORT_GROUP,
    Participant,
    make_slot_key,
    now_iso,
    parse_slot_key,
    participant_response_editable,
    participant_name_identity_key,
)
from schedule_adjustment_tool.domain.amendments import (
    AMENDMENT_DRAFT_SOURCES,
    active_amendment,
    amendment_requests,
    amendment_unavailable_slots_by_participant,
    empty_amendment_workspace,
    normalize_amendment_workspace,
)
from schedule_adjustment_tool.domain.password_secrets import (
    PasswordSecretError,
    decrypt_password_secret,
    encrypt_password_secret,
    password_secret_key_configured,
)
from schedule_adjustment_tool.domain.schedule_model import (
    ScheduleModelError,
    normalize_schedule,
)
from schedule_adjustment_tool.storage.schedule_revision_store import (
    apply_confirmed_migration as _apply_confirmed_schedule_migration,
    apply_rollback as _apply_schedule_migration_rollback,
    cross_project_conflicts as _schedule_cross_project_conflicts,
    insert_revision as _insert_schedule_revision,
    list_revisions as _list_schedule_revisions,
    load_effective_schedule as _load_effective_schedule,
    load_revision as _load_schedule_revision,
    migration_plan as _schedule_migration_plan,
    revision_guard_matches as _schedule_revision_guard_matches,
    rollback_plan as _schedule_migration_rollback_plan,
)
from schedule_adjustment_tool.storage.performance import (
    current_metrics,
    measure_storage_operation,
)
from schedule_adjustment_tool.storage.version_updates import (
    acknowledge_support_role_notice as _acknowledge_support_role_notice,
    load_support_role_notice as _load_support_role_notice,
    migrate_v101_support_role_unspecified,
)
from schedule_adjustment_tool.integrations.turso_encryption import (
    build_encrypted_database_url,
)


DOCUMENT_KINDS = {
    "config",
    "participants",
    "candidates",
    "confirmed_candidate",
    "history",
    "schedule_amendments",
}
CONFIG_FIELD_NAMES = set(Config().to_dict())
PROJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_audit_actor: contextvars.ContextVar[str] = contextvars.ContextVar(
    "audit_actor", default="system"
)
LOGGER = logging.getLogger("schedule_adjustment_tool.storage")
_initialization_lock = threading.RLock()
_initialized_storage_keys: set[str] = set()
_cleanup_checked_dates: dict[str, str] = {}


class StorageError(RuntimeError):
    pass


class StorageConflictError(StorageError):
    pass


class InvalidProjectIdError(StorageError):
    pass


class _Row:
    def __init__(self, columns: list[str], values: tuple[Any, ...]) -> None:
        self._columns = columns
        self._values = values
        self._index = {column: index for index, column in enumerate(columns)}

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> list[str]:
        return list(self._columns)


class _Cursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def _columns(self) -> list[str]:
        return [column[0] for column in (self._cursor.description or [])]

    def _wrap_row(self, row: Any) -> _Row | None:
        if row is None:
            return None
        return _Row(self._columns(), tuple(row))

    def fetchone(self) -> _Row | None:
        started = time.perf_counter()
        try:
            row = self._cursor.fetchone()
        finally:
            metrics = current_metrics()
            if metrics is not None:
                metrics.record_fetch(time.perf_counter() - started)
        metrics = current_metrics()
        if metrics is not None:
            metrics.record_result(row)
        return self._wrap_row(row)

    def fetchall(self) -> list[_Row]:
        started = time.perf_counter()
        try:
            rows = self._cursor.fetchall()
        finally:
            metrics = current_metrics()
            if metrics is not None:
                metrics.record_fetch(time.perf_counter() - started)
        metrics = current_metrics()
        if metrics is not None:
            metrics.record_result(rows)
        return [
            wrapped
            for row in rows
            if (wrapped := self._wrap_row(row)) is not None
        ]


class _Connection:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def execute(self, *args: Any, **kwargs: Any) -> _Cursor:
        started = time.perf_counter()
        try:
            cursor = self._connection.execute(*args, **kwargs)
        finally:
            metrics = current_metrics()
            if metrics is not None:
                parameters = args[1] if len(args) > 1 else kwargs.get("parameters")
                metrics.record_sql(parameters, time.perf_counter() - started)
        return _Cursor(cursor)

    def executescript(self, *args: Any, **kwargs: Any) -> _Cursor:
        started = time.perf_counter()
        try:
            cursor = self._connection.executescript(*args, **kwargs)
        finally:
            metrics = current_metrics()
            if metrics is not None:
                metrics.record_sql(None, time.perf_counter() - started)
        return _Cursor(cursor)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise StorageError(f"{name} is not configured.")
    if value in {"libsql://...", "YOUR_TOKEN", "YOUR_DATABASE_TOKEN"}:
        raise StorageError(f"{name} contains a placeholder value.")
    return value


def _optional_env(name: str) -> str:
    return os.getenv(name, "").strip()


def _database_url() -> str:
    try:
        return build_encrypted_database_url(
            _required_env("TURSO_DATABASE_URL"),
            cipher=_optional_env("TURSO_ENCRYPTION_CIPHER"),
            hexkey=_optional_env("TURSO_ENCRYPTION_HEXKEY"),
        )
    except ValueError as error:
        raise StorageError(str(error)) from error


def _auth_token_for_database(database_url: str) -> str:
    if database_url.startswith("file:"):
        return _optional_env("TURSO_AUTH_TOKEN")
    return _required_env("TURSO_AUTH_TOKEN")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise StorageError("保存データのJSONが破損しています。") from error


def _normalize_system_settings(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    cohorts = raw.get("active_cohorts", [14, 15, 16, 17])
    normalized = sorted(
        {
            int(cohort)
            for cohort in cohorts
            if str(cohort).isdigit() and int(cohort) >= 1
        }
    )
    return {
        "active_cohorts": normalized or [14, 15, 16, 17],
        "privacy_notice": str(raw.get("privacy_notice", "")),
        "data_retention_days": bounded_int(
            raw.get("data_retention_days", DEFAULT_DELETED_RETENTION_DAYS),
            DEFAULT_DELETED_RETENTION_DAYS,
            1,
            MAX_RETENTION_DAYS,
        ),
        "maintenance_mode": bool(raw.get("maintenance_mode", False)),
    }


def _raise_connection_error(error: Exception) -> None:
    if isinstance(error, StorageError):
        raise error
    raise StorageError(
        "Tursoへの接続または保存に失敗しました。"
        " しばらく待ってから再読み込みしてください。"
    ) from error


@contextmanager
def _connection(*, write: bool = False) -> Iterator[_Connection]:
    try:
        database_url = _database_url()
        connect_started = time.perf_counter()
        connection = _Connection(
            libsql.connect(
                database=database_url,
                auth_token=_auth_token_for_database(database_url),
            )
        )
        metrics = current_metrics()
        if metrics is not None:
            metrics.record_connection(time.perf_counter() - connect_started)
        connection.execute("PRAGMA foreign_keys = ON")
        if write:
            connection.execute("BEGIN IMMEDIATE")
    except Exception as error:
        _raise_connection_error(error)
    try:
        yield connection
        if write:
            commit_started = time.perf_counter()
            connection.commit()
            metrics = current_metrics()
            if metrics is not None:
                metrics.commit_seconds += time.perf_counter() - commit_started
    except Exception as error:
        if write:
            try:
                connection.rollback()
            except Exception:
                pass
        _raise_connection_error(error)
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _create_schema(connection: _Connection) -> None:
    schema = """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            deleted_at TEXT
        );
        CREATE TABLE IF NOT EXISTS documents (
            project_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (project_id, kind),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS common_participants (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            profile_payload TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_participations (
            project_id TEXT NOT NULL,
            participant_id TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            roster_payload TEXT NOT NULL,
            attributes_payload TEXT NOT NULL,
            requirements_payload TEXT NOT NULL,
            response_payload TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (project_id, participant_id),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY (participant_id) REFERENCES common_participants(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS candidate_data (
            project_id TEXT NOT NULL,
            candidate_number INTEGER NOT NULL,
            payload TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (project_id, candidate_number),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS confirmed_candidate_data (
            project_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS schedule_revisions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            source TEXT NOT NULL,
            parent_revision_id TEXT,
            candidate_number INTEGER,
            change_note TEXT NOT NULL DEFAULT '',
            metadata_payload TEXT NOT NULL,
            migration_id TEXT,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            UNIQUE(project_id, revision_number),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY (parent_revision_id) REFERENCES schedule_revisions(id)
        );
        CREATE TABLE IF NOT EXISTS schedule_sessions (
            id TEXT PRIMARY KEY,
            revision_id TEXT NOT NULL,
            session_uid TEXT NOT NULL,
            session_order INTEGER NOT NULL,
            session_date TEXT NOT NULL,
            period INTEGER NOT NULL,
            group_index INTEGER NOT NULL,
            meeting_mode TEXT NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE(revision_id, session_uid),
            FOREIGN KEY (revision_id) REFERENCES schedule_revisions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS session_assignments (
            session_id TEXT NOT NULL,
            participant_id TEXT NOT NULL,
            role TEXT NOT NULL,
            assignment_order INTEGER NOT NULL,
            participant_name TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (session_id, participant_id, role),
            FOREIGN KEY (session_id) REFERENCES schedule_sessions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS active_schedule_revisions (
            project_id TEXT PRIMARY KEY,
            revision_id TEXT NOT NULL UNIQUE,
            activated_at TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY (revision_id) REFERENCES schedule_revisions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS schedule_migrations (
            id TEXT PRIMARY KEY,
            migration_name TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL,
            rolled_back_at TEXT
        );
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            kind TEXT NOT NULL,
            payload TEXT,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            project_id TEXT,
            target TEXT,
            detail TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            password_plain TEXT,
            password_secret TEXT,
            password_source TEXT,
            password_updated_at TEXT,
            is_system_admin INTEGER NOT NULL DEFAULT 0,
            is_schedule_manager INTEGER NOT NULL DEFAULT 0,
            is_participant INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        );
        CREATE TABLE IF NOT EXISTS memberships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('manager', 'participant')),
            participant_id TEXT,
            account_source TEXT,
            UNIQUE(user_id, project_id, role),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS job_locks (
            project_id TEXT PRIMARY KEY,
            locked_by TEXT NOT NULL,
            locked_until TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(user_id);
        CREATE INDEX IF NOT EXISTS idx_common_participants_name
            ON common_participants(name);
        CREATE INDEX IF NOT EXISTS idx_project_participations_project
            ON project_participations(project_id, sort_order);
        CREATE INDEX IF NOT EXISTS idx_candidate_data_project
            ON candidate_data(project_id, candidate_number);
        CREATE INDEX IF NOT EXISTS idx_schedule_revisions_project
            ON schedule_revisions(project_id, revision_number);
        CREATE INDEX IF NOT EXISTS idx_schedule_sessions_slot
            ON schedule_sessions(session_date, period);
        CREATE INDEX IF NOT EXISTS idx_session_assignments_participant
            ON session_assignments(participant_id, session_id);
        """
    connection.executescript(schema)


def _insert_document(
    connection: _Connection,
    project_id: str,
    kind: str,
    value: Any,
) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO documents(project_id, kind, payload, version, updated_at)
        VALUES (?, ?, ?, 1, ?)
        """,
        (project_id, kind, _json_dumps(value), now_iso()),
    )


def _response_snapshot(participant: Participant) -> dict[str, Any]:
    return {
        "availability": sorted(set(participant.availability)),
        "zoom_availability": sorted(
            set(participant.zoom_availability) - set(participant.availability)
        ),
        "support_requested_count": participant.support_requested_count,
        "submitted_at": participant.submitted_at,
        "input_status": participant.input_status,
        "updated_at": participant.updated_at,
    }


def _apply_response_snapshot(
    participant: Participant,
    response: dict[str, Any],
) -> None:
    normalized = _normalize_response_snapshot(response)
    participant.availability = list(normalized["availability"])
    participant.zoom_availability = list(normalized["zoom_availability"])
    participant.support_requested_count = normalized["support_requested_count"]
    participant.submitted_at = str(normalized["submitted_at"])
    participant.input_status = str(normalized["input_status"])
    participant.updated_at = str(normalized["updated_at"])


def _normalize_response_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    normalized = Participant.from_dict(
        {
            "id": "response",
            "name": "response",
            **value,
        }
    )
    if normalized.input_status not in {"not_started", "draft", "submitted"}:
        normalized.input_status = "not_started"
    return _response_snapshot(normalized)


def _split_response_payload(
    response: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    participant_raw = response.get("participant_response")
    participant_response = _normalize_response_snapshot(
        participant_raw if isinstance(participant_raw, dict) else response
    )
    manager_raw = response.get("manager_response")
    manager_response: dict[str, Any] = {}
    if isinstance(manager_raw, dict) and manager_raw:
        manager_response = {
            **_normalize_response_snapshot(manager_raw),
            "active": bool(manager_raw.get("active", False)),
            "updated_by": str(manager_raw.get("updated_by", "")),
            "cleared_at": str(manager_raw.get("cleared_at", "")),
            "cleared_by": str(manager_raw.get("cleared_by", "")),
        }
    manager_active = bool(manager_response.get("active", False))
    effective = (
        _normalize_response_snapshot(manager_response)
        if manager_active
        else participant_response
    )
    return (
        participant_response,
        manager_response,
        effective,
        "manager" if manager_active else "participant",
    )


def _combined_response_payload(
    participant_response: dict[str, Any],
    manager_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    participant_response = _normalize_response_snapshot(participant_response)
    manager_response = dict(manager_response or {})
    manager_active = bool(manager_response.get("active", False))
    if manager_response:
        metadata = {
            "active": manager_active,
            "updated_by": str(manager_response.get("updated_by", "")),
            "cleared_at": str(manager_response.get("cleared_at", "")),
            "cleared_by": str(manager_response.get("cleared_by", "")),
        }
        manager_response = {
            **_normalize_response_snapshot(manager_response),
            **metadata,
        }
    effective = (
        _normalize_response_snapshot(manager_response)
        if manager_active
        else participant_response
    )
    return {
        **effective,
        "participant_response": participant_response,
        "manager_response": manager_response,
        "response_source": "manager" if manager_active else "participant",
    }


def _participant_response_payload(participant: Participant) -> dict[str, Any]:
    participant_response = (
        participant.participant_response
        if participant.participant_response
        else _response_snapshot(participant)
    )
    manager_response = dict(participant.manager_response)
    if manager_response:
        manager_response["active"] = participant.response_source == "manager"
    return _combined_response_payload(participant_response, manager_response)


def _participant_payloads(participant: Participant) -> dict[str, dict[str, Any]]:
    return {
        "roster": {
            "id": participant.id,
            "name": participant.name,
            "registered_by": participant.registered_by,
            "approved": participant.approved,
            "active": participant.active,
            "user_id": participant.user_id,
        },
        "attributes": {
            "group_number": participant.group_number,
            "cohort": participant.cohort,
            "humanities_or_science": participant.humanities_or_science,
            "department": participant.department,
            "department_detail": participant.department_detail,
            "attributes_changed_by_participant": (
                participant.attributes_changed_by_participant
            ),
            "attributes_changed_at": participant.attributes_changed_at,
        },
        "requirements": {
            "required_university_count": participant.required_university_count,
            "required_high_school_count": participant.required_high_school_count,
            "total_extra_limit": participant.total_extra_limit,
            "support_desired_count": participant.support_desired_count,
            "practice_role_unspecified": participant.practice_role_unspecified,
            "practice_participation_count": participant.practice_participation_count,
            "notes": participant.notes,
        },
        "response": _participant_response_payload(participant),
    }


def _execute_multirow_insert(
    connection: _Connection,
    statement: str,
    rows: list[tuple[Any, ...]],
    *,
    suffix: str = "",
) -> int:
    if not rows:
        return 0
    row_placeholder = "(" + ", ".join("?" for _ in rows[0]) + ")"
    affected_rows = 0
    for offset in range(0, len(rows), 64):
        chunk = rows[offset : offset + 64]
        placeholders = ", ".join(row_placeholder for _ in chunk)
        parameters = tuple(value for row in chunk for value in row)
        cursor = connection.execute(
            f"{statement} VALUES {placeholders}{suffix}",
            parameters,
        )
        if cursor.rowcount >= 0:
            affected_rows += cursor.rowcount
    return affected_rows


def _common_participant_profile(participant: Participant) -> dict[str, Any]:
    return {
        "id": participant.id,
        "name": participant.name,
        "user_id": participant.user_id,
        "cohort": participant.cohort,
        "humanities_or_science": participant.humanities_or_science,
        "department": participant.department,
        "department_detail": participant.department_detail,
    }


def _upsert_common_participant(
    connection: _Connection,
    participant: Participant,
) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    connection.execute(
        """
        INSERT INTO common_participants
        (id, name, profile_payload, active, created_at, updated_at)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            profile_payload = excluded.profile_payload,
            active = excluded.active,
            updated_at = excluded.updated_at
        """,
        (
            participant.id,
            participant.name,
            _json_dumps(_common_participant_profile(participant)),
            timestamp,
            timestamp,
        ),
    )


def _upsert_common_participants(
    connection: _Connection,
    participants: list[Participant],
    *,
    timestamp: str,
) -> None:
    _execute_multirow_insert(
        connection,
        """
        INSERT INTO common_participants
        (id, name, profile_payload, active, created_at, updated_at)
        """,
        [
            (
                participant.id,
                participant.name,
                _json_dumps(_common_participant_profile(participant)),
                1,
                timestamp,
                timestamp,
            )
            for participant in participants
        ],
        suffix="""
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            profile_payload = excluded.profile_payload,
            active = excluded.active,
            updated_at = excluded.updated_at
        """,
    )


def _upsert_project_participant_rows(
    connection: _Connection,
    rows: list[tuple[Any, ...]],
    *,
    expected_versions: bool = False,
) -> int:
    suffix = """
        ON CONFLICT(project_id, participant_id) DO UPDATE SET
            sort_order = excluded.sort_order,
            roster_payload = excluded.roster_payload,
            attributes_payload = excluded.attributes_payload,
            requirements_payload = excluded.requirements_payload,
            response_payload = excluded.response_payload,
            version = excluded.version,
            updated_at = excluded.updated_at
        """
    if expected_versions:
        suffix += """
        WHERE project_participations.version = excluded.version - 1
        """
    return _execute_multirow_insert(
        connection,
        """
        INSERT INTO project_participations
        (project_id, participant_id, sort_order, roster_payload,
         attributes_payload, requirements_payload, response_payload,
         version, updated_at)
        """,
        rows,
        suffix=suffix,
    )


def _common_participant_to_dict(row: Any) -> dict[str, Any]:
    profile = _json_loads(row["profile_payload"], {})
    if not isinstance(profile, dict):
        profile = {}
    row_keys = set(row.keys())
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "active": bool(row["active"]),
        "updated_at": (
            str(row["updated_at"]) if "updated_at" in row_keys else ""
        ),
        "project_count": (
            int(row["project_count"]) if "project_count" in row_keys else 0
        ),
        **profile,
    }


def _participant_from_common_row(row: Any, registered_by: str) -> Participant:
    profile = _common_participant_to_dict(row)
    participant = Participant.create(str(profile["name"]), registered_by)
    participant.id = str(profile["id"])
    participant.user_id = str(profile.get("user_id", "") or "")
    participant.cohort = profile.get("cohort")
    participant.humanities_or_science = str(
        profile.get("humanities_or_science", "") or ""
    )
    participant.department = str(profile.get("department", "") or "")
    participant.department_detail = str(profile.get("department_detail", "") or "")
    return participant


def _common_participant_row(
    connection: _Connection,
    participant_id: str,
) -> Any | None:
    return connection.execute(
        """
        SELECT id, name, profile_payload, active, updated_at,
               0 AS project_count
        FROM common_participants
        WHERE id = ? AND active = 1
        """,
        (str(participant_id),),
    ).fetchone()


def _common_participant_name_conflicts(
    connection: _Connection,
    participants: list[Participant],
) -> list[str]:
    participant_ids_by_name: dict[str, set[str]] = {}
    display_names_by_key: dict[str, str] = {}
    for participant in participants:
        name = participant.name.strip()
        key = participant_name_identity_key(name)
        participant_ids_by_name.setdefault(key, set()).add(participant.id)
        display_names_by_key.setdefault(key, name)
    if not participant_ids_by_name:
        return []
    rows = connection.execute(
        """
        SELECT id, name
        FROM common_participants
        WHERE active = 1
        """
    ).fetchall()
    conflicts = {
        display_names_by_key[key]
        for row in rows
        if (
            (key := participant_name_identity_key(row["name"]))
            in participant_ids_by_name
            and str(row["id"]) not in participant_ids_by_name[key]
        )
    }
    return sorted(conflicts, key=str.casefold)


def _raise_common_participant_name_conflict(conflicts: list[str]) -> None:
    if conflicts:
        raise StorageError(
            "登録済み参加者に登録済みの参加者名は新規作成できません: "
            + "、".join(conflicts)
        )


def _participant_from_common_and_payloads(
    common: dict[str, Any],
    roster: dict[str, Any],
    attributes: dict[str, Any],
    requirements: dict[str, Any],
    response: dict[str, Any],
    project_id: str = "",
) -> Participant:
    profile = _json_loads(common.get("profile_payload"), {})
    if not isinstance(profile, dict):
        profile = {}
    (
        participant_response,
        manager_response,
        effective_response,
        response_source,
    ) = _split_response_payload(response)
    participant = Participant.from_dict(
        {
            **roster,
            **attributes,
            **requirements,
            **effective_response,
            "id": common.get("id") or roster.get("id"),
            "name": common.get("name") or roster.get("name", ""),
            "user_id": profile.get("user_id", roster.get("user_id", "")),
            "cohort": profile.get("cohort", attributes.get("cohort")),
            "humanities_or_science": profile.get(
                "humanities_or_science",
                attributes.get("humanities_or_science", ""),
            ),
            "department": profile.get("department", attributes.get("department", "")),
            "department_detail": profile.get(
                "department_detail",
                attributes.get("department_detail", ""),
            ),
            "participant_response": participant_response,
            "manager_response": manager_response,
            "response_source": response_source,
        }
    )
    participant.storage_version = int(common.get("storage_version", 0) or 0)
    participant.storage_project_id = project_id
    return participant


def _participant_from_payloads(
    roster: dict[str, Any],
    attributes: dict[str, Any],
    requirements: dict[str, Any],
    response: dict[str, Any],
) -> Participant:
    (
        participant_response,
        manager_response,
        effective_response,
        response_source,
    ) = _split_response_payload(response)
    return Participant.from_dict(
        {
            **roster,
            **attributes,
            **requirements,
            **effective_response,
            "participant_response": participant_response,
            "manager_response": manager_response,
            "response_source": response_source,
        }
    )


def _replace_participant_rows(
    connection: _Connection,
    project_id: str,
    participants: list[Participant],
) -> None:
    existing_rows = {
        str(row["participant_id"]): row
        for row in connection.execute(
            """
            SELECT participant_id, sort_order, roster_payload,
                   attributes_payload, requirements_payload,
                   response_payload, version
            FROM project_participations WHERE project_id = ?
            """,
            (project_id,),
        ).fetchall()
    }
    timestamp = now_iso()
    common_participants: list[Participant] = []
    participation_rows: list[tuple[Any, ...]] = []
    for index, participant in enumerate(participants):
        common_participants.append(participant)
        payloads = _participant_payloads(participant)
        serialized = {
            kind: _json_dumps(payloads[kind])
            for kind in ("roster", "attributes", "requirements", "response")
        }
        existing = existing_rows.get(participant.id)
        row_changed = existing is None or any(
            (
                int(existing["sort_order"]) != index,
                existing["roster_payload"] != serialized["roster"],
                existing["attributes_payload"] != serialized["attributes"],
                existing["requirements_payload"] != serialized["requirements"],
                existing["response_payload"] != serialized["response"],
            )
        )
        next_version = (
            1
            if existing is None
            else int(existing["version"]) + int(row_changed)
        )
        participation_rows.append(
            (
                project_id,
                participant.id,
                index,
                serialized["roster"],
                serialized["attributes"],
                serialized["requirements"],
                serialized["response"],
                next_version,
                timestamp,
            ),
        )
        participant.storage_version = next_version
        participant.storage_project_id = project_id
    _upsert_common_participants(
        connection,
        common_participants,
        timestamp=timestamp,
    )
    _upsert_project_participant_rows(connection, participation_rows)
    participant_ids = [participant.id for participant in participants]
    if participant_ids:
        placeholders = ",".join("?" for _ in participant_ids)
        connection.execute(
            f"DELETE FROM project_participations "
            f"WHERE project_id = ? AND participant_id NOT IN ({placeholders})",
            (project_id, *participant_ids),
        )
    else:
        connection.execute(
            "DELETE FROM project_participations WHERE project_id = ?",
            (project_id,),
        )


def _participant_rows(connection: _Connection, project_id: str) -> list[Participant]:
    rows = connection.execute(
        """
        SELECT cp.id, cp.name, cp.profile_payload,
               pp.roster_payload, pp.attributes_payload,
               pp.requirements_payload, pp.response_payload,
               pp.version AS storage_version
        FROM project_participations pp
        JOIN common_participants cp ON cp.id = pp.participant_id
        WHERE pp.project_id = ?
        ORDER BY pp.sort_order, cp.name
        """,
        (project_id,),
    ).fetchall()
    return [
        _participant_from_common_and_payloads(
            {
                "id": row["id"],
                "name": row["name"],
                "profile_payload": row["profile_payload"],
                "storage_version": row["storage_version"],
            },
            _json_loads(row["roster_payload"], {}),
            _json_loads(row["attributes_payload"], {}),
            _json_loads(row["requirements_payload"], {}),
            _json_loads(row["response_payload"], {}),
            project_id,
        )
        for row in rows
    ]


def _participant_row(
    connection: _Connection, project_id: str, participant_id: str
) -> Participant | None:
    row = connection.execute(
        """
        SELECT cp.id, cp.name, cp.profile_payload,
               pp.roster_payload, pp.attributes_payload,
               pp.requirements_payload, pp.response_payload,
               pp.version AS storage_version
        FROM project_participations pp
        JOIN common_participants cp ON cp.id = pp.participant_id
        WHERE pp.project_id = ? AND pp.participant_id = ?
        """,
        (project_id, participant_id),
    ).fetchone()
    if row is None:
        return None
    return _participant_from_common_and_payloads(
        {
            "id": row["id"],
            "name": row["name"],
            "profile_payload": row["profile_payload"],
            "storage_version": row["storage_version"],
        },
        _json_loads(row["roster_payload"], {}),
        _json_loads(row["attributes_payload"], {}),
        _json_loads(row["requirements_payload"], {}),
        _json_loads(row["response_payload"], {}),
        project_id,
    )


def _write_participants_snapshot(
    connection: _Connection,
    project_id: str,
    changed_participants: list[Participant] | None = None,
) -> int:
    current_row = connection.execute(
        "SELECT payload, version FROM documents WHERE project_id = ? AND kind = ?",
        (_validate_project_id(project_id), "participants"),
    ).fetchone()
    current_serialized = current_row["payload"] if current_row else None
    current_payload = _json_loads(current_serialized, [])
    current_version = int(current_row["version"]) if current_row else 0
    metrics = current_metrics()
    if metrics is not None:
        metrics.add_snapshot_read_bytes(current_serialized)
    snapshot: list[dict[str, Any]] | None = None
    if isinstance(current_payload, list) and changed_participants:
        snapshot = [
            item for item in current_payload if isinstance(item, dict)
        ]
        indexes = {
            str(item.get("id")): index
            for index, item in enumerate(snapshot)
            if str(item.get("id", ""))
        }
        if all(participant.id in indexes for participant in changed_participants):
            for participant in changed_participants:
                snapshot[indexes[participant.id]] = participant.to_dict()
    if snapshot is None:
        snapshot = [
            participant.to_dict()
            for participant in _participant_rows(connection, project_id)
        ]
    if metrics is not None:
        metrics.add_snapshot_write_bytes(snapshot)
    return _write_document(
        connection,
        project_id,
        "participants",
        snapshot,
        current_document=(
            (current_serialized, current_version)
            if current_row is not None
            else None
        ),
    )


def _write_participants_snapshots_batch(
    connection: _Connection,
    changed_by_project: dict[str, list[Participant]],
) -> dict[str, int]:
    """Write one legacy snapshot per project with batched SQL round trips."""

    if not changed_by_project:
        return {}
    project_ids = list(changed_by_project)
    placeholders = ",".join("?" for _ in project_ids)
    current_rows = connection.execute(
        f"""
        SELECT project_id, payload, version
        FROM documents
        WHERE kind = 'participants' AND project_id IN ({placeholders})
        """,
        tuple(project_ids),
    ).fetchall()
    current_by_project = {
        str(row["project_id"]): row for row in current_rows
    }
    backups: list[tuple[Any, ...]] = []
    documents: list[tuple[Any, ...]] = []
    versions: dict[str, int] = {}
    prune_project_ids: list[str] = []
    metrics = current_metrics()
    for project_id, changed_participants in changed_by_project.items():
        current_row = current_by_project.get(project_id)
        current_serialized = current_row["payload"] if current_row else None
        current_payload = _json_loads(current_serialized, [])
        current_version = int(current_row["version"]) if current_row else 0
        if metrics is not None:
            metrics.add_snapshot_read_bytes(current_serialized)
        snapshot: list[dict[str, Any]] | None = None
        if isinstance(current_payload, list) and changed_participants:
            snapshot = [
                item for item in current_payload if isinstance(item, dict)
            ]
            indexes = {
                str(item.get("id")): index
                for index, item in enumerate(snapshot)
                if str(item.get("id", ""))
            }
            if all(
                participant.id in indexes
                for participant in changed_participants
            ):
                for participant in changed_participants:
                    snapshot[indexes[participant.id]] = participant.to_dict()
        if snapshot is None:
            snapshot = [
                participant.to_dict()
                for participant in _participant_rows(connection, project_id)
            ]
        if metrics is not None:
            metrics.add_snapshot_write_bytes(snapshot)
        serialized_snapshot = _json_dumps(snapshot)
        if current_row is not None and current_serialized == serialized_snapshot:
            versions[project_id] = current_version
            continue
        new_version = current_version + 1
        versions[project_id] = new_version
        if current_row is not None:
            backups.append(
                (
                    project_id,
                    "participants",
                    current_serialized,
                    current_version,
                    now_iso(),
                )
            )
        documents.append(
            (
                project_id,
                "participants",
                serialized_snapshot,
                new_version,
                now_iso(),
            )
        )
        prune_project_ids.append(project_id)

    _execute_multirow_insert(
        connection,
        """
        INSERT INTO backups(project_id, kind, payload, version, created_at)
        """,
        backups,
    )
    _execute_multirow_insert(
        connection,
        """
        INSERT INTO documents(project_id, kind, payload, version, updated_at)
        """,
        documents,
        suffix="""
        ON CONFLICT(project_id, kind) DO UPDATE SET
            payload = excluded.payload,
            version = excluded.version,
            updated_at = excluded.updated_at
        """,
    )
    for project_id in prune_project_ids:
        _prune_backups(connection, project_id, "participants")
    return versions


def _ensure_participant_rows(connection: _Connection, project_id: str) -> None:
    row_count = connection.execute(
        "SELECT COUNT(*) FROM project_participations WHERE project_id = ?",
        (project_id,),
    ).fetchone()[0]
    if row_count:
        return
    raw, _ = _document(connection, project_id, "participants", [])
    if not isinstance(raw, list):
        return
    participants = [
        Participant.from_dict(item)
        for item in raw
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    ]
    _replace_participant_rows(connection, project_id, participants)


def _migrate_participant_rows(connection: _Connection) -> None:
    migrated = connection.execute(
        "SELECT value FROM metadata WHERE key = 'participant_rows_migrated'"
    ).fetchone()
    if migrated:
        return
    project_rows = connection.execute("SELECT id FROM projects").fetchall()
    for project_row in project_rows:
        _ensure_participant_rows(connection, project_row["id"])
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        ("participant_rows_migrated", now_iso()),
    )


def _replace_candidate_rows(
    connection: _Connection,
    project_id: str,
    candidates: list[dict[str, Any]],
) -> None:
    connection.execute(
        "DELETE FROM candidate_data WHERE project_id = ?",
        (project_id,),
    )
    timestamp = now_iso()
    for index, candidate in enumerate(candidates, start=1):
        connection.execute(
            """
            INSERT INTO candidate_data
            (project_id, candidate_number, payload, version, updated_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (project_id, index, _json_dumps(candidate), timestamp),
        )


def _candidate_rows(
    connection: _Connection,
    project_id: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT payload FROM candidate_data
        WHERE project_id = ?
        ORDER BY candidate_number
        """,
        (project_id,),
    ).fetchall()
    candidates = [_json_loads(row["payload"], {}) for row in rows]
    if not all(isinstance(candidate, dict) for candidate in candidates):
        raise StorageError("候補データの形式が不正です。")
    return candidates


def _write_candidates_snapshot(
    connection: _Connection,
    project_id: str,
    *,
    expected_version: int | None = None,
) -> int:
    return _write_document(
        connection,
        project_id,
        "candidates",
        _candidate_rows(connection, project_id),
        expected_version=expected_version,
    )


def _ensure_candidate_rows(connection: _Connection, project_id: str) -> None:
    row_count = connection.execute(
        "SELECT COUNT(*) FROM candidate_data WHERE project_id = ?",
        (project_id,),
    ).fetchone()[0]
    if row_count:
        return
    raw, _ = _document(connection, project_id, "candidates", [])
    if not isinstance(raw, list):
        return
    candidates = [item for item in raw if isinstance(item, dict)]
    _replace_candidate_rows(connection, project_id, candidates)


def _confirmed_candidate_row(
    connection: _Connection,
    project_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT payload FROM confirmed_candidate_data
        WHERE project_id = ?
        """,
        (project_id,),
    ).fetchone()
    if not row:
        return None
    raw = _json_loads(row["payload"], None)
    return raw if isinstance(raw, dict) else None


def _effective_confirmed_candidate_row(
    connection: _Connection,
    project_id: str,
) -> dict[str, Any] | None:
    return _load_effective_schedule(
        connection,
        project_id,
        _confirmed_candidate_row(connection, project_id),
    )


def _write_confirmed_candidate_row(
    connection: _Connection,
    project_id: str,
    confirmed: dict[str, Any] | None,
    *,
    updated_at: str | None = None,
) -> None:
    if confirmed is None:
        connection.execute(
            "DELETE FROM confirmed_candidate_data WHERE project_id = ?",
            (project_id,),
        )
        return
    timestamp = updated_at or now_iso()
    connection.execute(
        """
        INSERT INTO confirmed_candidate_data(project_id, payload, version, updated_at)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(project_id) DO UPDATE SET
            payload = excluded.payload,
            version = confirmed_candidate_data.version + 1,
            updated_at = excluded.updated_at
        """,
        (project_id, _json_dumps(confirmed), timestamp),
    )


def _ensure_confirmed_candidate_row(
    connection: _Connection,
    project_id: str,
) -> None:
    row = connection.execute(
        "SELECT 1 FROM confirmed_candidate_data WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row:
        return
    raw, _ = _document(connection, project_id, "confirmed_candidate", None)
    if isinstance(raw, dict):
        _write_confirmed_candidate_row(connection, project_id, raw)


def _migrate_candidate_rows(connection: _Connection) -> None:
    migrated = connection.execute(
        "SELECT value FROM metadata WHERE key = 'candidate_rows_migrated'"
    ).fetchone()
    if migrated:
        return
    project_rows = connection.execute("SELECT id FROM projects").fetchall()
    for project_row in project_rows:
        project_id = project_row["id"]
        _ensure_candidate_rows(connection, project_id)
        _ensure_confirmed_candidate_row(connection, project_id)
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        ("candidate_rows_migrated", now_iso()),
    )


def _migrate_password_plain_to_secret(connection: _Connection) -> None:
    if not password_secret_key_configured():
        return
    rows = connection.execute(
        """
        SELECT id, password_plain FROM users
        WHERE password_plain IS NOT NULL
          AND password_plain <> ''
          AND password_secret IS NULL
        """
    ).fetchall()
    for row in rows:
        try:
            password_secret = encrypt_password_secret(str(row["password_plain"]))
        except PasswordSecretError as error:
            raise StorageError(str(error)) from error
        connection.execute(
            """
            UPDATE users
            SET password_secret = ?, password_plain = NULL
            WHERE id = ?
            """,
            (password_secret, row["id"]),
        )


def _ensure_default_settings(connection: _Connection) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO system_settings(key, payload, version, updated_at)
        VALUES ('global', ?, 1, ?)
        """,
        (
            _json_dumps(
                {
                    "active_cohorts": [14, 15, 16, 17],
                    "privacy_notice": "",
                    "data_retention_days": DEFAULT_DELETED_RETENTION_DAYS,
                }
            ),
            now_iso(),
        ),
    )


def _mark_initialized(connection: _Connection) -> None:
    initialized = connection.execute(
        "SELECT value FROM metadata WHERE key = 'turso_initialized'"
    ).fetchone()
    if initialized:
        return
    _ensure_default_settings(connection)
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES ('turso_initialized', ?)",
        (now_iso(),),
    )


def _cleanup_retention_in_transaction(connection: _Connection) -> None:
    settings_row = connection.execute(
        "SELECT payload FROM system_settings WHERE key = 'global'"
    ).fetchone()
    stored_settings = (
        _json_loads(settings_row["payload"], {}) if settings_row else {}
    )
    deleted_days = configured_deleted_retention_days(
        stored_settings.get("data_retention_days", DEFAULT_DELETED_RETENTION_DAYS)
    )
    audit_days = configured_audit_retention_days()
    deleted_before = (
        datetime.now(timezone.utc) - timedelta(days=deleted_days)
    ).isoformat()
    audit_before = (
        datetime.now(timezone.utc) - timedelta(days=audit_days)
    ).isoformat()
    connection.execute(
        "DELETE FROM projects WHERE deleted_at IS NOT NULL AND deleted_at < ?",
        (deleted_before,),
    )
    connection.execute(
        "DELETE FROM audit_logs WHERE created_at < ?", (audit_before,)
    )


def initialize_storage(*, force: bool = False) -> None:
    """Initialize one database once per process, plus one cleanup check per day."""

    storage_key = hashlib.sha256(_database_url().encode()).hexdigest()
    today = datetime.now(timezone.utc).date().isoformat()
    with _initialization_lock:
        needs_initialization = force or storage_key not in _initialized_storage_keys
        needs_cleanup_check = (
            force or _cleanup_checked_dates.get(storage_key) != today
        )
        if not needs_initialization and not needs_cleanup_check:
            return

        with _connection(write=True) as connection:
            if needs_initialization:
                _create_schema(connection)
                for statement in (
                    "ALTER TABLE users ADD COLUMN is_schedule_manager INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE users ADD COLUMN is_participant INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE users ADD COLUMN password_plain TEXT",
                    "ALTER TABLE users ADD COLUMN password_secret TEXT",
                    "ALTER TABLE memberships ADD COLUMN account_source TEXT",
                    "ALTER TABLE users ADD COLUMN password_source TEXT",
                    "ALTER TABLE users ADD COLUMN password_updated_at TEXT",
                ):
                    try:
                        connection.execute(statement)
                    except Exception as error:
                        if "duplicate column" not in str(error).lower():
                            raise
                _mark_initialized(connection)
                _migrate_participant_rows(connection)
                migrate_v101_support_role_unspecified(
                    connection,
                    json_loads=_json_loads,
                    json_dumps=_json_dumps,
                    write_participants_snapshot=_write_participants_snapshot,
                    audit=_audit,
                    now_iso=now_iso,
                    legacy_support_group=LEGACY_SUPPORT_GROUP,
                )
                _migrate_candidate_rows(connection)
                _migrate_password_plain_to_secret(connection)
            if needs_cleanup_check:
                last_cleanup = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'last_cleanup'"
                ).fetchone()
                if not last_cleanup or last_cleanup["value"] != today:
                    connection.execute(
                        "INSERT OR REPLACE INTO metadata(key, value) "
                        "VALUES ('last_cleanup', ?)",
                        (today,),
                    )
                    _cleanup_retention_in_transaction(connection)

        _initialized_storage_keys.add(storage_key)
        _cleanup_checked_dates[storage_key] = today


def _validate_project_id(project_id: str) -> str:
    normalized = str(project_id).strip().lower()
    if not PROJECT_ID_PATTERN.fullmatch(normalized):
        raise InvalidProjectIdError("企画IDの形式が不正です。")
    return normalized


def set_audit_actor(actor: str) -> None:
    _audit_actor.set(actor.strip() or "anonymous")


def _audit(
    connection: _Connection,
    action: str,
    *,
    project_id: str | None = None,
    target: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_logs(actor, action, project_id, target, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            _audit_actor.get(),
            action,
            project_id,
            target,
            _json_dumps(detail or {}),
            now_iso(),
        ),
    )


def _audit_many(
    connection: _Connection,
    events: list[tuple[str, str | None, str, dict[str, Any]]],
) -> None:
    timestamp = now_iso()
    _execute_multirow_insert(
        connection,
        """
        INSERT INTO audit_logs
        (actor, action, project_id, target, detail, created_at)
        """,
        [
            (
                _audit_actor.get(),
                action,
                project_id,
                target,
                _json_dumps(detail),
                timestamp,
            )
            for action, project_id, target, detail in events
        ],
    )


def _document(
    connection: _Connection, project_id: str, kind: str, default: Any
) -> tuple[Any, int]:
    row = connection.execute(
        "SELECT payload, version FROM documents WHERE project_id = ? AND kind = ?",
        (_validate_project_id(project_id), kind),
    ).fetchone()
    return (_json_loads(row["payload"], default), int(row["version"])) if row else (
        default,
        0,
    )


def _write_document(
    connection: _Connection,
    project_id: str,
    kind: str,
    value: Any,
    *,
    expected_version: int | None = None,
    updated_at: str | None = None,
    current_document: tuple[Any, int] | None = None,
) -> int:
    project_id = _validate_project_id(project_id)
    if kind not in DOCUMENT_KINDS:
        raise StorageError("保存データ種別が不正です。")
    if current_document is None:
        current = connection.execute(
            "SELECT payload, version FROM documents WHERE project_id = ? AND kind = ?",
            (project_id, kind),
        ).fetchone()
        current_payload = current["payload"] if current else None
        current_version = int(current["version"]) if current else 0
    else:
        current_payload, current_version = current_document
        current = {"payload": current_payload, "version": current_version}
    if expected_version is not None and expected_version != current_version:
        raise StorageConflictError(
            "別の利用者が先に更新しました。画面を再読み込みしてください。"
        )
    serialized_value = _json_dumps(value)
    if current and current_payload == serialized_value:
        return current_version
    if current:
        connection.execute(
            """
            INSERT INTO backups(project_id, kind, payload, version, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, kind, current_payload, current_version, now_iso()),
        )
    new_version = current_version + 1
    timestamp = updated_at or now_iso()
    connection.execute(
        """
        INSERT INTO documents(project_id, kind, payload, version, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(project_id, kind) DO UPDATE SET
            payload = excluded.payload,
            version = excluded.version,
            updated_at = excluded.updated_at
        """,
        (project_id, kind, serialized_value, new_version, timestamp),
    )
    _prune_backups(connection, project_id, kind)
    return new_version


def _prune_backups(
    connection: _Connection, project_id: str, kind: str
) -> None:
    limit = configured_backup_limit()
    connection.execute(
        """
        DELETE FROM backups
        WHERE id IN (
            SELECT id FROM backups
            WHERE project_id = ? AND kind = ?
            ORDER BY id DESC LIMIT -1 OFFSET ?
        )
        """,
        (project_id, kind, limit),
    )


def document_version(project_id: str, kind: str) -> int:
    initialize_storage()
    with _connection() as connection:
        return _document(connection, project_id, kind, None)[1]


def load_system_settings() -> dict[str, Any]:
    initialize_storage()
    with _connection() as connection:
        row = connection.execute(
            "SELECT payload FROM system_settings WHERE key = 'global'"
        ).fetchone()
    raw = _json_loads(row["payload"], {}) if row else {}
    return _normalize_system_settings(raw)


def save_system_settings(settings: dict[str, Any]) -> None:
    payload = {
        "active_cohorts": sorted(
            {
                int(value)
                for value in settings.get("active_cohorts", [])
                if str(value).isdigit() and int(value) >= 1
            }
        )
        or [14, 15, 16, 17],
        "privacy_notice": str(settings.get("privacy_notice", ""))[:4000],
        "data_retention_days": bounded_int(
            settings.get("data_retention_days", DEFAULT_DELETED_RETENTION_DAYS),
            DEFAULT_DELETED_RETENTION_DAYS,
            1,
            MAX_RETENTION_DAYS,
        ),
        "maintenance_mode": bool(settings.get("maintenance_mode", False)),
    }
    initialize_storage()
    with _connection(write=True) as connection:
        connection.execute(
            """
            INSERT INTO system_settings(key, payload, version, updated_at)
            VALUES ('global', ?, 1, ?)
            ON CONFLICT(key) DO UPDATE SET
                payload = excluded.payload,
                version = system_settings.version + 1,
                updated_at = excluded.updated_at
            """,
            (_json_dumps(payload), now_iso()),
        )
        _audit(connection, "system_settings.updated")


def ensure_projects() -> list[dict[str, Any]]:
    initialize_storage()
    projects = list_projects()
    if projects:
        return projects
    with _connection(write=True) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM projects WHERE deleted_at IS NULL"
        ).fetchone()[0]
        if not count:
            _create_project_in_transaction(connection, "新しい日程調整")
    return list_projects()


def list_projects(*, include_deleted: bool = False) -> list[dict[str, Any]]:
    initialize_storage()
    where = "" if include_deleted else "WHERE deleted_at IS NULL"
    with _connection() as connection:
        rows = connection.execute(
            f"""
            SELECT id, title, status, created_at, updated_at, archived,
                   sort_order, deleted_at
            FROM projects {where}
            ORDER BY archived, sort_order, title
            """
        ).fetchall()
    return [
        {
            **dict(row),
            "archived": bool(row["archived"]),
        }
        for row in rows
    ]


def update_project_organization(rows: list[dict[str, Any]]) -> None:
    initialize_storage()
    with _connection(write=True) as connection:
        for row in rows:
            project_id = _validate_project_id(str(row.get("id") or row.get("ID")))
            connection.execute(
                """
                UPDATE projects SET archived = ?, sort_order = ?, updated_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (
                    int(bool(row.get("archived", row.get("アーカイブ", False)))),
                    max(0, int(row.get("sort_order", row.get("表示順", 0)))),
                    now_iso(),
                    project_id,
                ),
            )
        _audit(connection, "projects.organized")


def _create_project_in_transaction(
    connection: _Connection,
    title: str,
    *,
    config: Config | None = None,
    participants: list[Participant] | None = None,
) -> str:
    project_id = uuid4().hex
    config = config or Config()
    config.schema_version = 10
    config.project_id = project_id
    normalized_title = title.strip()[:120]
    if not normalized_title:
        raise StorageError("企画名を入力してください。")
    config.title = normalized_title
    config.status = "draft"
    sort_order = connection.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM projects WHERE deleted_at IS NULL"
    ).fetchone()[0]
    timestamp = now_iso()
    connection.execute(
        """
        INSERT INTO projects
        (id, title, status, created_at, updated_at, archived, sort_order)
        VALUES (?, ?, ?, ?, ?, 0, ?)
        """,
        (project_id, config.title, config.status, timestamp, timestamp, sort_order),
    )
    _replace_participant_rows(connection, project_id, participants or [])
    values = {
        "config": config.to_dict(),
        "participants": [
            participant.to_dict() for participant in (participants or [])
        ],
        "candidates": [],
        "confirmed_candidate": None,
        "history": [],
    }
    for kind, value in values.items():
        _insert_document(connection, project_id, kind, value)
    _replace_candidate_rows(connection, project_id, [])
    return project_id


def create_project(
    title: str,
    *,
    config: Config | None = None,
    copy_from_project_id: str | None = None,
    copy_participants: bool = False,
) -> str:
    initialize_storage()
    copied_participants: list[Participant] = []
    if config is None and copy_from_project_id:
        config = Config.from_dict(load_config(copy_from_project_id).to_dict())
    if copy_from_project_id and copy_participants:
        for source in load_participants(copy_from_project_id):
            participant = Participant.from_dict(source.to_dict())
            participant.id = uuid4().hex
            participant.user_id = ""
            participant.availability = []
            participant.zoom_availability = []
            participant.input_status = "not_started"
            participant.submitted_at = ""
            participant.updated_at = ""
            copied_participants.append(participant)
    with _connection(write=True) as connection:
        project_id = _create_project_in_transaction(
            connection,
            title,
            config=config,
            participants=copied_participants,
        )
        _audit(connection, "project.created", project_id=project_id)
    return project_id


def update_project_index(config: Config) -> None:
    normalized_title = config.title.strip()[:120]
    if not normalized_title:
        raise StorageError("企画名を入力してください。")
    with _connection(write=True) as connection:
        connection.execute(
            """
            UPDATE projects SET title = ?, status = ?, updated_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            (normalized_title, config.status, now_iso(), config.project_id),
        )


def delete_project(project_id: str) -> None:
    project_id = _validate_project_id(project_id)
    initialize_storage()
    with _connection(write=True) as connection:
        connection.execute(
            "UPDATE projects SET deleted_at = ?, updated_at = ? WHERE id = ?",
            (now_iso(), now_iso(), project_id),
        )
        _audit(connection, "project.deleted", project_id=project_id)
        remaining = connection.execute(
            "SELECT COUNT(*) FROM projects WHERE deleted_at IS NULL"
        ).fetchone()[0]
        if not remaining:
            _create_project_in_transaction(connection, "新しい日程調整")


def restore_deleted_project(project_id: str) -> None:
    project_id = _validate_project_id(project_id)
    initialize_storage()
    with _connection(write=True) as connection:
        connection.execute(
            "UPDATE projects SET deleted_at = NULL, updated_at = ? WHERE id = ?",
            (now_iso(), project_id),
        )
        _audit(connection, "project.restored", project_id=project_id)


def load_config(project_id: str) -> Config:
    initialize_storage()
    with ExitStack() as stack:
        stack.enter_context(
            measure_storage_operation(
                "project_config_load",
                logger=LOGGER,
                project_count=1,
            )
        )
        connection = stack.enter_context(_connection())
        raw, version = _document(connection, project_id, "config", {})
    config = Config.from_dict(raw if isinstance(raw, dict) else {})
    config.schema_version = 10
    config.project_id = _validate_project_id(project_id)
    config._storage_version = version
    return config


def load_manager_project_overview(project_id: str) -> dict[str, Any]:
    """Load manager-home aggregates without transferring project JSON rows."""

    initialize_storage()
    project_id = _validate_project_id(project_id)
    with ExitStack() as stack:
        stack.enter_context(
            measure_storage_operation(
                "manager_project_overview",
                logger=LOGGER,
                project_count=1,
            )
        )
        connection = stack.enter_context(_connection())
        row = connection.execute(
            """
            WITH participant_summary AS (
                SELECT project_id,
                       COUNT(*) AS participant_count,
                       SUM(CASE WHEN
                           COALESCE(json_extract(roster_payload, '$.active'), 0) = 1
                           AND COALESCE(json_extract(roster_payload, '$.approved'), 0) = 1
                           THEN 1 ELSE 0 END) AS target_count,
                       SUM(CASE WHEN
                           COALESCE(json_extract(roster_payload, '$.active'), 0) = 1
                           AND COALESCE(json_extract(roster_payload, '$.approved'), 0) = 1
                           AND json_extract(response_payload, '$.input_status') = 'submitted'
                           THEN 1 ELSE 0 END) AS submitted_count,
                       SUM(CASE WHEN
                           COALESCE(json_extract(roster_payload, '$.active'), 0) = 1
                           AND COALESCE(json_extract(roster_payload, '$.approved'), 0) = 0
                           THEN 1 ELSE 0 END) AS pending_approval_count,
                       SUM(CASE WHEN
                           COALESCE(json_extract(roster_payload, '$.user_id'), '') <> ''
                           THEN 1 ELSE 0 END) AS account_count,
                       SUM(CASE WHEN
                           COALESCE(json_extract(roster_payload, '$.active'), 0) = 1
                           AND COALESCE(json_extract(roster_payload, '$.approved'), 0) = 1
                           AND json_extract(response_payload, '$.input_status')
                               IN ('draft', 'submitted')
                           THEN 1 ELSE 0 END) AS answered_count,
                       SUM(CASE WHEN
                           COALESCE(json_extract(roster_payload, '$.active'), 0) = 1
                           AND COALESCE(json_extract(roster_payload, '$.approved'), 0) = 1
                           AND json_extract(response_payload, '$.response_source') = 'manager'
                           THEN 1 ELSE 0 END) AS manager_response_count,
                       SUM(CASE WHEN
                           COALESCE(json_extract(roster_payload, '$.active'), 0) = 1
                           AND COALESCE(json_extract(roster_payload, '$.approved'), 0) = 1
                           AND COALESCE(json_extract(requirements_payload,
                               '$.practice_role_unspecified'), 0) = 1
                           THEN 1 ELSE 0 END) AS role_unspecified_count,
                       SUM(CASE WHEN
                           COALESCE(json_extract(roster_payload, '$.active'), 0) = 1
                           AND COALESCE(json_extract(roster_payload, '$.approved'), 0) = 1
                           AND (
                               COALESCE(json_extract(requirements_payload,
                                   '$.practice_role_unspecified'), 0) = 1
                               OR json_extract(requirements_payload,
                                   '$.required_university_count') IS NOT NULL
                               OR json_extract(requirements_payload,
                                   '$.required_high_school_count') IS NOT NULL
                               OR json_extract(requirements_payload,
                                   '$.total_extra_limit') IS NOT NULL
                               OR json_extract(requirements_payload,
                                   '$.practice_participation_count') IS NOT NULL
                           )
                           THEN 1 ELSE 0 END) AS individual_condition_count
                FROM project_participations
                WHERE project_id = ?
                GROUP BY project_id
            ), candidate_summary AS (
                SELECT project_id,
                       COUNT(*) AS candidate_count,
                       SUM(CASE WHEN
                           COALESCE(json_extract(payload,
                               '$.metrics.is_strict_candidate'), 1) = 0
                           THEN 1 ELSE 0 END) AS candidate_warning_count
                FROM candidate_data
                WHERE project_id = ?
                GROUP BY project_id
            )
            SELECT p.id, p.title, p.status,
                   config_doc.payload AS config_payload,
                   config_doc.version AS config_version,
                   COALESCE(ps.participant_count, 0) AS participant_count,
                   COALESCE(ps.target_count, 0) AS target_count,
                   COALESCE(ps.submitted_count, 0) AS submitted_count,
                   COALESCE(ps.pending_approval_count, 0) AS pending_approval_count,
                   COALESCE(ps.account_count, 0) AS account_count,
                   COALESCE(ps.answered_count, 0) AS answered_count,
                   COALESCE(ps.manager_response_count, 0) AS manager_response_count,
                   COALESCE(ps.role_unspecified_count, 0) AS role_unspecified_count,
                   COALESCE(ps.individual_condition_count, 0)
                       AS individual_condition_count,
                   COALESCE(cs.candidate_count, 0) AS candidate_count,
                   COALESCE(cs.candidate_warning_count, 0)
                       AS candidate_warning_count,
                   COALESCE(candidate_doc.version, 0) AS candidates_version,
                   CASE WHEN active.project_id IS NOT NULL
                              OR (confirmed.project_id IS NOT NULL
                                  AND confirmed.payload IS NOT NULL
                                  AND confirmed.payload <> 'null')
                        THEN 1 ELSE 0 END AS confirmed,
                   COALESCE(
                       json_extract(confirmed.payload, '$.candidate_number'),
                       revision.candidate_number,
                       '-'
                   ) AS confirmed_candidate_number
            FROM projects p
            LEFT JOIN documents config_doc
              ON config_doc.project_id = p.id AND config_doc.kind = 'config'
            LEFT JOIN documents candidate_doc
              ON candidate_doc.project_id = p.id
             AND candidate_doc.kind = 'candidates'
            LEFT JOIN confirmed_candidate_data confirmed
              ON confirmed.project_id = p.id
            LEFT JOIN active_schedule_revisions active
              ON active.project_id = p.id
            LEFT JOIN schedule_revisions revision
              ON revision.id = active.revision_id
            LEFT JOIN participant_summary ps ON ps.project_id = p.id
            LEFT JOIN candidate_summary cs ON cs.project_id = p.id
            WHERE p.id = ?
            """,
            (project_id, project_id, project_id),
        ).fetchone()
    if row is None:
        raise StorageError("指定した企画が見つかりません。")
    raw_config = _json_loads(row["config_payload"], {})
    config = Config.from_dict(raw_config if isinstance(raw_config, dict) else {})
    config.schema_version = 10
    config.project_id = project_id
    config._storage_version = int(row["config_version"] or 0)
    return {
        "config": config,
        "participant_count": int(row["participant_count"] or 0),
        "target_count": int(row["target_count"] or 0),
        "submitted_count": int(row["submitted_count"] or 0),
        "pending_approval_count": int(row["pending_approval_count"] or 0),
        "account_count": int(row["account_count"] or 0),
        "answered_count": int(row["answered_count"] or 0),
        "manager_response_count": int(row["manager_response_count"] or 0),
        "role_unspecified_count": int(row["role_unspecified_count"] or 0),
        "individual_condition_count": int(
            row["individual_condition_count"] or 0
        ),
        "candidate_count": int(row["candidate_count"] or 0),
        "candidate_warning_count": int(row["candidate_warning_count"] or 0),
        "candidate_version": int(row["candidates_version"] or 0),
        "confirmed": bool(row["confirmed"]),
        "confirmed_candidate_number": row["confirmed_candidate_number"] or "-",
        "config_issue_count": len(config.validate()),
    }


def save_config(config: Config, *, expected_version: int | None = None) -> int:
    initialize_storage()
    config.schema_version = 10
    config.project_id = _validate_project_id(config.project_id)
    errors = config.validate()
    if errors:
        raise StorageError(" / ".join(errors))
    with _connection(write=True) as connection:
        version = _write_document(
            connection,
            config.project_id,
            "config",
            config.to_dict(),
            expected_version=expected_version,
        )
        connection.execute(
            """
            UPDATE projects SET title = ?, status = ?, updated_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            (config.title[:120], config.status, now_iso(), config.project_id),
        )
        _audit(connection, "config.updated", project_id=config.project_id)
    config._storage_version = version
    return version


def _save_config_fields_in_transaction(
    connection: _Connection,
    project_id: str,
    updates: dict[str, Any],
    *,
    expected_version: int | None = None,
) -> int:
    project_id = _validate_project_id(project_id)
    invalid_fields = set(updates) - CONFIG_FIELD_NAMES
    if invalid_fields:
        raise StorageError("企画情報の更新項目が不正です。")
    raw, current_version = _document(connection, project_id, "config", {})
    current = Config.from_dict(raw if isinstance(raw, dict) else {})
    merged = current.to_dict()
    merged.update(updates)
    merged["schema_version"] = 10
    merged["project_id"] = project_id
    updated = Config.from_dict(merged)
    errors = updated.validate()
    if errors:
        raise StorageError(" / ".join(errors))
    version = _write_document(
        connection,
        project_id,
        "config",
        updated.to_dict(),
        expected_version=(
            current_version if expected_version is None else expected_version
        ),
    )
    connection.execute(
        """
        UPDATE projects SET title = ?, status = ?, updated_at = ?
        WHERE id = ? AND deleted_at IS NULL
        """,
        (updated.title[:120], updated.status, now_iso(), project_id),
    )
    _audit(
        connection,
        "config.fields.updated",
        project_id=project_id,
        detail={"fields": sorted(updates)},
    )
    return version


def save_config_fields(
    project_id: str,
    updates: dict[str, Any],
    *,
    expected_version: int | None = None,
) -> int:
    initialize_storage()
    with _connection(write=True) as connection:
        return _save_config_fields_in_transaction(
            connection,
            project_id,
            updates,
            expected_version=expected_version,
        )


def load_participants(project_id: str) -> list[Participant]:
    initialize_storage()
    with _connection() as connection:
        return _participant_rows(connection, _validate_project_id(project_id))


def save_participants(
    project_id: str,
    participants: list[Participant],
    *,
    expected_version: int | None = None,
) -> int:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    normalized_names = [
        participant_name_identity_key(participant.name) for participant in participants
    ]
    if any(not name for name in normalized_names):
        raise StorageError("参加者名を空欄にはできません。")
    if len(normalized_names) != len(set(normalized_names)):
        raise StorageError("参加者名が重複しています。")
    with _connection(write=True) as connection:
        current_versions = {
            str(row["participant_id"]): int(row["version"])
            for row in connection.execute(
                "SELECT participant_id, version FROM project_participations "
                "WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        }
        incoming_ids = {participant.id for participant in participants}
        if set(current_versions) - incoming_ids or any(
            participant.storage_version
            and (
                participant.storage_project_id == project_id
                or participant.id in current_versions
            )
            and current_versions.get(participant.id)
            != participant.storage_version
            for participant in participants
        ):
            raise StorageConflictError(
                "別の利用者が参加者情報を先に更新しました。"
                "画面を再読み込みしてください。"
            )
        _raise_common_participant_name_conflict(
            _common_participant_name_conflicts(connection, participants)
        )
        _replace_participant_rows(connection, project_id, participants)
        version = _write_document(
            connection,
            project_id,
            "participants",
            [participant.to_dict() for participant in participants],
            expected_version=expected_version,
        )
        _audit(connection, "participants.updated", project_id=project_id)
    return version


def add_participants(
    project_id: str,
    participants: list[Participant],
) -> list[Participant]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    if not participants:
        return []
    normalized_names = [
        participant_name_identity_key(participant.name)
        for participant in participants
    ]
    if any(not name for name in normalized_names):
        raise StorageError("参加者名を空欄にはできません。")
    if len(normalized_names) != len(set(normalized_names)):
        raise StorageError("参加者名が重複しています。")
    participant_ids = [str(participant.id) for participant in participants]
    if len(participant_ids) != len(set(participant_ids)):
        raise StorageError("参加者IDが重複しています。")
    with _connection(write=True) as connection:
        current_participants = _participant_rows(connection, project_id)
        current_ids = {participant.id for participant in current_participants}
        if current_ids.intersection(participant_ids):
            raise StorageConflictError(
                "追加対象の参加者がすでに企画へ登録されています。"
                "画面を再読み込みしてください。"
            )
        current_names = {
            participant_name_identity_key(participant.name)
            for participant in current_participants
        }
        if current_names.intersection(normalized_names):
            raise StorageError("同じ名前の参加者がすでに登録されています。")
        _raise_common_participant_name_conflict(
            _common_participant_name_conflicts(connection, participants)
        )
        sort_order = int(
            connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 "
                "FROM project_participations WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
        )
        timestamp = now_iso()
        participation_rows = []
        for offset, participant in enumerate(participants):
            payloads = _participant_payloads(participant)
            participation_rows.append(
                (
                    project_id,
                    participant.id,
                    sort_order + offset,
                    _json_dumps(payloads["roster"]),
                    _json_dumps(payloads["attributes"]),
                    _json_dumps(payloads["requirements"]),
                    _json_dumps(payloads["response"]),
                    1,
                    timestamp,
                )
            )
            participant.storage_version = 1
            participant.storage_project_id = project_id
        _upsert_common_participants(
            connection,
            participants,
            timestamp=timestamp,
        )
        _upsert_project_participant_rows(connection, participation_rows)
        _write_participants_snapshot(connection, project_id)
        _audit_many(
            connection,
            [
                (
                    "participant.created",
                    project_id,
                    participant.id,
                    {"registered_by": participant.registered_by},
                )
                for participant in participants
            ],
        )
    return participants


def _save_participant_responses_for_project_in_transaction(
    connection: _Connection,
    project_id: str,
    participants: list[Participant],
    *,
    current_rows: dict[str, Any] | None = None,
    timestamp: str | None = None,
    skip_common_upsert: bool = False,
    defer_snapshot: bool = False,
    defer_audit: bool = False,
) -> list[int]:
    """CAS-update one project's responses and rebuild its snapshot once."""

    project_id = _validate_project_id(project_id)
    if not participants:
        return []
    participant_ids = [participant.id for participant in participants]
    if len(participant_ids) != len(set(participant_ids)):
        raise StorageError("同じ企画・参加者への回答を重複して保存できません。")
    if current_rows is None:
        placeholders = ",".join("?" for _ in participant_ids)
        rows = connection.execute(
            f"""
            SELECT participant_id, sort_order, roster_payload,
                   requirements_payload, response_payload, version
            FROM project_participations
            WHERE project_id = ? AND participant_id IN ({placeholders})
            """,
            (project_id, *participant_ids),
        ).fetchall()
        row_by_id = {str(row["participant_id"]): row for row in rows}
    else:
        row_by_id = current_rows
    timestamp = timestamp or now_iso()
    update_rows: list[tuple[Any, ...]] = []
    audit_events: list[tuple[str, str | None, str, dict[str, Any]]] = []
    prepared: list[
        tuple[
            Participant,
            int,
            dict[str, Any],
            dict[str, Any],
            dict[str, Any],
            str,
        ]
    ] = []
    for participant in participants:
        row = row_by_id.get(participant.id)
        if row is None:
            raise StorageError("指定した参加者が見つかりません。")
        current_version = int(row["version"])
        effective_version = participant.storage_version or current_version
        if effective_version != current_version:
            raise StorageConflictError(
                "別の画面から同じ参加者の回答が先に更新されました。"
                "画面を再読み込みしてください。"
            )
        payloads = _participant_payloads(participant)
        participant_response = _response_snapshot(participant)
        (
            _stored_participant_response,
            manager_response,
            _stored_effective_response,
            _stored_response_source,
        ) = _split_response_payload(_json_loads(row["response_payload"], {}))
        response_payload = _combined_response_payload(
            participant_response,
            manager_response,
        )
        (
            _participant_response,
            _manager_response,
            effective_response,
            response_source,
        ) = _split_response_payload(response_payload)
        prepared.append(
            (
                participant,
                current_version,
                participant_response,
                manager_response,
                effective_response,
                response_source,
            )
        )
        update_rows.append(
            (
                project_id,
                participant.id,
                int(row["sort_order"]),
                str(row["roster_payload"]),
                _json_dumps(payloads["attributes"]),
                str(row["requirements_payload"]),
                _json_dumps(response_payload),
                current_version + 1,
                timestamp,
            )
        )
        audit_events.append(
            (
                "participant.response.updated",
                project_id,
                participant.id,
                {"effective_source": response_source},
            )
        )

    database_started = time.perf_counter()
    if not skip_common_upsert:
        _upsert_common_participants(
            connection, participants, timestamp=timestamp
        )
    affected = _execute_multirow_insert(
        connection,
        """
        INSERT INTO project_participations
        (project_id, participant_id, sort_order, roster_payload,
         attributes_payload, requirements_payload, response_payload,
         version, updated_at)
        """,
        update_rows,
        suffix="""
        ON CONFLICT(project_id, participant_id) DO UPDATE SET
            attributes_payload = excluded.attributes_payload,
            response_payload = excluded.response_payload,
            version = project_participations.version + 1,
            updated_at = excluded.updated_at
        WHERE project_participations.version = excluded.version - 1
        """,
    )
    if affected != len(update_rows):
        raise StorageConflictError(
            "別の画面から同じ参加者の回答が先に更新されました。"
            "画面を再読み込みしてください。"
        )
    for (
        participant,
        current_version,
        participant_response,
        manager_response,
        effective_response,
        response_source,
    ) in prepared:
        participant.storage_version = current_version + 1
        participant.participant_response = participant_response
        participant.manager_response = manager_response
        participant.response_source = response_source
        _apply_response_snapshot(participant, effective_response)
    database_elapsed = time.perf_counter() - database_started
    snapshot_started = time.perf_counter()
    version = 0
    if not defer_snapshot:
        version = _write_participants_snapshot(
            connection, project_id, participants
        )
    snapshot_elapsed = time.perf_counter() - snapshot_started
    audit_started = time.perf_counter()
    if not defer_audit:
        _audit_many(connection, audit_events)
    audit_elapsed = time.perf_counter() - audit_started
    if not defer_snapshot and not defer_audit:
        LOGGER.info(
            "participant_response_project_timing project_id=%s response_count=%d "
            "db_update_seconds=%.4f snapshot_seconds=%.4f audit_seconds=%.4f",
            project_id,
            len(participants),
            database_elapsed,
            snapshot_elapsed,
            audit_elapsed,
        )
    return [version] * len(participants)


def _save_participant_response_in_transaction(
    connection: _Connection,
    project_id: str,
    participant: Participant,
    *,
    expected_version: int | None = None,
) -> int:
    project_id = _validate_project_id(project_id)
    row = connection.execute(
        "SELECT version, response_payload FROM project_participations "
        "WHERE project_id = ? AND participant_id = ?",
        (project_id, participant.id),
    ).fetchone()
    if row is None:
        raise StorageError("指定した参加者が見つかりません。")
    current_version = int(row["version"])
    effective_version = (
        expected_version
        if expected_version is not None
        else participant.storage_version or current_version
    )
    if effective_version != current_version:
        raise StorageConflictError(
            "別の画面から同じ参加者の回答が先に更新されました。"
            "画面を再読み込みしてください。"
        )
    _upsert_common_participant(connection, participant)
    payloads = _participant_payloads(participant)
    participant_response = _response_snapshot(participant)
    (
        _stored_participant_response,
        manager_response,
        _stored_effective_response,
        _stored_response_source,
    ) = _split_response_payload(_json_loads(row["response_payload"], {}))
    response_payload = _combined_response_payload(
        participant_response,
        manager_response,
    )
    (
        _participant_response,
        manager_response,
        effective_response,
        response_source,
    ) = _split_response_payload(response_payload)
    cursor = connection.execute(
        """
        UPDATE project_participations
        SET attributes_payload = ?,
            response_payload = ?,
            version = version + 1,
            updated_at = ?
        WHERE project_id = ? AND participant_id = ? AND version = ?
        """,
        (
            _json_dumps(payloads["attributes"]),
            _json_dumps(response_payload),
            now_iso(),
            project_id,
            participant.id,
            effective_version,
        ),
    )
    if getattr(cursor, "rowcount", 1) == 0:
        raise StorageConflictError(
            "別の画面から同じ参加者の回答が先に更新されました。"
            "画面を再読み込みしてください。"
        )
    participant.storage_version = current_version + 1
    participant.participant_response = participant_response
    participant.manager_response = manager_response
    participant.response_source = response_source
    _apply_response_snapshot(participant, effective_response)
    version = _write_participants_snapshot(
        connection, project_id, [participant]
    )
    _audit(
        connection,
        "participant.response.updated",
        project_id=project_id,
        target=participant.id,
        detail={"effective_source": response_source},
    )
    return version


def _validate_participant_response_config_guard(
    connection: _Connection,
    project_ids: list[str],
    expected_config_versions: dict[str, int] | None,
) -> None:
    """Check project reception state once before any response writes."""

    if not project_ids:
        return
    placeholders = ",".join("?" for _ in project_ids)
    rows = connection.execute(
        f"""
        SELECT project_id, payload, version
        FROM documents
        WHERE kind = 'config' AND project_id IN ({placeholders})
        """,
        tuple(project_ids),
    ).fetchall()
    configs: dict[str, tuple[Config, int]] = {}
    for row in rows:
        project_id = str(row["project_id"])
        raw_config = _json_loads(row["payload"], {})
        config = Config.from_dict(
            raw_config if isinstance(raw_config, dict) else {}
        )
        config.project_id = project_id
        configs[project_id] = (config, int(row["version"] or 0))

    for project_id in project_ids:
        current = configs.get(project_id)
        if current is None:
            raise StorageError("指定した企画が見つかりません。")
        config, current_version = current
        if expected_config_versions is not None:
            expected_version = expected_config_versions.get(project_id)
            if expected_version is None or expected_version != current_version:
                raise StorageConflictError(
                    "企画の受付設定が変更されました。"
                    "画面を再読み込みしてください。"
                )
        if not participant_response_editable(config):
            raise StorageConflictError(
                "企画の回答受付が終了しています。"
                "画面を再読み込みしてください。"
            )


def save_participant_response(
    project_id: str,
    participant: Participant,
    *,
    expected_version: int | None = None,
) -> int:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection(write=True) as connection:
        _validate_participant_response_config_guard(
            connection,
            [project_id],
            None,
        )
        return _save_participant_response_in_transaction(
            connection,
            project_id,
            participant,
            expected_version=expected_version,
        )


def save_participant_responses(
    responses: list[tuple[str, Participant]],
    *,
    expected_config_versions: dict[str, int] | None = None,
) -> list[int]:
    """Save responses for multiple projects atomically."""

    initialize_storage()
    normalized: list[tuple[str, Participant]] = [
        (_validate_project_id(project_id), participant)
        for project_id, participant in responses
    ]
    identities = [(project_id, participant.id) for project_id, participant in normalized]
    if len(identities) != len(set(identities)):
        raise StorageError("同じ企画・参加者への回答を重複して保存できません。")
    normalized_config_versions = (
        {
            _validate_project_id(project_id): int(version)
            for project_id, version in expected_config_versions.items()
        }
        if expected_config_versions is not None
        else None
    )
    grouped: dict[str, list[Participant]] = {}
    for project_id, participant in normalized:
        grouped.setdefault(project_id, []).append(participant)
    versions_by_identity: dict[tuple[str, str], int] = {}
    transaction_started = time.perf_counter()
    with measure_storage_operation(
        "participant_response_save",
        logger=LOGGER,
        response_count=len(normalized),
        project_count=len(grouped),
    ) as metrics:
        with _connection(write=True) as connection:
            config_guard_started = time.perf_counter()
            _validate_participant_response_config_guard(
                connection,
                list(grouped),
                normalized_config_versions,
            )
            metrics.set(
                config_guard_seconds=round(
                    time.perf_counter() - config_guard_started, 6
                ),
                config_guard_project_count=len(grouped),
            )
            project_placeholders = ",".join("?" for _ in grouped)
            participant_ids = list(
                dict.fromkeys(participant.id for _, participant in normalized)
            )
            participant_placeholders = ",".join("?" for _ in participant_ids)
            current_rows_by_project: dict[str, dict[str, Any]] = defaultdict(dict)
            if grouped and participant_ids:
                current_rows = connection.execute(
                    f"""
                    SELECT project_id, participant_id, sort_order,
                           roster_payload, requirements_payload,
                           response_payload, version
                    FROM project_participations
                    WHERE project_id IN ({project_placeholders})
                      AND participant_id IN ({participant_placeholders})
                    """,
                    (*grouped.keys(), *participant_ids),
                ).fetchall()
                for row in current_rows:
                    current_rows_by_project[str(row["project_id"])][
                        str(row["participant_id"])
                    ] = row

            common_by_id = {
                participant.id: participant
                for _, participant in normalized
            }
            _upsert_common_participants(
                connection,
                list(common_by_id.values()),
                timestamp=now_iso(),
            )
            save_timestamp = now_iso()
            changed_by_project: dict[str, list[Participant]] = {}
            audit_events: list[
                tuple[str, str | None, str, dict[str, Any]]
            ] = []
            for project_id, participants in grouped.items():
                _save_participant_responses_for_project_in_transaction(
                    connection,
                    project_id,
                    participants,
                    current_rows=current_rows_by_project.get(project_id, {}),
                    timestamp=save_timestamp,
                    skip_common_upsert=True,
                    defer_snapshot=True,
                    defer_audit=True,
                )
                changed_by_project[project_id] = participants
                audit_events.extend(
                    (
                        "participant.response.updated",
                        project_id,
                        participant.id,
                        {"effective_source": participant.response_source},
                    )
                    for participant in participants
                )
            snapshot_started = time.perf_counter()
            snapshot_versions = _write_participants_snapshots_batch(
                connection, changed_by_project
            )
            metrics.set(
                snapshot_batch_seconds=round(
                    time.perf_counter() - snapshot_started, 6
                )
            )
            audit_started = time.perf_counter()
            _audit_many(connection, audit_events)
            metrics.set(
                audit_batch_seconds=round(
                    time.perf_counter() - audit_started, 6
                )
            )
            for project_id, participants in grouped.items():
                version = snapshot_versions[project_id]
                versions_by_identity.update(
                    {
                        (project_id, participant.id): version
                        for participant in participants
                    }
                )
        metrics.set(
            response_count=len(normalized),
            project_count=len(grouped),
        )
    LOGGER.info(
        "participant_response_transaction_timing response_count=%d "
        "project_count=%d transaction_seconds=%.4f",
        len(normalized),
        len(grouped),
        time.perf_counter() - transaction_started,
    )
    return [
        versions_by_identity[(project_id, participant.id)]
        for project_id, participant in normalized
    ]


def save_manager_response_override(
    project_id: str,
    participant: Participant,
    *,
    expected_version: int | None = None,
) -> int:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection(write=True) as connection:
        row = connection.execute(
            "SELECT version, response_payload FROM project_participations "
            "WHERE project_id = ? AND participant_id = ?",
            (project_id, participant.id),
        ).fetchone()
        if row is None:
            raise StorageError("指定した参加者が見つかりません。")
        current_version = int(row["version"])
        effective_version = (
            expected_version
            if expected_version is not None
            else participant.storage_version or current_version
        )
        if effective_version != current_version:
            raise StorageConflictError(
                "別の画面から同じ参加者の回答が先に更新されました。"
                "画面を再読み込みしてください。"
            )
        participant_response, _manager, _effective, _source = (
            _split_response_payload(
                _json_loads(row["response_payload"], {})
            )
        )
        manager_response = {
            **_response_snapshot(participant),
            "active": True,
            "updated_by": _audit_actor.get(),
            "cleared_at": "",
            "cleared_by": "",
        }
        response_payload = _combined_response_payload(
            participant_response,
            manager_response,
        )
        cursor = connection.execute(
            """
            UPDATE project_participations
            SET response_payload = ?,
                version = version + 1,
                updated_at = ?
            WHERE project_id = ? AND participant_id = ? AND version = ?
            """,
            (
                _json_dumps(response_payload),
                now_iso(),
                project_id,
                participant.id,
                effective_version,
            ),
        )
        if getattr(cursor, "rowcount", 1) == 0:
            raise StorageConflictError(
                "別の画面から同じ参加者の回答が先に更新されました。"
                "画面を再読み込みしてください。"
            )
        participant.storage_version = current_version + 1
        participant.participant_response = participant_response
        participant.manager_response = manager_response
        participant.response_source = "manager"
        version = _write_participants_snapshot(connection, project_id)
        _audit(
            connection,
            "participant.manager_response.saved",
            project_id=project_id,
            target=participant.id,
        )
    return version


def clear_manager_response_override(
    project_id: str,
    participant_id: str,
    *,
    expected_version: int | None = None,
) -> int:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    participant_id = str(participant_id)
    with _connection(write=True) as connection:
        row = connection.execute(
            "SELECT version, response_payload FROM project_participations "
            "WHERE project_id = ? AND participant_id = ?",
            (project_id, participant_id),
        ).fetchone()
        if row is None:
            raise StorageError("指定した参加者が見つかりません。")
        current_version = int(row["version"])
        effective_version = (
            current_version
            if expected_version is None
            else int(expected_version)
        )
        if effective_version != current_version:
            raise StorageConflictError(
                "別の画面から同じ参加者の回答が先に更新されました。"
                "画面を再読み込みしてください。"
            )
        participant_response, manager_response, _effective, source = (
            _split_response_payload(
                _json_loads(row["response_payload"], {})
            )
        )
        if source != "manager":
            return _document(connection, project_id, "participants", [])[1]
        manager_response.update(
            {
                "active": False,
                "cleared_at": now_iso(),
                "cleared_by": _audit_actor.get(),
            }
        )
        response_payload = _combined_response_payload(
            participant_response,
            manager_response,
        )
        cursor = connection.execute(
            """
            UPDATE project_participations
            SET response_payload = ?,
                version = version + 1,
                updated_at = ?
            WHERE project_id = ? AND participant_id = ? AND version = ?
            """,
            (
                _json_dumps(response_payload),
                now_iso(),
                project_id,
                participant_id,
                effective_version,
            ),
        )
        if getattr(cursor, "rowcount", 1) == 0:
            raise StorageConflictError(
                "別の画面から同じ参加者の回答が先に更新されました。"
                "画面を再読み込みしてください。"
            )
        version = _write_participants_snapshot(connection, project_id)
        _audit(
            connection,
            "participant.manager_response.cleared",
            project_id=project_id,
            target=participant_id,
        )
    return version


def reset_participant_responses(project_id: str) -> int:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    response_payload = _json_dumps(
        _combined_response_payload(
            {
            "availability": [],
            "zoom_availability": [],
            "support_requested_count": None,
            "submitted_at": "",
            "input_status": "not_started",
            "updated_at": now_iso(),
            }
        )
    )
    with _connection(write=True) as connection:
        connection.execute(
            """
            UPDATE project_participations
            SET response_payload = ?,
                version = version + 1,
                updated_at = ?
            WHERE project_id = ?
            """,
            (response_payload, now_iso(), project_id),
        )
        version = _write_participants_snapshot(connection, project_id)
        _audit(connection, "participants.responses.reset", project_id=project_id)
    return version


def save_participant_admin_fields(project_id: str, participant: Participant) -> int:
    initialize_storage()
    return save_participant_admin_fields_bulk(project_id, [participant])


def save_participant_admin_fields_bulk(
    project_id: str,
    participants: list[Participant],
) -> int:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection(write=True) as connection:
        version, participant_versions = (
            _save_participant_admin_fields_bulk_in_transaction(
                connection,
                project_id,
                participants,
            )
        )
    for participant in participants:
        participant.storage_version = participant_versions[participant.id]
    return version


def _save_participant_admin_fields_bulk_in_transaction(
    connection: _Connection,
    project_id: str,
    participants: list[Participant],
) -> tuple[int, dict[str, int]]:
    current_participants = _participant_rows(connection, project_id)
    current_by_id = {
        participant.id: participant for participant in current_participants
    }
    participant_by_id = {
        participant.id: participant for participant in participants
    }
    missing_ids = [
        participant.id
        for participant in participants
        if participant.id not in current_by_id
    ]
    if missing_ids:
        raise StorageError("指定した参加者が見つかりません。")
    if any(
        participant.storage_version
        and participant.storage_version
        != current_by_id[participant.id].storage_version
        for participant in participants
    ):
        raise StorageConflictError(
            "別の利用者が参加者情報を先に更新しました。"
            "画面を再読み込みしてください。"
        )
    merged_participants = [
        participant_by_id.get(current.id, current)
        for current in current_participants
    ]
    normalized_names = [
        participant_name_identity_key(participant.name)
        for participant in merged_participants
    ]
    if any(not name for name in normalized_names):
        raise StorageError("参加者名を空欄にはできません。")
    if len(normalized_names) != len(set(normalized_names)):
        raise StorageError("参加者名が重複しています。")
    sort_order_by_id = {
        participant.id: index
        for index, participant in enumerate(merged_participants)
    }
    timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    common_participants: list[Participant] = []
    participation_rows: list[tuple[Any, ...]] = []
    for participant in participants:
        common_participants.append(participant)
        payloads = _participant_payloads(participant)
        participation_rows.append(
            (
                project_id,
                participant.id,
                sort_order_by_id[participant.id],
                _json_dumps(payloads["roster"]),
                _json_dumps(payloads["attributes"]),
                _json_dumps(payloads["requirements"]),
                _json_dumps(
                    _participant_response_payload(current_by_id[participant.id])
                ),
                current_by_id[participant.id].storage_version + 1,
                timestamp,
            ),
        )
    _upsert_common_participants(
        connection,
        common_participants,
        timestamp=timestamp,
    )
    affected_rows = _upsert_project_participant_rows(
        connection,
        participation_rows,
        expected_versions=True,
    )
    if affected_rows != len(participation_rows):
        raise StorageConflictError(
            "別の利用者が参加者情報を先に更新しました。"
            "画面を再読み込みしてください。"
        )
    participant_versions = {
        participant.id: current_by_id[participant.id].storage_version + 1
        for participant in participants
    }
    version = _write_participants_snapshot(connection, project_id)
    _audit(
        connection,
        "participants.admin_fields.updated",
        project_id=project_id,
    )
    return version, participant_versions


def delete_participants(project_id: str, participant_ids: list[str]) -> int:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    normalized_ids = list(dict.fromkeys(str(item) for item in participant_ids))
    if not normalized_ids:
        raise StorageError("削除する参加者が指定されていません。")
    with _connection(write=True) as connection:
        current_participants = _participant_rows(connection, project_id)
        current_by_id = {
            participant.id: participant for participant in current_participants
        }
        participant_names: dict[str, str] = {}
        for participant_id in normalized_ids:
            participant = current_by_id.get(participant_id)
            if participant is None:
                raise StorageError("指定した参加者が見つかりません。")
            participant_names[participant_id] = participant.name
        blocked_ids = _project_has_active_participant_references(
            connection,
            project_id,
            participant_names,
        )
        if any(participant_id in blocked_ids for participant_id in normalized_ids):
            raise StorageError(
                "確定日程で使用中のため削除できません。"
                "先に確定日程または改訂作業から参加者を外してください。"
            )
        placeholders = ",".join("?" for _ in normalized_ids)
        connection.execute(
            f"DELETE FROM project_participations "
            f"WHERE project_id = ? AND participant_id IN ({placeholders})",
            (project_id, *normalized_ids),
        )
        connection.execute(
            f"DELETE FROM memberships "
            f"WHERE project_id = ? AND participant_id IN ({placeholders})",
            (project_id, *normalized_ids),
        )
        version = _write_participants_snapshot(connection, project_id)
        _audit_many(
            connection,
            [
                ("participant.deleted", project_id, participant_id, {})
                for participant_id in normalized_ids
            ],
        )
    return version


def delete_participant(project_id: str, participant_id: str) -> int:
    return delete_participants(project_id, [str(participant_id)])


def clear_participants(project_id: str) -> int:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection(write=True) as connection:
        connection.execute(
            "DELETE FROM project_participations WHERE project_id = ?",
            (project_id,),
        )
        connection.execute(
            """
            DELETE FROM memberships
            WHERE project_id = ? AND role = 'participant'
            """,
            (project_id,),
        )
        version = _write_participants_snapshot(connection, project_id)
        _audit(connection, "participants.cleared", project_id=project_id)
    return version


def add_participant(
    project_id: str,
    participants: list[Participant],
    name: str,
    registered_by: str,
) -> Participant:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    normalized_name = name.strip()[:120]
    if not normalized_name:
        raise StorageError("参加者名を入力してください。")
    with _connection(write=True) as connection:
        current_participants = _participant_rows(connection, project_id)
        for participant in current_participants:
            if participant_name_identity_key(
                participant.name
            ) == participant_name_identity_key(normalized_name):
                return participant
        participant = Participant.create(normalized_name, registered_by)
        _raise_common_participant_name_conflict(
            _common_participant_name_conflicts(connection, [participant])
        )
        _upsert_common_participant(connection, participant)
        payloads = _participant_payloads(participant)
        sort_order = connection.execute(
            """
            SELECT COALESCE(MAX(sort_order), -1) + 1
            FROM project_participations WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO project_participations
            (project_id, participant_id, sort_order, roster_payload,
             attributes_payload, requirements_payload, response_payload,
             version, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                project_id,
                participant.id,
                sort_order,
                _json_dumps(payloads["roster"]),
                _json_dumps(payloads["attributes"]),
                _json_dumps(payloads["requirements"]),
                _json_dumps(payloads["response"]),
                now_iso(),
            ),
        )
        _write_participants_snapshot(connection, project_id)
        _audit(
            connection,
            "participant.created",
            project_id=project_id,
            target=participant.id,
            detail={"registered_by": registered_by},
        )
    return participant


def load_candidates(project_id: str) -> list[dict[str, Any]]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection() as connection:
        return _candidate_rows(connection, project_id)


def load_support_role_version_notice(project_id: str) -> dict[str, Any] | None:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection() as connection:
        return _load_support_role_notice(
            connection,
            project_id,
            json_loads=_json_loads,
        )


def acknowledge_support_role_version_notice(project_id: str) -> bool:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection(write=True) as connection:
        return _acknowledge_support_role_notice(
            connection,
            project_id,
            json_loads=_json_loads,
            json_dumps=_json_dumps,
            audit=_audit,
            now_iso=now_iso,
        )


def load_candidates_version(project_id: str) -> int:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection() as connection:
        row = connection.execute(
            "SELECT version FROM documents "
            "WHERE project_id = ? AND kind = 'candidates'",
            (project_id,),
        ).fetchone()
    return int(row["version"]) if row else 0


def load_candidates_with_version(
    project_id: str,
) -> tuple[list[dict[str, Any]], int]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection() as connection:
        candidates = _candidate_rows(connection, project_id)
        row = connection.execute(
            "SELECT version FROM documents "
            "WHERE project_id = ? AND kind = 'candidates'",
            (project_id,),
        ).fetchone()
        version = int(row["version"]) if row else 0
    return candidates, version


def save_candidates(
    project_id: str,
    candidates: list[dict[str, Any]],
    *,
    expected_version: int | None = None,
) -> int:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection(write=True) as connection:
        _replace_candidate_rows(connection, project_id, candidates)
        version = _write_document(
            connection,
            project_id,
            "candidates",
            candidates,
            expected_version=expected_version,
        )
        _audit(connection, "candidates.updated", project_id=project_id)
    return version


def save_project_state_updates(
    project_id: str,
    *,
    config_updates: dict[str, Any] | None = None,
    participants: list[Participant] | None = None,
    clear_candidates: bool = False,
    expected_config_version: int | None = None,
    expected_candidate_version: int | None = None,
) -> dict[str, int | None]:
    """Save related manager changes in one database transaction."""

    initialize_storage()
    project_id = _validate_project_id(project_id)
    participant_versions: dict[str, int] = {}
    config_version: int | None = None
    participants_version: int | None = None
    candidate_version: int | None = None
    with _connection(write=True) as connection:
        if config_updates:
            config_version = _save_config_fields_in_transaction(
                connection,
                project_id,
                config_updates,
                expected_version=expected_config_version,
            )
        if participants is not None:
            participants_version, participant_versions = (
                _save_participant_admin_fields_bulk_in_transaction(
                    connection,
                    project_id,
                    participants,
                )
            )
        if clear_candidates:
            _replace_candidate_rows(connection, project_id, [])
            candidate_version = _write_document(
                connection,
                project_id,
                "candidates",
                [],
                expected_version=expected_candidate_version,
            )
            _audit(connection, "candidates.updated", project_id=project_id)
    for participant in participants or []:
        participant.storage_version = participant_versions[participant.id]
    return {
        "config_version": config_version,
        "participants_version": participants_version,
        "candidate_version": candidate_version,
    }


def load_confirmed_candidate(project_id: str) -> dict[str, Any] | None:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection() as connection:
        return _effective_confirmed_candidate_row(connection, project_id)


def list_schedule_revisions(project_id: str) -> list[dict[str, Any]]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection() as connection:
        return _list_schedule_revisions(connection, project_id)


def load_schedule_revision(
    project_id: str,
    revision_id: str,
) -> dict[str, Any] | None:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection() as connection:
        revision = _load_schedule_revision(connection, str(revision_id))
        if revision is None:
            return None
        revision_project = connection.execute(
            "SELECT project_id FROM schedule_revisions WHERE id = ?",
            (str(revision_id),),
        ).fetchone()
        if revision_project is None or str(revision_project["project_id"]) != project_id:
            raise StorageError("指定した日程revisionが企画に属していません。")
        return revision


def _schedule_amendment_workspace_in_transaction(
    connection: _Connection,
    project_id: str,
) -> tuple[dict[str, Any], int]:
    raw, version = _document(
        connection,
        project_id,
        "schedule_amendments",
        empty_amendment_workspace(),
    )
    workspace = normalize_amendment_workspace(raw)
    workspace["_storage_version"] = version
    return workspace, version


def load_schedule_amendment_workspace(project_id: str) -> dict[str, Any]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection() as connection:
        workspace, _version = _schedule_amendment_workspace_in_transaction(
            connection,
            project_id,
        )
        return workspace


def save_schedule_amendment_request(
    project_id: str,
    participant_id: str,
    unavailable_slots: list[str],
    *,
    reason: str = "",
    expected_revision_id: str | None,
    expected_participant_version: int | None = None,
    expected_workspace_version: int | None = None,
) -> dict[str, Any]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    participant_id = str(participant_id)
    normalized_slots: list[str] = []
    for raw_slot in unavailable_slots:
        slot = str(raw_slot)
        try:
            parse_slot_key(slot)
        except (TypeError, ValueError):
            raise StorageError("変更依頼の不可能コマ形式が不正です。") from None
        normalized_slots.append(slot)
    normalized_slots = sorted(set(normalized_slots))
    if not normalized_slots:
        raise StorageError("不可能になったコマを1つ以上選択してください。")

    with _connection(write=True) as connection:
        try:
            active_revision_id, guard_matches = _schedule_revision_guard_matches(
                connection,
                project_id,
                expected_revision_id,
            )
        except ScheduleModelError as error:
            raise StorageError(str(error)) from error
        if not active_revision_id:
            raise StorageError("公開中の日程がないため改訂を開始できません。")
        if not guard_matches:
            raise StorageConflictError(
                "公開日程が先に更新されました。画面を再読み込みしてください。"
            )
        workspace, workspace_version = (
            _schedule_amendment_workspace_in_transaction(
                connection,
                project_id,
            )
        )
        if expected_workspace_version is not None and (
            expected_workspace_version != workspace_version
        ):
            raise StorageConflictError(
                "改訂作業が先に更新されました。画面を再読み込みしてください。"
            )
        amendment = active_amendment(workspace)
        if amendment is not None and (
            str(amendment.get("base_revision_id", ""))
            != active_revision_id
        ):
            raise StorageConflictError(
                "改訂元の公開版が変更されています。"
                "現在の改訂作業を破棄して作り直してください。"
            )

        row = connection.execute(
            "SELECT version FROM project_participations "
            "WHERE project_id = ? AND participant_id = ?",
            (project_id, participant_id),
        ).fetchone()
        participant = _participant_row(connection, project_id, participant_id)
        if row is None or participant is None:
            raise StorageError("変更依頼者が見つかりません。")
        current_participant_version = int(row["version"])
        effective_participant_version = (
            current_participant_version
            if expected_participant_version is None
            else int(expected_participant_version)
        )
        if effective_participant_version != current_participant_version:
            raise StorageConflictError(
                "変更依頼者の回答が先に更新されました。"
                "画面を再読み込みしてください。"
            )
        currently_possible_slots = (
            set(participant.availability)
            | set(participant.zoom_availability)
        )
        already_impossible = (
            set(normalized_slots) - currently_possible_slots
        )
        if already_impossible:
            raise StorageError(
                "現在すでに参加不可能なコマは、"
                "新しい変更依頼として追加できません。"
            )
        already_requested = set()
        if amendment is not None:
            already_requested = set(
                amendment_unavailable_slots_by_participant(
                    amendment
                ).get(participant_id, [])
            )
        if set(normalized_slots) & already_requested:
            raise StorageError(
                "この改訂作業ですでに登録済みの不可能コマが"
                "含まれています。"
            )

        timestamp = now_iso()
        request = {
            "id": uuid4().hex,
            "requester_id": participant_id,
            "requester_name": participant.name,
            "unavailable_slots": normalized_slots,
            "reason": str(reason).strip()[:1000],
            "created_at": timestamp,
            "created_by": _audit_actor.get(),
            "input_source": "schedule_amendment_request",
            "participant_response_version": current_participant_version,
        }
        if amendment is None:
            amendment_id = uuid4().hex
            amendment = {
                "id": amendment_id,
                "status": "draft",
                "base_revision_id": active_revision_id,
                "requests": [request],
                "requester_ids": [participant_id],
                "unavailable_slots_by_participant": {
                    participant_id: normalized_slots
                },
                # Older application versions can still read the first request.
                "requester_id": participant_id,
                "requester_name": participant.name,
                "unavailable_slots": normalized_slots,
                "reason": request["reason"],
                "proposal_revision_ids": [],
                "selected_revision_ids": [],
                "superseded_revision_ids": [],
                "reply_memos": {},
                "created_at": timestamp,
                "created_by": _audit_actor.get(),
                "updated_at": timestamp,
                "updated_by": _audit_actor.get(),
                "published_revision_id": "",
            }
            workspace["active_amendment_id"] = amendment_id
            workspace["amendments"].append(amendment)
            audit_action = "schedule.amendment.request.created"
        else:
            amendment_id = str(amendment["id"])
            requests = amendment_requests(amendment)
            requests.append(request)
            amendment["requests"] = requests
            unavailable_by_participant = (
                amendment_unavailable_slots_by_participant(amendment)
            )
            amendment["requester_ids"] = list(
                unavailable_by_participant
            )
            amendment["unavailable_slots_by_participant"] = (
                unavailable_by_participant
            )
            previous_ids = list(
                map(str, amendment.get("proposal_revision_ids", []))
            )
            amendment["superseded_revision_ids"] = list(
                dict.fromkeys(
                    [
                        *map(
                            str,
                            amendment.get(
                                "superseded_revision_ids",
                                [],
                            ),
                        ),
                        *previous_ids,
                    ]
                )
            )
            amendment["proposal_revision_ids"] = []
            amendment["selected_revision_ids"] = []
            amendment["reply_memos"] = {}
            amendment["updated_at"] = timestamp
            amendment["updated_by"] = _audit_actor.get()
            audit_action = "schedule.amendment.request.added"
        workspace.pop("_storage_version", None)
        version = _write_document(
            connection,
            project_id,
            "schedule_amendments",
            workspace,
            expected_version=workspace_version,
        )
        workspace["_storage_version"] = version
        _audit(
            connection,
            audit_action,
            project_id=project_id,
            target=amendment_id,
            detail={
                "request_id": request["id"],
                "requester_id": participant_id,
                "unavailable_slots": normalized_slots,
                "base_revision_id": active_revision_id,
            },
        )
        return workspace


def save_schedule_amendment_drafts(
    project_id: str,
    schedules: list[dict[str, Any]],
    *,
    amendment_id: str,
    source: str,
    expected_revision_id: str | None,
    expected_workspace_version: int | None = None,
    source_revision_id: str | None = None,
    replace_proposals: bool = False,
    change_note: str = "",
) -> dict[str, Any]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    source = str(source)
    if source not in AMENDMENT_DRAFT_SOURCES:
        raise StorageError("改訂案の作成元が不正です。")
    if not schedules:
        raise StorageError("保存する改訂案がありません。")
    with _connection(write=True) as connection:
        try:
            active_revision_id, guard_matches = _schedule_revision_guard_matches(
                connection,
                project_id,
                expected_revision_id,
            )
        except ScheduleModelError as error:
            raise StorageError(str(error)) from error
        if not guard_matches or not active_revision_id:
            raise StorageConflictError(
                "公開日程が先に更新されました。画面を再読み込みしてください。"
            )
        workspace, workspace_version = (
            _schedule_amendment_workspace_in_transaction(
                connection,
                project_id,
            )
        )
        if expected_workspace_version is not None and (
            expected_workspace_version != workspace_version
        ):
            raise StorageConflictError(
                "改訂作業が先に更新されました。画面を再読み込みしてください。"
            )
        amendment = active_amendment(workspace)
        if amendment is None or str(amendment.get("id")) != str(amendment_id):
            raise StorageError("進行中の改訂作業が見つかりません。")
        if str(amendment.get("base_revision_id", "")) != active_revision_id:
            raise StorageConflictError(
                "改訂元の公開版が変更されています。改訂を作り直してください。"
            )

        parent_revision_id = active_revision_id
        if source == "manual_amendment" and source_revision_id:
            source_schedule = _load_schedule_revision(
                connection,
                str(source_revision_id),
            )
            source_owner = connection.execute(
                "SELECT project_id, source FROM schedule_revisions WHERE id = ?",
                (str(source_revision_id),),
            ).fetchone()
            if (
                source_schedule is None
                or source_owner is None
                or str(source_owner["project_id"]) != project_id
                or str(source_owner["source"]) not in AMENDMENT_DRAFT_SOURCES
                or str(
                    source_schedule.get("amendment", {}).get("id", "")
                )
                != str(amendment_id)
            ):
                raise StorageError("手動調整元の改訂案が見つかりません。")
            parent_revision_id = str(source_revision_id)

        timestamp = now_iso()
        created: list[dict[str, Any]] = []
        created_ids: list[str] = []
        for index, raw_schedule in enumerate(schedules, start=1):
            proposed = deepcopy(raw_schedule)
            proposed.pop("schedule_revision", None)
            proposal_metadata = dict(proposed.get("amendment", {}))
            proposal_metadata.update(
                {
                    "id": str(amendment_id),
                    "base_revision_id": active_revision_id,
                    "kind": source,
                    "source_revision_id": str(source_revision_id or ""),
                }
            )
            proposed["amendment"] = proposal_metadata
            try:
                revision_id, revision = _insert_schedule_revision(
                    connection,
                    project_id=project_id,
                    schedule=proposed,
                    source=source,
                    actor=_audit_actor.get(),
                    timestamp=timestamp,
                    parent_revision_id=parent_revision_id,
                    change_note=(
                        str(change_note).strip()[:950]
                        or (
                            f"変更最小化探索による改訂案{index}"
                            if source == "amendment_proposal"
                            else (
                                "改訂案を手動調整"
                                if source_revision_id
                                else "公開版から完全手動で改訂案を作成"
                            )
                        )
                    ),
                    activate=False,
                )
            except ScheduleModelError as error:
                raise StorageError(str(error)) from error
            created.append(revision)
            created_ids.append(revision_id)

        previous_ids = [
            str(value)
            for value in amendment.get("proposal_revision_ids", [])
        ]
        if replace_proposals:
            manual_previous_ids = [
                revision_id
                for revision_id in previous_ids
                if (
                    row := connection.execute(
                        "SELECT source FROM schedule_revisions WHERE id = ?",
                        (revision_id,),
                    ).fetchone()
                )
                is not None
                and str(row["source"]) == "manual_amendment"
            ]
            replaced_ids = [
                revision_id
                for revision_id in previous_ids
                if revision_id not in manual_previous_ids
            ]
            amendment["superseded_revision_ids"] = list(
                dict.fromkeys(
                    [
                        *amendment.get("superseded_revision_ids", []),
                        *replaced_ids,
                    ]
                )
            )
            amendment["proposal_revision_ids"] = list(
                dict.fromkeys([*manual_previous_ids, *created_ids])
            )
            amendment["selected_revision_ids"] = created_ids[:3]
        else:
            amendment["proposal_revision_ids"] = list(
                dict.fromkeys([*previous_ids, *created_ids])
            )
            selected = [
                str(value)
                for value in amendment.get("selected_revision_ids", [])
                if str(value) in amendment["proposal_revision_ids"]
            ]
            amendment["selected_revision_ids"] = list(
                dict.fromkeys([*selected, *created_ids])
            )[:3]
        amendment["updated_at"] = timestamp
        amendment["updated_by"] = _audit_actor.get()
        workspace.pop("_storage_version", None)
        version = _write_document(
            connection,
            project_id,
            "schedule_amendments",
            workspace,
            expected_version=workspace_version,
        )
        workspace["_storage_version"] = version
        _audit(
            connection,
            "schedule.amendment.drafts.created",
            project_id=project_id,
            target=str(amendment_id),
            detail={
                "source": source,
                "revision_ids": created_ids,
                "base_revision_id": active_revision_id,
            },
        )
        return {
            "drafts": created,
            "workspace": workspace,
        }


def select_schedule_amendment_proposals(
    project_id: str,
    amendment_id: str,
    revision_ids: list[str],
    *,
    expected_workspace_version: int | None = None,
) -> dict[str, Any]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    selected = list(dict.fromkeys(map(str, revision_ids)))
    if len(selected) > 3:
        raise StorageError("DMにまとめる改訂案は3案まで選択できます。")
    with _connection(write=True) as connection:
        workspace, workspace_version = (
            _schedule_amendment_workspace_in_transaction(
                connection,
                project_id,
            )
        )
        if expected_workspace_version is not None and (
            expected_workspace_version != workspace_version
        ):
            raise StorageConflictError(
                "改訂作業が先に更新されました。画面を再読み込みしてください。"
            )
        amendment = active_amendment(workspace)
        if amendment is None or str(amendment.get("id")) != str(amendment_id):
            raise StorageError("進行中の改訂作業が見つかりません。")
        available_ids = set(map(str, amendment.get("proposal_revision_ids", [])))
        if set(selected) - available_ids:
            raise StorageError("選択した改訂案が現在の改訂作業に属していません。")
        amendment["selected_revision_ids"] = selected
        amendment["updated_at"] = now_iso()
        amendment["updated_by"] = _audit_actor.get()
        workspace.pop("_storage_version", None)
        version = _write_document(
            connection,
            project_id,
            "schedule_amendments",
            workspace,
            expected_version=workspace_version,
        )
        workspace["_storage_version"] = version
        return workspace


def save_schedule_amendment_reply(
    project_id: str,
    amendment_id: str,
    participant_id: str,
    option: str,
    *,
    status: str,
    note: str = "",
    expected_workspace_version: int | None = None,
) -> dict[str, Any]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    status = str(status)
    if status not in {"unanswered", "possible", "impossible"}:
        raise StorageError("返信メモの状態が不正です。")
    try:
        day_text, period_text, meeting_mode = str(option).rsplit("#", 2)
        parse_slot_key(f"{day_text}#{int(period_text)}")
    except (TypeError, ValueError):
        raise StorageError("返信メモの日程形式が不正です。") from None
    if meeting_mode not in {"in_person", "zoom"}:
        raise StorageError("返信メモの開催形式が不正です。")
    with _connection(write=True) as connection:
        workspace, workspace_version = (
            _schedule_amendment_workspace_in_transaction(
                connection,
                project_id,
            )
        )
        if expected_workspace_version is not None and (
            expected_workspace_version != workspace_version
        ):
            raise StorageConflictError(
                "改訂作業が先に更新されました。画面を再読み込みしてください。"
            )
        amendment = active_amendment(workspace)
        if amendment is None or str(amendment.get("id")) != str(amendment_id):
            raise StorageError("進行中の改訂作業が見つかりません。")
        reply_memos = amendment.setdefault("reply_memos", {})
        participant_replies = reply_memos.setdefault(str(participant_id), {})
        participant_replies[str(option)] = {
            "status": status,
            "note": str(note).strip()[:1000],
            "updated_at": now_iso(),
            "updated_by": _audit_actor.get(),
        }
        amendment["updated_at"] = now_iso()
        amendment["updated_by"] = _audit_actor.get()
        workspace.pop("_storage_version", None)
        version = _write_document(
            connection,
            project_id,
            "schedule_amendments",
            workspace,
            expected_version=workspace_version,
        )
        workspace["_storage_version"] = version
        _audit(
            connection,
            "schedule.amendment.reply.saved",
            project_id=project_id,
            target=str(amendment_id),
            detail={
                "participant_id": str(participant_id),
                "option": str(option),
                "status": status,
            },
        )
        return workspace


def _restore_legacy_amendment_response_overrides(
    connection: _Connection,
    project_id: str,
    amendment: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Undo response overrides written by older amendment-request code."""

    requests_by_participant: dict[str, list[dict[str, Any]]] = (
        defaultdict(list)
    )
    for request in amendment_requests(amendment):
        if (
            str(request.get("input_source", ""))
            == "schedule_amendment_request"
        ):
            continue
        requests_by_participant[str(request["requester_id"])].append(
            request
        )

    restored: list[str] = []
    skipped: list[str] = []
    for participant_id, requests in requests_by_participant.items():
        row = connection.execute(
            "SELECT version FROM project_participations "
            "WHERE project_id = ? AND participant_id = ?",
            (project_id, participant_id),
        ).fetchone()
        if row is None:
            skipped.append(participant_id)
            continue
        current_version = int(row["version"])
        requested_slots = {
            str(slot)
            for request in requests
            for slot in request.get("unavailable_slots", [])
        }
        created_at_values = sorted(
            str(request.get("created_at", ""))
            for request in requests
            if str(request.get("created_at", ""))
        )
        if not created_at_values:
            skipped.append(participant_id)
            continue

        restored_payload: dict[str, Any] | None = None
        backup_rows = connection.execute(
            """
            SELECT payload
            FROM backups
            WHERE project_id = ? AND kind = 'participants'
              AND created_at <= ?
            ORDER BY id DESC
            """,
            (project_id, created_at_values[0]),
        ).fetchall()
        for backup_row in backup_rows:
            snapshots = _json_loads(backup_row["payload"], [])
            if not isinstance(snapshots, list):
                continue
            snapshot = next(
                (
                    item
                    for item in snapshots
                    if isinstance(item, dict)
                    and str(item.get("id", "")) == participant_id
                ),
                None,
            )
            if snapshot is None:
                continue
            try:
                snapshot_version = int(
                    snapshot.get("storage_version", 0)
                )
            except (TypeError, ValueError):
                continue
            if current_version != snapshot_version + len(requests):
                continue
            snapshot_possible_slots = {
                *map(str, snapshot.get("availability", [])),
                *map(str, snapshot.get("zoom_availability", [])),
            }
            if not requested_slots <= snapshot_possible_slots:
                continue
            restored_payload = _participant_response_payload(
                Participant.from_dict(snapshot)
            )
            break

        if restored_payload is None:
            skipped.append(participant_id)
            continue
        cursor = connection.execute(
            """
            UPDATE project_participations
            SET response_payload = ?,
                version = version + 1,
                updated_at = ?
            WHERE project_id = ? AND participant_id = ? AND version = ?
            """,
            (
                _json_dumps(restored_payload),
                now_iso(),
                project_id,
                participant_id,
                current_version,
            ),
        )
        if getattr(cursor, "rowcount", 1) == 0:
            skipped.append(participant_id)
            continue
        restored.append(participant_id)

    if restored:
        _write_participants_snapshot(connection, project_id)
    return restored, skipped


def discard_schedule_amendment(
    project_id: str,
    amendment_id: str,
    *,
    expected_workspace_version: int | None = None,
) -> dict[str, Any]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection(write=True) as connection:
        workspace, workspace_version = (
            _schedule_amendment_workspace_in_transaction(
                connection,
                project_id,
            )
        )
        if expected_workspace_version is not None and (
            expected_workspace_version != workspace_version
        ):
            raise StorageConflictError(
                "改訂作業が先に更新されました。画面を再読み込みしてください。"
            )
        amendment = active_amendment(workspace)
        if amendment is None or str(amendment.get("id")) != str(amendment_id):
            raise StorageError("進行中の改訂作業が見つかりません。")
        restored_participant_ids, skipped_participant_ids = (
            _restore_legacy_amendment_response_overrides(
                connection,
                project_id,
                amendment,
            )
        )
        amendment["status"] = "discarded"
        amendment["updated_at"] = now_iso()
        amendment["updated_by"] = _audit_actor.get()
        workspace["active_amendment_id"] = ""
        workspace.pop("_storage_version", None)
        version = _write_document(
            connection,
            project_id,
            "schedule_amendments",
            workspace,
            expected_version=workspace_version,
        )
        workspace["_storage_version"] = version
        _audit(
            connection,
            "schedule.amendment.discarded",
            project_id=project_id,
            target=str(amendment_id),
            detail={
                "legacy_participant_responses_restored": (
                    restored_participant_ids
                ),
                "legacy_participant_responses_skipped": (
                    skipped_participant_ids
                ),
            },
        )
        return workspace


def _save_schedule_revision_in_transaction(
    connection: _Connection,
    project_id: str,
    schedule: dict[str, Any],
    *,
    source: str,
    change_note: str,
    expected_revision_id: str | None,
    project_status: str,
) -> dict[str, Any]:
    allowed_sources = {
        "manual",
        "edit",
        "reoptimization",
        "restore",
        "amendment_publish",
    }
    if source not in allowed_sources:
        raise StorageError("日程revisionの更新元が不正です。")
    try:
        active_revision_id, guard_matches = _schedule_revision_guard_matches(
            connection, project_id, expected_revision_id
        )
    except ScheduleModelError as error:
        raise StorageError(str(error)) from error
    if not guard_matches:
        raise StorageConflictError(
            "別の利用者が確定日程を先に更新しました。"
            "画面を再読み込みしてください。"
        )
    change_note = str(change_note).strip()[:1000]
    timestamp = now_iso()
    proposed = deepcopy(schedule)
    proposed.setdefault("confirmed_at", timestamp)
    proposed["revised_at"] = timestamp
    current_public = (
        _load_schedule_revision(connection, active_revision_id)
        if active_revision_id
        else None
    ) or {}
    try:
        current_publication_number = int(
            current_public.get("publication_number", 0)
        )
    except (TypeError, ValueError):
        current_publication_number = 0
    proposed.setdefault(
        "publication_number",
        max(0, current_publication_number) + 1,
    )
    try:
        normalized_schedule = normalize_schedule(proposed, project_id)
        conflicts = _schedule_cross_project_conflicts(
            connection,
            project_id,
            proposed,
            normalized_schedule=normalized_schedule,
        )
        if conflicts:
            raise StorageConflictError(
                f"他企画の確定日程と{len(conflicts)}件競合しています。"
                "最新データを確認してください。"
            )
        revision_id, revised = _insert_schedule_revision(
            connection,
            project_id=project_id,
            schedule=proposed,
            source=source,
            actor=_audit_actor.get(),
            timestamp=timestamp,
            parent_revision_id=active_revision_id or None,
            change_note=change_note,
            normalized_schedule=normalized_schedule,
        )
    except ScheduleModelError as error:
        raise StorageError(str(error)) from error
    _write_confirmed_candidate_row(
        connection, project_id, revised, updated_at=timestamp
    )
    _write_document(
        connection,
        project_id,
        "confirmed_candidate",
        revised,
        updated_at=timestamp,
    )
    history, history_version = _document(connection, project_id, "history", [])
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "action": "schedule_revision_created",
            "at": timestamp,
            "actor": _audit_actor.get(),
            "revision_id": revision_id,
            "source": source,
            "change_note": change_note,
        }
    )
    _write_document(
        connection,
        project_id,
        "history",
        history,
        expected_version=history_version,
    )
    _save_config_fields_in_transaction(
        connection, project_id, {"status": project_status}
    )
    _audit(
        connection,
        "schedule.revision.created",
        project_id=project_id,
        target=revision_id,
        detail={"source": source, "change_note": change_note},
    )
    return revised


def save_schedule_revision(
    project_id: str,
    schedule: dict[str, Any],
    *,
    source: str,
    change_note: str = "",
    expected_revision_id: str | None = None,
    project_status: str = "confirmed",
) -> dict[str, Any]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection(write=True) as connection:
        return _save_schedule_revision_in_transaction(
            connection,
            project_id,
            schedule,
            source=source,
            change_note=change_note,
            expected_revision_id=expected_revision_id,
            project_status=project_status,
        )


def publish_schedule_amendment_draft(
    project_id: str,
    revision_id: str,
    *,
    amendment_id: str,
    expected_revision_id: str | None,
    expected_workspace_version: int | None = None,
    change_note: str = "",
) -> dict[str, Any]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    revision_id = str(revision_id)
    with _connection(write=True) as connection:
        try:
            active_revision_id, guard_matches = _schedule_revision_guard_matches(
                connection,
                project_id,
                expected_revision_id,
            )
        except ScheduleModelError as error:
            raise StorageError(str(error)) from error
        if not active_revision_id or not guard_matches:
            raise StorageConflictError(
                "公開日程が先に更新されました。画面を再読み込みしてください。"
            )
        workspace, workspace_version = (
            _schedule_amendment_workspace_in_transaction(
                connection,
                project_id,
            )
        )
        if expected_workspace_version is not None and (
            expected_workspace_version != workspace_version
        ):
            raise StorageConflictError(
                "改訂作業が先に更新されました。画面を再読み込みしてください。"
            )
        amendment = active_amendment(workspace)
        if amendment is None or str(amendment.get("id")) != str(amendment_id):
            raise StorageError("進行中の改訂作業が見つかりません。")
        if str(amendment.get("base_revision_id", "")) != active_revision_id:
            raise StorageConflictError(
                "改訂元の公開版が変更されています。改訂を作り直してください。"
            )
        if revision_id not in set(
            map(str, amendment.get("proposal_revision_ids", []))
        ):
            raise StorageError("選択した改訂案が現在の改訂作業に属していません。")
        draft = _load_schedule_revision(connection, revision_id)
        owner = connection.execute(
            "SELECT project_id, source FROM schedule_revisions WHERE id = ?",
            (revision_id,),
        ).fetchone()
        if (
            draft is None
            or owner is None
            or str(owner["project_id"]) != project_id
            or str(owner["source"]) not in AMENDMENT_DRAFT_SOURCES
            or str(draft.get("amendment", {}).get("id", ""))
            != str(amendment_id)
        ):
            raise StorageError("公開する改訂案が見つかりません。")

        unavailable_by_participant = {
            participant_id: set(slots)
            for participant_id, slots in (
                amendment_unavailable_slots_by_participant(
                    amendment
                ).items()
            )
        }
        for session in draft.get("sessions", []):
            member_ids = {
                *map(str, session.get("university_role_member_ids", [])),
                *map(str, session.get("high_school_role_member_ids", [])),
            }
            slot_key = make_slot_key(
                str(session.get("date", "")),
                int(session.get("period", 0)),
            )
            if any(
                participant_id in member_ids
                and slot_key in unavailable_slots
                for participant_id, unavailable_slots
                in unavailable_by_participant.items()
            ):
                raise StorageError(
                    "変更依頼者が不可能としたコマを含むため"
                    "公開できません。"
                )

        current_public = _load_schedule_revision(
            connection,
            active_revision_id,
        ) or {}
        try:
            current_publication_number = int(
                current_public.get("publication_number", 1)
            )
        except (TypeError, ValueError):
            current_publication_number = 1
        proposed = deepcopy(draft)
        proposed.pop("schedule_revision", None)
        proposed["publication_number"] = max(
            1,
            current_publication_number,
        ) + 1
        proposal_metadata = dict(proposed.get("amendment", {}))
        proposal_metadata.update(
            {
                "id": str(amendment_id),
                "base_revision_id": active_revision_id,
                "kind": "amendment_publish",
                "selected_draft_revision_id": revision_id,
            }
        )
        proposed["amendment"] = proposal_metadata
        revised = _save_schedule_revision_in_transaction(
            connection,
            project_id,
            proposed,
            source="amendment_publish",
            change_note=(
                str(change_note).strip()
                or f"改訂案 revision {revision_id} を公開"
            ),
            expected_revision_id=active_revision_id,
            project_status="confirmed",
        )
        amendment["status"] = "published"
        amendment["published_revision_id"] = str(
            revised.get("schedule_revision", {}).get("id", "")
        )
        amendment["selected_draft_revision_id"] = revision_id
        amendment["updated_at"] = now_iso()
        amendment["updated_by"] = _audit_actor.get()
        workspace["active_amendment_id"] = ""
        workspace.pop("_storage_version", None)
        version = _write_document(
            connection,
            project_id,
            "schedule_amendments",
            workspace,
            expected_version=workspace_version,
        )
        workspace["_storage_version"] = version
        _audit(
            connection,
            "schedule.amendment.published",
            project_id=project_id,
            target=amendment["published_revision_id"],
            detail={
                "amendment_id": str(amendment_id),
                "draft_revision_id": revision_id,
            },
        )
        return {
            "schedule": revised,
            "workspace": workspace,
        }


def restore_schedule_revision(
    project_id: str,
    revision_id: str,
    *,
    expected_revision_id: str | None,
    change_note: str = "",
) -> dict[str, Any]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection(write=True) as connection:
        schedule = _load_schedule_revision(connection, str(revision_id))
        owner = connection.execute(
            "SELECT project_id FROM schedule_revisions WHERE id = ?",
            (str(revision_id),),
        ).fetchone()
        if schedule is None or owner is None or str(owner["project_id"]) != project_id:
            raise StorageError("復元する日程revisionが見つかりません。")
        schedule.pop("schedule_revision", None)
        return _save_schedule_revision_in_transaction(
            connection,
            project_id,
            schedule,
            source="restore",
            change_note=change_note or f"revision {revision_id} を復元",
            expected_revision_id=expected_revision_id,
            project_status="confirmed",
        )


def migrate_confirmed_schedules(*, dry_run: bool = True) -> dict[str, Any]:
    initialize_storage()
    if dry_run:
        with _connection() as connection:
            report, _entries = _schedule_migration_plan(connection)
        return {**report, "dry_run": True, "status": "ready"}
    with _connection(write=True) as connection:
        report = _apply_confirmed_schedule_migration(
            connection,
            actor=_audit_actor.get(),
            timestamp=now_iso(),
        )
        _audit(
            connection,
            "schedule.migration.applied",
            detail={
                "migration_id": report.get("migration_id"),
                "projects": report.get("eligible_project_count", 0),
            },
        )
    return report


def rollback_schedule_migration(
    migration_id: str,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    initialize_storage()
    if dry_run:
        with _connection() as connection:
            report = _schedule_migration_rollback_plan(
                connection, str(migration_id)
            )
        return {
            key: value for key, value in report.items() if key != "created"
        } | {"dry_run": True}
    with _connection(write=True) as connection:
        report = _apply_schedule_migration_rollback(
            connection,
            migration_id=str(migration_id),
            timestamp=now_iso(),
        )
        _audit(
            connection,
            "schedule.migration.rolled_back",
            detail={"migration_id": str(migration_id)},
        )
    return report


def list_common_participants() -> list[dict[str, Any]]:
    initialize_storage()
    with _connection() as connection:
        rows = connection.execute(
            """
            SELECT cp.id, cp.name, cp.profile_payload, cp.active,
                   cp.updated_at, COUNT(pp.project_id) AS project_count
            FROM common_participants cp
            LEFT JOIN project_participations pp
              ON pp.participant_id = cp.id
            WHERE cp.active = 1
            GROUP BY cp.id, cp.name, cp.profile_payload, cp.active,
                     cp.updated_at
            ORDER BY cp.name, cp.id
            """
        ).fetchall()
    return [_common_participant_to_dict(row) for row in rows]


def list_project_participant_options(
    project_id: str,
) -> list[dict[str, Any]]:
    """Return only the fields needed to choose a participant in the UI."""

    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection() as connection:
        rows = connection.execute(
            """
            SELECT cp.id, cp.name, cp.active AS common_active,
                   pp.roster_payload, pp.sort_order
            FROM project_participations pp
            JOIN common_participants cp ON cp.id = pp.participant_id
            WHERE pp.project_id = ?
            ORDER BY pp.sort_order, cp.name
            """,
            (project_id,),
        ).fetchall()
    options: list[dict[str, Any]] = []
    for row in rows:
        roster = _json_loads(row["roster_payload"], {})
        if not isinstance(roster, dict):
            roster = {}
        options.append(
            {
                "id": str(row["id"]),
                "name": str(row["name"]),
                "active": bool(row["common_active"])
                and bool(roster.get("active", True)),
            }
        )
    return options


def list_project_ids_for_participants(
    participant_ids: list[str],
) -> dict[str, list[str]]:
    """Return affected project IDs for a set of common participants."""

    normalized_ids = list(dict.fromkeys(str(item).strip() for item in participant_ids))
    normalized_ids = [item for item in normalized_ids if item]
    if not normalized_ids:
        return {}
    initialize_storage()
    placeholders = ",".join("?" for _ in normalized_ids)
    with _connection() as connection:
        rows = connection.execute(
            f"""
            SELECT participant_id, project_id
            FROM project_participations
            WHERE participant_id IN ({placeholders})
            ORDER BY participant_id, project_id
            """,
            tuple(normalized_ids),
        ).fetchall()
    result = {participant_id: [] for participant_id in normalized_ids}
    for row in rows:
        result.setdefault(str(row["participant_id"]), []).append(
            str(row["project_id"])
        )
    return result


def _schedule_references_participant(
    schedule: dict[str, Any] | None,
    participant_id: str,
    participant_name: str,
) -> bool:
    if not isinstance(schedule, dict):
        return False
    identity_key = participant_name_identity_key(participant_name)
    for session in schedule.get("sessions", []):
        if not isinstance(session, dict):
            continue
        member_ids = {
            str(value)
            for field in (
                "university_role_member_ids",
                "high_school_role_member_ids",
            )
            for value in session.get(field, [])
        }
        if participant_id in member_ids:
            return True
        member_names = {
            participant_name_identity_key(str(value))
            for field in (
                "university_role_members",
                "high_school_role_members",
            )
            for value in session.get(field, [])
        }
        if identity_key and identity_key in member_names:
            return True
    return any(
        isinstance(summary, dict)
        and (
            str(summary.get("participant_id", "")) == participant_id
            or (
                identity_key
                and participant_name_identity_key(
                    str(summary.get("name", ""))
                )
                == identity_key
            )
        )
        for summary in schedule.get("participant_summary", [])
    )


def _json_references_participant(value: Any, participant_id: str) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"participant_id", "requester_id"} and str(nested) == participant_id:
                return True
            if key == "unavailable_slots_by_participant" and isinstance(nested, dict):
                if participant_id in {str(item) for item in nested}:
                    return True
            if _json_references_participant(nested, participant_id):
                return True
    elif isinstance(value, list):
        return any(
            _json_references_participant(item, participant_id) for item in value
        )
    return False


def _project_has_active_participant_references(
    connection: _Connection,
    project_id: str,
    participant_names: dict[str, str],
) -> set[str]:
    if not participant_names:
        return set()
    participant_ids = list(participant_names)
    placeholders = ",".join("?" for _ in participant_ids)
    assignments = connection.execute(
        f"""
        SELECT assignment.participant_id
        FROM active_schedule_revisions active
        JOIN schedule_sessions session
          ON session.revision_id = active.revision_id
        JOIN session_assignments assignment
          ON assignment.session_id = session.id
        WHERE active.project_id = ?
          AND assignment.participant_id IN ({placeholders})
        """,
        (project_id, *participant_ids),
    ).fetchall()
    blocked = {str(row["participant_id"]) for row in assignments}
    confirmed_schedule = _effective_confirmed_candidate_row(connection, project_id)
    workspace, _version = _document(
        connection, project_id, "schedule_amendments", {}
    )
    active_workspace = active_amendment(workspace)
    for participant_id, participant_name in participant_names.items():
        if participant_id in blocked:
            continue
        if _schedule_references_participant(
            confirmed_schedule,
            participant_id,
            participant_name,
        ) or _json_references_participant(active_workspace, participant_id):
            blocked.add(participant_id)
    return blocked


def _project_has_active_participant_reference(
    connection: _Connection,
    project_id: str,
    participant_id: str,
    participant_name: str,
) -> bool:
    return bool(
        _project_has_active_participant_references(
            connection,
            project_id,
            {participant_id: participant_name},
        )
    )


def update_common_participant(
    participant_id: str,
    *,
    name: str,
    cohort: int | None,
    humanities_or_science: str,
    department: str,
    department_detail: str,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    initialize_storage()
    participant_id = str(participant_id)
    normalized_name = str(name).strip()
    if not normalized_name:
        raise StorageError("参加者名を空欄にはできません。")
    with _connection(write=True) as connection:
        row = connection.execute(
            """
            SELECT id, name, profile_payload, active, updated_at,
                   0 AS project_count
            FROM common_participants
            WHERE id = ? AND active = 1
            """,
            (participant_id,),
        ).fetchone()
        if row is None:
            raise StorageError("指定した参加者が見つかりません。")
        if (
            expected_updated_at is not None
            and str(row["updated_at"]) != str(expected_updated_at)
        ):
            raise StorageConflictError(
                "別の利用者が参加者情報を先に更新しました。"
                "画面を再読み込みしてください。"
            )
        identity = participant_name_identity_key(normalized_name)
        for other in connection.execute(
            """
            SELECT id, name FROM common_participants
            WHERE active = 1 AND id <> ?
            """,
            (participant_id,),
        ).fetchall():
            if participant_name_identity_key(str(other["name"])) == identity:
                raise StorageError("同じ名前の参加者がすでに登録されています。")
        profile = _json_loads(row["profile_payload"], {})
        if not isinstance(profile, dict):
            profile = {}
        participant = Participant.from_dict(
            {
                **profile,
                "id": participant_id,
                "name": normalized_name,
                "cohort": cohort,
                "humanities_or_science": humanities_or_science,
                "department": department,
                "department_detail": department_detail,
            }
        )
        timestamp = datetime.now(timezone.utc).isoformat(
            timespec="microseconds"
        )
        parameters: tuple[Any, ...]
        where = "id = ?"
        parameters = (
            participant.name,
            _json_dumps(_common_participant_profile(participant)),
            timestamp,
            participant_id,
        )
        if expected_updated_at is not None:
            where += " AND updated_at = ?"
            parameters += (str(expected_updated_at),)
        cursor = connection.execute(
            f"""
            UPDATE common_participants
            SET name = ?, profile_payload = ?, updated_at = ?
            WHERE {where}
            """,
            parameters,
        )
        if getattr(cursor, "rowcount", 1) == 0:
            raise StorageConflictError(
                "別の利用者が参加者情報を先に更新しました。"
                "画面を再読み込みしてください。"
            )
        project_ids = [
            str(project_row["project_id"])
            for project_row in connection.execute(
                """
                SELECT project_id FROM project_participations
                WHERE participant_id = ?
                """,
                (participant_id,),
            ).fetchall()
        ]
        if project_ids:
            connection.execute(
                """
                UPDATE project_participations
                SET version = version + 1, updated_at = ?
                WHERE participant_id = ?
                """,
                (timestamp, participant_id),
            )
        for project_id in project_ids:
            _write_participants_snapshot(connection, project_id)
        _audit(
            connection,
            "common_participant.updated",
            target=participant_id,
            detail={"project_count": len(project_ids)},
        )
        updated = connection.execute(
            """
            SELECT cp.id, cp.name, cp.profile_payload, cp.active,
                   cp.updated_at, COUNT(pp.project_id) AS project_count
            FROM common_participants cp
            LEFT JOIN project_participations pp
              ON pp.participant_id = cp.id
            WHERE cp.id = ?
            GROUP BY cp.id, cp.name, cp.profile_payload, cp.active,
                     cp.updated_at
            """,
            (participant_id,),
        ).fetchone()
    return _common_participant_to_dict(updated)


def update_common_participants(
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    initialize_storage()
    if not updates:
        return []
    normalized_updates: list[dict[str, Any]] = []
    update_ids: list[str] = []
    for update in updates:
        participant_id = str(update.get("participant_id", "")).strip()
        if not participant_id:
            raise StorageError("参加者IDが空です。")
        if participant_id in update_ids:
            raise StorageError("同じ参加者を複数回更新できません。")
        name = str(update.get("name", "")).strip()
        if not name:
            raise StorageError("参加者名を空欄にはできません。")
        update_ids.append(participant_id)
        normalized_updates.append(
            {
                "participant_id": participant_id,
                "name": name,
                "cohort": update.get("cohort"),
                "humanities_or_science": str(
                    update.get("humanities_or_science", "") or ""
                ),
                "department": str(update.get("department", "") or ""),
                "department_detail": str(
                    update.get("department_detail", "") or ""
                ),
                "expected_updated_at": update.get("expected_updated_at"),
            }
        )

    transaction_started = time.perf_counter()
    with _connection(write=True) as connection:
        rows = connection.execute(
            """
            SELECT id, name, profile_payload, active, updated_at
            FROM common_participants
            WHERE active = 1
            """
        ).fetchall()
        row_by_id = {str(row["id"]): row for row in rows}
        for update in normalized_updates:
            row = row_by_id.get(update["participant_id"])
            if row is None:
                raise StorageError("指定した参加者が見つかりません。")
            expected_updated_at = update["expected_updated_at"]
            if (
                expected_updated_at is not None
                and str(row["updated_at"]) != str(expected_updated_at)
            ):
                raise StorageConflictError(
                    "別の利用者が参加者情報を先に更新しました。"
                    "画面を再読み込みしてください。"
                )

        desired_names = {
            str(row["id"]): str(row["name"]) for row in rows
        }
        desired_names.update(
            {
                update["participant_id"]: update["name"]
                for update in normalized_updates
            }
        )
        names_to_ids: dict[str, list[str]] = {}
        for participant_id, name in desired_names.items():
            names_to_ids.setdefault(
                participant_name_identity_key(name),
                [],
            ).append(participant_id)
        if any(len(ids) > 1 for ids in names_to_ids.values()):
            raise StorageError("同じ名前の参加者がすでに登録されています。")

        participants: list[Participant] = []
        for update in normalized_updates:
            row = row_by_id[update["participant_id"]]
            profile = _json_loads(row["profile_payload"], {})
            if not isinstance(profile, dict):
                profile = {}
            participants.append(
                Participant.from_dict(
                    {
                        **profile,
                        "id": update["participant_id"],
                        "name": update["name"],
                        "cohort": update["cohort"],
                        "humanities_or_science": update[
                            "humanities_or_science"
                        ],
                        "department": update["department"],
                        "department_detail": update["department_detail"],
                    }
                )
            )

        timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        database_started = time.perf_counter()
        _upsert_common_participants(
            connection,
            participants,
            timestamp=timestamp,
        )
        placeholders = ",".join("?" for _ in update_ids)
        project_rows = connection.execute(
            f"SELECT DISTINCT project_id FROM project_participations "
            f"WHERE participant_id IN ({placeholders})",
            tuple(update_ids),
        ).fetchall()
        project_count_rows = connection.execute(
            f"SELECT participant_id, COUNT(DISTINCT project_id) AS project_count "
            f"FROM project_participations WHERE participant_id IN ({placeholders}) "
            f"GROUP BY participant_id",
            tuple(update_ids),
        ).fetchall()
        project_count_by_id = {
            str(row["participant_id"]): int(row["project_count"])
            for row in project_count_rows
        }
        if project_rows:
            connection.execute(
                f"UPDATE project_participations SET version = version + 1, "
                f"updated_at = ? WHERE participant_id IN ({placeholders})",
                (timestamp, *update_ids),
            )
        project_ids = [str(row["project_id"]) for row in project_rows]
        database_elapsed = time.perf_counter() - database_started
        snapshot_started = time.perf_counter()
        for project_id in project_ids:
            _write_participants_snapshot(connection, project_id)
        snapshot_elapsed = time.perf_counter() - snapshot_started
        audit_started = time.perf_counter()
        _audit_many(
            connection,
            [
                (
                    "common_participant.updated",
                    None,
                    participant.id,
                    {
                        "project_count": project_count_by_id.get(
                            participant.id, 0
                        )
                    },
                )
                for participant in participants
            ],
        )
        audit_elapsed = time.perf_counter() - audit_started
        result_rows = connection.execute(
            f"""
            SELECT cp.id, cp.name, cp.profile_payload, cp.active,
                   cp.updated_at, COUNT(pp.project_id) AS project_count
            FROM common_participants cp
            LEFT JOIN project_participations pp
              ON pp.participant_id = cp.id
            WHERE cp.id IN ({placeholders})
            GROUP BY cp.id, cp.name, cp.profile_payload, cp.active,
                     cp.updated_at
            """,
            tuple(update_ids),
        ).fetchall()
    LOGGER.info(
        "common_participant_bulk_save_timing participant_count=%d "
        "project_count=%d db_update_seconds=%.4f snapshot_seconds=%.4f "
        "audit_seconds=%.4f transaction_seconds=%.4f",
        len(normalized_updates),
        len(project_ids),
        database_elapsed,
        snapshot_elapsed,
        audit_elapsed,
        time.perf_counter() - transaction_started,
    )
    result_by_id = {
        str(row["id"]): _common_participant_to_dict(row)
        for row in result_rows
    }
    return [result_by_id[participant_id] for participant_id in update_ids]


def delete_common_participant(
    participant_id: str,
    *,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    initialize_storage()
    participant_id = str(participant_id)
    with _connection(write=True) as connection:
        row = connection.execute(
            """
            SELECT id, name, updated_at
            FROM common_participants
            WHERE id = ? AND active = 1
            """,
            (participant_id,),
        ).fetchone()
        if row is None:
            raise StorageError("指定した参加者が見つかりません。")
        if (
            expected_updated_at is not None
            and str(row["updated_at"]) != str(expected_updated_at)
        ):
            raise StorageConflictError(
                "別の利用者が参加者情報を先に更新しました。"
                "画面を再読み込みしてください。"
            )
        participant_name = str(row["name"])
        project_rows = connection.execute(
            """
            SELECT pp.project_id, p.title
            FROM project_participations pp
            JOIN projects p ON p.id = pp.project_id
            WHERE pp.participant_id = ?
            ORDER BY p.sort_order, p.title
            """,
            (participant_id,),
        ).fetchall()
        blocking_projects = [
            str(project_row["title"])
            for project_row in project_rows
            if _project_has_active_participant_reference(
                connection,
                str(project_row["project_id"]),
                participant_id,
                participant_name,
            )
        ]
        if blocking_projects:
            raise StorageError(
                "確定日程で使用中のため削除できません。"
                "先に次の企画の確定日程を修正または取り消してください: "
                + "、".join(blocking_projects)
            )

        project_ids = [
            str(project_row["project_id"]) for project_row in project_rows
        ]
        connection.execute(
            "DELETE FROM memberships WHERE participant_id = ?",
            (participant_id,),
        )
        for project_id in project_ids:
            connection.execute(
                """
                DELETE FROM project_participations
                WHERE project_id = ? AND participant_id = ?
                """,
                (project_id, participant_id),
            )
            remaining = connection.execute(
                """
                SELECT participant_id
                FROM project_participations
                WHERE project_id = ?
                ORDER BY sort_order, participant_id
                """,
                (project_id,),
            ).fetchall()
            for index, remaining_row in enumerate(remaining):
                connection.execute(
                    """
                    UPDATE project_participations
                    SET sort_order = ?
                    WHERE project_id = ? AND participant_id = ?
                    """,
                    (index, project_id, remaining_row["participant_id"]),
                )
            _replace_candidate_rows(connection, project_id, [])
            _write_candidates_snapshot(connection, project_id)
            _write_participants_snapshot(connection, project_id)

        timestamp = datetime.now(timezone.utc).isoformat(
            timespec="microseconds"
        )
        parameters: tuple[Any, ...] = (timestamp, participant_id)
        where = "id = ? AND active = 1"
        if expected_updated_at is not None:
            where += " AND updated_at = ?"
            parameters += (str(expected_updated_at),)
        cursor = connection.execute(
            f"""
            UPDATE common_participants
            SET active = 0, updated_at = ?
            WHERE {where}
            """,
            parameters,
        )
        if getattr(cursor, "rowcount", 1) == 0:
            raise StorageConflictError(
                "別の利用者が参加者情報を先に更新しました。"
                "画面を再読み込みしてください。"
            )
        _audit(
            connection,
            "common_participant.deleted",
            target=participant_id,
            detail={
                "name": participant_name,
                "project_count": len(project_ids),
                "project_ids": project_ids,
            },
        )
    return {
        "participant_id": participant_id,
        "name": participant_name,
        "project_ids": project_ids,
        "project_count": len(project_ids),
    }


def add_common_participant_to_project(
    project_id: str,
    participant_id: str,
    *,
    registered_by: str = "admin",
) -> Participant:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    participant_id = str(participant_id)
    with _connection(write=True) as connection:
        existing = _participant_row(connection, project_id, participant_id)
        if existing:
            return existing
        row = _common_participant_row(connection, participant_id)
        if row is None:
            raise StorageError("指定した登録済み参加者が見つかりません。")
        participant = _participant_from_common_row(row, registered_by)
        payloads = _participant_payloads(participant)
        sort_order = connection.execute(
            """
            SELECT COALESCE(MAX(sort_order), -1) + 1
            FROM project_participations WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO project_participations
            (project_id, participant_id, sort_order, roster_payload,
             attributes_payload, requirements_payload, response_payload,
             version, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                project_id,
                participant.id,
                sort_order,
                _json_dumps(payloads["roster"]),
                _json_dumps(payloads["attributes"]),
                _json_dumps(payloads["requirements"]),
                _json_dumps(payloads["response"]),
                now_iso(),
            ),
        )
        _write_participants_snapshot(connection, project_id)
        _audit(
            connection,
            "project_participation.created",
            project_id=project_id,
            target=participant.id,
            detail={"registered_by": registered_by},
        )
    return participant


def load_cross_project_blocked_slots(
    project_id: str,
    participant_ids: list[str],
) -> dict[str, set[str]]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    target_ids = {str(participant_id) for participant_id in participant_ids}
    blocked: dict[str, set[str]] = {
        participant_id: set() for participant_id in target_ids
    }
    if not target_ids:
        return blocked
    placeholders = ",".join("?" for _ in target_ids)
    parameters = (project_id, *sorted(target_ids))
    with _connection() as connection:
        normalized_rows = connection.execute(
            f"""
            SELECT ss.session_date, ss.period, sa.participant_id
            FROM active_schedule_revisions active
            JOIN schedule_revisions revision ON revision.id = active.revision_id
            JOIN schedule_sessions ss ON ss.revision_id = revision.id
            JOIN session_assignments sa ON sa.session_id = ss.id
            LEFT JOIN documents document
              ON document.project_id = revision.project_id
             AND document.kind = 'confirmed_candidate'
            WHERE revision.project_id != ?
              AND sa.participant_id IN ({placeholders})
              AND (
                  document.updated_at IS NULL
                  OR document.updated_at <= active.activated_at
              )
            """,
            parameters,
        ).fetchall()
        legacy_rows = connection.execute(
            """
            SELECT confirmed.project_id, confirmed.payload
            FROM confirmed_candidate_data confirmed
            LEFT JOIN active_schedule_revisions active
              ON active.project_id = confirmed.project_id
            LEFT JOIN documents document
              ON document.project_id = confirmed.project_id
             AND document.kind = 'confirmed_candidate'
            WHERE confirmed.project_id != ?
              AND (
                  active.project_id IS NULL
                  OR document.updated_at > active.activated_at
              )
            """,
            (project_id,),
        ).fetchall()
    for row in normalized_rows:
        blocked[str(row["participant_id"])].add(
            make_slot_key(str(row["session_date"]), int(row["period"]))
        )
    for row in legacy_rows:
        confirmed = _json_loads(row["payload"], {})
        if not isinstance(confirmed, dict):
            continue
        for session in confirmed.get("sessions", []):
            if not isinstance(session, dict):
                continue
            try:
                slot_key = make_slot_key(
                    str(session.get("date", "")),
                    int(session.get("period", 0)),
                )
            except (TypeError, ValueError):
                continue
            member_ids = (
                list(session.get("university_role_member_ids", []) or [])
                + list(session.get("high_school_role_member_ids", []) or [])
            )
            for participant_id in member_ids:
                participant_id = str(participant_id)
                if participant_id in blocked:
                    blocked[participant_id].add(slot_key)
    return blocked


def load_participant_view_data(
    project_id: str,
    *,
    participant_id: str = "",
    include_all_participants: bool = False,
    include_confirmed: bool = False,
) -> dict[str, Any]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection() as connection:
        raw_config, config_version = _document(
            connection, project_id, "config", {}
        )
        config = Config.from_dict(raw_config if isinstance(raw_config, dict) else {})
        config.schema_version = 10
        config.project_id = project_id
        config._storage_version = config_version
        if include_all_participants or not participant_id:
            participants = _participant_rows(connection, project_id)
        else:
            participant = _participant_row(connection, project_id, participant_id)
            participants = [participant] if participant else []
        return {
            "config": config,
            "participants": participants,
            "confirmed": (
                _effective_confirmed_candidate_row(connection, project_id)
                if include_confirmed
                else None
            ),
        }


def load_participant_workspace_data(
    participant_id: str,
    allowed_project_ids: list[str],
    *,
    include_confirmed: bool = False,
) -> dict[str, dict[str, Any]]:
    """Load one participant's workspace for several projects in one session."""

    initialize_storage()
    participant_id = str(participant_id).strip()
    project_ids: list[str] = []
    for raw_project_id in allowed_project_ids:
        project_id = _validate_project_id(raw_project_id)
        if project_id not in project_ids:
            project_ids.append(project_id)
    if not participant_id or not project_ids:
        return {}

    placeholders = ",".join("?" for _ in project_ids)
    with ExitStack() as stack:
        metrics = stack.enter_context(
            measure_storage_operation(
                "participant_workspace_load",
                logger=LOGGER,
                project_count=len(project_ids),
                response_count=1,
                include_confirmed=bool(include_confirmed),
            )
        )
        connection = stack.enter_context(_connection())
        if not include_confirmed:
            rows = connection.execute(
                f"""
                SELECT d.project_id, d.payload AS config_payload,
                       d.version AS config_version,
                       (SELECT payload FROM system_settings
                        WHERE key = 'global') AS settings_payload,
                       cp.id, cp.name, cp.profile_payload,
                       pp.roster_payload, pp.attributes_payload,
                       pp.requirements_payload, pp.response_payload,
                       pp.version AS storage_version
                FROM documents d
                JOIN project_participations pp
                  ON pp.project_id = d.project_id
                 AND pp.participant_id = ?
                JOIN common_participants cp
                  ON cp.id = pp.participant_id
                WHERE d.kind = 'config'
                  AND d.project_id IN ({placeholders})
                """,
                (participant_id, *project_ids),
            ).fetchall()
            settings_payload = rows[0]["settings_payload"] if rows else None
            privacy_notice = _normalize_system_settings(
                _json_loads(settings_payload, {})
            )["privacy_notice"]
            result: dict[str, dict[str, Any]] = {}
            for row in rows:
                project_id = str(row["project_id"])
                raw_config = _json_loads(row["config_payload"], {})
                config = Config.from_dict(
                    raw_config if isinstance(raw_config, dict) else {}
                )
                config.schema_version = 10
                config.project_id = project_id
                config._storage_version = int(row["config_version"])
                participant = _participant_from_common_and_payloads(
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "profile_payload": row["profile_payload"],
                        "storage_version": row["storage_version"],
                    },
                    _json_loads(row["roster_payload"], {}),
                    _json_loads(row["attributes_payload"], {}),
                    _json_loads(row["requirements_payload"], {}),
                    _json_loads(row["response_payload"], {}),
                    project_id,
                )
                result[project_id] = {
                    "config": config,
                    "participants": [participant],
                    "confirmed": None,
                    "privacy_notice": privacy_notice,
                }
            metrics.set(response_count=len(result))
            return result

        config_rows = connection.execute(
            f"""
            SELECT project_id, payload, version,
                   (SELECT payload FROM system_settings
                    WHERE key = 'global') AS settings_payload
            FROM documents
            WHERE kind = 'config' AND project_id IN ({placeholders})
            """,
            tuple(project_ids),
        ).fetchall()
        settings_payload = (
            config_rows[0]["settings_payload"] if config_rows else None
        )
        privacy_notice = _normalize_system_settings(
            _json_loads(settings_payload, {})
        )["privacy_notice"]
        configs: dict[str, Config] = {}
        for row in config_rows:
            raw_config = _json_loads(row["payload"], {})
            config = Config.from_dict(
                raw_config if isinstance(raw_config, dict) else {}
            )
            config.schema_version = 10
            config.project_id = str(row["project_id"])
            config._storage_version = int(row["version"])
            configs[config.project_id] = config

        participant_rows = connection.execute(
            f"""
            SELECT cp.id, cp.name, cp.profile_payload,
                   pp.project_id, pp.roster_payload,
                   pp.attributes_payload, pp.requirements_payload,
                   pp.response_payload, pp.version AS storage_version
            FROM project_participations pp
            JOIN common_participants cp ON cp.id = pp.participant_id
            WHERE pp.participant_id = ?
              AND pp.project_id IN ({placeholders})
            """,
            (participant_id, *project_ids),
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in participant_rows:
            project_id = str(row["project_id"])
            config = configs.get(project_id)
            if config is None:
                continue
            participant = _participant_from_common_and_payloads(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "profile_payload": row["profile_payload"],
                    "storage_version": row["storage_version"],
                },
                _json_loads(row["roster_payload"], {}),
                _json_loads(row["attributes_payload"], {}),
                _json_loads(row["requirements_payload"], {}),
                _json_loads(row["response_payload"], {}),
                project_id,
            )
            result[project_id] = {
                "config": config,
                "participants": [participant],
                "privacy_notice": privacy_notice,
                "confirmed": (
                    _effective_confirmed_candidate_row(connection, project_id)
                    if include_confirmed
                    else None
                ),
            }
        metrics.set(response_count=len(result))
    return result


def load_project_data(project_id: str) -> dict[str, Any]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with ExitStack() as stack:
        stack.enter_context(
            measure_storage_operation(
                "project_data_load",
                logger=LOGGER,
                project_count=1,
            )
        )
        connection = stack.enter_context(_connection())
        raw_config, config_version = _document(
            connection, project_id, "config", {}
        )
        config = Config.from_dict(raw_config if isinstance(raw_config, dict) else {})
        config.schema_version = 10
        config.project_id = project_id
        config._storage_version = config_version
        candidate_version_row = connection.execute(
            "SELECT version FROM documents "
            "WHERE project_id = ? AND kind = 'candidates'",
            (project_id,),
        ).fetchone()
        return {
            "config": config,
            "participants": _participant_rows(connection, project_id),
            "candidates": _candidate_rows(connection, project_id),
            "candidates_version": (
                int(candidate_version_row["version"])
                if candidate_version_row
                else 0
            ),
            "confirmed": _effective_confirmed_candidate_row(connection, project_id),
        }


def confirm_candidate(
    project_id: str,
    candidate: dict[str, Any],
    candidate_number: int,
    *,
    project_status: str | None = None,
    expected_revision_id: str | None = None,
    return_config_version: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], int | None]:
    project_id = _validate_project_id(project_id)
    proposed_confirmed = {
        **candidate,
        "candidate_number": candidate_number,
        "confirmed_at": now_iso(),
    }
    raw_sessions = candidate.get("sessions", [])
    session_count = len(raw_sessions) if isinstance(raw_sessions, list) else 0
    assignment_count = 0
    if isinstance(raw_sessions, list):
        for session in raw_sessions:
            if not isinstance(session, dict):
                continue
            for key in (
                "university_role_member_ids",
                "high_school_role_member_ids",
            ):
                members = session.get(key, [])
                if isinstance(members, list):
                    assignment_count += len(members)
    initialize_storage()
    with ExitStack() as stack:
        metrics = stack.enter_context(
            measure_storage_operation(
                "schedule_confirm",
                logger=LOGGER,
                project_count=1,
                session_count=session_count,
                assignment_count=assignment_count,
            )
        )
        connection = stack.enter_context(_connection(write=True))
        try:
            guard_started = time.perf_counter()
            active_revision_id, guard_matches = _schedule_revision_guard_matches(
                connection, project_id, expected_revision_id
            )
            metrics.set(revision_guard_seconds=round(
                time.perf_counter() - guard_started, 6
            ))
        except ScheduleModelError as error:
            raise StorageError(str(error)) from error
        if not guard_matches:
            raise StorageConflictError(
                "別の利用者が確定日程を先に更新しました。"
                "画面を再読み込みしてください。"
            )
        current_public = (
            _load_schedule_revision(connection, active_revision_id)
            if active_revision_id
            else None
        ) or {}
        config_version: int | None = None
        try:
            current_publication_number = int(
                current_public.get("publication_number", 0)
            )
        except (TypeError, ValueError):
            current_publication_number = 0
        proposed_confirmed["publication_number"] = (
            max(0, current_publication_number) + 1
        )
        try:
            normalization_started = time.perf_counter()
            normalized_schedule = normalize_schedule(proposed_confirmed, project_id)
            metrics.set(normalization_seconds=round(
                time.perf_counter() - normalization_started, 6
            ))
            conflict_started = time.perf_counter()
            conflicts = _schedule_cross_project_conflicts(
                connection,
                project_id,
                proposed_confirmed,
                normalized_schedule=normalized_schedule,
            )
            metrics.set(conflict_check_seconds=round(
                time.perf_counter() - conflict_started, 6
            ))
            if conflicts:
                raise StorageConflictError(
                    f"他企画の確定日程と{len(conflicts)}件競合しています。"
                    "候補を再生成してください。"
                )
            revision_started = time.perf_counter()
            revision_id, confirmed = _insert_schedule_revision(
                connection,
                project_id=project_id,
                schedule=proposed_confirmed,
                source="candidate",
                actor=_audit_actor.get(),
                timestamp=proposed_confirmed["confirmed_at"],
                parent_revision_id=active_revision_id or None,
                change_note=f"候補{candidate_number}を確定",
                normalized_schedule=normalized_schedule,
            )
            metrics.set(revision_save_seconds=round(
                time.perf_counter() - revision_started, 6
            ))
        except ScheduleModelError as error:
            raise StorageError(str(error)) from error
        compatibility_started = time.perf_counter()
        _write_confirmed_candidate_row(
            connection,
            project_id,
            confirmed,
            updated_at=proposed_confirmed["confirmed_at"],
        )
        _write_document(
            connection,
            project_id,
            "confirmed_candidate",
            confirmed,
            updated_at=proposed_confirmed["confirmed_at"],
        )
        metrics.set(compatibility_save_seconds=round(
            time.perf_counter() - compatibility_started, 6
        ))
        history_audit_started = time.perf_counter()
        history, history_version = _document(
            connection, project_id, "history", []
        )
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "action": "candidate_confirmed",
                "at": confirmed["confirmed_at"],
                "actor": _audit_actor.get(),
                "candidate_number": candidate_number,
                "metrics": candidate.get("metrics", {}),
            }
        )
        _write_document(
            connection,
            project_id,
            "history",
            history,
            expected_version=history_version,
        )
        _audit(
            connection,
            "candidate.confirmed",
            project_id=project_id,
            detail={
                "candidate_number": candidate_number,
                "revision_id": revision_id,
            },
        )
        metrics.set(history_audit_seconds=round(
            time.perf_counter() - history_audit_started, 6
        ))
        if project_status is not None:
            config_started = time.perf_counter()
            config_version = _save_config_fields_in_transaction(
                connection, project_id, {"status": project_status}
            )
            metrics.set(config_update_seconds=round(
                time.perf_counter() - config_started, 6
            ))
    return (confirmed, config_version) if return_config_version else confirmed


def clear_confirmed_candidate(
    project_id: str,
    *,
    project_status: str | None = None,
    expected_revision_id: str | None = None,
) -> None:
    project_id = _validate_project_id(project_id)
    initialize_storage()
    with _connection(write=True) as connection:
        try:
            _active_revision_id, guard_matches = _schedule_revision_guard_matches(
                connection, project_id, expected_revision_id
            )
        except ScheduleModelError as error:
            raise StorageError(str(error)) from error
        if not guard_matches:
            raise StorageConflictError(
                "別の利用者が確定日程を先に更新しました。"
                "画面を再読み込みしてください。"
            )
        connection.execute(
            "DELETE FROM active_schedule_revisions WHERE project_id = ?",
            (project_id,),
        )
        _write_confirmed_candidate_row(connection, project_id, None)
        _write_document(connection, project_id, "confirmed_candidate", None)
        _audit(connection, "candidate.unconfirmed", project_id=project_id)
        if project_status is not None:
            _save_config_fields_in_transaction(
                connection, project_id, {"status": project_status}
            )


def reset_project_data(
    project_id: str,
    *,
    reset_availability: bool = False,
    reset_candidates: bool = False,
    reset_all: bool = False,
) -> None:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    with _connection(write=True) as connection:
        if reset_all:
            raw_config, _config_version = _document(
                connection, project_id, "config", {}
            )
            current_config = Config.from_dict(
                raw_config if isinstance(raw_config, dict) else {}
            )
            new_config = Config(
                schema_version=10,
                project_id=project_id,
                title=current_config.title,
                status="draft",
            )
            _write_document(
                connection, project_id, "config", new_config.to_dict()
            )
            connection.execute(
                """
                UPDATE projects
                SET title = ?, status = 'draft', updated_at = ?
                WHERE id = ?
                """,
                (new_config.title[:120], now_iso(), project_id),
            )
            connection.execute(
                "DELETE FROM project_participations WHERE project_id = ?",
                (project_id,),
            )
            connection.execute(
                "DELETE FROM memberships WHERE project_id = ? AND role = 'participant'",
                (project_id,),
            )
            _write_participants_snapshot(connection, project_id)
            _replace_candidate_rows(connection, project_id, [])
            _write_document(connection, project_id, "candidates", [])
            connection.execute(
                "DELETE FROM active_schedule_revisions WHERE project_id = ?",
                (project_id,),
            )
            _write_confirmed_candidate_row(connection, project_id, None)
            _write_document(connection, project_id, "confirmed_candidate", None)
            _write_document(
                connection,
                project_id,
                "schedule_amendments",
                empty_amendment_workspace(),
            )
        elif reset_availability:
            response_payload = _json_dumps(
                _combined_response_payload(
                    {
                        "availability": [],
                        "zoom_availability": [],
                        "support_requested_count": None,
                        "submitted_at": "",
                        "input_status": "not_started",
                        "updated_at": now_iso(),
                    }
                )
            )
            connection.execute(
                """
                UPDATE project_participations
                SET response_payload = ?, version = version + 1, updated_at = ?
                WHERE project_id = ?
                """,
                (response_payload, now_iso(), project_id),
            )
            _write_participants_snapshot(connection, project_id)
        elif reset_candidates:
            _replace_candidate_rows(connection, project_id, [])
            _write_document(connection, project_id, "candidates", [])
            connection.execute(
                "DELETE FROM active_schedule_revisions WHERE project_id = ?",
                (project_id,),
            )
            _write_confirmed_candidate_row(connection, project_id, None)
            _write_document(connection, project_id, "confirmed_candidate", None)
        _audit(
            connection,
            "project.reset",
            project_id=project_id,
            detail={
                "availability": reset_availability,
                "candidates": reset_candidates,
                "all": reset_all,
            },
        )


def export_all_data(project_id: str) -> dict[str, Any]:
    project_id = _validate_project_id(project_id)
    return {
        "config": load_config(project_id).to_dict(),
        "participants": [
            participant.to_dict() for participant in load_participants(project_id)
        ],
        "candidates": load_candidates(project_id),
        "confirmed_candidate": load_confirmed_candidate(project_id),
        "exported_at": now_iso(),
    }


def record_audit_event(
    action: str,
    *,
    project_id: str | None = None,
    target: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    initialize_storage()
    normalized_project_id = (
        _validate_project_id(project_id) if project_id else None
    )
    with _connection(write=True) as connection:
        _audit(
            connection,
            action,
            project_id=normalized_project_id,
            target=target,
            detail=detail,
        )


def list_audit_logs(limit: int = 500) -> list[dict[str, Any]]:
    initialize_storage()
    with _connection() as connection:
        rows = connection.execute(
            """
            SELECT actor, action, project_id, target, detail, created_at
            FROM audit_logs ORDER BY id DESC LIMIT ?
            """,
            (max(1, min(5000, limit)),),
        ).fetchall()
    return [
        {
            **dict(row),
            "detail": _json_loads(row["detail"], {}),
        }
        for row in rows
    ]


def list_backups(project_id: str) -> list[dict[str, Any]]:
    project_id = _validate_project_id(project_id)
    initialize_storage()
    with _connection() as connection:
        rows = connection.execute(
            """
            SELECT id, kind, version, created_at
            FROM backups WHERE project_id = ? ORDER BY id DESC
            """,
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def restore_backup(
    backup_id: int,
    *,
    expected_revision_id: str | None = None,
) -> None:
    initialize_storage()
    with _connection(write=True) as connection:
        row = connection.execute(
            "SELECT project_id, kind, payload FROM backups WHERE id = ?",
            (int(backup_id),),
        ).fetchone()
        if not row:
            raise StorageError("指定したバックアップが見つかりません。")
        value = _json_loads(row["payload"], None)
        if row["kind"] == "participants" and isinstance(value, list):
            restored_participants = [
                Participant.from_dict(item)
                for item in value
                if isinstance(item, dict)
            ]
            _replace_participant_rows(
                connection,
                row["project_id"],
                restored_participants,
            )
            value = [participant.to_dict() for participant in restored_participants]
        elif row["kind"] == "candidates" and isinstance(value, list):
            _replace_candidate_rows(
                connection,
                row["project_id"],
                [item for item in value if isinstance(item, dict)],
            )
        elif row["kind"] == "confirmed_candidate":
            project_id = str(row["project_id"])
            try:
                active_revision_id, guard_matches = (
                    _schedule_revision_guard_matches(
                        connection, project_id, expected_revision_id
                    )
                )
            except ScheduleModelError as error:
                raise StorageError(str(error)) from error
            effective_expected_revision_id = (
                active_revision_id or ""
                if expected_revision_id is None
                else expected_revision_id
            )
            if not guard_matches:
                raise StorageConflictError(
                    "別の利用者が確定日程を先に更新しました。"
                    "画面を再読み込みしてください。"
                )
            if isinstance(value, dict):
                restored_schedule = deepcopy(value)
                restored_schedule.pop("schedule_revision", None)
                _save_schedule_revision_in_transaction(
                    connection,
                    project_id,
                    restored_schedule,
                    source="restore",
                    change_note=f"バックアップ#{int(backup_id)}を復元",
                    expected_revision_id=effective_expected_revision_id,
                    project_status="confirmed",
                )
            else:
                connection.execute(
                    "DELETE FROM active_schedule_revisions WHERE project_id = ?",
                    (project_id,),
                )
                _write_confirmed_candidate_row(connection, project_id, None)
                _write_document(connection, project_id, "confirmed_candidate", None)
        else:
            _write_document(connection, row["project_id"], row["kind"], value)
        _audit(
            connection,
            "backup.restored",
            project_id=row["project_id"],
            detail={"backup_id": backup_id, "kind": row["kind"]},
        )


def acquire_job_lock(project_id: str, owner: str, seconds: int) -> bool:
    project_id = _validate_project_id(project_id)
    initialize_storage()
    now = datetime.now(timezone.utc)
    until = now + timedelta(seconds=max(1, seconds))
    with _connection(write=True) as connection:
        connection.execute(
            "DELETE FROM job_locks WHERE locked_until <= ?",
            (now.isoformat(),),
        )
        try:
            connection.execute(
                """
                INSERT INTO job_locks(project_id, locked_by, locked_until)
                VALUES (?, ?, ?)
                """,
                (project_id, owner[:120], until.isoformat()),
            )
        except ValueError:
            return False
        _audit(connection, "search.locked", project_id=project_id)
    return True


def release_job_lock(project_id: str, owner: str) -> None:
    project_id = _validate_project_id(project_id)
    with _connection(write=True) as connection:
        connection.execute(
            "DELETE FROM job_locks WHERE project_id = ? AND locked_by = ?",
            (project_id, owner[:120]),
        )


def cleanup_retention() -> None:
    with _connection(write=True) as connection:
        _cleanup_retention_in_transaction(connection)


def create_user(
    username: str,
    password_hash: str,
    *,
    is_system_admin: bool = False,
    is_schedule_manager: bool = False,
    is_participant: bool = False,
    password_plain: str = "",
    password_source: str = "",
) -> str:
    initialize_storage()
    normalized_username = username.strip()[:120]
    if not normalized_username:
        raise StorageError("ユーザー名を入力してください。")
    if not password_hash:
        raise StorageError("パスワードハッシュが空です。")
    try:
        password_secret = (
            encrypt_password_secret(password_plain) if password_plain else None
        )
    except PasswordSecretError as error:
        raise StorageError(str(error)) from error
    user_id = uuid4().hex
    timestamp = now_iso()
    with _connection(write=True) as connection:
        try:
            connection.execute(
                """
                INSERT INTO users
                (id, username, password_hash, is_system_admin,
                 is_schedule_manager, is_participant, active, created_at,
                 password_secret, password_source, password_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    normalized_username,
                    password_hash,
                    int(is_system_admin),
                    int(is_schedule_manager),
                    int(is_participant),
                    timestamp,
                    password_secret,
                    password_source or None,
                    timestamp if password_plain else None,
                ),
            )
        except ValueError as error:
            raise StorageError("同じユーザー名が既に存在します。") from error
        _audit(connection, "user.created", target=user_id)
    return user_id


def bulk_create_participant_users(
    project_id: str,
    accounts: list[dict[str, str]],
) -> list[dict[str, str]]:
    initialize_storage()
    project_id = _validate_project_id(project_id)
    created: list[dict[str, str]] = []
    timestamp = now_iso()
    with _connection(write=True) as connection:
        for account in accounts:
            username = str(account.get("username", "")).strip()[:120]
            password_hash = str(account.get("password_hash", ""))
            password_plain = str(account.get("password_plain", ""))
            participant_id = str(account.get("participant_id", ""))
            account_source = str(account.get("account_source", "") or "")
            password_source = str(
                account.get("password_source", "") or account_source
            )
            if not username:
                raise StorageError("ユーザー名を入力してください。")
            if not password_hash:
                raise StorageError("パスワードハッシュが空です。")
            if not participant_id:
                raise StorageError("参加者IDが空です。")
            try:
                password_secret = (
                    encrypt_password_secret(password_plain)
                    if password_plain
                    else None
                )
            except PasswordSecretError as error:
                raise StorageError(str(error)) from error
            user_id = uuid4().hex
            try:
                connection.execute(
                    """
                    INSERT INTO users
                    (id, username, password_hash, is_system_admin,
                     is_schedule_manager, is_participant, active, created_at,
                     password_secret, password_source, password_updated_at)
                    VALUES (?, ?, ?, 0, 0, 0, 1, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        username,
                        password_hash,
                        timestamp,
                        password_secret,
                        password_source or None,
                        timestamp if password_plain else None,
                    ),
                )
            except Exception as error:
                raise StorageError("同じユーザー名が既に存在します。") from error
            connection.execute(
                """
                INSERT INTO memberships
                (user_id, project_id, role, participant_id, account_source)
                VALUES (?, ?, 'participant', ?, ?)
                ON CONFLICT(user_id, project_id, role) DO UPDATE SET
                    participant_id = excluded.participant_id,
                    account_source = excluded.account_source
                """,
                (user_id, project_id, participant_id, account_source or None),
            )
            created.append({**account, "user_id": user_id, "username": username})
        _audit(
            connection,
            "participant_users.bulk_created",
            project_id=project_id,
            detail={"count": len(created)},
        )
    return created


def update_user(
    user_id: str,
    *,
    is_system_admin: bool,
    active: bool,
    is_schedule_manager: bool = False,
    is_participant: bool | None = None,
    password_hash: str | None = None,
    password_plain: str | None = None,
    password_source: str = "",
) -> None:
    initialize_storage()
    with _connection(write=True) as connection:
        current = connection.execute(
            """
            SELECT is_system_admin, is_schedule_manager, is_participant, active
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if not current:
            raise StorageError("指定したアカウントが見つかりません。")
        removes_active_admin = (
            bool(current["is_system_admin"])
            and bool(current["active"])
            and (not is_system_admin or not active)
        )
        if removes_active_admin:
            other_admins = connection.execute(
                """
                SELECT COUNT(*) FROM users
                WHERE id <> ? AND is_system_admin = 1 AND active = 1
                """,
                (user_id,),
            ).fetchone()[0]
            if not other_admins:
                raise StorageError(
                    "最後の有効なシステム管理者は停止・降格できません。"
                )
        values: list[Any] = [
            int(is_system_admin),
            int(is_schedule_manager),
            int(
                bool(current["is_participant"])
                if is_participant is None
                else is_participant
            ),
            int(active),
        ]
        password_clause = ""
        if password_hash:
            try:
                password_secret = (
                    encrypt_password_secret(password_plain)
                    if password_plain
                    else None
                )
            except PasswordSecretError as error:
                raise StorageError(str(error)) from error
            password_clause = (
                ", password_hash = ?, password_secret = ?, password_plain = NULL,"
                " password_source = ?, password_updated_at = ?"
            )
            values.extend([password_hash, password_secret, password_source or None, now_iso()])
        values.append(user_id)
        connection.execute(
            f"""
            UPDATE users
            SET is_system_admin = ?, is_schedule_manager = ?,
                is_participant = ?,
                active = ? {password_clause}
            WHERE id = ?
            """,
            values,
        )
        _audit(
            connection,
            "user.updated",
            target=user_id,
            detail={
                "is_system_admin": is_system_admin,
                "is_schedule_manager": is_schedule_manager,
                "is_participant": (
                    bool(current["is_participant"])
                    if is_participant is None
                    else is_participant
                ),
                "active": active,
                "password_changed": bool(password_hash),
            },
        )


def bulk_update_user_passwords(
    updates: list[dict[str, str]],
    *,
    password_source: str = "",
) -> int:
    initialize_storage()
    if not updates:
        return 0
    timestamp = now_iso()
    with _connection(write=True) as connection:
        updated_count = 0
        for update in updates:
            user_id = str(update.get("user_id", ""))
            password_hash = str(update.get("password_hash", ""))
            password_plain = str(update.get("password_plain", ""))
            if not user_id:
                raise StorageError("指定したアカウントが見つかりません。")
            if not password_hash:
                raise StorageError("パスワードハッシュが空です。")
            try:
                password_secret = (
                    encrypt_password_secret(password_plain)
                    if password_plain
                    else None
                )
            except PasswordSecretError as error:
                raise StorageError(str(error)) from error
            updated = connection.execute(
                """
                UPDATE users
                SET password_hash = ?,
                    password_secret = ?,
                    password_plain = NULL,
                    password_source = ?,
                    password_updated_at = ?
                WHERE id = ?
                """,
                (
                    password_hash,
                    password_secret,
                    str(update.get("password_source") or password_source) or None,
                    timestamp,
                    user_id,
                ),
            ).rowcount
            if not updated:
                raise StorageError("指定したアカウントが見つかりません。")
            updated_count += int(updated)
        _audit(
            connection,
            "users.passwords.bulk_updated",
            detail={"count": updated_count},
        )
    return updated_count


def delete_user(user_id: str) -> None:
    initialize_storage()
    with _connection(write=True) as connection:
        current = connection.execute(
            """
            SELECT is_system_admin, active FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if not current:
            raise StorageError("指定したアカウントが見つかりません。")
        if bool(current["is_system_admin"]) and bool(current["active"]):
            other_admins = connection.execute(
                """
                SELECT COUNT(*) FROM users
                WHERE id <> ? AND is_system_admin = 1 AND active = 1
                """,
                (user_id,),
            ).fetchone()[0]
            if not other_admins:
                raise StorageError("最後の有効なシステム管理者は削除できません。")
        connection.execute("DELETE FROM memberships WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
        _audit(connection, "user.deleted", target=user_id)


def bulk_delete_users(user_ids: list[str]) -> int:
    initialize_storage()
    normalized_ids = [str(user_id) for user_id in user_ids if str(user_id)]
    if not normalized_ids:
        return 0
    with _connection(write=True) as connection:
        placeholders = ",".join("?" for _ in normalized_ids)
        rows = connection.execute(
            f"""
            SELECT id, is_system_admin, active
            FROM users
            WHERE id IN ({placeholders})
            """,
            normalized_ids,
        ).fetchall()
        if len(rows) != len(set(normalized_ids)):
            raise StorageError("指定したアカウントが見つかりません。")
        removes_active_admin = any(
            bool(row["is_system_admin"]) and bool(row["active"]) for row in rows
        )
        if removes_active_admin:
            other_admins = connection.execute(
                f"""
                SELECT COUNT(*) FROM users
                WHERE id NOT IN ({placeholders})
                  AND is_system_admin = 1
                  AND active = 1
                """,
                normalized_ids,
            ).fetchone()[0]
            if not other_admins:
                raise StorageError("最後の有効なシステム管理者は削除できません。")
        connection.execute(
            f"DELETE FROM memberships WHERE user_id IN ({placeholders})",
            normalized_ids,
        )
        deleted = connection.execute(
            f"DELETE FROM users WHERE id IN ({placeholders})",
            normalized_ids,
        ).rowcount
        _audit(
            connection,
            "users.bulk_deleted",
            detail={"count": int(deleted)},
        )
    return int(deleted)


def get_user_by_username(username: str) -> dict[str, Any] | None:
    initialize_storage()
    with _connection() as connection:
        row = connection.execute(
            """
            SELECT id, username, password_hash, is_system_admin,
                   is_schedule_manager, is_participant, active, created_at,
                   last_login_at, password_plain, password_secret,
                   password_source, password_updated_at
            FROM users WHERE username = ? COLLATE NOCASE
            """,
            (username.strip(),),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["is_system_admin"] = bool(result["is_system_admin"])
    result["is_schedule_manager"] = bool(result["is_schedule_manager"])
    result["is_participant"] = bool(result["is_participant"])
    result["active"] = bool(result["active"])
    return result


def load_login_snapshot(username: str) -> dict[str, Any] | None:
    """Load authentication data and maintenance state in one read connection."""

    initialize_storage()
    with _connection() as connection:
        rows = connection.execute(
            """
            SELECT u.id, u.username, u.password_hash,
                   u.is_system_admin, u.is_schedule_manager, u.is_participant,
                   u.active, u.created_at, u.last_login_at,
                   u.password_plain, u.password_secret,
                   u.password_source, u.password_updated_at,
                   m.project_id AS membership_project_id,
                   m.role AS membership_role,
                   m.participant_id AS membership_participant_id,
                   settings.payload AS settings_payload
            FROM users u
            LEFT JOIN memberships m ON m.user_id = u.id
            LEFT JOIN system_settings settings ON settings.key = 'global'
            WHERE u.username = ? COLLATE NOCASE
            ORDER BY m.id
            """,
            (username.strip(),),
        ).fetchall()
    if not rows:
        return None
    first = rows[0]
    user = {
        "id": first["id"],
        "username": first["username"],
        "password_hash": first["password_hash"],
        "is_system_admin": bool(first["is_system_admin"]),
        "is_schedule_manager": bool(first["is_schedule_manager"]),
        "is_participant": bool(first["is_participant"]),
        "active": bool(first["active"]),
        "created_at": first["created_at"],
        "last_login_at": first["last_login_at"],
        "password_plain": first["password_plain"],
        "password_secret": first["password_secret"],
        "password_source": first["password_source"],
        "password_updated_at": first["password_updated_at"],
    }
    memberships: list[dict[str, Any]] = []
    seen_memberships: set[tuple[str, str, str]] = set()
    for row in rows:
        project_id = row["membership_project_id"]
        role = row["membership_role"]
        if project_id is None or role is None:
            continue
        key = (str(project_id), str(role), str(row["membership_participant_id"] or ""))
        if key in seen_memberships:
            continue
        seen_memberships.add(key)
        memberships.append(
            {
                "project_id": str(project_id),
                "role": str(role),
                "participant_id": row["membership_participant_id"],
            }
        )
    settings_payload = first["settings_payload"]
    return {
        "user": user,
        "memberships": memberships,
        "system_settings": _normalize_system_settings(
            _json_loads(settings_payload, {})
        ),
    }


def _decrypted_password(row: Any) -> str:
    try:
        if row["password_secret"]:
            return decrypt_password_secret(str(row["password_secret"]))
    except (KeyError, PasswordSecretError):
        return ""
    try:
        return str(row["password_plain"] or "")
    except (KeyError, TypeError):
        return ""


def list_users() -> list[dict[str, Any]]:
    initialize_storage()
    with _connection() as connection:
        rows = connection.execute(
            """
            SELECT id, username, is_system_admin, is_schedule_manager,
                   is_participant, active, created_at, last_login_at,
                   password_plain, password_secret,
                   password_source, password_updated_at
            FROM users ORDER BY username COLLATE NOCASE
            """
        ).fetchall()
    return [
        {
            **dict(row),
            "password_plain": _decrypted_password(row),
            "is_system_admin": bool(row["is_system_admin"]),
            "is_schedule_manager": bool(row["is_schedule_manager"]),
            "is_participant": bool(row["is_participant"]),
            "active": bool(row["active"]),
        }
        for row in rows
    ]


def record_login(user_id: str) -> None:
    with _connection(write=True) as connection:
        connection.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (now_iso(), user_id),
        )
        _audit(connection, "user.logged_in", target=user_id)


def assign_membership(
    user_id: str,
    project_id: str,
    role: str,
    participant_id: str = "",
    account_source: str = "",
) -> None:
    if role not in {"manager", "participant"}:
        raise StorageError("権限種別が不正です。")
    project_id = _validate_project_id(project_id)
    initialize_storage()
    with _connection(write=True) as connection:
        connection.execute(
            """
            INSERT INTO memberships
            (user_id, project_id, role, participant_id, account_source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, project_id, role) DO UPDATE SET
                participant_id = excluded.participant_id,
                account_source = COALESCE(excluded.account_source, account_source)
            """,
            (user_id, project_id, role, participant_id or None, account_source or None),
        )
        _audit(
            connection,
            "membership.assigned",
            project_id=project_id,
            target=user_id,
            detail={"role": role, "participant_id": participant_id},
        )


def remove_membership(user_id: str, project_id: str, role: str) -> None:
    if role not in {"manager", "participant"}:
        raise StorageError("権限種別が不正です。")
    project_id = _validate_project_id(project_id)
    initialize_storage()
    with _connection(write=True) as connection:
        deleted = connection.execute(
            """
            DELETE FROM memberships
            WHERE user_id = ? AND project_id = ? AND role = ?
            """,
            (user_id, project_id, role),
        ).rowcount
        if not deleted:
            raise StorageError("指定した企画内権限が見つかりません。")
        _audit(
            connection,
            "membership.removed",
            project_id=project_id,
            target=user_id,
            detail={"role": role},
        )


def memberships_for_user(user_id: str) -> list[dict[str, Any]]:
    initialize_storage()
    with _connection() as connection:
        rows = connection.execute(
            """
            SELECT project_id, role, participant_id
            FROM memberships WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def all_memberships() -> list[dict[str, Any]]:
    initialize_storage()
    with _connection() as connection:
        rows = connection.execute(
            """
            SELECT m.user_id, u.username, m.project_id, p.title AS project_title,
                   m.role, m.participant_id, m.account_source,
                   u.password_plain, u.password_secret,
                   u.password_source, u.password_updated_at
            FROM memberships m
            JOIN users u ON u.id = m.user_id
            JOIN projects p ON p.id = m.project_id
            ORDER BY u.username, p.title, m.role
            """
        ).fetchall()
    return [
        {**dict(row), "password_plain": _decrypted_password(row)}
        for row in rows
    ]


def database_fingerprint() -> str:
    return hashlib.sha256(_required_env("TURSO_DATABASE_URL").encode()).hexdigest()


def database_identifier() -> str:
    """Return a stable identifier without exposing the database URL."""

    return hashlib.sha256(_required_env("TURSO_DATABASE_URL").encode()).hexdigest()
