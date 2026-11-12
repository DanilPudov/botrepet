"""
Реферальная программа: лист «Рефералы» в таблице «История».

Колонки (A..E):
  A Дата | B Кто привёл | C Кого привёл | D Статус | E Вознаграждение (₽)

Репетитор получает 500 ₽, когда приведённый им репетитор закроет комиссию
и заработает на эти 500 ₽ своими 20%.
"""
from __future__ import annotations

import datetime as dt

import sheets_util
from sheets_util import load_config, log, norm_name

SHEET = "Рефералы"
REWARD = 500
STATUS_WAIT = "ждёт закрытия комиссии"
STATUS_PAID = "начислено"
COLUMNS = ["Дата", "Кто привёл", "Кого привёл", "Статус", "Вознаграждение (₽)"]


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
    sheets_util.update_values(sh_id, f"'{SHEET}'!A1:E1", [COLUMNS])
    log(f"Создан лист «{SHEET}»")
    return SHEET


def _rows() -> list[list[str]]:
    cfg = load_config()
    return sheets_util.get_values(cfg["sheets"]["history"], f"'{SHEET}'!A1:E")


def referrals_of(tutor: str) -> list[dict]:
    out = []
    for n, row in enumerate(_rows(), start=1):
        if n == 1 or len(row) < 2 or not norm_name(row[1]):
            continue
        if norm_name(row[1]) != norm_name(tutor):
            continue
        out.append({
            "дата": row[0] if row else "",
            "кого": row[2] if len(row) > 2 else "",
            "статус": row[3] if len(row) > 3 else "",
            "вознаграждение": row[4] if len(row) > 4 else "",
            "row": n,
        })
    return out


def add_referral(tutor: str, referee: str) -> int:
    """Записывает «кого привёл». Возвращает номер строки."""
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
        sh_id, f"'{SHEET}'!A{row_num}:E{row_num}",
        [[dt.date.today().strftime("%d.%m.%Y"), tutor, referee,
          STATUS_WAIT, REWARD]])
    log(f"РЕФЕРАЛ добавлен строка {row_num}: {tutor} привёл {referee}")
    return row_num


def has_referral(tutor: str, referee: str) -> bool:
    want = norm_name(referee)
    return any(norm_name(r["кого"]) == want for r in referrals_of(tutor))
