"""Application-owned actions used by system-administration screens.

The system screens contain only their own presentation and orchestration.  The
confirmation dialogs and account-generation tools remain owned by the manager
application, so that the screens do not import ``ui.app``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class SystemManagementCallbacks:
    """App-owned dialogs and account tools supplied while a screen is rendered."""

    common_participant_table_updates: Callable[..., tuple[list[dict], list[str]]]
    format_datetime: Callable[[str], str]
    confirm_common_participant_delete: Callable[[dict], None]
    render_bulk_password_reset: Callable[[list[dict]], None]
    render_bulk_account_delete: Callable[[list[dict]], None]
    render_individual_account_generator: Callable[[list[dict]], None]
    render_project_creator: Callable[..., None]
    confirm_project_reset: Callable[[str, str], None]
    confirm_project_delete: Callable[[str, str], None]
    confirm_backup_restore: Callable[[int, str], None]


_CALLBACKS: ContextVar[SystemManagementCallbacks | None] = ContextVar(
    "system_management_callbacks",
    default=None,
)


@contextmanager
def system_management_callbacks(
    callbacks: SystemManagementCallbacks,
) -> Iterator[None]:
    """Bind app-only actions for the duration of a system-screen render."""

    token = _CALLBACKS.set(callbacks)
    try:
        yield
    finally:
        _CALLBACKS.reset(token)


def _callbacks() -> SystemManagementCallbacks:
    callbacks = _CALLBACKS.get()
    if callbacks is None:
        raise RuntimeError("System-management callbacks are not bound.")
    return callbacks


def common_participant_table_updates(*args, **kwargs):
    return _callbacks().common_participant_table_updates(*args, **kwargs)


def format_datetime_with_weekday(value: str) -> str:
    return _callbacks().format_datetime(value)


def common_participant_delete_confirmation_dialog(profile: dict) -> None:
    _callbacks().confirm_common_participant_delete(profile)


def render_bulk_account_password_reset(users: list[dict]) -> None:
    _callbacks().render_bulk_password_reset(users)


def render_bulk_account_deletion(users: list[dict]) -> None:
    _callbacks().render_bulk_account_delete(users)


def render_individual_participant_account_generator(projects: list[dict]) -> None:
    _callbacks().render_individual_account_generator(projects)


def render_system_project_creator(projects: list[dict], *, key_prefix: str) -> None:
    _callbacks().render_project_creator(projects, key_prefix=key_prefix)


def reset_confirmation_dialog(project_id: str, reset_type: str) -> None:
    _callbacks().confirm_project_reset(project_id, reset_type)


def delete_project_confirmation_dialog(project_id: str, title: str) -> None:
    _callbacks().confirm_project_delete(project_id, title)


def backup_restore_confirmation_dialog(backup_id: int, project_id: str) -> None:
    _callbacks().confirm_backup_restore(backup_id, project_id)
