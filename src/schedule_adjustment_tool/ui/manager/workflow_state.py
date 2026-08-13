from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from schedule_adjustment_tool.ui.manager.routes import WORKFLOW_STEPS
from schedule_adjustment_tool.ui.manager.view_models import ManagerProjectSummary


class StepStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    NOT_CREATED = "not_created"
    NEEDS_REVIEW = "needs_review"
    UNSAVED = "unsaved"
    COMPLETE = "complete"


STEP_STATUS_LABELS = {
    StepStatus.NOT_STARTED: "未着手",
    StepStatus.IN_PROGRESS: "進行中",
    StepStatus.NOT_CREATED: "未作成",
    StepStatus.NEEDS_REVIEW: "要確認",
    StepStatus.UNSAVED: "未保存",
    StepStatus.COMPLETE: "完了",
}


@dataclass(frozen=True)
class WorkflowStepState:
    step_id: str
    status: StepStatus
    label: str
    detail: str


def _state(
    step_id: str,
    status: StepStatus,
    detail: str,
) -> WorkflowStepState:
    return WorkflowStepState(
        step_id=step_id,
        status=status,
        label=STEP_STATUS_LABELS[status],
        detail=detail,
    )


def _response_reception_complete(summary: ManagerProjectSummary) -> bool:
    """Return whether the target response stage is normally complete.

    Only active, approved scheduling targets are included in the summary, so
    responses from participants outside the adjustment target do not block the
    workflow.
    """

    return (
        summary.target_count > 0
        and summary.submitted_count >= summary.target_count
        and summary.status in {"closed", "confirmed"}
        and not summary.allow_edits_after_deadline
    )


