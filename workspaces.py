"""Кабинеты репетиторов: у кого своя таблица и как считать выплату.

Данил, Вика и Максим — семья, работают на себя: им показываем полную сумму.
Остальные репетиторы работают через Данила: им 80% (минус 20% комиссии).
"""
from __future__ import annotations

from sheets_util import load_config, norm_name

# Личные листы внутри главной таблицы (для тех, у кого нет своей)
PERSONAL_SHEETS = {
    "Данил Пудов": "История Данил",
    "Вика": "История Вика",
}

# Кому показывать 100% (без вычета 20%)
FULL_PAY_TUTORS = ("Данил Пудов", "Виктория Пудова", "Максим Пудов")

# Название листа занятий в личной таблице
WORKSPACE_LESSONS_SHEET = "Занятия"

# Аккаунты с собственной таблицей
WORKSPACES = {
    "Максим Пудов": "1ta6RaPecmNJamIpx-WTphkbsO1ulucwxWEMqvJ_Eh1Q",
    "Виктория Пудова": "1KN-od3mOUEXLejQDngbHQQe1cItaJ8nl9HfOdTP5YbU",
}

# Синонимы имён -> каноничное имя
ALIASES = {
    "максим": "Максим Пудов",
    "макс": "Максим Пудов",
    "вика": "Виктория Пудова",
    "виктория": "Виктория Пудова",
    "виктория пудова": "Виктория Пудова",
}


def canon_tutor(name: str) -> str:
    """Привести имя репетитора к каноничному виду."""
    n = norm_name(name)
    low = n.lower()
    if low in ALIASES:
        return ALIASES[low]
    for key, val in ALIASES.items():
        if low.startswith(key + " ") or low == key:
            return val
    return n


def workspace_id(tutor: str) -> str | None:
    """ID личной таблицы репетитора (None — работает в главной)."""
    t = canon_tutor(tutor)
    for name, sid in WORKSPACES.items():
        if norm_name(name).lower() == t.lower():
            return sid
    return None


def is_full_pay(tutor: str) -> bool:
    """Показывать 100% (семья) или 80% (остальные)."""
    t = canon_tutor(tutor).lower()
    for name in FULL_PAY_TUTORS:
        if norm_name(name).lower() == t:
            return True
    return False


def payout(total: int, tutor: str) -> int:
    """Сумма к выплате: 100% семье, 80% остальным."""
    if is_full_pay(tutor):
        return int(total)
    return int(total * 0.8)


def payout_note(tutor: str) -> str:
    """Подпись к выплате."""
    if is_full_pay(tutor):
        return "к выплате"
    return "к выплате, 80%"


# --------------------------------------------------------------------------- #
# Активный репетитор: по нему модули выбирают свою или общую таблицу
# --------------------------------------------------------------------------- #
_ACTIVE = {"tutor": ""}


def set_active(tutor: str) -> None:
    """Запомнить, от чьего имени сейчас работаем."""
    _ACTIVE["tutor"] = canon_tutor(tutor or "")


def active_tutor() -> str:
    return _ACTIVE.get("tutor") or ""


def spreadsheet() -> str:
    """ID таблицы для текущего репетитора: своя, иначе главная."""
    t = active_tutor()
    sid = workspace_id(t) if t else None
    if sid:
        return sid
    return load_config()["sheets"]["history"]


def is_own_table() -> bool:
    """Работаем ли в личной таблице (Максим/Вика)."""
    return workspace_id(active_tutor()) is not None


def is_family(tutor: str) -> bool:
    """Пудовы: своя семья — им одна неделя и без пояснений о комиссии."""
    return is_full_pay(tutor)


def week_span(tutor: str) -> int:
    """Сколько дней показывать в «Текущих»: семье — 7, остальным — 14."""
    return 7 if is_family(tutor) else 14
