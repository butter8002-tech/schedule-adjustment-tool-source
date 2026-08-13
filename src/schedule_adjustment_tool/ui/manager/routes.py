from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

HOME_ROUTE_ID = "home"

RouteCategory = Literal["workflow", "post_publish", "utility"]


@dataclass(frozen=True)
class ScreenRoute:
    id: str
    title: str
    purpose: str
    primary_action: str


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    number: int
    title: str
    summary: str
    screens: tuple[ScreenRoute, ...]

    @property
    def default_route_id(self) -> str:
        return self.screens[0].id


@dataclass(frozen=True)
class AuxiliaryRoute:
    id: str
    title: str
    category: RouteCategory
    purpose: str
    primary_action: str


WORKFLOW_STEPS = (
    WorkflowStep(
        id="project_setup",
        number=1,
        title="企画情報の設定",
        summary="企画の概要と、参加者が回答できる日時範囲を準備します。",
        screens=(
            ScreenRoute(
                id="project_setup/basic",
                title="基本情報",
                purpose="企画名、説明、本番日など、企画そのものの情報を扱います。",
                primary_action="基本情報を保存",
            ),
        ),
    ),
    WorkflowStep(
        id="participants",
        number=2,
        title="参加者の準備",
        summary="名簿、所属、日調対象とアカウントを準備します。",
        screens=(
            ScreenRoute(
                id="participants/groups",
                title="班構成",
                purpose="班数と各班の文理構成を扱います。",
                primary_action="班構成を保存",
            ),
            ScreenRoute(
                id="participants/roster",
                title="参加者追加",
                purpose="参加者の追加と共通名簿からの取り込みを扱います。",
                primary_action="参加者を新規登録・追加",
            ),
            ScreenRoute(
                id="participants/membership",
                title="参加者設定",
                purpose="日調対象、承認、班、期、文理、所属を扱います。",
                primary_action="参加者設定を保存",
            ),
            ScreenRoute(
                id="participants/accounts",
                title="アカウント",
                purpose="参加者用アカウントの作成、再設定、配布用出力を扱います。",
                primary_action="参加者アカウントを準備",
            ),
        ),
    ),
    WorkflowStep(
        id="responses",
        number=3,
        title="回答を集める",
        summary="提出状況を確認し、必要な場合だけ回答内容を補います。",
        screens=(
            ScreenRoute(
                id="project_setup/response_window",
                title="回答受付",
                purpose="回答期間、対象曜日・時限、締切と受付状態を扱います。",
                primary_action="回答受付設定を保存",
            ),
            ScreenRoute(
                id="responses/status",
                title="提出状況",
                purpose="提出済み・下書き・未入力の状況と案内対象を確認します。",
                primary_action="案内対象を確認",
            ),
            ScreenRoute(
                id="responses/content",
                title="回答内容",
                purpose="参加可能日時を一覧またはカレンダーで確認します。",
                primary_action="回答内容を確認",
            ),
            ScreenRoute(
                id="responses/proxy",
                title="代理入力",
                purpose="選択した一人の代理入力を入力・復元します。",
                primary_action="代理入力を保存",
            ),
        ),
    ),
    WorkflowStep(
        id="conditions",
        number=4,
        title="探索条件の設定",
        summary="成立条件、個人別条件、評価条件を探索前に確認します。",
        screens=(
            ScreenRoute(
                id="conditions/feasibility",
                title="成立条件",
                purpose="役割人数、必要回数、同時開催数などの共通条件を扱います。",
                primary_action="成立条件を保存",
            ),
            ScreenRoute(
                id="conditions/individual",
                title="個別条件",
                purpose=(
                    "役割指定なし、個人別必要回数、追加上限など、"
                    "候補探索だけに使う個人条件を扱います。"
                ),
                primary_action="個別条件を保存",
            ),
            ScreenRoute(
                id="conditions/evaluation",
                title="評価条件",
                purpose="評価方針と優先度、本番日前後や時限の回避条件を扱います。",
                primary_action="評価条件を保存",
            ),
            ScreenRoute(
                id="conditions/advanced",
                title="探索方法",
                purpose="探索モード、候補数、探索時間、乱数シードなどを扱います。",
                primary_action="探索方法を保存",
            ),
        ),
    ),
    WorkflowStep(
        id="candidates",
        number=5,
        title="候補の作成・比較",
        summary="候補作成、比較、詳細確認、調整を順番に行います。",
        screens=(
            ScreenRoute(
                id="candidates/create",
                title="候補作成",
                purpose="自動探索または手動+自動調整で候補を作ります。",
                primary_action="候補探索を開始",
            ),
            ScreenRoute(
                id="candidates/list",
                title="候補一覧・詳細",
                purpose=(
                    "保存候補の評価値を比較し、選択した候補の"
                    "日程と参加者別集計を続けて確認します。"
                ),
                primary_action="候補を選んで内容を確認",
            ),
            ScreenRoute(
                id="candidates/adjust",
                title="候補調整",
                purpose="元候補を残したまま複製し、手動調整した候補を保存します。",
                primary_action="調整した候補を保存",
            ),
        ),
    ),
    WorkflowStep(
        id="publish",
        number=6,
        title="公開前確認",
        summary="選択した候補と警告を確認し、参加者へ公開します。",
        screens=(
            ScreenRoute(
                id="publish/review",
                title="公開前確認",
                purpose="選択候補、未充足、注意事項、参加者からの見え方を確認します。",
                primary_action="この日程を公開",
            ),
        ),
    ),
)

