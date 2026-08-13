from __future__ import annotations


DEPARTMENT_OPTIONS = [
    "",
    "教養学部(前期)",
    "法学部",
    "医学部",
    "工学部",
    "文学部",
    "理学部",
    "農学部",
    "経済学部",
    "教養学部(後期)",
    "教育学部",
    "薬学部",
    "その他",
]

DEPARTMENT_DETAILS = {
    "教養学部(前期)": [
        "文科一類",
        "文科二類",
        "文科三類",
        "理科一類",
        "理科二類",
        "理科三類",
    ],
    "法学部": ["第1類", "第2類", "第3類"],
    "医学部": ["医学科", "健康総合科学科"],
    "工学部": [
        "社会基盤",
        "建築",
        "都市",
        "機械",
        "機械情報",
        "航空宇宙",
        "精密",
        "電子情報",
        "電気電子",
        "物理工",
        "計数",
        "マテリアル",
        "応用化学",
        "化学システム",
        "化学生命",
        "システム創成",
    ],
    "文学部": ["人文学科"],
    "理学部": [
        "数学",
        "情報科学",
        "物理",
        "天文",
        "地球惑星物理",
        "地球惑星環境",
        "化学",
        "生物化学",
        "生物",
        "生物情報科学",
    ],
    "農学部": ["応用生命科学", "環境資源科学", "獣医学"],
    "経済学部": ["経済", "経営", "金融"],
    "教養学部(後期)": ["教養", "学際科学", "統合自然科学"],
    "教育学部": ["総合教育科学科"],
    "薬学部": ["薬学科", "薬科学科"],
}


def normalize_department(
    department: str, detail: str = ""
) -> tuple[str, str]:
    department = department.strip()
    detail = detail.strip()
    legacy_early = {
        "文科一類",
        "文科二類",
        "文科三類",
        "理科一類",
        "理科二類",
        "理科三類",
    }
    if department in legacy_early:
        return "教養学部(前期)", department
    if department == "教養学部":
        return "教養学部(後期)", detail if detail in DEPARTMENT_DETAILS["教養学部(後期)"] else ""
    if department in DEPARTMENT_OPTIONS:
        if department in DEPARTMENT_DETAILS:
            allowed = DEPARTMENT_DETAILS[department]
            return department, detail if detail in allowed else ""
        return department, detail if department == "その他" else ""
    if department:
        return "その他", detail or department
    return "", ""


def department_detail_options(department: str) -> list[str]:
    if department in DEPARTMENT_DETAILS:
        return [""] + DEPARTMENT_DETAILS[department]
    return []


def display_department(department: str, detail: str) -> str:
    if not department:
        return ""
    if detail:
        return f"{department} / {detail}"
    return department
