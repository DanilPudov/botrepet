"""
Запись строки о занятии в таблицу и чтение занятий из таблицы.

Куда писать — config.json:
  "write": {"target": "history"}  -> лист «Занятия» в таблице «История» (тест)
  "write": {"target": "recept"}   -> лист «Чеки» в таблице «Чеки Репет» (боевой)

Формат строки (A..J) одинаковый:
  A Время (ПО ЧЕКУ) | B Дата | C Репетитор | D Имя ученика (имя родителя) |
  E Сумма | F Комментарий | G Первое ли занятие | H Как прошло занятие |
  I Будут заниматься? | J Комиссия

Запись всегда идёт в строку ПОСЛЕ последней занятой (ничего не перебивает).
Оформление как в «Чеки Репет»: белый текст, по центру, без заливки фона,
тонкие серые границы.
"""
from __future__ import annotations

import datetime as dt

from sheets_util import get_values, update_values, load_config, log, norm_name

KEY_COLS = (2, 3, 5)  # C репетитор, D ученик, F сумма (0-based)
FIRST_DATA_ROW = 2

HEADER = ["Время (ПО ЧЕКУ)", "Дата", "Репетитор", "Имя ученика",
          "Имя родителя", "Сумма", "Комментарий", "Первое ли занятие",
          "Как прошло занятие", "Будут заниматься?",
          "Комиссия (заполняется при первом занятии)", "ID занятия"]

TARGET_HISTORY_SHEET = "Занятия"

WHITE = {"red": 1, "green": 1, "blue": 1}
BORDER = {"red": 0.2627451, "green": 0.2627451, "blue": 0.2627451}


# --------------------------------------------------------------------------- #
# Куда пишем
# --------------------------------------------------------------------------- #
def _write_cfg() -> dict:
    return (load_config().get("write") or {})


PERSONAL_SHEETS = {
    "Данил Пудов": "История Данил",
    "Вика": "История Вика",
}


def sheet_for_tutor(tutor: str) -> str:
    """Лист назначения для репетитора (у Данила и Вики — свои листы)."""
    t = norm_name(tutor)
    for name, title in PERSONAL_SHEETS.items():
        if norm_name(name) == t or (name == "Вика" and "вика" in t):
            return title
    return _write_cfg().get("history_sheet") or TARGET_HISTORY_SHEET


# --- кэш чтения (сокращает число запросов к Google API) ---
import time as _time

_READ_CACHE: dict = {}
_READ_TTL = 25.0  # секунд


def _cache_get(key):
    item = _READ_CACHE.get(key)
    if not item:
        return None
    ts, value = item
    if _time.time() - ts > _READ_TTL:
        _READ_CACHE.pop(key, None)
        return None
    return value


def _cache_put(key, value):
    _READ_CACHE[key] = (_time.time(), value)
    if len(_READ_CACHE) > 40:
        oldest = sorted(_READ_CACHE.items(), key=lambda kv: kv[1][0])[:10]
        for k, _ in oldest:
            _READ_CACHE.pop(k, None)
    return value


def invalidate_cache() -> None:
    """Сбросить кэш после записи в таблицу."""
    _READ_CACHE.clear()


def _target(tutor: str = "") -> tuple[str, str]:
    """Куда писать занятие.

    Данил Пудов / Вика — в свой лист «История Данил» / «История Вика».
    Все остальные репетиторы — в «Чеки Репет» → лист «Чеки».
    """
    cfg = load_config()
    sheet = sheet_for_tutor(tutor) if tutor else PERSONAL_SHEETS["Данил Пудов"]
    if sheet in PERSONAL_SHEETS.values():
        return cfg["sheets"]["history"], sheet
    return cfg["sheets"]["recept"], cfg["sheets"]["recept_checks_sheet"]


def target_name() -> str:
    cfg = load_config()
    if _write_cfg().get("target", "recept") == "history":
        return f"«История» / {_target()[1]}"
    return f"«Чеки Репет» / {cfg['sheets']['recept_checks_sheet']}"


def ensure_target_sheet(title: str | None = None,
                        sh_id: str | None = None) -> str:
    """Создать лист назначения с заголовком, если его нет."""
    from sheets_util import sheets
    if sh_id is None:
        sh_id, default_title = _target()
        title = title or default_title
    title = title or "Чеки"
    meta = sheets().spreadsheets().get(spreadsheetId=sh_id).execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if title not in titles:
        sheets().spreadsheets().batchUpdate(
            spreadsheetId=sh_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]}).execute()
        update_values(sh_id, f"'{title}'!A1:L1", [HEADER])
        log(f"Создан лист «{title}»")
    else:
        # Шапка может быть защищена владельцем таблицы — не ломаем запись.
        try:
            cur = get_values(sh_id, f"'{title}'!A1:L1")
            if not cur or len(cur[0]) < 12:
                update_values(sh_id, f"'{title}'!A1:L1", [HEADER])
        except Exception as e:  # noqa: BLE001
            log(f"Шапка «{title}» не обновлена (защита?): {e}")
    return title



