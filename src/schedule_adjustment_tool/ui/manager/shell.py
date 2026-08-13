from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import streamlit as st


from schedule_adjustment_tool.domain.models import Config, Participant
from schedule_adjustment_tool.ui.manager.routes import (
    ALL_ROUTE_IDS,
    AUXILIARY_ROUTES,
    HOME_ROUTE_ID,
    WORKFLOW_STEPS,
    AuxiliaryRoute,
    ScreenRoute,
    adjacent_workflow_routes,
    auxiliary_route,
    normalize_route_id,
    screen_route,
    workflow_step_for_route,
)
from schedule_adjustment_tool.ui.manager.session_state import (
    manager_completed_steps,
    manager_dirty_steps,
    manager_review_steps,
    manager_route_key,
    manager_started_steps,
    manager_status_overrides,
    mark_manager_step_completed,
    set_manager_route,
)
from schedule_adjustment_tool.ui.manager.view_models import (
    ManagerProjectSummary,
    ManagerScreenContext,
    build_manager_screen_context,
)
from schedule_adjustment_tool.ui.manager.workflow_state import (
    StepStatus,
    WorkflowStepState,
    derive_workflow_states,
    next_recommended_step,
)
from schedule_adjustment_tool.ui.presentation import STATUS_LABELS

PROJECT_SELECTOR_KEY = "manager_ui_project_selector"
RouteHandler = Callable[[], None]

STEP_STATUS_APPEARANCE = {
    StepStatus.NOT_STARTED: (
        ":material/radio_button_unchecked:",
        "gray",
    ),
    StepStatus.IN_PROGRESS: (
        ":material/radio_button_checked:",
        "blue",
    ),
    StepStatus.NOT_CREATED: (
        ":material/remove_circle:",
        "gray",
    ),
    StepStatus.NEEDS_REVIEW: (
        ":material/warning:",
        "orange",
    ),
    StepStatus.UNSAVED: (
        ":material/edit:",
        "red",
    ),
    StepStatus.COMPLETE: (
        ":material/check_circle:",
        "green",
    ),
}

AUXILIARY_ICONS = {
    "post_publish/current": ":material/event_available:",
    "post_publish/amendments": ":material/edit_calendar:",
    "utility/export": ":material/download:",
    "utility/access": ":material/lock:",
}

WORKFLOW_COMPLETION_ACTIONS = {
    "project_setup/basic": (
        "project_setup",
        "工程1を完了：工程2へ",
        "participants/groups",
        True,
    ),
    "participants/membership": (
        "participants",
        "工程2を完了：工程3へ",
        "project_setup/response_window",
        False,
    ),
    "participants/accounts": (
        "participants",
        "工程2を完了：工程3へ",
        "project_setup/response_window",
        True,
    ),
    "conditions/advanced": (
        "conditions",
        "工程4を完了：工程5へ",
        "candidates/create",
        True,
    ),
    "candidates/list": (
        "candidates",
        "工程5を完了：工程6へ",
        "publish/review",
        False,
    ),
    "candidates/adjust": (
        "candidates",
        "工程5を完了：工程6へ",
        "publish/review",
        True,
    ),
}


def _project_status_appearance(status: str) -> tuple[str, str]:
    return {
        "draft": (":material/edit_note:", "gray"),
        "collecting": (":material/how_to_reg:", "green"),
        "closed": (":material/event_busy:", "orange"),
        "confirmed": (":material/event_available:", "blue"),
    }.get(status, (":material/info:", "gray"))


def _global_navigation_state(
    states: tuple[WorkflowStepState, ...],
) -> tuple[str, str, str]:
    statuses = {state.status for state in states}
    if StepStatus.UNSAVED in statuses:
        return "未保存あり", ":material/edit:", "red"
    if StepStatus.NEEDS_REVIEW in statuses:
        return "要確認あり", ":material/warning:", "orange"
    if statuses == {StepStatus.COMPLETE}:
        return "全工程完了", ":material/check_circle:", "green"
    return "作業中", ":material/pending:", "blue"


def _navigate(project_id: str, route_id: str) -> None:
    selected_route_id = normalize_route_id(route_id)
    set_manager_route(project_id, selected_route_id)
    st.rerun()