def derive_workflow_states(
    summary: ManagerProjectSummary,
    *,
    dirty_steps: Iterable[str] = (),
    review_steps: Iterable[str] = (),
    started_steps: Iterable[str] = (),
    completed_steps: Iterable[str] = (),
    status_overrides: dict[str, str] | None = None,
) -> tuple[WorkflowStepState, ...]:
    dirty = set(dirty_steps)
    review = set(review_steps)
    started = set(started_steps)
    completed = set(completed_steps)
    overrides = {
        str(step_id): str(status)
        for step_id, status in (status_overrides or {}).items()
    }
    upstream_ids = {"project_setup", "participants", "responses", "conditions"}
    candidate_inputs_changed = bool((dirty | review) & upstream_ids)

    if (
        summary.is_pristine
        and not dirty
        and not review
        and not started
        and not completed
        and not overrides
    ):
        return (
            _state(
                "project_setup",
                StepStatus.NOT_STARTED,
                "基本情報を確認して保存してください。",
            ),
            _state(
                "participants",
                StepStatus.NOT_STARTED,
                "基本情報の保存後に参加者の準備を行います。",
            ),
            _state(
                "responses",
                StepStatus.NOT_STARTED,
                "参加者の準備後に回答受付を開始します。",
            ),
            _state(
                "conditions",
                StepStatus.NOT_STARTED,
                "参加者と回答の準備後に探索条件を確認します。",
            ),
            _state(
                "candidates",
                StepStatus.NOT_CREATED,
                "探索条件の確認後に候補を作成します。",
            ),
            _state(
                "publish",
                StepStatus.NOT_STARTED,
                "候補作成後に公開前確認を行います。",
            ),
        )

    if summary.config_issue_count:
        project_setup = _state(
            "project_setup",
            StepStatus.NEEDS_REVIEW,
            f"設定に{summary.config_issue_count}件の確認事項があります。",
        )
    else:
        project_setup = _state(
            "project_setup",
            StepStatus.COMPLETE,
            "基本情報を登録済みです。",
        )

    if summary.pending_approval_count:
        participants = _state(
            "participants",
            StepStatus.NEEDS_REVIEW,
            f"承認待ちの参加者が{summary.pending_approval_count}人います。",
        )
    elif summary.target_count:
        participants = _state(
            "participants",
            StepStatus.COMPLETE,
            f"日調対象は{summary.target_count}人です。",
        )
    else:
        participants = _state(
            "participants",
            StepStatus.NOT_STARTED,
            "日調対象の参加者がいません。",
        )

    if summary.status == "draft":
        responses = _state(
            "responses",
            StepStatus.NOT_STARTED,
            "回答受付の設定を確認し、受付を開始してください。",
        )
    elif not summary.target_count:
        responses = _state(
            "responses",
            StepStatus.NOT_STARTED,
            "日調対象者を準備してください。",
        )
    elif summary.submitted_count < summary.target_count:
        reception_closed = summary.status in {"closed", "confirmed"}
        responses = _state(
            "responses",
            (
                StepStatus.NEEDS_REVIEW
                if reception_closed
                else StepStatus.IN_PROGRESS
            ),
            (
                f"回答受付は終了していますが、提出済みは"
                f"{summary.submitted_count}/{summary.target_count}人です。"
                if reception_closed
                else (
                    f"提出済みは{summary.submitted_count}/"
                    f"{summary.target_count}人です。"
                )
            ),
        )
    elif summary.status == "collecting":
        responses = _state(
            "responses",
            StepStatus.IN_PROGRESS,
            (
                f"日調対象{summary.target_count}人が提出済みです。"
                "回答受付を締め切ると、次の確認へ進めます。"
            ),
        )
    else:
        responses = _state(
            "responses",
            (
                StepStatus.NEEDS_REVIEW
                if summary.allow_edits_after_deadline
                else StepStatus.COMPLETE
            ),
            (
                f"全員提出済みですが、締切後の編集許可がONです。"
                "チェックを外すことを推奨します。"
                if summary.allow_edits_after_deadline
                else f"日調対象{summary.target_count}人が提出済みです。"
            ),
        )

    if summary.config_issue_count:
        conditions = _state(
            "conditions",
            StepStatus.NEEDS_REVIEW,
            "企画情報の設定を確認してください。",
        )
    elif not summary.target_count:
        conditions = _state(
            "conditions",
            StepStatus.NOT_STARTED,
            "日調対象者の準備後に探索条件を確認します。",
        )
    elif summary.submitted_count < summary.target_count:
        conditions = _state(
            "conditions",
            StepStatus.NEEDS_REVIEW,
            "未提出者を含めて探索条件を確認してください。",
        )
    elif summary.status in {"draft", "collecting"}:
        conditions = _state(
            "conditions",
            StepStatus.IN_PROGRESS,
            "回答受付の前後でも条件は先行確認できます。回答を締め切ると通常工程として完了します。",
        )
    else:
        conditions = _state(
            "conditions",
            StepStatus.COMPLETE,
            "現在のデータから探索条件を確認できます。",
        )

    response_reception_complete = _response_reception_complete(summary)
    if (
        summary.confirmed
        and "candidates" in completed
        and "candidates" not in (dirty | review)
    ):
        candidates = _state(
            "candidates",
            StepStatus.COMPLETE,
            "工程6の公開前確認を完了したため、工程5も完了しています。",
        )
    elif summary.candidate_count and candidate_inputs_changed:
        candidates = _state(
            "candidates",
            StepStatus.NEEDS_REVIEW,
            "前工程に変更があるため、保存候補の再確認が必要です。",
        )
    elif summary.candidate_count and summary.candidate_warning_count:
        candidates = _state(
            "candidates",
            StepStatus.NEEDS_REVIEW,
            f"保存候補{summary.candidate_warning_count}件に警告があります。内容を確認してください。",
        )
    elif summary.candidate_count and not response_reception_complete:
        candidates = _state(
            "candidates",
            (
                StepStatus.NEEDS_REVIEW
                if summary.submitted_count < summary.target_count
                else StepStatus.IN_PROGRESS
            ),
            (
                "日調対象に未提出者がいるため、候補は先行作業として保存されています。"
                if summary.submitted_count < summary.target_count
                else "保存候補はあります。回答受付を締め切ると通常工程として確認できます。"
            ),
        )
    elif summary.candidate_count:
        candidates = _state(
            "candidates",
            StepStatus.COMPLETE,
            f"保存候補が{summary.candidate_count}件あります。",
        )
    else:
        candidates = _state(
            "candidates",
            StepStatus.NOT_CREATED,
            "保存候補はまだありません。",
        )

    if summary.confirmed:
        publish = _state(
            "publish",
            StepStatus.COMPLETE,
            f"候補{summary.confirmed_candidate_number}を公開中です。",
        )
    elif summary.candidate_count and candidate_inputs_changed:
        publish = _state(
            "publish",
            StepStatus.NEEDS_REVIEW,
            "前工程の変更を反映した候補が必要です。",
        )
    elif summary.candidate_count and summary.candidate_warning_count:
        publish = _state(
            "publish",
            StepStatus.NEEDS_REVIEW,
            "候補に警告があります。内容を確認してから公開してください。",
        )
    elif (
        "candidates" in completed
        and not (dirty | review)
    ):
        publish = _state(
            "publish",
            StepStatus.IN_PROGRESS,
            "工程5を完了しました。公開する候補を最終確認してください。",
        )
    elif summary.candidate_count:
        publish = _state(
            "publish",
            StepStatus.NEEDS_REVIEW,
            "公開する候補を選び、最終確認してください。",
        )
    else:
        publish = _state(
            "publish",
            StepStatus.NOT_STARTED,
            "候補作成後に公開前確認を行います。",
        )

    states = {
        state.step_id: state
        for state in (
            project_setup,
            participants,
            responses,
            conditions,
            candidates,
            publish,
        )
    }
    for step_id in review:
        if (
            step_id in states
            and step_id not in dirty
            and states[step_id].status != StepStatus.NOT_CREATED
        ):
            states[step_id] = _state(
                step_id,
                StepStatus.NEEDS_REVIEW,
                "前の操作による影響を確認してください。",
            )
    for step_id in dirty:
        if step_id in states:
            states[step_id] = _state(
                step_id,
                StepStatus.UNSAVED,
                "この工程に未保存の変更があります。",
            )

    # A copied project inherits the shape of the source workflow only for the
    # current session.  A source completion is deliberately downgraded to a
    # review state so that the copied data is checked before it is used.
    for step_id, status_value in overrides.items():
        if step_id not in states or step_id in dirty:
            continue
        try:
            status = StepStatus(status_value)
        except ValueError:
            continue
        # A copied project keeps publication explicitly incomplete until the
        # copied candidate has been reviewed and stage 5 is acknowledged.
        # Once stage 5 is completed, let the normal derived publication state
        # become IN_PROGRESS (or NEEDS_REVIEW when an upstream change exists).
        if (
            step_id == "publish"
            and status == StepStatus.NOT_STARTED
            and "candidates" in completed
        ):
            continue
        if status == StepStatus.COMPLETE:
            status = StepStatus.NEEDS_REVIEW
        states[step_id] = _state(
            step_id,
            status,
            "複製元の状態を引き継いでいます。内容を確認してください。",
        )

    # Explicit completion buttons are session-only workflow acknowledgements.
    # They may refine a factually complete state, but must not turn a missing,
    # pending, or review-required state into COMPLETE.  This keeps先行作業
    # available without presenting it as ordinary workflow completion.
    for step_id in completed:
        if (
            step_id in states
            and step_id not in dirty
            and step_id not in review
            and states[step_id].status == StepStatus.COMPLETE
        ):
            states[step_id] = _state(
                step_id,
                StepStatus.COMPLETE,
                "この工程を完了として確認済みです。",
            )

    for step_id in started - completed:
        if step_id in states and step_id not in dirty and step_id not in review:
            if states[step_id].status not in {
                StepStatus.NOT_CREATED,
                StepStatus.NOT_STARTED,
            } or step_id in {"project_setup", "participants", "conditions", "candidates"}:
                states[step_id] = _state(
                    step_id,
                    StepStatus.IN_PROGRESS,
                    "保存した内容を確認し、工程を完了してください。",
                )
    return tuple(states[step.id] for step in WORKFLOW_STEPS)


def next_recommended_step(
    states: Iterable[WorkflowStepState],
) -> WorkflowStepState | None:
    state_list = list(states)
    return next(
        (
            state
            for state in state_list
            if state.status
            not in {
                StepStatus.COMPLETE,
            }
        ),
        state_list[-1] if state_list else None,
    )
