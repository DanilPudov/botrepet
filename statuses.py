"""
Статус оплаты по отметкам в листе «История».

Правила (как в таблице отмечает организатор):
  сумма зелёная  -> paid     (✅ оплачено, подтверждено выпиской или вручную)
  сумма красная  -> no       (❌ не подтверждено)
  значение «0» или пусто -> deleted (🚫 оплаты нет / удалено)

Ключ: (репетитор, дата, ученик, сумма).
"""
from __future__ import annotations

import datetime as dt

from sheets_util import get_values, load_config, norm_name

GREEN = (0.0, 0.72, 0.25)
RED = (0.9, 0.15, 0.15)


def _color_close(color: dict | None, ref: tuple) -> bool:
    if not color:
        return False
    try:
        return (abs(float(color.get("red", 0)) - ref[0]) < 0.25
                and abs(float(color.get("green", 0)) - ref[1]) < 0.25
                and abs(float(color.get("blue", 0)) - ref[2]) < 0.25)
    except (TypeError, ValueError):
        return False


def _parse(value) -> tuple[dt.date | None, int | None]:
    try:
        d = dt.datetime.strptime(str(value).strip(), "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return None, None
    return d, None


def _sum(value) -> int | None:
    s = str(value).replace("\u00a0", "").replace(" ", "").replace("₽", "").replace(",", ".")
    if not s:
        return None
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def load_status_map() -> dict[tuple, str]:
    """{(репетитор, дата, ученик, сумма): 'paid'|'no'|'deleted'}."""
    cfg = load_config()
    sh_id = cfg["sheets"]["history"]
    title = cfg["sheets"]["history_sheet"]

    values = get_values(sh_id, f"'{title}'!A1:J")
    from sheets_util import sheets
    grid = sheets().spreadsheets().get(
        spreadsheetId=sh_id, ranges=[f"'{title}'!A1:J"],
        includeGridData=True).execute()

    row_data = (grid.get("sheets", [{}])[0].get("data", [{}])[0]
                .get("rowData", []))

    out: dict[tuple, str] = {}
    for idx, row in enumerate(values):
        if len(row) < 5:
            continue
        tutor = norm_name(row[2]) if len(row) > 2 else ""
        if not tutor:
            continue
        try:
            day = dt.datetime.strptime(str(row[1]).strip(), "%d.%m.%Y").date()
        except (ValueError, TypeError):
            continue
        student = norm_name(row[3]) if len(row) > 3 else ""
        raw_sum = str(row[4]).strip() if len(row) > 4 else ""
        amount = _sum(raw_sum)

        # цвет суммы (колонка E, индекс 4)
        fg = None
        if idx < len(row_data):
            cells = row_data[idx].get("values", [])
            if len(cells) > 4:
                fmt = cells[4].get("userEnteredFormat", {}) or {}
                fg = (fmt.get("textFormat") or {}).get("foregroundColor")

        if not raw_sum or raw_sum == "0":
            status = "deleted"
        elif _color_close(fg, GREEN):
            status = "paid"
        elif _color_close(fg, RED):
            status = "no"
        else:
            continue  # обычный (не помеченный) цвет — статус не показываем

        if amount is None:
            amount = 0
        out[(tutor, day, student, amount)] = status
    return out