def render_manager_project_selector(
    projects: list[dict[str, Any]],
    active_project_id: str,
    *,
    force_active_project: bool = False,
) -> str:
    project_ids = [str(project["id"]) for project in projects]
    if not project_ids:
        return active_project_id
    if active_project_id not in project_ids:
        active_project_id = ""
    if (
        force_active_project
        or st.session_state.get(PROJECT_SELECTOR_KEY, "") not in project_ids
    ):
        st.session_state[PROJECT_SELECTOR_KEY] = active_project_id

    with st.sidebar:
        st.title("日程調整")
        st.caption("スケジュール担当者")
        selected_project_id = st.selectbox(
            "企画",
            project_ids,
            index=(
                project_ids.index(active_project_id)
                if active_project_id
                else None
            ),
            placeholder="企画を選択してください",
            format_func=lambda value: next(
                (
                    str(project.get("title", "名称未設定"))
                    for project in projects
                    if str(project.get("id")) == value
                ),
                value,
            ),
            key=PROJECT_SELECTOR_KEY,
        )
        selected_project = next(
            (
                project
                for project in projects
                if str(project.get("id")) == selected_project_id
            ),
            {},
        )
        status = str(selected_project.get("status", ""))
        status_label = STATUS_LABELS.get(status, status or "未設定")
        status_icon, status_color = _project_status_appearance(status)
        st.badge(
            status_label,
            icon=status_icon,
            color=status_color,
        )
        st.caption("選択した企画の進捗を表示します。")
        st.divider()
    return str(selected_project_id or "")


def _render_sidebar_navigation(
    project_id: str,
    route_id: str,
    states: tuple[WorkflowStepState, ...],
) -> None:
    state_by_step = {state.step_id: state for state in states}
    current_step = workflow_step_for_route(route_id)
    global_label, global_icon, global_color = _global_navigation_state(states)

    with st.sidebar:
        st.subheader("日程作成の6工程")
        st.badge(global_label, icon=global_icon, color=global_color)
        st.caption("作業する工程を選んでください。")

        if st.button(
            "ホーム",
            key=f"manager_ui_nav_{project_id}_home",
            type="primary" if route_id == HOME_ROUTE_ID else "tertiary",
            icon=":material/home:",
            width="stretch",
        ):
            _navigate(project_id, HOME_ROUTE_ID)

        for step in WORKFLOW_STEPS:
            state = state_by_step[step.id]
            icon, _color = STEP_STATUS_APPEARANCE[state.status]
            active = current_step is not None and current_step.id == step.id
            label = f"{step.number}. {step.title} — {state.label}"
            if st.button(
                label,
                key=f"manager_ui_nav_{project_id}_{step.id}",
                type="primary" if active else "tertiary",
                icon=icon,
                help=state.detail,
                width="stretch",
            ):
                _navigate(
                    project_id,
                    route_id if active else step.default_route_id,
                )

        post_publish_routes = [
            route
            for route in AUXILIARY_ROUTES
            if route.category == "post_publish"
        ]
        utility_routes = [
            route
            for route in AUXILIARY_ROUTES
            if route.category == "utility"
        ]

        st.divider()
        st.caption("公開後")
        for route in post_publish_routes:
            if st.button(
                route.title,
                key=f"manager_ui_nav_{project_id}_{route.id}",
                type="primary" if route.id == route_id else "tertiary",
                icon=AUXILIARY_ICONS[route.id],
                help=route.purpose,
                width="stretch",
            ):
                _navigate(project_id, route.id)

        st.caption("補助機能")
        for route in utility_routes:
            if st.button(
                route.title,
                key=f"manager_ui_nav_{project_id}_{route.id}",
                type="primary" if route.id == route_id else "tertiary",
                icon=AUXILIARY_ICONS[route.id],
                help=route.purpose,
                width="stretch",
            ):
                _navigate(project_id, route.id)


def _render_project_summary(context: ManagerScreenContext) -> None:
    summary = context.summary
    st.caption(
        " / ".join(
            (
                f"日調対象 {summary.target_count}人",
                (
                    f"提出 {summary.submitted_count}/"
                    f"{summary.target_count}人"
                ),
                f"保存候補 {summary.candidate_count}件",
                f"公開日程 {'あり' if summary.confirmed else 'なし'}",
            )
        )
    )


