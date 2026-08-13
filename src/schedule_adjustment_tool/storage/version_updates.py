"""Shared, idempotent data updates required by application releases."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


SUPPORT_ROLE_MIGRATION_KEY = (
    "version_update_v2_support_role_unspecified_notice_repair"
)
SUPPORT_ROLE_NOTICE_PREFIX = "version_notice_v2_support_role_unspecified:"


def support_role_notice_key(project_id: str) -> str:
    return f"{SUPPORT_ROLE_NOTICE_PREFIX}{project_id}"


def migrate_v101_support_role_unspecified(
    connection: Any,
    *,
    json_loads: Callable[[object, Any], Any],
    json_dumps: Callable[[Any], str],
    write_participants_snapshot: Callable[[Any, str], int],
    audit: Callable[..., None],
    now_iso: Callable[[], str],
    legacy_support_group: str,
) -> dict[str, int]:
    """Persist the role-unspecified behavior used for support participants in 1.0.1."""

    migrated = connection.execute(
        "SELECT value FROM metadata WHERE key = ?",
        (SUPPORT_ROLE_MIGRATION_KEY,),
    ).fetchone()
    if migrated:
        return {}

    existing_notice_rows = connection.execute(
        "SELECT key FROM metadata WHERE key LIKE ?",
        (f"{SUPPORT_ROLE_NOTICE_PREFIX}%",),
    ).fetchall()
    projects_with_notice = {
        str(row["key"])[len(SUPPORT_ROLE_NOTICE_PREFIX) :]
        for row in existing_notice_rows
    }
    rows = connection.execute(
        """
        SELECT project_id, participant_id, attributes_payload,
               requirements_payload
        FROM project_participations
        """
    ).fetchall()
    affected_by_project: dict[str, int] = {}
    changed_projects: set[str] = set()
    timestamp = now_iso()
    for row in rows:
        attributes = json_loads(row["attributes_payload"], {})
        requirements = json_loads(row["requirements_payload"], {})
        if not isinstance(attributes, dict) or not isinstance(requirements, dict):
            continue
        project_id = str(row["project_id"])
        if project_id in projects_with_notice:
            continue
        if str(attributes.get("group_number", "")) != legacy_support_group:
            continue
        affected_by_project[project_id] = affected_by_project.get(project_id, 0) + 1
        if requirements.get("practice_role_unspecified") is True:
            continue
        requirements["practice_role_unspecified"] = True
        connection.execute(
            """
            UPDATE project_participations
            SET requirements_payload = ?, version = version + 1, updated_at = ?
            WHERE project_id = ? AND participant_id = ?
            """,
            (
                json_dumps(requirements),
                timestamp,
                row["project_id"],
                row["participant_id"],
            ),
        )
        changed_projects.add(project_id)

    for project_id, affected_count in affected_by_project.items():
        if project_id in changed_projects:
            write_participants_snapshot(connection, project_id)
        notice = {
            "notice_id": SUPPORT_ROLE_MIGRATION_KEY,
            "affected_count": affected_count,
            "migrated_at": timestamp,
            "acknowledged_at": "",
        }
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
            (support_role_notice_key(project_id), json_dumps(notice)),
        )
        audit(
            connection,
            "version_update.support_role_unspecified",
            project_id=project_id,
            detail={
                "affected_count": affected_count,
                "data_changed": project_id in changed_projects,
            },
        )

    migration_result = {
        "affected_project_count": len(affected_by_project),
        "affected_participant_count": sum(affected_by_project.values()),
        "migrated_at": timestamp,
    }
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        (SUPPORT_ROLE_MIGRATION_KEY, json_dumps(migration_result)),
    )
    return affected_by_project


def load_support_role_notice(
    connection: Any,
    project_id: str,
    *,
    json_loads: Callable[[object, Any], Any],
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key = ?",
        (support_role_notice_key(project_id),),
    ).fetchone()
    if not row:
        return None
    notice = json_loads(row["value"], {})
    if not isinstance(notice, dict) or notice.get("acknowledged_at"):
        return None
    try:
        affected_count = int(notice.get("affected_count", 0))
    except (TypeError, ValueError):
        return None
    if affected_count <= 0:
        return None
    return {**notice, "affected_count": affected_count}


def acknowledge_support_role_notice(
    connection: Any,
    project_id: str,
    *,
    json_loads: Callable[[object, Any], Any],
    json_dumps: Callable[[Any], str],
    audit: Callable[..., None],
    now_iso: Callable[[], str],
) -> bool:
    key = support_role_notice_key(project_id)
    row = connection.execute(
        "SELECT value FROM metadata WHERE key = ?",
        (key,),
    ).fetchone()
    if not row:
        return False
    notice = json_loads(row["value"], {})
    if not isinstance(notice, dict):
        return False
    if notice.get("acknowledged_at"):
        return True
    notice["acknowledged_at"] = now_iso()
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        (key, json_dumps(notice)),
    )
    audit(
        connection,
        "version_notice.support_role_unspecified.acknowledged",
        project_id=project_id,
        detail={"affected_count": int(notice.get("affected_count", 0) or 0)},
    )
    return True
