"""Лист «FAQ» в таблице «История»: колонка вопросов и колонка ответов."""
from __future__ import annotations

from sheets_util import (get_values, update_values, clear_values, load_config,
                         log, sheets, names_match, norm_name)

SHEET = "FAQ"
HEADER = ["Вопрос", "Ответ", "Кто спросил", "Дата"]


def ensure_sheet() -> None:
    """Создать лист «FAQ» с шапкой, если его ещё нет."""
    cfg = load_config()
    sh_id = cfg["sheets"]["history"]
    try:
        meta = sheets().spreadsheets().get(spreadsheetId=sh_id).execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    except Exception as e:  # noqa: BLE001
        log(f"FAQ: не смог прочитать список листов: {e}")
        return
    if SHEET in titles:
        return
    try:
        sheets().spreadsheets().batchUpdate(
            spreadsheetId=sh_id,
            body={"requests": [{"addSheet": {"properties": {"title": SHEET}}}]},
        ).execute()
        update_values(sh_id, f"'{SHEET}'!A1:D1", [HEADER])
        log(f"Создан лист «{SHEET}»")
    except Exception as e:  # noqa: BLE001
        log(f"FAQ: не смог создать лист: {e}")


def rows() -> list[dict]:
    """Все строки FAQ (без шапки)."""
    ensure_sheet()
    cfg = load_config()
    try:
        values = get_values(cfg["sheets"]["history"], f"'{SHEET}'!A1:D")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for n, row in enumerate(values[1:], start=2):
        q = str(row[0]).strip() if len(row) > 0 else ""
        a = str(row[1]).strip() if len(row) > 1 else ""
        if not q and not a:
            continue
        out.append({
            "row": n,
            "вопрос": q,
            "ответ": a,
            "кто": str(row[2]).strip() if len(row) > 2 else "",
            "дата": str(row[3]).strip() if len(row) > 3 else "",
        })
    return out


def add_entry(question: str, answer: str = "", sender: str = "",
              date: str = "") -> int:
    """Добавить вопрос (и, возможно, ответ). Возвращает номер строки."""
    ensure_sheet()
    cfg = load_config()
    sh_id = cfg["sheets"]["history"]
    values = get_values(sh_id, f"'{SHEET}'!A1:D") or []
    row = max(len(values) + 1, 2)
    update_values(sh_id, f"'{SHEET}'!A{row}:D{row}",
                  [[question, answer, sender, date]])
    log(f"FAQ добавлен вопрос в строку {row}")
    return row


def set_answer(row_num: int, answer: str) -> bool:
    """Записать ответ в строку."""
    ensure_sheet()
    cfg = load_config()
    try:
        update_values(cfg["sheets"]["history"], f"'{SHEET}'!B{row_num}", [[answer]])
        log(f"FAQ ответ добавлен в строку {row_num}")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"FAQ: не смог записать ответ: {e}")
        return False