def _render_home(
    context: ManagerScreenContext,
    states: tuple[WorkflowStepState, ...],
) -> None:
    st.caption(f"{context.summary.title} ＞ 企画ホーム")
    st.header("企画の現在地")
    st.write(
        "6工程の状態と、次に確認する作業を一覧します。"
        "設定の確認や変更は、各工程のメニューから行えます。"
    )
    completed_count = sum(
        state.status == StepStatus.COMPLETE for state in states
    )
    st.progress(
        completed_count / len(WORKFLOW_STEPS),
        text=f"{completed_count} / {len(WORKFLOW_STEPS)} 工程完了",
    )
    _render_project_summary(context)

    next_step_state = next_recommended_step(states)
    if next_step_state is not None:
        step = next(
            step
            for step in WORKFLOW_STEPS
            if step.id == next_step_state.step_id
        )
        icon, color = STEP_STATUS_APPEARANCE[
            next_step_state.status
        ]
        with st.container(border=True):
            st.subheader("次に確認する工程")
            st.badge(
                next_step_state.label,
                icon=icon,
                color=color,
            )
            st.markdown(f"**工程{step.number}　{step.title}**")
            st.caption(next_step_state.detail)
            if st.button(
                f"工程{step.number}を開く",
                key=f"manager_ui_home_next_{context.project_id}",
                type="primary",
                icon=":material/arrow_forward:",
                icon_position="right",
                width="stretch",
            ):
                _navigate(context.project_id, step.default_route_id)

    st.subheader("6工程の状態")
    for step, state in zip(WORKFLOW_STEPS, states, strict=True):
        icon, color = STEP_STATUS_APPEARANCE[state.status]
        with st.container(border=True):
            st.markdown(f"**{step.number}. {step.title}**")
            st.badge(
                state.label,
                icon=icon,
                color=color,
            )
            st.caption(state.detail)


def _render_workflow_header(
    context: ManagerScreenContext,
    route: ScreenRoute,
    state: WorkflowStepState,
) -> Any:
    step = workflow_step_for_route(route.id)
    if step is None:
        return
    icon, color = STEP_STATUS_APPEARANCE[state.status]
    st.caption(
        f"{context.summary.title} ＞ 日程作成 ＞ "
        f"工程{step.number} ＞ {route.title}"
    )
    st.header(step.title)
    st.badge(state.label, icon=icon, color=color)
    st.progress(
        step.number / len(WORKFLOW_STEPS),
        text=f"工程 {step.number} / {len(WORKFLOW_STEPS)}",
    )
    st.caption(state.detail)

    tab_containers = st.tabs(
        [screen.title for screen in step.screens],
        default=route.title,
        key=f"manager_workflow_tabs_{context.project_id}_{step.id}",
        on_change="rerun",
    )
    selected_index = next(
        (
            index
            for index, container in enumerate(tab_containers)
            if container.open
        ),
        next(
            index
            for index, screen in enumerate(step.screens)
            if screen.id == route.id
        ),
    )
    selected_route = step.screens[selected_index]
    if selected_route.id != route.id:
        set_manager_route(context.project_id, selected_route.id)
        st.rerun()
    return tab_containers[selected_index]


def _render_auxiliary_header(
    context: ManagerScreenContext,
    route: AuxiliaryRoute,
) -> None:
    category_label = (
        "公開後の操作"
        if route.category == "post_publish"
        else "補助機能"
    )
    st.caption(f"{context.summary.title} ＞ {category_label} ＞ {route.title}")
    st.header(route.title)


def _render_workflow_screen(
    context: ManagerScreenContext,
    route: ScreenRoute,
    states: tuple[WorkflowStepState, ...],
    route_handlers: Mapping[str, RouteHandler],
) -> None:
    step = workflow_step_for_route(route.id)
    if step is None:
        return
    state = next(state for state in states if state.step_id == step.id)
    selected_tab = _render_workflow_header(context, route, state)
    with selected_tab:
        st.subheader(route.title)
        route_handlers[route.id]()
        _render_workflow_footer(context.project_id, route)


