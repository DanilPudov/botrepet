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

from sheets_util import get_values, update_values, load_config, log, norm_name, _exec

KEY_COLS = (2, 3, 5)  # C репетитор, D ученик, F сумма (0-based)
FIRST_DATA_ROW = 2

HEADER = ["Время (ПО ЧЕКУ)", "Дата", "Репетитор", "Имя ученика",
          "Имя родителя", "Сумма", "Комментарий", "Первое ли занятие",
          "Как прошло занятие", "Будут заниматься?",
          "Комиссия (заполняется при первом занятии)", "ID занятия"]

TARGET_HISTORY_SHEET = "Занятия"

WHITE = {"red": 1, "green": 1, "blue": 1}
BLACK = {"red": 0, "green": 0, "blue": 0}
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
_READ_TTL = 60.0  # секунд


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

    У Максима и Вики — своя отдельная таблица (если доступна).
    Данил Пудов — «История Данил» в главной таблице.
    Остальные репетиторы — «Чеки Репет» → лист «Чеки».
    """
    cfg = load_config()
    # личная таблица репетитора (Максим, Вика)
    try:
        import workspaces as _ws
        sid = _ws.workspace_id(tutor) if tutor else None
        if sid:
            return sid, _ws.WORKSPACE_LESSONS_SHEET
    except Exception as e:  # noqa: BLE001
        log(f"Не смог определить личную таблицу: {e}")
    sheet = sheet_for_tutor(tutor) if tutor else PERSONAL_SHEETS["Данил Пудов"]
    if sheet in PERSONAL_SHEETS.values():
        return cfg["sheets"]["history"], sheet
    return cfg["sheets"]["recept"], cfg["sheets"]["recept_checks_sheet"]


def target_name(tutor: str = "") -> str:
    """Куда записано занятие — человеческим языком, по факту."""
    cfg = load_config()
    try:
        import workspaces as _ws
        sid = _ws.workspace_id(tutor) if tutor else None
        if sid:
            owner = "Максима" if "Максим" in tutor else (
                "Вики" if "Вик" in tutor else "")
            base = (f"таблица {owner}".strip() if owner else "личная таблица")
            return f"{base} / «{_ws.WORKSPACE_LESSONS_SHEET}»"
    except Exception:  # noqa: BLE001
        pass
    if _write_cfg().get("target", "recept") == "history":
        return f"«История» / {_target(tutor)[1]}"
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


def _text_color(sh_id: str) -> dict:
    """Цвет текста: в личных таблицах (Максим, Вика) — чёрный, иначе белый."""
    try:
        import workspaces as _ws
        for sid in _ws.WORKSPACES.values():
            if sid == sh_id:
                return BLACK
    except Exception:  # noqa: BLE001
        pass
    return WHITE


def _format_row(sh_id: str, title: str, row: int) -> None:
    """Текст по центру, без фона, тонкие серые границы — как в «Чеках»."""
    from sheets_util import sheets
    rng = {"sheetId": None, "startRowIndex": row - 1, "endRowIndex": row,
           "startColumnIndex": 0, "endColumnIndex": 10}
    requests = [
        {"repeatCell": {
            "range": rng,
            "cell": {"userEnteredFormat": {
                "textFormat": {"foregroundColor": _text_color(sh_id)},
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
    # Сумма всегда остаётся БЕЛОЙ: подтверждает и перекрашивает человек,
    # а не бот. Цвет ставится вручную в таблице или кнопкой организатора.
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
_MONTHS_RU = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "ма": 5, "мая": 5, "май": 5,
    "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


def _parse_day(value) -> dt.date | None:
    """Дата из ячейки в разных форматах, встречающихся в таблице.

    Понимает: 15.11.2026, 15.11.26, 2026-11-15, 15/11/2026, 15,11,2026,
    16:09.2026 (опечатка), 19 апр было занятие, объекты date/datetime.
    """
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value or "").strip()
    if not s:
        return None

    # «15.11.26» и прочие разделители на точку/слэш/запятую/двоеточие
    norm = (s.replace("/", ".").replace(",", ".")
             .replace(":", ".").replace(";", "."))
    parts = [p for p in norm.split(".") if p]
    if len(parts) == 3 and all(p.strip().isdigit() for p in parts):
        a, b, cc = (int(p) for p in parts)
        # год в конце: 26 -> 2026, 2026 -> 2026
        if cc < 100:
            cc += 2000
        for day, month in ((a, b), (b, a)):
            if 1 <= day <= 31 and 1 <= month <= 12 and 2000 <= cc <= 2100:
                try:
                    return dt.date(cc, month, day)
                except ValueError:
                    continue

    # ISO: 2026-11-15
    try:
        return dt.datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        pass

    # «19 апр было занятие», «25апр было занятие»
    import re as _re
    m = _re.search(r"(\d{1,2})\s*([а-яё]{3,})", s.lower())
    if m:
        day = int(m.group(1))
        mon = m.group(2)[:3]
        month = _MONTHS_RU.get(mon)
        if month:
            year = dt.date.today().year
            ym = _re.search(r"(20\d{2})", s)
            if ym:
                year = int(ym.group(1))
            try:
                return dt.date(year, month, day)
            except ValueError:
                pass

    return None


def _parse_sum(value) -> int:
    s = str(value).replace("\u00a0", "").replace(" ", "").replace("₽", "").replace(",", ".")
    try:
        return int(round(float(s)))
    except ValueError:
        return 0


def _rows_from(sh_id: str, title: str,
               values: list | None = None) -> list[dict]:
    """Читает занятия по схеме:
    A время | B дата | C репетитор | D имя ученика | E имя родителя |
    F сумма | G комментарий | H первое | I как прошло | J будут |
    K комиссия | L ID занятия
    """
    if values is not None:
        rows = values
    else:
        try:
            rows = get_values(sh_id, f"'{title}'!A1:L")
        except Exception:  # noqa: BLE001
            return []
    out = []
    for row in rows[1:]:
        if len(row) < 4:        # нужны хотя бы A..D; сумма может быть пустой
            continue
        tutor = norm_name(row[2]) if len(row) > 2 else ""
        d = _parse_day(row[1]) if len(row) > 1 else None
        if not tutor or d is None:
            continue
        student = _cell(row, 3)
        parent = _cell(row, 4)
        if not parent and "(" in student:
            # старый лист «История»: D = «Ученик (Родитель)»
            _l = student.find("(")
            _r = student.rfind(")")
            if _l != -1 and _r > _l:
                parent = student[_l + 1:_r].strip()
                student = student[:_l].strip()
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
    # личные таблицы (Максим, Вика) — свои листы «Занятия»
    try:
        import workspaces as _ws
        for _tutor, _sid in _ws.WORKSPACES.items():
            out.append((_sid, _ws.WORKSPACE_LESSONS_SHEET))
    except Exception as e:  # noqa: BLE001
        log(f"Личные таблицы для флажков не добавлены: {e}")
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
    from sheets_util import sheets
    want = norm_name(str(tutor or ""))
    row_nums = [n for n, r in enumerate(rows, 1)
                if n >= FIRST_DATA_ROW and norm_name(_cell(r, 2)) == want]
    if not row_nums:
        return 0
    gid = _sheet_gid(sh_id, sheet_title)
    requests = [{
        "repeatCell": {
            "range": {"sheetId": gid,
                      "startRowIndex": n - 1, "endRowIndex": n,
                      "startColumnIndex": 2, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {
                "textFormat": {"foregroundColor": color}}},
            "fields": "userEnteredFormat.textFormat.foregroundColor",
        }
    } for n in row_nums[:900]]
    try:
        sheets().spreadsheets().batchUpdate(
            spreadsheetId=sh_id, body={"requests": requests}).execute()
    except Exception as e:  # noqa: BLE001
        log(f"Не покрасил имя репетитора: {e}")
        return 0
    return len(requests)


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

    На каждый лист — один запрос и только нужные колонки (F и L),
    а не весь диапазон A1:L. Результат кэшируется на 25 секунд.
    """
    cached = _cache_get(("flags",))
    if cached is not None:
        return cached
    from sheets_util import sheets
    cfg = load_config()
    hist_id = cfg["sheets"]["history"]
    recept_id = cfg["sheets"]["recept"]
    checks_title = cfg["sheets"]["recept_checks_sheet"]

    out: dict[str, str] = {}
    fields = ("sheets(data(rowData(values("
              "formattedValue,effectiveFormat/textFormat/foregroundColor))))")

    def _scan(sh_id: str, title: str) -> None:
        try:
            grid = sheets().spreadsheets().get(
                spreadsheetId=sh_id,
                ranges=[f"'{title}'!F1:F", f"'{title}'!L1:L"],
                includeGridData=True, fields=fields,
            ).execute()
        except Exception as e:  # noqa: BLE001
            log(f"Не прочитал цвета «{title}»: {e}")
            return
        per_range = grid.get("sheets") or []
        if not per_range:
            return

        def _rows_of(sheet_obj, pos=0):
            data = (sheet_obj.get("data") or [])
            if len(data) <= pos:
                return []
            return data[pos].get("rowData", [])

        first = per_range[0]
        data_parts = first.get("data") or []
        if len(data_parts) >= 2:
            # Google вернул оба диапазона внутри одной записи листа
            sum_rows = _rows_of(first, 0)   # F (сумма)
            id_rows = _rows_of(first, 1)    # L (ID)
        elif len(per_range) >= 2:
            sum_rows = _rows_of(per_range[0], 0)
            id_rows = _rows_of(per_range[1], 0)
        else:
            return
        for idx, id_row in enumerate(id_rows):
            cells = id_row.get("values", [])
            if not cells:
                continue
            lid = str(cells[0].get("formattedValue") or "").strip()
            if not lid:
                continue
            fg = None
            if idx < len(sum_rows):
                sc = sum_rows[idx].get("values", [])
                if sc:
                    fg = ((sc[0].get("effectiveFormat") or {})
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

    try:
        meta = sheets().spreadsheets().get(spreadsheetId=hist_id).execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    except Exception:  # noqa: BLE001
        titles = []
    for title in [t for t in titles if t == "Занятия" or t.startswith("История")]:
        _scan(hist_id, title)
    _scan(recept_id, checks_title)
    # личные таблицы (Максим, Вика): свои листы «Занятия»
    try:
        import workspaces as _ws
        for _tutor, _sid in _ws.WORKSPACES.items():
            _scan(_sid, _ws.WORKSPACE_LESSONS_SHEET)
    except Exception as e:  # noqa: BLE001
        log(f"Не прочитал флажки личных таблиц: {e}")

    return _cache_put(("flags",), out)


def id_prefix() -> str:
    """Префикс ID на сегодня: «1211» для 12.11 (DDMM)."""
    t = dt.date.today()
    return f"{t.day}{t.month:02d}"


def _ids_from_all() -> list[str]:
    """Все ID из листов занятий + «Чеки Репет» (пакетное чтение)."""
    from sheets_util import get_values_batch
    cfg = load_config()
    hist_id = cfg["sheets"]["history"]
    recept_id = cfg["sheets"]["recept"]
    out: list[str] = []

    def _titles() -> list[str]:
        from sheets_util import sheets
        try:
            meta = sheets().spreadsheets().get(spreadsheetId=hist_id).execute()
            titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
        except Exception:  # noqa: BLE001
            titles = []
        found = [t for t in titles
                 if t == "Занятия" or t == "История" or t.startswith("История ")]
        if not found:
            found = [(_write_cfg().get("history_sheet")
                      or TARGET_HISTORY_SHEET)]
        return found

    try:
        titles = _titles()
        for vals in get_values_batch(hist_id,
                                     [f"'{t}'!A1:L" for t in titles]):
            out += [str(r[11]).strip() for r in vals[1:]
                    if len(r) > 11 and str(r[11]).strip()]
    except Exception as e:  # noqa: BLE001
        log(f"ID: не прочитал «Историю»: {e}")

    try:
        vals = get_values_batch(
            recept_id, [f"'{cfg['sheets']['recept_checks_sheet']}'!A1:L"])[0]
        out += [str(r[11]).strip() for r in vals[1:]
                if len(r) > 11 and str(r[11]).strip()]
    except Exception as e:  # noqa: BLE001
        log(f"ID: не прочитал «Чеки»: {e}")

    # личные таблицы (Максим, Вика) — иначе там появлялись те же ID
    try:
        import workspaces as _ws
        for _tutor, _sid in _ws.WORKSPACES.items():
            try:
                vals = get_values_batch(
                    _sid, [f"'{_ws.WORKSPACE_LESSONS_SHEET}'!A1:L"])[0]
                out += [str(r[11]).strip() for r in vals[1:]
                        if len(r) > 11 and str(r[11]).strip()]
            except Exception as e:  # noqa: BLE001
                log(f"ID: не прочитал {_tutor}: {e}")
    except Exception as e:  # noqa: BLE001
        log(f"ID: личные таблицы не прочитаны: {e}")
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

    ID уникальны по всем листам: счётчик продолжает существующие номера,
    а занятый ID никогда не выдаётся повторно.
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

    # все уже занятые ID (по всем листам) и максимум по префиксам
    used: set[str] = set()
    max_by_prefix: dict[str, int] = {}
    try:
        for _sh, _t in _all_sheet_targets():
            try:
                vals = get_values(_sh, f"'{_t}'!A1:L")
            except Exception:  # noqa: BLE001
                continue
            for r in vals[1:]:
                lid = str(r[11]).strip() if len(r) > 11 else ""
                if not lid or lid == "ID занятия":
                    continue
                used.add(lid)
                pre = lid[:-1] if lid[-1].isdigit() else ""
                # префикс — цифры даты DDMM перед счётчиком
                pass
    except Exception as e:  # noqa: BLE001
        log(f"ID: не собрал занятые номера: {e}")

    def _max_counter(prefix: str) -> int:
        mx = 0
        for lid in used:
            if lid.startswith(prefix) and lid[len(prefix):].isdigit():
                mx = max(mx, int(lid[len(prefix):]))
        return mx

    counters: dict[str, int] = {}

    def _next_id(prefix: str) -> str:
        if prefix not in counters:
            counters[prefix] = _max_counter(prefix)
        while True:
            counters[prefix] += 1
            cand = prefix + str(counters[prefix])
            if cand not in used:
                used.add(cand)
                return cand

    today_prefix = id_prefix()
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
        row_prefix = today_prefix
        d = _parse_day(row[1]) if len(row) > 1 else None
        if isinstance(d, dt.date):
            row_prefix = f"{d.day}{d.month:02d}"
        try:
            update_values(sh_id, f"'{title}'!L{n + 1}", [[_next_id(row_prefix)]])
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
    # все листы «Истории» — ОДНИМ batchGet вместо N чтений
    try:
        from sheets_util import get_values_batch
        batch = get_values_batch(
            hist_id, [f"'{t}'!A1:L" for t in lesson_sheets])
    except Exception as e:  # noqa: BLE001
        log(f"Пакетное чтение не удалось: {e}")
        batch = [None] * len(lesson_sheets)
    for t, vals in zip(lesson_sheets, batch):
        for x in _rows_from(hist_id, t, values=vals):
            x["лист"] = t
            lessons.append(x)

    # «Чеки» — основной лист занятий репетиторов, читаем всегда.
    # ID досоздаются один раз на старте (ensure_checks_ids в прогреве),
    # а не при каждом чтении: это экономит ~5 секунд.
    try:
        for x in _rows_from(cfg["sheets"]["recept"],
                            cfg["sheets"]["recept_checks_sheet"]):
            x["лист"] = "Чеки"
            lessons.append(x)
    except Exception as e:  # noqa: BLE001
        log(f"Не прочитал «Чеки»: {e}")

    # личные таблицы репетиторов (Максим, Вика) — у каждого своя книга
    try:
        import workspaces as _ws
        for _tutor, _sid in _ws.WORKSPACES.items():
            try:
                for x in _rows_from(_sid, _ws.WORKSPACE_LESSONS_SHEET):
                    x["лист"] = f"Занятия ({_tutor})"
                    lessons.append(x)
            except Exception as e:  # noqa: BLE001
                log(f"Не прочитал таблицу {_tutor}: {e}")
    except Exception as e:  # noqa: BLE001
        log(f"Личные таблицы не прочитаны: {e}")

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
    # если поле не передано — оставляем как было в таблице
    try:
        old = get_values(sh_id, f"'{sheet_title}'!A{row}:L{row}")
        old = old[0] if old else []
    except Exception:  # noqa: BLE001
        old = []

    def _keep(key: str, idx: int, default: str = "") -> str:
        if key in record and record.get(key) is not None:
            return str(record.get(key, ""))
        if len(old) > idx and str(old[idx]).strip():
            return str(old[idx])
        return default

    values = [[
        _keep("время", 0),
        _keep("дата", 1),
        _keep("репетитор", 2),
        _keep("ученик", 3),
        _keep("родитель", 4),
        _keep("сумма", 5),
        _keep("комментарий", 6),
        _keep("первое", 7, "нет"),
        _keep("как_прошло", 8, "Отлично"),
        _keep("будут", 9, "Да"),
        _keep("комиссия", 10),
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