# --------------------------------------------------------------------------- #
# Строки
# --------------------------------------------------------------------------- #
def _cell(row: list, idx: int) -> str:
    return str(row[idx]).strip() if len(row) > idx else ""


def is_data_row(row: list) -> bool:
    return any(_cell(row, i) for i in KEY_COLS)


def next_lesson_id(title: str | None = None) -> int:
    """Следующий ID: префикс DDMM + счётчик (12111, 12112, … 121110)."""
    prefix = id_prefix()
    mx = 0
    for lid in _ids_from_all():
        if not lid.startswith(prefix):
            continue
        suffix = lid[len(prefix):]
        if suffix.isdigit():
            mx = max(mx, int(suffix))
    return int(prefix + str(mx + 1))


def find_next_free_row(title: str | None = None,
                       sh_id: str | None = None) -> int:
    """Первая строка, где нет занятия (ключевые поля пусты).

    Пустые строки с «нет/Отлично/Да» по умолчанию занятыми не считаются —
    иначе бот писал бы далеко вниз, и записи терялись из виду.
    """
    if sh_id is None:
        sh_id, default_title = _target()
        title = title or default_title
    title = title or "Чеки"
    try:
        rows = get_values(sh_id, f"'{title}'!A1:L")
    except Exception:  # noqa: BLE001 — листа ещё нет
        return FIRST_DATA_ROW
    last = FIRST_DATA_ROW - 1
    for n, row in enumerate(rows, start=1):
        if n < FIRST_DATA_ROW:
            continue
        if any(_cell(row, i) for i in (0, 1, 2, 3, 5, 11)):
            last = n
        elif last >= FIRST_DATA_ROW:
            # нашли дырку после данных — пишем в неё
            return n
    return max(last + 1, FIRST_DATA_ROW)


