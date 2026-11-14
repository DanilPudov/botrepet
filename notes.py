"""
Заметки репетиторов: лист «Заметки» в таблице «История».

Колонки (A..C):
  A Репетитор | B Заметка | C Обновлено

Одна строка на репетитора. Строка ищется по колонке A нормализованным
сравнением (norm_name). Запись — только update_values с точным диапазоном
(values.append запрещён, см. памятку).
"""
from __future__ import annotations

import datetime as dt

import workspaces
import sheets_util
from sheets_util import load_config, log, norm_name

SHEET = "Заметки"
COLUMNS = ["Репетитор", "Заметка", "Обновлено"]
DATE_FMT = "%d.%m.%Y"


# --------------------------------------------------------------------------- #
# Вспомогательное
# --------------------------------------------------------------------------- #
def _history_id() -> str:
    return workspaces.spreadsheet()


def _header_matches(row: list[str] | None) -> bool:
    """Строка — это наша шапка A1:C1?"""
    if not row or len(row) < len(COLUMNS):
        return False
    got = [norm_name(c).lower() for c in row[: len(COLUMNS)]]
    return got == [c.lower() for c in COLUMNS]


def _rows() -> list[list[str]]:
    try:
        ensure_sheet()
    except Exception as e:  # noqa: BLE001
        log(f"Не смог подготовить лист «{SHEET}»: {e}")
    return sheets_util.get_values(_history_id(), f"'{SHEET}'!A1:C")


def _last_row(rows: list[list[str]]) -> int:
    """Номер последней занятой строки (шапка = 1, если лист пуст)."""
    last = 1
    for n, row in enumerate(rows, start=1):
        if any(str(x).strip() for x in row):
            last = n
    return last


def _find(tutor: str) -> tuple[int, list[str]] | None:
    """(номер строки, строка) для репетитора или None."""
    key = norm_name(tutor)
    if not key:
        return None
    for n, row in enumerate(_rows(), start=1):
        if n == 1 and _header_matches(row):
            continue
        if not row or not norm_name(row[0]):
            continue
        if norm_name(row[0]) == key:
            return n, row
    return None


# --------------------------------------------------------------------------- #
# Лист
# --------------------------------------------------------------------------- #
def ensure_sheet() -> str:
    """Создать лист «Заметки» при отсутствии, иначе дотянуть шапку A1:C1."""
    from sheets_util import sheets

    sh_id = _history_id()
    if SHEET not in sheets_util.sheet_titles(sh_id):
        sheets().spreadsheets().batchUpdate(
            spreadsheetId=sh_id,
            body={"requests": [{"addSheet": {"properties": {"title": SHEET}}}]}
        ).execute()
        sheets_util.update_values(sh_id, f"'{SHEET}'!A1:C1", [COLUMNS])
        sheets_util.forget_sheet_titles(sh_id)
        log(f"Создан лист «{SHEET}»")
        return SHEET
    header = sheets_util.get_values(sh_id, f"'{SHEET}'!A1:C1")
    if not _header_matches(header[0] if header else None):
        sheets_util.update_values(sh_id, f"'{SHEET}'!A1:C1", [COLUMNS])
        log(f"Лист «{SHEET}»: шапка обновлена")
    return SHEET


# --------------------------------------------------------------------------- #
# Чтение
# --------------------------------------------------------------------------- #
def all_notes() -> list[dict]:
    """Все заметки: [{"репетитор", "заметка", "обновлено", "row"}]."""
    out: list[dict] = []
    for n, row in enumerate(_rows(), start=1):
        if n == 1 and _header_matches(row):
            continue
        if not row or not norm_name(row[0]):
            continue
        out.append({
            "репетитор": norm_name(row[0]),
            "заметка": str(row[1]) if len(row) > 1 else "",
            "обновлено": str(row[2]) if len(row) > 2 else "",
            "row": n,
        })
    return out


def get_note(tutor: str) -> str:
    """Заметка репетитора (пустая строка, если заметки нет)."""
    found = _find(tutor)
    if not found:
        return ""
    row = found[1]
    return str(row[1]).strip() if len(row) > 1 else ""


# --------------------------------------------------------------------------- #
# Запись
# --------------------------------------------------------------------------- #
def set_note(tutor: str, text: str) -> bool:
    """Обновить существующую строку репетитора или добавить новую в конец."""
    name = norm_name(tutor)
    if not name:
        return False
    ensure_sheet()
    sh_id = _history_id()
    today = dt.date.today().strftime(DATE_FMT)
    found = _find(tutor)
    if found:
        row_num = found[0]
        action = "обновлена"
    else:
        row_num = _last_row(_rows()) + 1
        action = "добавлена"
    sheets_util.update_values(
        sh_id, f"'{SHEET}'!A{row_num}:C{row_num}", [[name, text, today]])
    log(f"ЗАМЕТКА {action} строка {row_num}: {name}")
    return True


def clear_note(tutor: str) -> bool:
    """Очистить строку заметки. False — если строки репетитора нет."""
    found = _find(tutor)
    if not found:
        return False
    row_num = found[0]
    sheets_util.clear_values(_history_id(), f"'{SHEET}'!A{row_num}:C{row_num}")
    log(f"ЗАМЕТКА очищена строка {row_num}: {norm_name(tutor)}")
    return True
