from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from uuid import uuid4

from schedule_adjustment_tool.domain.schedule_model import (
    ROLE_FIELDS,
    ScheduleModelError,
    normalize_schedule,
    schedule_metadata,
)


CONFIRMED_SCHEDULE_MIGRATION = "confirmed_schedule_v1"


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except json.JSONDecodeError as error:
        raise ScheduleModelError("日程revisionのJSONが破損しています。") from error


def _candidate_number(schedule: dict[str, Any]) -> int | None:
    try:
        value = int(schedule.get("candidate_number"))
    except (TypeError, ValueError):
        return None
    return value if value >= 1 else None


def _execute_multirow_insert(
    connection: Any,
    statement: str,
    rows: list[tuple[Any, ...]],
) -> None:
    """Insert revision child rows in bounded batches for SQLite and Turso."""

    if not rows:
        return
    row_placeholder = "(" + ", ".join("?" for _ in rows[0]) + ")"
    for offset in range(0, len(rows), 64):
        chunk = rows[offset : offset + 64]
        placeholders = ", ".join(row_placeholder for _ in chunk)
        parameters = tuple(value for row in chunk for value in row)
        connection.execute(
            f"{statement} VALUES {placeholders}",
            parameters,
        )


def insert_revision(
    connection: Any,
    *,
    project_id: str,
    schedule: dict[str, Any],
    source: str,
    actor: str,
    timestamp: str,
    parent_revision_id: str | None = None,
    change_note: str = "",
    migration_id: str | None = None,
    activate: bool = True,
    normalized_schedule: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    normalized = normalized_schedule or normalize_schedule(schedule, project_id)
    revision_id = uuid4().hex
    revision_number = int(
        connection.execute(
            "SELECT COALESCE(MAX(revision_number), 0) + 1 "
            "FROM schedule_revisions WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO schedule_revisions
        (id, project_id, revision_number, source, parent_revision_id,
         candidate_number, change_note, metadata_payload, migration_id,
         created_at, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision_id,
            project_id,
            revision_number,
            source[:40],
            parent_revision_id,
            _candidate_number(normalized),
            change_note[:1000],
            _dumps(schedule_metadata(normalized)),
            migration_id,
            timestamp,
            actor[:120],
        ),
    )
    session_rows: list[tuple[Any, ...]] = []
    assignment_rows: list[tuple[Any, ...]] = []
    for session_order, session in enumerate(normalized["sessions"]):
        session_row_id = uuid4().hex
        session_rows.append(
            (
                session_row_id,
                revision_id,
                str(session["session_id"]),
                session_order,
                str(session["date"]),
                int(session["period"]),
                int(session["group_index"]),
                str(session["meeting_mode"]),
                _dumps(session),
            ),
        )
        for role, ids_field, names_field in ROLE_FIELDS:
            member_ids = list(session.get(ids_field, []))
            member_names = list(session.get(names_field, []))
            for assignment_order, participant_id in enumerate(member_ids):
                participant_name = (
                    str(member_names[assignment_order])
                    if assignment_order < len(member_names)
                    else ""
                )
                assignment_rows.append(
                    (
                        session_row_id,
                        str(participant_id),
                        role,
                        assignment_order,
                        participant_name,
                        "{}",
                    ),
                )
    _execute_multirow_insert(
        connection,
        """
        INSERT INTO schedule_sessions
        (id, revision_id, session_uid, session_order, session_date,
         period, group_index, meeting_mode, payload)
        """,
        session_rows,
    )
    _execute_multirow_insert(
        connection,
        """
        INSERT INTO session_assignments
        (session_id, participant_id, role, assignment_order,
         participant_name, payload)
        """,
        assignment_rows,
    )
    if activate:
        connection.execute(
            """
            INSERT INTO active_schedule_revisions
            (project_id, revision_id, activated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                revision_id = excluded.revision_id,
                activated_at = excluded.activated_at
            """,
            (project_id, revision_id, timestamp),
        )
    normalized["schedule_revision"] = {
        "id": revision_id,
        "number": revision_number,
        "source": source,
        "parent_revision_id": parent_revision_id,
        "change_note": change_note,
        "created_at": timestamp,
        "created_by": actor,
    }
    return revision_id, normalized


def load_revision(connection: Any, revision_id: str) -> dict[str, Any] | None:
    revision = connection.execute(
        """
        SELECT id, project_id, revision_number, source, parent_revision_id,
               candidate_number, change_note, metadata_payload,
               created_at, created_by
        FROM schedule_revisions WHERE id = ?
        """,
        (revision_id,),
    ).fetchone()
    if revision is None:
        return None
    metadata = _loads(revision["metadata_payload"], {})
    if not isinstance(metadata, dict):
        raise ScheduleModelError("日程revisionのmetadata形式が不正です。")
    session_rows = connection.execute(
        """
        SELECT id, session_uid, session_order, session_date, period,
               group_index, meeting_mode, payload
        FROM schedule_sessions
        WHERE revision_id = ?
        ORDER BY session_order, id
        """,
        (revision_id,),
    ).fetchall()
    assignment_rows = connection.execute(
        """
        SELECT sa.session_id, sa.participant_id, sa.role,
               sa.assignment_order, sa.participant_name
        FROM session_assignments sa
        JOIN schedule_sessions ss ON ss.id = sa.session_id
        WHERE ss.revision_id = ?
        ORDER BY ss.session_order, sa.role, sa.assignment_order
        """,
        (revision_id,),
    ).fetchall()
    assignments_by_session: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for assignment in assignment_rows:
        assignments_by_session.setdefault(
            str(assignment["session_id"]),
            {"university": [], "high_school": []},
        )[str(assignment["role"])].append(
            (
                str(assignment["participant_id"]),
                str(assignment["participant_name"]),
            )
        )
    sessions: list[dict[str, Any]] = []
    for row in session_rows:
        payload = _loads(row["payload"], {})
        session = payload if isinstance(payload, dict) else {}
        session.update(
            {
                "session_id": str(row["session_uid"]),
                "date": str(row["session_date"]),
                "period": int(row["period"]),
                "group_index": int(row["group_index"]),
                "meeting_mode": str(row["meeting_mode"]),
            }
        )
        role_assignments = assignments_by_session.get(
            str(row["id"]), {"university": [], "high_school": []}
        )
        for role, ids_field, names_field in ROLE_FIELDS:
            pairs = role_assignments.get(role, [])
            session[ids_field] = [participant_id for participant_id, _ in pairs]
            session[names_field] = [name for _, name in pairs]
        sessions.append(session)
    result = deepcopy(metadata)
    result["sessions"] = sessions
    result["schedule_revision"] = {
        "id": str(revision["id"]),
        "number": int(revision["revision_number"]),
        "source": str(revision["source"]),
        "parent_revision_id": revision["parent_revision_id"],
        "change_note": str(revision["change_note"]),
        "created_at": str(revision["created_at"]),
        "created_by": str(revision["created_by"]),
    }
    return result


def load_active_revision(connection: Any, project_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT revision_id FROM active_schedule_revisions WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return load_revision(connection, str(row["revision_id"])) if row else None


def active_revision_pointer(
    connection: Any,
    project_id: str,
) -> tuple[str, str]:
    row = connection.execute(
        "SELECT revision_id, activated_at FROM active_schedule_revisions "
        "WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        return "", ""
    return str(row["revision_id"]), str(row["activated_at"])


def _newer_confirmed_document(
    connection: Any,
    project_id: str,
    activated_at: str,
) -> tuple[bool, Any]:
    document = connection.execute(
        "SELECT payload, updated_at FROM documents "
        "WHERE project_id = ? AND kind = 'confirmed_candidate'",
        (project_id,),
    ).fetchone()
    if document is None or str(document["updated_at"]) <= activated_at:
        return False, None
    return True, _loads(document["payload"], None)


def load_effective_schedule(
    connection: Any,
    project_id: str,
    legacy_schedule: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Prefer newer legacy writes during a mixed-version rollback window."""

    revision_id, activated_at = active_revision_pointer(connection, project_id)
    if not revision_id:
        return legacy_schedule
    active = load_revision(connection, revision_id)
    has_override, override = _newer_confirmed_document(
        connection, project_id, activated_at
    )
    if not has_override:
        return active if active is not None else legacy_schedule
    if override is None:
        return None
    if not isinstance(override, dict):
        raise ScheduleModelError("旧形式の確定日程データが不正です。")
    effective = deepcopy(override)
    if active is not None and not isinstance(
        effective.get("schedule_revision"), dict
    ):
        effective["schedule_revision"] = {
            **active.get("schedule_revision", {}),
            "compatibility_source": "newer_legacy_document",
        }
    return effective


def empty_revision_guard_matches_legacy_clear(
    connection: Any,
    project_id: str,
    activated_at: str,
) -> bool:
    has_override, override = _newer_confirmed_document(
        connection, project_id, activated_at
    )
    return has_override and override is None


def revision_guard_matches(
    connection: Any,
    project_id: str,
    expected_revision_id: str | None,
) -> tuple[str, bool]:
    revision_id, activated_at = active_revision_pointer(connection, project_id)
    if expected_revision_id is None or str(expected_revision_id) == revision_id:
        return revision_id, True
    if (
        str(expected_revision_id) == ""
        and revision_id
        and empty_revision_guard_matches_legacy_clear(
            connection, project_id, activated_at
        )
    ):
        return revision_id, True
    return revision_id, False


def cross_project_conflicts(
    connection: Any,
    project_id: str,
    schedule: dict[str, Any],
    *,
    normalized_schedule: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    normalized = normalized_schedule or normalize_schedule(schedule, project_id)
    requested: set[tuple[str, str, int]] = set()
    for session in normalized["sessions"]:
        for _role, ids_field, _names_field in ROLE_FIELDS:
            requested.update(
                (
                    str(participant_id),
                    str(session["date"]),
                    int(session["period"]),
                )
                for participant_id in session.get(ids_field, [])
                if not str(participant_id).startswith("unresolved-")
            )
    if not requested:
        return []
    participant_ids = sorted({item[0] for item in requested})
    placeholders = ",".join("?" for _ in participant_ids)
    rows = connection.execute(
        f"""
        SELECT revision.project_id, ss.session_date, ss.period,
               sa.participant_id
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
        (project_id, *participant_ids),
    ).fetchall()
    conflicts = [
        {
            "project_id": str(row["project_id"]),
            "participant_id": str(row["participant_id"]),
            "date": str(row["session_date"]),
            "period": int(row["period"]),
        }
        for row in rows
        if (
            str(row["participant_id"]),
            str(row["session_date"]),
            int(row["period"]),
        )
        in requested
    ]
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
    for row in legacy_rows:
        legacy = _loads(row["payload"], {})
        if not isinstance(legacy, dict):
            continue
        for session in legacy.get("sessions", []):
            if not isinstance(session, dict):
                continue
            try:
                day_text = str(session["date"])
                period = int(session["period"])
            except (KeyError, TypeError, ValueError):
                continue
            member_ids = list(session.get("university_role_member_ids", [])) + list(
                session.get("high_school_role_member_ids", [])
            )
            for participant_id in member_ids:
                key = (str(participant_id), day_text, period)
                if key in requested:
                    conflicts.append(
                        {
                            "project_id": str(row["project_id"]),
                            "participant_id": key[0],
                            "date": day_text,
                            "period": period,
                        }
                    )
    unique = {
        (
            conflict["project_id"],
            conflict["participant_id"],
            conflict["date"],
            conflict["period"],
        ): conflict
        for conflict in conflicts
    }
    return list(unique.values())


def list_revisions(connection: Any, project_id: str) -> list[dict[str, Any]]:
    active = connection.execute(
        "SELECT revision_id FROM active_schedule_revisions WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    active_id = str(active["revision_id"]) if active else ""
    rows = connection.execute(
        """
        SELECT r.id, r.revision_number, r.source, r.parent_revision_id,
               r.candidate_number, r.change_note, r.metadata_payload,
               r.created_at, r.created_by,
               COUNT(s.id) AS session_count
        FROM schedule_revisions r
        LEFT JOIN schedule_sessions s ON s.revision_id = r.id
        WHERE r.project_id = ?
        GROUP BY r.id, r.revision_number, r.source, r.parent_revision_id,
                 r.candidate_number, r.change_note, r.created_at, r.created_by
        ORDER BY r.revision_number DESC
        """,
        (project_id,),
    ).fetchall()
    return [
        {
            "id": str(row["id"]),
            "revision_number": int(row["revision_number"]),
            "source": str(row["source"]),
            "parent_revision_id": row["parent_revision_id"],
            "candidate_number": row["candidate_number"],
            "change_note": str(row["change_note"]),
            "metadata": (
                _loads(row["metadata_payload"], {})
                if isinstance(_loads(row["metadata_payload"], {}), dict)
                else {}
            ),
            "created_at": str(row["created_at"]),
            "created_by": str(row["created_by"]),
            "session_count": int(row["session_count"]),
            "active": str(row["id"]) == active_id,
        }
        for row in rows
    ]


def migration_plan(connection: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = connection.execute(
        """
        SELECT c.project_id, c.payload, p.title,
               CASE WHEN a.project_id IS NULL THEN 0 ELSE 1 END AS has_active
        FROM confirmed_candidate_data c
        JOIN projects p ON p.id = c.project_id
        LEFT JOIN active_schedule_revisions a ON a.project_id = c.project_id
        ORDER BY c.project_id
        """
    ).fetchall()
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    skipped_active = 0
    for row in rows:
        project_id = str(row["project_id"])
        if int(row["has_active"]):
            skipped_active += 1
            continue
        try:
            raw = _loads(row["payload"], None)
            normalized = normalize_schedule(raw, project_id)
        except ScheduleModelError as error:
            errors.append(
                {
                    "project_id": project_id,
                    "title": str(row["title"]),
                    "error": str(error),
                }
            )
            continue
        assignment_count = sum(
            len(session.get("university_role_member_ids", []))
            + len(session.get("high_school_role_member_ids", []))
            for session in normalized["sessions"]
        )
        entries.append(
            {
                "project_id": project_id,
                "title": str(row["title"]),
                "schedule": normalized,
                "session_count": len(normalized["sessions"]),
                "assignment_count": assignment_count,
            }
        )
    report = {
        "migration_name": CONFIRMED_SCHEDULE_MIGRATION,
        "confirmed_project_count": len(rows),
        "eligible_project_count": len(entries),
        "skipped_active_revision_count": skipped_active,
        "session_count": sum(entry["session_count"] for entry in entries),
        "assignment_count": sum(entry["assignment_count"] for entry in entries),
        "errors": errors,
        "projects": [
            {
                key: entry[key]
                for key in ("project_id", "title", "session_count", "assignment_count")
            }
            for entry in entries
        ],
    }
    return report, entries


def apply_confirmed_migration(
    connection: Any,
    *,
    actor: str,
    timestamp: str,
) -> dict[str, Any]:
    report, entries = migration_plan(connection)
    if report["errors"]:
        raise ScheduleModelError(
            "dry-runで不正な確定日程が見つかりました。移行は実行されていません。"
        )
    if not entries:
        return {**report, "dry_run": False, "migration_id": None, "status": "no_changes"}
    migration_id = uuid4().hex
    created: list[dict[str, str]] = []
    for entry in entries:
        revision_id, _ = insert_revision(
            connection,
            project_id=entry["project_id"],
            schedule=entry["schedule"],
            source="migration",
            actor=actor,
            timestamp=timestamp,
            change_note="legacy confirmed_candidate_dataからの初期移行",
            migration_id=migration_id,
        )
        created.append(
            {"project_id": entry["project_id"], "revision_id": revision_id}
        )
    detail = {
        "created": created,
        "legacy_rows_unchanged": True,
        "session_count": report["session_count"],
        "assignment_count": report["assignment_count"],
    }
    connection.execute(
        """
        INSERT INTO schedule_migrations
        (id, migration_name, status, detail, created_at, rolled_back_at)
        VALUES (?, ?, 'applied', ?, ?, NULL)
        """,
        (migration_id, CONFIRMED_SCHEDULE_MIGRATION, _dumps(detail), timestamp),
    )
    return {
        **report,
        "dry_run": False,
        "migration_id": migration_id,
        "status": "applied",
        "legacy_rows_unchanged": True,
    }


def rollback_plan(connection: Any, migration_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT status, detail FROM schedule_migrations WHERE id = ?",
        (migration_id,),
    ).fetchone()
    if row is None:
        raise ScheduleModelError("指定したmigrationが見つかりません。")
    if str(row["status"]) != "applied":
        raise ScheduleModelError("指定したmigrationは適用中ではありません。")
    detail = _loads(row["detail"], {})
    created = detail.get("created", []) if isinstance(detail, dict) else []
    if not isinstance(created, list):
        raise ScheduleModelError("migration履歴の形式が不正です。")
    created_revision_ids = {
        str(item.get("revision_id"))
        for item in created
        if isinstance(item, dict) and item.get("revision_id")
    }
    for item in created:
        if not isinstance(item, dict):
            continue
        project_id = str(item.get("project_id", ""))
        revision_id = str(item.get("revision_id", ""))
        active = connection.execute(
            "SELECT revision_id FROM active_schedule_revisions WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if active is None or str(active["revision_id"]) != revision_id:
            raise ScheduleModelError(
                "移行後に有効revisionが変更されているためrollbackできません。"
            )
        children = connection.execute(
            "SELECT id FROM schedule_revisions WHERE parent_revision_id = ?",
            (revision_id,),
        ).fetchall()
        if any(str(child["id"]) not in created_revision_ids for child in children):
            raise ScheduleModelError(
                "移行revisionを元にした後続編集があるためrollbackできません。"
            )
    return {
        "migration_id": migration_id,
        "status": "ready",
        "revision_count": len(created_revision_ids),
        "legacy_rows_unchanged": bool(detail.get("legacy_rows_unchanged", False)),
        "created": created,
    }


def apply_rollback(
    connection: Any,
    *,
    migration_id: str,
    timestamp: str,
) -> dict[str, Any]:
    report = rollback_plan(connection, migration_id)
    for item in report["created"]:
        project_id = str(item["project_id"])
        revision_id = str(item["revision_id"])
        connection.execute(
            "DELETE FROM active_schedule_revisions "
            "WHERE project_id = ? AND revision_id = ?",
            (project_id, revision_id),
        )
        connection.execute(
            "DELETE FROM schedule_revisions WHERE id = ?",
            (revision_id,),
        )
    connection.execute(
        """
        UPDATE schedule_migrations
        SET status = 'rolled_back', rolled_back_at = ?
        WHERE id = ? AND status = 'applied'
        """,
        (timestamp, migration_id),
    )
    return {
        key: value for key, value in report.items() if key != "created"
    } | {"status": "rolled_back", "dry_run": False}