def _format_row(sh_id: str, title: str, row: int) -> None:
    """Белый текст, по центру, без фона, тонкие серые границы — как в «Чеках»."""
    from sheets_util import sheets
    rng = {"sheetId": None, "startRowIndex": row - 1, "endRowIndex": row,
           "startColumnIndex": 0, "endColumnIndex": 10}
    requests = [
        {"repeatCell": {
            "range": rng,
            "cell": {"userEnteredFormat": {
                "textFormat": {"foregroundColor": WHITE},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
            }},
            "fields": ("userEnteredFormat.textFormat.foregroundColor,"
                       "userEnteredFormat.horizontalAlignment,"
                       "userEnteredFormat.verticalAlignment")}},
        {"repeatCell": {
            "range": {**rng, "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "TIME",
                                                            "pattern": "h:mm"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {
            "range": {**rng, "startColumnIndex": 1, "endColumnIndex": 2},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE",
                                                            "pattern": "dd.mm.yyyy"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        {"updateBorders": {
            "range": rng,
            "top": {"style": "SOLID", "width": 1, "color": BORDER},
            "bottom": {"style": "SOLID", "width": 1, "color": BORDER},
            "left": {"style": "SOLID", "width": 1, "color": BORDER},
            "right": {"style": "SOLID", "width": 1, "color": BORDER},
            "innerHorizontal": {"style": "SOLID", "width": 1, "color": BORDER},
            "innerVertical": {"style": "SOLID", "width": 1, "color": BORDER},
        }},
    ]
    # sheetId нужен для диапазона — выясняем по названию листа
    meta = sheets().spreadsheets().get(spreadsheetId=sh_id).execute()
    sid = next(s["properties"]["sheetId"] for s in meta["sheets"]
               if s["properties"]["title"] == title)
    for req in requests:
        req_list = req["repeatCell"]["range"] if "repeatCell" in req else req["updateBorders"]["range"]
        req_list["sheetId"] = sid
    sheets().spreadsheets().batchUpdate(
        spreadsheetId=sh_id, body={"requests": requests}).execute()


def append_check(record: dict) -> int:
    """Дописывает занятие в свободную строку. Возвращает номер строки."""
    tutor = record.get("репетитор", "")
    sh_id, title = _target(tutor)
    ensure_target_sheet(title, sh_id)

    row_num = find_next_free_row(title, sh_id)
    _lid = str(next_lesson_id(title))
    values = [[
        record.get("время", ""),
        record.get("дата", ""),
        record.get("репетитор", ""),
        record.get("ученик", ""),
        record.get("родитель", ""),
        record.get("сумма", ""),
        record.get("комментарий", ""),
        record.get("первое", "нет"),
        record.get("как_прошло", "Отлично"),
        record.get("будут", "Да"),
        record.get("комиссия", ""),
        _lid,
    ]]
    update_values(sh_id, f"'{title}'!A{row_num}:L{row_num}", values,
                  value_input_option="USER_ENTERED")
    invalidate_cache()

    # сверка: перечитали строку
    back = get_values(sh_id, f"'{title}'!A{row_num}:F{row_num}")
    got = back[0] if back else []
    if len(got) < 6 or not _cell(got, 2) or not _cell(got, 3) or not _cell(got, 5):
        raise RuntimeError(f"Не удалось подтвердить запись в строку {row_num}: {got}")

    try:
        _format_row(sh_id, title, row_num)
    except Exception as e:  # noqa: BLE001 — формат не должен ломать запись
        log(f"Не удалось оформить строку {row_num}: {e}")

    log(f"CHECK записан в «{title}» строку {row_num}: {values[0]}")
    # Красим только при явном подтверждении оплаты.
    # Иначе оставляем белым — «не проверено».
    if record.get("оплачено") is True:
        try:
            set_lesson_paid(_lid, True, title=title)
        except Exception as e:  # noqa: BLE001
            log(f"Не удалось покрасить сумму в зелёный: {e}")
    elif record.get("оплачено") is False:
        try:
            set_lesson_paid(_lid, False, title=title)
        except Exception as e:  # noqa: BLE001
            log(f"Не удалось покрасить сумму в красный: {e}")
    # цвет имени репетитора, если настроен
    try:
        _tcolor = get_tutor_color(tutor)
        if _tcolor is not None:
            _color_cell(sh_id, title, row_num, 2, _tcolor)
    except Exception as e:  # noqa: BLE001
        log(f"Не смог покрасить имя репетитора: {e}")
    return row_num


# --------------------------------------------------------------------------- #
# Чтение занятий из таблицы (включая строки, добавленные руками)
# --------------------------------------------------------------------------- #
def _parse_day(value) -> dt.date | None:
    try:
        return dt.datetime.strptime(str(value).strip(), "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return None


def _parse_sum(value) -> int:
    s = str(value).replace("\u00a0", "").replace(" ", "").replace("₽", "").replace(",", ".")
    try:
        return int(round(float(s)))
    except ValueError:
        return 0


def _rows_from(sh_id: str, title: str) -> list[dict]:
    """Читает занятия по схеме:
    A время | B дата | C репетитор | D имя ученика | E имя родителя |
    F сумма | G комментарий | H первое | I как прошло | J будут |
    K комиссия | L ID занятия
    """
    try:
        rows = get_values(sh_id, f"'{title}'!A1:L")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for row in rows[1:]:
        if len(row) < 6:
            continue
        tutor = norm_name(row[2]) if len(row) > 2 else ""
        d = _parse_day(row[1]) if len(row) > 1 else None
        if not tutor or d is None:
            continue
        student = _cell(row, 3)
        parent = _cell(row, 4)
        if not parent and "(" in student and student.rstrip().endswith(")"):
            # старый лист «История»: D = «Ученик (Родитель)»
            _i = student.index("(")
            parent = student[_i + 1:student.rindex(")")].strip()
            student = student[:_i].strip()
        out.append({
            "дата": d,
            "время": _cell(row, 0),
            "репетитор": tutor,
            "ученик": student,
            "родитель": parent,
            "сумма": _parse_sum(_cell(row, 5)),
            "комментарий": _cell(row, 6),
            "первое": _cell(row, 7),
            "как_прошло": _cell(row, 8),
            "будут": _cell(row, 9),
            "комиссия": _cell(row, 10),
            "id": _cell(row, 11),
            "лист": _cell(row, 12) if len(row) > 12 else title,
        })
    return out



GREEN = {"red": 0.0, "green": 0.6, "blue": 0.0}
RED = {"red": 0.9, "green": 0.1, "blue": 0.1}


def _sheet_gid(sh_id: str, title: str) -> int:
    from sheets_util import sheets
    meta = sheets().spreadsheets().get(spreadsheetId=sh_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == title:
            return s["properties"]["sheetId"]
    return 0


def _find_lesson_row(lesson_id, title: str | None = None) -> int | None:
    sh_id, default_title = _target()
    title = title or default_title
    try:
        rows = get_values(sh_id, f"'{title}'!A1:L")
    except Exception:  # noqa: BLE001
        return None
    for n, row in enumerate(rows, start=1):
        if n == 1:
            continue
        if len(row) > 11 and str(row[11]).strip() == str(lesson_id).strip():
            return n
    return None


def _all_sheet_targets() -> list[tuple[str, str]]:
    """Все места, где лежат занятия: [(id таблицы, лист)]."""
    from sheets_util import sheets
    cfg = load_config()
    hist_id = cfg["sheets"]["history"]
    out: list[tuple[str, str]] = []
    try:
        meta = sheets().spreadsheets().get(spreadsheetId=hist_id).execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    except Exception:  # noqa: BLE001
        titles = []
    personal = [t for t in titles if t in ("История Данил", "История Вика")]
    others = [t for t in titles
              if (t == "Занятия" or t == "История" or t.startswith("История "))
              and t not in personal]
    for t in personal + others:
        out.append((hist_id, t))
    # «Чеки Репет» → «Чеки» (основной лист репетиторов)
    out.append((cfg["sheets"]["recept"], cfg["sheets"]["recept_checks_sheet"]))
    return out


def _all_lesson_sheets() -> list[str]:
    """Названия листов с занятиями (для совместимости)."""
    return [t for _, t in _all_sheet_targets()]


def _find_lesson_anywhere(lesson_id) -> tuple[str, str, int] | None:
    """Найти (id таблицы, лист, строка) занятия по ID во всех листах."""
    lid = str(lesson_id).strip()
    for sh_id, title in _all_sheet_targets():
        try:
            rows = get_values(sh_id, f"'{title}'!A1:L")
        except Exception:  # noqa: BLE001
            continue
        for n, row in enumerate(rows, start=1):
            if n == 1 or len(row) <= 11:
                continue
            if str(row[11]).strip() == lid:
                return sh_id, title, n
    return None


NAME_COLORS = {
    "белый": {"red": 1, "green": 1, "blue": 1},
    "чёрный": {"red": 0, "green": 0, "blue": 0},
    "черный": {"red": 0, "green": 0, "blue": 0},
    "красный": {"red": 0.9, "green": 0.1, "blue": 0.1},
    "зелёный": {"red": 0.0, "green": 0.6, "blue": 0.0},
    "зеленый": {"red": 0.0, "green": 0.6, "blue": 0.0},
    "синий": {"red": 0.1, "green": 0.3, "blue": 0.85},
    "голубой": {"red": 0.2, "green": 0.65, "blue": 0.9},
    "жёлтый": {"red": 0.95, "green": 0.75, "blue": 0.0},
    "желтый": {"red": 0.95, "green": 0.75, "blue": 0.0},
    "оранжевый": {"red": 0.95, "green": 0.5, "blue": 0.0},
    "фиолетовый": {"red": 0.55, "green": 0.25, "blue": 0.85},
    "розовый": {"red": 0.95, "green": 0.45, "blue": 0.7},
    "серый": {"red": 0.5, "green": 0.5, "blue": 0.5},
}


def parse_color(text: str) -> dict | None:
    """Цвет из названия («красный») или HEX (#FF0000 / FF0000 / F00)."""
    s = str(text or "").strip().lower().replace("ё", "е")
    if not s:
        return None
    if s in NAME_COLORS:
        return NAME_COLORS[s]
    for name, color in NAME_COLORS.items():
        if name.replace("ё", "е") == s:
            return color
    hex_part = s.lstrip("#")
    if len(hex_part) == 3 and all(c in "0123456789abcdef" for c in hex_part):
        hex_part = "".join(c * 2 for c in hex_part)
    if len(hex_part) == 6 and all(c in "0123456789abcdef" for c in hex_part):
        return {
            "red": int(hex_part[0:2], 16) / 255.0,
            "green": int(hex_part[2:4], 16) / 255.0,
            "blue": int(hex_part[4:6], 16) / 255.0,
        }
    return None


def set_name_color(lesson_id, color_text: str,
                   title: str | None = None) -> bool:
    """Покрасить имя ученика (колонка D) в заданный цвет."""
    color = parse_color(color_text)
    if color is None:
        return False
    found = None
    if title:
        sh, t = _sheet_spreadsheet(title)
        row_num = _find_row_by_lesson_id(lesson_id, title)
        if row_num:
            found = (sh, t, row_num)
    if not found:
        found = _find_lesson_anywhere(lesson_id)
    if not found:
        return False
    from sheets_util import sheets
    sh_id, sheet_title, row = found
    sheets().spreadsheets().batchUpdate(
        spreadsheetId=sh_id,
        body={"requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": _sheet_gid(sh_id, sheet_title),
                    "startRowIndex": row - 1,
                    "endRowIndex": row,
                    "startColumnIndex": 3,   # D — имя ученика
                    "endColumnIndex": 4,
                },
                "cell": {"userEnteredFormat": {
                    "textFormat": {"foregroundColor": color}}},
                "fields": "userEnteredFormat.textFormat.foregroundColor",
            }
        }]}).execute()
    invalidate_cache()
    log(f"ЦВЕТ имени {lesson_id} ({sheet_title}): {color_text}")
    return True


def get_tutor_color(tutor: str) -> dict | None:
    """Сохранённый цвет имени репетитора (из настроек)."""
    from sheets_util import load_state
    colors = (load_state().get("tutor_colors") or {})
    raw = colors.get(str(tutor or "").strip().lower())
    return parse_color(raw) if raw else None


def _color_cell(sh_id: str, sheet_title: str, row: int, column: int,
                color: dict) -> None:
    """Покрасить текст одной ячейки (column: 0=A, 2=C, 3=D)."""
    from sheets_util import sheets
    sheets().spreadsheets().batchUpdate(
        spreadsheetId=sh_id,
        body={"requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": _sheet_gid(sh_id, sheet_title),
                    "startRowIndex": row - 1,
                    "endRowIndex": row,
                    "startColumnIndex": column,
                    "endColumnIndex": column + 1,
                },
                "cell": {"userEnteredFormat": {
                    "textFormat": {"foregroundColor": color}}},
                "fields": "userEnteredFormat.textFormat.foregroundColor",
            }
        }]}).execute()