POST_PUBLISH_ROUTES = (
    AuxiliaryRoute(
        id="post_publish/current",
        title="公開中の日程",
        category="post_publish",
        purpose="現在公開されている日程と履歴を確認します。",
        primary_action="公開中の日程を確認",
    ),
    AuxiliaryRoute(
        id="post_publish/amendments",
        title="公開後の変更",
        category="post_publish",
        purpose="変更依頼から再公開までを通常の日程作成と分けて進めます。",
        primary_action="変更依頼を開始",
    ),
)

UTILITY_ROUTES = (
    AuxiliaryRoute(
        id="utility/export",
        title="データ出力",
        category="utility",
        purpose="企画データや日程表を用途別に出力します。",
        primary_action="出力するデータを選択",
    ),
    AuxiliaryRoute(
        id="utility/access",
        title="アクセス設定",
        category="utility",
        purpose="企画アクセスパスワードなどのアクセス設定を扱います。",
        primary_action="アクセス設定を保存",
    ),
)

AUXILIARY_ROUTES = POST_PUBLISH_ROUTES + UTILITY_ROUTES
WORKFLOW_ROUTE_IDS = tuple(
    screen.id for step in WORKFLOW_STEPS for screen in step.screens
)
ALL_ROUTE_IDS = (HOME_ROUTE_ID,) + WORKFLOW_ROUTE_IDS + tuple(
    route.id for route in AUXILIARY_ROUTES
)
ROUTE_ALIASES = {
    "candidates/detail": "candidates/list",
}


def normalize_route_id(route_id: object) -> str:
    normalized = str(route_id or "")
    normalized = ROUTE_ALIASES.get(normalized, normalized)
    return normalized if normalized in ALL_ROUTE_IDS else HOME_ROUTE_ID


def workflow_step_for_route(route_id: str) -> WorkflowStep | None:
    return next(
        (
            step
            for step in WORKFLOW_STEPS
            if any(screen.id == route_id for screen in step.screens)
        ),
        None,
    )


def screen_route(route_id: str) -> ScreenRoute | None:
    return next(
        (
            screen
            for step in WORKFLOW_STEPS
            for screen in step.screens
            if screen.id == route_id
        ),
        None,
    )


def auxiliary_route(route_id: str) -> AuxiliaryRoute | None:
    return next(
        (route for route in AUXILIARY_ROUTES if route.id == route_id),
        None,
    )


def adjacent_workflow_routes(
    route_id: str,
) -> tuple[ScreenRoute | None, ScreenRoute | None]:
    screens = [
        screen for step in WORKFLOW_STEPS for screen in step.screens
    ]
    try:
        index = next(
            index for index, screen in enumerate(screens) if screen.id == route_id
        )
    except StopIteration:
        return None, None
    previous = screens[index - 1] if index > 0 else None
    following = screens[index + 1] if index + 1 < len(screens) else None
    return previous, following