def _render_workflow_footer(
    project_id: str,
    route: ScreenRoute,
) -> None:
    previous, following = adjacent_workflow_routes(route.id)
    if previous is None:
        previous_label = "ホームへ"
        previous_route_id = HOME_ROUTE_ID
    else:
        previous_label = f"前へ：{previous.title}"
        previous_route_id = previous.id

    if following is not None:
        next_label = (
            "次へ：工程4"
            if route.id == "responses/proxy"
            else f"次へ：{following.title}"
        )
        next_route_id = following.id
    else:
        next_label = "公開中の日程へ"
        next_route_id = "post_publish/current"

    completion = WORKFLOW_COMPLETION_ACTIONS.get(route.id)
    if completion is not None:
        completion_step, completion_label, completion_route, replace_next = (
            completion
        )
        if replace_next:
            next_label = ""
            next_route_id = ""

    current_step = workflow_step_for_route(route.id)
    previous_step = workflow_step_for_route(previous.id) if previous else None
    show_previous = previous is not None and (
        current_step is None
        or previous_step is None
        or previous_step.id == current_step.id
    )

    st.divider()
    with st.container(key="manager_ui_workflow_footer"):
        previous_column, next_column = st.columns(2, gap="large")
        if show_previous:
            if previous_column.button(
                previous_label,
                key=f"manager_ui_previous_{project_id}_{route.id}",
                icon=":material/arrow_back:",
                type="tertiary",
                width="stretch",
            ):
                _navigate(project_id, previous_route_id)
        if next_label:
            if next_column.button(
                next_label,
                key=f"manager_ui_next_{project_id}_{route.id}",
                icon=":material/arrow_forward:",
                icon_position="right",
                type="secondary",
                width="stretch",
            ):
                _navigate(project_id, next_route_id)
        if completion is not None:
            if next_column.button(
                completion_label,
                key=f"manager_ui_complete_{project_id}_{route.id}",
                icon=":material/check:",
                icon_position="left",
                type="primary",
                width="stretch",
            ):
                mark_manager_step_completed(project_id, completion_step)
                _navigate(project_id, completion_route)


def _render_auxiliary_screen(
    context: ManagerScreenContext,
    route: AuxiliaryRoute,
    route_handlers: Mapping[str, RouteHandler],
) -> None:
    _render_auxiliary_header(context, route)
    route_handlers[route.id]()


def render_manager_shell(
    project_id: str,
    config: Config,
    participants: list[Participant],
    candidates: list[dict[str, Any]],
    confirmed: dict[str, Any] | None,
    *,
    route_handlers: Mapping[str, RouteHandler],
    summary: ManagerProjectSummary | None = None,
) -> None:
    context = build_manager_screen_context(
        project_id,
        config,
        participants,
        candidates,
        confirmed,
    )
    if summary is not None:
        context = ManagerScreenContext(
            project_id=context.project_id,
            config=context.config,
            participants=context.participants,
            candidates=context.candidates,
            confirmed_candidate=context.confirmed_candidate,
            summary=summary,
        )
    route_key = manager_route_key(project_id)
    route_id = normalize_route_id(
        st.session_state.get(route_key, HOME_ROUTE_ID)
    )
    st.session_state[route_key] = route_id
    states = derive_workflow_states(
        context.summary,
        dirty_steps=manager_dirty_steps(project_id),
        review_steps=manager_review_steps(project_id),
        started_steps=manager_started_steps(project_id),
        completed_steps=manager_completed_steps(project_id),
        status_overrides=manager_status_overrides(project_id),
    )
    missing_handlers = set(ALL_ROUTE_IDS) - {HOME_ROUTE_ID} - set(
        route_handlers
    )
    if missing_handlers:
        missing = ", ".join(sorted(missing_handlers))
        raise ValueError(f"管理者画面の処理が未接続です: {missing}")

    _render_sidebar_navigation(project_id, route_id, states)

    if route_id == HOME_ROUTE_ID:
        _render_home(context, states)
    else:
        workflow_route = screen_route(route_id)
        if workflow_route is not None:
            _render_workflow_screen(
                context,
                workflow_route,
                states,
                route_handlers,
            )
        else:
            supporting_route = auxiliary_route(route_id)
            if supporting_route is not None:
                _render_auxiliary_screen(
                    context,
                    supporting_route,
                    route_handlers,
                )