def apply_tutor_color(tutor: str, color: dict | None = None) -> int:
    """Покрасить имя репетитора (колонка C) во всех его занятиях.

    color=None — взять сохранённый цвет. Возвращает число строк.
    """
    if color is None:
        color = get_tutor_color(tutor)
    if color is None:
        return 0
    sh_id, sheet_title = _target(tutor)
    try:
        rows = get_values(sh_id, f"'{sheet_title}'!A1:L")
    except Exception:  # noqa: BLE001
        return 0
    count = 0
    want = norm_name(str(tutor or ""))
    for n, r in enumerate(rows, 1):
        if n < FIRST_DATA_ROW:
            continue
        if norm_name(_cell(r, 2)) == want:
            try:
                _color_cell(sh_id, sheet_title, n, 2, color)
                count += 1
            except Exception:  # noqa: BLE001
                pass
    return count


def set_tutor_color(tutor: str, color_text: str) -> tuple[bool, int]:
    """Сохранить цвет репетитора и покрасить существующие строки."""
    from sheets_util import load_state, save_state
    color = parse_color(color_text)
    if color is None:
        return False, 0
    st = load_state()
    st.setdefault("tutor_colors", {})[str(tutor or "").strip().lower()] = \
        str(color_text).strip()
    save_state(st)
    count = apply_tutor_color(tutor, color)
    log(f"ЦВЕТ репетитора {tutor}: {color_text} (строк: {count})")
    return True, count


