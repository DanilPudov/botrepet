"""
Раздел «Отзывы»: лист «Отзывы» в таблице «История».

Колонки (A..F):
  A Дата | B Репетитор | C Ученик | D Текст/ссылка на отзыв |
  E Начислено (₽) | F Статус

За каждый добавленный отзыв репетитору начисляется 400 ₽.
"""
from __future__ import annotations

import datetime as dt

import sheets_util
from sheets_util import load_config, log, norm_name

SHEET = "Отзывы"
REWARD = 400
COLUMNS = ["Дата", "Репетитор", "Ученик", "Отзыв", "Начислено (₽)", "Статус"]


def ensure_sheet() -> str:
    from sheets_util import sheets
    cfg = load_config()
    sh_id = cfg["sheets"]["history"]
    meta = sheets().spreadsheets().get(spreadsheetId=sh_id).execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if SHEET in titles:
        return SHEET
    sheets().spreadsheets().batchUpdate(
        spreadsheetId=sh_id,
        body={"requests": [{"addSheet": {"properties": {"title": SHEET}}}]}).execute()
    sheets_util.update_values(sh_id, f"'{SHEET}'!A1:F1", [COLUMNS])
    log(f"Создан лист «{SHEET}»")
    return SHEET


def _rows() -> list[list[str]]:
    cfg = load_config()
    return sheets_util.get_values(cfg["sheets"]["history"], f"'{SHEET}'!A1:F")


def reviews_of(tutor: str) -> list[dict]:
    out, total = [], 0
    for n, row in enumerate(_rows(), start=1):
        if n == 1 or len(row) < 2 or not norm_name(row[1]):
            continue
        if norm_name(row[1]) != norm_name(tutor):
            continue
        amount = 0
        try:
            amount = int(float(str(row[4]).replace(" ", "").replace("₽", "")))
        except (ValueError, IndexError):
            amount = 0
        total += amount
        out.append({
            "дата": row[0] if row else "",
            "ученик": row[2] if len(row) > 2 else "",
            "отзыв": row[3] if len(row) > 3 else "",
            "начислено": amount,
            "статус": row[5] if len(row) > 5 else "",
            "row": n,
        })
    return out


def add_review(tutor: str, student: str, text: str) -> int:
    """Добавляет отзыв и начисление. Возвращает номер строки."""
    ensure_sheet()
    cfg = load_config()
    sh_id = cfg["sheets"]["history"]
    rows = _rows()
    last = 1
    for n, row in enumerate(rows, start=1):
        if any(str(x).strip() for x in row):
            last = n
    row_num = last + 1
    sheets_util.update_values(
        sh_id, f"'{SHEET}'!A{row_num}:F{row_num}",
        [[dt.date.today().strftime("%d.%m.%Y"), tutor, student, text,
          REWARD, "начислено"]])
    log(f"ОТЗЫВ добавлен строка {row_num}: {tutor} — {student} (+{REWARD})")
    return row_num
