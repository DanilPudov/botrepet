"""
Предложения по улучшению бота: лист «Предложения» в таблице «История».

Колонки (A..E):
  A Дата | B Репетитор | C Предложение | D Статус | E Источник (ID)

Статус по умолчанию — «новое».
"""
from __future__ import annotations

import datetime as dt

import sheets_util
from sheets_util import load_config, log, norm_name

SHEET = "Предложения"
STATUS_NEW = "новое"
STATUS_ACCEPTED = "принято"
STATUS_REJECTED = "отклонено"
STATUS_CONFIRMED = "подтверждено"

GREEN = {"red": 0.0, "green": 0.6, "blue": 0.0}
COLUMNS = ["Дата", "Репетитор", "Предложение", "Статус", "Источник (ID)"]


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


def _mark_text_color(sh_id: str, row_num: int, green: bool) -> None:
    """Зелёный текст предложения = обработано."""
    from sheets_util import sheets
    color = GREEN if green else {"red": 0.0, "green": 0.0, "blue": 0.0}
    sheets().spreadsheets().batchUpdate(
        spreadsheetId=sh_id,
        body={"requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": _sheet_gid(sh_id),
                    "startRowIndex": row_num - 1,
                    "endRowIndex": row_num,
                    "startColumnIndex": 2,
                    "endColumnIndex": 3,
                },
                "cell": {"userEnteredFormat": {
                    "textFormat": {"foregroundColor": color}}},
                "fields": "userEnteredFormat.textFormat.foregroundColor",
            }
        }]}).execute()


def _sheet_gid(sh_id: str) -> int:
    from sheets_util import sheets
    meta = sheets().spreadsheets().get(spreadsheetId=sh_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == SHEET:
            return s["properties"]["sheetId"]
    return 0


def set_status(row_num: int, status: str, green: bool = True) -> None:
    """Записать статус в D и покрасить текст предложения."""
    ensure_sheet()
    cfg = load_config()
    sh_id = cfg["sheets"]["history"]
    sheets_util.update_values(
        sh_id, f"'{SHEET}'!D{row_num}", [[status]])
    try:
        _mark_text_color(sh_id, row_num, green)
    except Exception as e:  # noqa: BLE001
        log(f"Не удалось покрасить предложение: {e}")
    log(f"ПРЕДЛОЖЕНИЕ строка {row_num}: {status}")


def get_suggestion(row_num: int) -> dict | None:
    rows = _rows()
    if row_num - 1 >= len(rows) or row_num < 2:
        return None
    row = rows[row_num - 1]
    return {
        "row": row_num,
        "дата": row[0] if row else "",
        "репетитор": row[1] if len(row) > 1 else "",
        "текст": row[2] if len(row) > 2 else "",
        "статус": row[3] if len(row) > 3 else "",
        "источник": row[4] if len(row) > 4 else "",
    }


def _is_green(row_num: int) -> bool:
    """Проверить, зелёный ли текст предложения (обработано)."""
    from sheets_util import sheets
    try:
        cfg = load_config()
        meta = sheets().spreadsheets().get(
            spreadsheetId=cfg["sheets"]["history"],
            ranges=[f"'{SHEET}'!C{row_num}"],
            includeGridData=True,
            fields="sheets(data(rowData(values(effectiveFormat/textFormat/foregroundColor))))"
        ).execute()
        cells = meta["sheets"][0]["data"][0]["rowData"][0]["values"]
        fg = (cells[0].get("effectiveFormat") or {}).get(
            "textFormat", {}).get("foregroundColor") or {}
        return (float(fg.get("green", 0)) > 0.4
                and float(fg.get("red", 0)) < 0.4)
    except Exception:  # noqa: BLE001
        return False


def pending_suggestions() -> list[dict]:
    """Необработанные предложения: статус «новое» (по колонке D)."""
    out = []
    seen = set()
    for n, row in enumerate(_rows(), start=1):
        if n == 1 or len(row) < 3 or not str(row[2]).strip():
            continue
        if n in seen:
            continue
        status = str(row[3]).strip().lower() if len(row) > 3 else ""
        if status and status not in (STATUS_NEW, "новое", "new"):
            continue
        item = get_suggestion(n)
        if item:
            out.append(item)
    return out


def all_suggestions() -> list[dict]:
    out = []
    for n, row in enumerate(_rows(), start=1):
        if n == 1 or len(row) < 3 or not str(row[2]).strip():
            continue
        item = get_suggestion(n)
        if item:
            item["зелёный"] = _is_green(n)
            out.append(item)
    return out


def add_suggestion(tutor: str, text: str, source: str = "") -> int:
    """Добавляет предложение. Возвращает номер строки."""
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
        [[dt.date.today().strftime("%d.%m.%Y"), tutor, text, STATUS_NEW, source]])
    log(f"ПРЕДЛОЖЕНИЕ добавлено строка {row_num}: {tutor}")
    return row_num


def suggestions_of(tutor: str) -> list[dict]:
    out = []
    for n, row in enumerate(_rows(), start=1):
        if n == 1 or len(row) < 2 or not norm_name(row[1]):
            continue
        if norm_name(row[1]) != norm_name(tutor):
            continue
        out.append({
            "дата": row[0] if row else "",
            "текст": row[2] if len(row) > 2 else "",
            "статус": row[3] if len(row) > 3 else "",
            "row": n,
        })
    return out