def get_row_lids(row_num: int, title: str | None = None) -> str:
    """ID занятия (колонка L) по номеру строки. Пустая строка, если нет."""
    sh_id, sheet_title = _sheet_spreadsheet(title)
    try:
        vals = get_values(sh_id, f"'{sheet_title}'!L{row_num}")
    except Exception:  # noqa: BLE001
        return ""
    if not vals or not vals[0]:
        return ""
    return str(vals[0][0]).strip()


def set_lesson_paid(lesson_id, paid: bool, title: str | None = None) -> bool:
    """Покрасить сумму занятия (колонка F): зелёный = подтверждено."""
    found = None
    if title:
        sh, t = _sheet_spreadsheet(title)
        row_num = _find_row_by_lesson_id(lesson_id, title)
        if row_num:
            found = (sh, t, row_num)
    if not found:
        found = _find_lesson_anywhere(lesson_id)
    if not found:
        return False
    from sheets_util import sheets
    sh_id, sheet_title, row = found
    color = GREEN if paid else RED
    sheets().spreadsheets().batchUpdate(
        spreadsheetId=sh_id,
        body={"requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": _sheet_gid(sh_id, sheet_title),
                    "startRowIndex": row - 1,
                    "endRowIndex": row,
                    "startColumnIndex": 5,   # F — сумма
                    "endColumnIndex": 6,
                },
                "cell": {"userEnteredFormat": {
                    "textFormat": {"foregroundColor": color}}},
                "fields": "userEnteredFormat.textFormat.foregroundColor",
            }
        }]}).execute()
    invalidate_cache()
    log(f"ЗАНЯТИЕ {lesson_id} ({sheet_title}): "
        f"{'подтверждено (зелёный)' if paid else 'не подтверждено (красный)'}")
    return True


def lesson_flags() -> dict[str, str]:
    """{id занятия: 'paid'|'no'|''} по цвету суммы (колонка F).

    Читает ВСЕ листы занятий: «Занятия», «История Данил», «История Вика»,
    «История». ID берётся из колонки L.
    """
    cached = _cache_get(("flags",))
    if cached is not None:
        return cached
    from sheets_util import sheets
    cfg = load_config()
    sh_id = cfg["sheets"]["history"]
    try:
        meta = sheets().spreadsheets().get(spreadsheetId=sh_id).execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    except Exception:  # noqa: BLE001
        return {}
    sheets_to_read = [t for t in titles
                      if t == "Занятия" or t.startswith("История")]
    out: dict[str, str] = {}
    for title in sheets_to_read:
        try:
            values = get_values(sh_id, f"'{title}'!A1:L")
            grid = sheets().spreadsheets().get(
                spreadsheetId=sh_id, ranges=[f"'{title}'!A1:L"],
                includeGridData=True,
                fields=("sheets(data(rowData(values("
                        "effectiveFormat/textFormat/foregroundColor))))")
            ).execute()
        except Exception as e:  # noqa: BLE001
            log(f"Не прочитал цвета «{title}»: {e}")
            continue
        row_data = (grid.get("sheets", [{}])[0].get("data", [{}])[0]
                    .get("rowData", []))
        for idx, row in enumerate(values):
            if len(row) < 12:
                continue
            lid = str(row[11]).strip()
            if not lid:
                continue
            fg = None
            if idx < len(row_data):
                cells = row_data[idx].get("values", [])
                if len(cells) > 5:  # сумма — колонка F (индекс 5)
                    fg = ((cells[5].get("effectiveFormat") or {})
                          .get("textFormat", {}).get("foregroundColor"))
            if not fg:
                out.setdefault(lid, "")
                continue
            r = float(fg.get("red", 0))
            g = float(fg.get("green", 0))
            if g > 0.4 and r < 0.4:
                out[lid] = "paid"
            elif r > 0.4 and g < 0.4:
                out[lid] = "no"
            else:
                out.setdefault(lid, "")
    return _cache_put(("flags",), out)


def id_prefix() -> str:
    """Префикс ID на сегодня: «1211» для 12.11 (DDMM)."""
    t = dt.date.today()
    return f"{t.day}{t.month:02d}"


def _ids_from_all() -> list[str]:
    """Все ID из листов занятий + «Чеки Репет»."""
    cfg = load_config()
    hist_id = cfg["sheets"]["history"]
    out: list[str] = []
    try:
        rows = get_values(hist_id, f"'{(_write_cfg().get('history_sheet') or TARGET_HISTORY_SHEET)}'!A1:L")
        out += [str(r[11]).strip() for r in rows[1:] if len(r) > 11 and str(r[11]).strip()]
    except Exception:  # noqa: BLE001
        pass
    try:
        meta = __import__("sheets_util").sheets().spreadsheets().get(
            spreadsheetId=hist_id).execute()
        for s in meta.get("sheets", []):
            t = s["properties"]["title"]
            if not (t == "Занятия" or t == "История"
                    or t.startswith("История ")):
                continue
            rows = get_values(hist_id, f"'{t}'!A1:L")
            out += [str(r[11]).strip() for r in rows[1:]
                    if len(r) > 11 and str(r[11]).strip()]
    except Exception:  # noqa: BLE001
        pass
    try:
        rec = cfg["sheets"]["recept"]
        rows = get_values(rec, f"'{cfg['sheets']['recept_checks_sheet']}'!A1:L")
        out += [str(r[11]).strip() for r in rows[1:]
                if len(r) > 11 and str(r[11]).strip()]
    except Exception:  # noqa: BLE001
        pass
    return out


def global_max_id() -> int:
    """Максимальный ID по ВСЕМ листам занятий (ID общие)."""
    cfg = load_config()
    hist_id = cfg["sheets"]["history"]
    from sheets_util import sheets
    try:
        meta = sheets().spreadsheets().get(spreadsheetId=hist_id).execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    except Exception:  # noqa: BLE001
        return 0
    mx = 0
    for t in titles:
        if not (t == "Занятия" or t == "История" or t.startswith("История ")):
            continue
        try:
            rows = get_values(hist_id, f"'{t}'!A1:L")
        except Exception:  # noqa: BLE001
            continue
        for row in rows[1:]:
            if len(row) > 11:
                try:
                    mx = max(mx, int(str(row[11]).strip()))
                except ValueError:
                    continue
    return mx


def ensure_checks_ids() -> int:
    """Проставить ID (колонка L) строкам «Чеки Репет» / «Чеки».

    Нумерация: префикс сегодняшней даты + счётчик (12111, 12112, …).
    Возвращает, сколько ID добавлено.
    """
    cfg = load_config()
    sh_id = cfg["sheets"]["recept"]
    title = cfg["sheets"]["recept_checks_sheet"]
    try:
        rows = get_values(sh_id, f"'{title}'!A1:L")
    except Exception:  # noqa: BLE001
        return 0
    if not rows:
        return 0
    prefix = id_prefix()
    mx = 0
    for row in rows[1:]:
        if len(row) > 11 and str(row[11]).strip().startswith(prefix):
            tail = str(row[11]).strip()[len(prefix):]
            if tail.isdigit():
                mx = max(mx, int(tail))
    # шапка «Чеков» защищена — пишем только по одной строке, пропуская ошибки
    added = 0
    failed = 0
    for n, row in enumerate(rows):
        if n == 0:
            continue
        cur = str(row[11]).strip() if len(row) > 11 else ""
        if cur:
            continue
        has_data = len(row) >= 4 and (str(row[2]).strip() or str(row[3]).strip())
        if not has_data:
            continue
        mx += 1
        try:
            update_values(sh_id, f"'{title}'!L{n + 1}",
                          [[prefix + str(mx)]])
            added += 1
        except Exception:  # noqa: BLE001
            failed += 1
    if added:
        invalidate_cache()
        log(f"ID добавлены в «{title}»: {added}"
            + (f" (не удалось из-за защиты: {failed})" if failed else ""))
    return added


def renumber_all_ids() -> dict[str, int]:
    """Перенумеровать ID во всех листах занятий так, чтобы они были уникальны.

    Возвращает {лист: сколько строк перенумеровано}.
    """
    cfg = load_config()
    hist_id = cfg["sheets"]["history"]
    result: dict[str, int] = {}
    next_id = 1
    for title in _all_lesson_sheets():
        try:
            rows = get_values(hist_id, f"'{title}'!A1:L")
        except Exception:  # noqa: BLE001
            continue
        if not rows:
            continue
        col = []
        changed = 0
        for n, row in enumerate(rows):
            if n == 0:
                col.append("ID занятия" if len(rows[0]) > 11 else "")
                continue
            has_data = len(row) >= 4 and (_cell(row, 2) or _cell(row, 3))
            if not has_data:
                col.append(str(row[11]).strip() if len(row) > 11 else "")
                continue
            old = str(row[11]).strip() if len(row) > 11 else ""
            new = str(next_id)
            next_id += 1
            if old != new:
                changed += 1
            col.append(new)
        if changed:
            try:
                update_values(hist_id, f"'{title}'!L1:L{len(rows)}",
                              [[v] for v in col])
                result[title] = changed
            except Exception as e:  # noqa: BLE001
                log(f"Не смог перенумеровать «{title}»: {e}")
    if result:
        invalidate_cache()
        log(f"ID перенумерованы: {result}")
    return result


def ensure_ids(sh_id: str, title: str, start_id: int | None = None) -> int:
    """Проставить ID (колонка L) всем строкам, где их нет. Одна запись.

    start_id — с какого номера начинать (чтобы ID не повторялись между листами).
    Возвращает, сколько ID добавлено.
    """
    try:
        rows = get_values(sh_id, f"'{title}'!A1:L")
    except Exception:  # noqa: BLE001
        return 0
    if not rows:
        return 0
    max_id = start_id if start_id is not None else 0
    col = []
    for n, row in enumerate(rows, start=1):
        lid = str(row[11]).strip() if len(row) > 11 else ""
        if lid:
            try:
                max_id = max(max_id, int(lid))
            except ValueError:
                pass
        col.append(lid)
    added = 0
    changed = False
    for n in range(1, len(rows)):  # без шапки
        row = rows[n]
        if n < 1:
            continue
        has_data = len(row) >= 3 and (_cell(row, 2) or _cell(row, 3))
        if not has_data or col[n]:
            continue
        max_id += 1
        col[n] = str(max_id)
        added += 1
        changed = True
    if changed:
        try:
            update_values(sh_id, f"'{title}'!L2:L{len(rows)}",
                          [[v] for v in col[1:]])
            invalidate_cache()
            log(f"ID добавлены в «{title}»: {added}")
        except Exception as e:  # noqa: BLE001
            log(f"Не смог проставить ID в «{title}»: {e}")
            return 0
    return added


def read_lessons(include_history: bool = False,
                 with_checks: bool = False) -> list[dict]:
    """Занятия из всех листов «Истории» (+ «Чеки Репет» при with_checks)."""
    key = ("lessons", bool(include_history), bool(with_checks))
    cached = _cache_get(key)
    if cached is not None:
        return cached
    cfg = load_config()
    hist_id = cfg["sheets"]["history"]
    from sheets_util import sheets
    try:
        meta = sheets().spreadsheets().get(spreadsheetId=hist_id).execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    except Exception:  # noqa: BLE001
        titles = []
    # личные листы (История Данил / История Вика) — в приоритете
    personal = [t for t in titles if t in ("История Данил", "История Вика")]
    other = [t for t in titles
             if (t.startswith("История ") or t == "История")
             and t not in personal]
    lesson_sheets = personal + other
    if not lesson_sheets:
        lesson_sheets = [_write_cfg().get("history_sheet") or TARGET_HISTORY_SHEET]
    lessons: list[dict] = []
    for t in lesson_sheets:
        for x in _rows_from(hist_id, t):
            x["лист"] = t
            lessons.append(x)

    # «Чеки» — основной лист занятий репетиторов, читаем всегда
    try:
        ensure_checks_ids()
    except Exception as e:  # noqa: BLE001
        log(f"ID в «Чеках» не проставлены: {e}")
    try:
        for x in _rows_from(cfg["sheets"]["recept"],
                            cfg["sheets"]["recept_checks_sheet"]):
            x["лист"] = "Чеки"
            lessons.append(x)
    except Exception as e:  # noqa: BLE001
        log(f"Не прочитал «Чеки»: {e}")

    if include_history:
        hist_sheet = cfg["sheets"]["history_sheet"]
        if hist_sheet not in lesson_sheets:
            for x in _rows_from(hist_id, hist_sheet):
                x["лист"] = hist_sheet
                lessons.append(x)

    seen: set[tuple] = set()
    unique: list[dict] = []
    for les in lessons:
        dedup_key = (les["репетитор"], les["дата"], norm_name(les["ученик"]),
                     les["сумма"], str(les.get("время", "")))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        unique.append(les)
    return _cache_put(key, unique)


def _sheet_spreadsheet(title: str | None) -> tuple[str, str]:
    """(id таблицы, лист) для известного названия листа."""
    cfg = load_config()
    title = title or (_write_cfg().get("history_sheet") or TARGET_HISTORY_SHEET)
    if title == cfg["sheets"]["recept_checks_sheet"]:
        return cfg["sheets"]["recept"], title
    return cfg["sheets"]["history"], title


def _find_row_by_lesson_id(lesson_id: str,
                           title: str | None = None) -> int | None:
    sh_id, title = _sheet_spreadsheet(title)
    try:
        rows = get_values(sh_id, f"'{title}'!A1:L")
    except Exception:  # noqa: BLE001
        return None
    lid = str(lesson_id).strip()
    for n, row in enumerate(rows, start=1):
        if n == 1:
            continue
        if len(row) > 11 and str(row[11]).strip() == lid:
            return n
    return None


def update_lesson(lesson_id: str, record: dict,
                  title: str | None = None) -> bool:
    """Перезаписать строку занятия по ID (A..L, с родителем)."""
    found = None
    if title:
        sh, t = _sheet_spreadsheet(title)
        row_num = _find_row_by_lesson_id(lesson_id, title)
        if row_num:
            found = (sh, t, row_num)
    if not found:
        found = _find_lesson_anywhere(lesson_id)
    if not found:
        return False
    sh_id, sheet_title, row = found
    values = [[
        record.get("время", ""),
        record.get("дата", ""),
        record.get("репетитор", ""),
        record.get("ученик", ""),
        record.get("родитель", ""),
        record.get("сумма", ""),
        record.get("комментарий", ""),
        record.get("первое", "нет"),
        record.get("как_прошло", "Отлично"),
        record.get("будут", "Да"),
        record.get("комиссия", ""),
        str(lesson_id),
    ]]
    update_values(sh_id, f"'{sheet_title}'!A{row}:L{row}", values)
    log(f"CHECK изменён id={lesson_id} ({sheet_title}) строка {row}")
    invalidate_cache()
    return True


def delete_lesson(lesson_id: str, title: str | None = None) -> bool:
    """Очистить строку занятия по ID (A..L, вместе с ID)."""
    from sheets_util import clear_values
    found = None
    if title:
        sh, t = _sheet_spreadsheet(title)
        row_num = _find_row_by_lesson_id(lesson_id, title)
        if row_num:
            found = (sh, t, row_num)
    if not found:
        found = _find_lesson_anywhere(lesson_id)
    if not found:
        return False
    sh_id, sheet_title, row = found
    clear_values(sh_id, f"'{sheet_title}'!A{row}:L{row}")
    log(f"CHECK удалён id={lesson_id} ({sheet_title}) строка {row}")
    invalidate_cache()
    return True


def read_rows() -> list[list[str]]:
    """Сырые строки листа назначения (совместимость)."""
    sh_id, title = _target()
    return get_values(sh_id, f"'{title}'!A1:J")


def today_ru() -> str:
    return dt.date.today().strftime("%d.%m.%Y")


def now_hhmm() -> str:
    return dt.datetime.now().strftime("%H:%M")


if __name__ == "__main__":
    print("Пишем в:", target_name())
    print("Следующая свободная строка:", find_next_free_row())
