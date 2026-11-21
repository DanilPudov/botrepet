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
import threading

from sheets_util import (get_values, update_values, load_config, log, norm_name,
                         legacy_record_id, next_record_id, _exec)

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
    _IDS_CACHE["ts"] = 0.0
    _IDS_CACHE["ids"] = []


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
        # Существующая шапка может быть защищена, а подпись ID в L необязательна.
        try:
            cur = get_values(sh_id, f"'{title}'!A1:L1")
            if not cur or not any(cur[0]):
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


def tutor_id_prefix(tutor: str = "") -> str:
    """Буква в начале ID для личных таблиц: V — Вика, M — Максим."""
    try:
        import workspaces as _ws
        t = _ws.canon_tutor(tutor)
        if t == "Виктория Пудова":
            return "V"
        if t == "Максим Пудов":
            return "M"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _fresh_series_ids(sh_id: str, title: str, personal: bool) -> list[str]:
    """Читаем L без кэша, полностью, во всех книгах одной серии ID."""
    from sheets_util import sheets
    books = {sh_id: {title}}
    if not personal:
        cfg = load_config()["sheets"]
        books.setdefault(cfg["recept"], set()).add(cfg["recept_checks_sheet"])
        history = cfg["history"]
        meta = _exec(sheets().spreadsheets().get(
            spreadsheetId=history, fields="sheets(properties(title))"))
        if "sheets" not in meta:
            raise RuntimeError("Не удалось получить список листов с занятиями")
        history_titles = books.setdefault(history, set())
        for sheet in meta["sheets"]:
            name = sheet["properties"]["title"]
            if name in ("История", "Занятия") or name.startswith("История "):
                history_titles.add(name)
    ids = []
    for book, titles in books.items():
        if not titles:
            continue
        ranges = ["'" + name.replace("'", "''") + "'!L2:L" for name in sorted(titles)]
        response = _exec(sheets().spreadsheets().values().batchGet(
            spreadsheetId=book, ranges=ranges))
        batches = response.get("valueRanges", [])
        if len(batches) != len(ranges) or any(not batch.get("range") for batch in batches):
            raise RuntimeError("Список ID занятий получен не полностью")
        for batch in batches:
            ids.extend(str(row[0]).strip() for row in batch.get("values", []) if row)
    return ids


def next_lesson_id(title: str | None = None, tutor: str = "",
                   sh_id: str | None = None, day: dt.date | None = None) -> str:
    """ID занятия по владельцу и дате с учётом уже занятых номеров."""
    import workspaces

    if sh_id is None or title is None:
        default_book, default_title = _target(tutor)
        sh_id = sh_id or default_book
        title = title or default_title
    series = "A" if title == "Чеки" else workspaces.record_owner_prefix(tutor)
    used = _fresh_series_ids(sh_id, title, series in ("M", "V"))
    return next_record_id(series + "L", day or dt.date.today(), used)


def next_lesson_id_old(title: str | None = None) -> int:
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
    from sheets_util import invalidate_values_cache
    invalidate_values_cache()
    rows = get_values(sh_id, f"'{title}'!A1:L")
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


def _background_color(cell_format: dict, theme_colors: dict) -> dict | None:
    style = cell_format.get("backgroundColorStyle") or {}
    if "rgbColor" in style:
        return style["rgbColor"]
    color = theme_colors.get(style.get("themeColor"))
    return color if color is not None else cell_format.get("backgroundColor")


def _text_color(background: dict) -> dict:
    red, green, blue = (background.get(channel, 0) for channel in ("red", "green", "blue"))
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
              for v in (red, green, blue)]
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    return BLACK if luminance >= 0.179 else WHITE


_TEXT_COLOR_FIELDS = ("userEnteredFormat.textFormat.foregroundColor,"
                      "userEnteredFormat.textFormat.foregroundColorStyle")


def _foreground(color: dict) -> dict:
    return {"foregroundColor": color, "foregroundColorStyle": {"rgbColor": color}}


def _format_row(sh_id: str, title: str, row: int) -> None:
    """Выбрать контрастный текст для каждой ячейки новой строки."""
    from sheets_util import sheets

    api = sheets().spreadsheets()
    escaped_title = title.replace("'", "''")
    meta = _exec(api.get(
        spreadsheetId=sh_id, ranges=[f"'{escaped_title}'!A{row}:L{row}"],
        fields=("properties(defaultFormat(backgroundColor,backgroundColorStyle),"
                "spreadsheetTheme(themeColors)),sheets(properties(sheetId,title),"
                "data(rowData(values(effectiveFormat(backgroundColor,backgroundColorStyle)))))"),
    ))
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == title)
    sid = sheet["properties"]["sheetId"]
    props = meta.get("properties", {})
    theme_colors = {
        item["colorType"]: item.get("color", {}).get("rgbColor")
        for item in props.get("spreadsheetTheme", {}).get("themeColors", [])
    }
    default_bg = _background_color(props.get("defaultFormat", {}), theme_colors)
    if default_bg is None:
        default_bg = WHITE
    grid = (sheet.get("data") or [{}])[0]
    cells = (grid.get("rowData") or [{}])[0].get("values", [])
    text_formats = []
    for col in range(len(HEADER)):
        cell = cells[col] if col < len(cells) else {}
        background = _background_color(cell.get("effectiveFormat") or {}, theme_colors)
        color = _text_color(default_bg if background is None else background)
        text_formats.append({"userEnteredFormat": {"textFormat": _foreground(color)}})

    rng = {"sheetId": sid, "startRowIndex": row - 1, "endRowIndex": row,
           "startColumnIndex": 0, "endColumnIndex": 10}
    requests = [
        {"updateCells": {
            "start": {"sheetId": sid, "rowIndex": row - 1, "columnIndex": 0},
            "rows": [{"values": text_formats}],
            "fields": _TEXT_COLOR_FIELDS}},
        {"repeatCell": {
            "range": rng,
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
            }},
            "fields": ("userEnteredFormat.horizontalAlignment,"
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
    _exec(api.batchUpdate(spreadsheetId=sh_id, body={"requests": requests}))


# Блокировка записи: два одновременных запроса не должны получить один ID
# и не должны занять одну и ту же свободную строку.
_ID_LOCK = threading.Lock()


def append_check(record: dict) -> int:
    """Дописывает занятие в свободную строку. Возвращает номер строки."""
    tutor = record.get("репетитор", "")
    sh_id, title = _target(tutor)
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
    ]]
    with _ID_LOCK:
        ensure_target_sheet(title, sh_id)
        row_num = find_next_free_row(title, sh_id)
        lid = next_lesson_id(title, tutor, sh_id, _parse_day(values[0][1]))
        values[0].append(lid)
        # Сбой оформления пустой строки не создаст дубль при повторе.
        _format_row(sh_id, title, row_num)
        update_values(sh_id, f"'{title}'!A{row_num}:L{row_num}", values,
                      value_input_option="USER_ENTERED")
        invalidate_cache()

    log(f"Занятие записано в строку {row_num}")
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
    # «18.01», «6.11» — день и месяц без года (так писали руками).
    # Берём текущий год, но если дата уехала бы в будущее — прошлый.
    if len(parts) == 2 and all(p.strip().isdigit() for p in parts):
        a, b = (int(p) for p in parts)
        for day, month in ((a, b), (b, a)):
            if 1 <= day <= 31 and 1 <= month <= 12:
                year = dt.date.today().year
                try:
                    cand = dt.date(year, month, day)
                except ValueError:
                    continue
                if (cand - dt.date.today()).days > 180:
                    try:
                        cand = dt.date(year - 1, month, day)
                    except ValueError:
                        pass
                return cand
    if len(parts) == 3 and all(p.strip().isdigit() for p in parts):
        a, b, cc = (int(p) for p in parts)
        # год в конце: 26 -> 2026, 2026 -> 2026
        if cc < 100:
            cc += 2000
        elif cc < 2000:
            cc = 2000 + (cc % 100)      # «27.12.1015» → 2015, «0206» → 2006
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
    except (ValueError, OverflowError):
        return 0


def _day_from_id(lesson_id: str) -> dt.date | None:
    """Дата из ID вида DDMM+счётчик (151117 -> 15.11).

    Нужна, когда в строке пустая дата: занятие всё равно должно
    попадать в бота, а не пропадать из списков.
    """
    lid = legacy_record_id(lesson_id, "L")
    if lid[:2] in ("AL", "DL", "ML", "VL") and lid[2:].isdigit():
        lid = lid[2:]
    if lid[:1] in ("M", "V") and lid[1:].isdigit():
        lid = lid[1:]
    if len(lid) < 5 or not lid[:4].isdigit():
        return None
    day, month = int(lid[:2]), int(lid[2:4])
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return None
    today = dt.date.today()
    for year in (today.year, today.year - 1):
        try:
            cand = dt.date(year, month, day)
        except ValueError:
            continue
        if cand <= today:
            return cand
    try:
        return dt.date(today.year, month, day)
    except ValueError:
        return None


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
        if d is None:
            # пустая дата: ID хранит её в начале (151117 → 15.11)
            d = _day_from_id(_cell(row, 11))
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
            "spreadsheet_id": sh_id,
            "sheet_title": title,
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
                "textFormat": _foreground(color)}},
            "fields": _TEXT_COLOR_FIELDS,
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
    log(f"Цвет обновлён (строк: {count})")
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
                    "textFormat": _foreground(color)}},
                "fields": _TEXT_COLOR_FIELDS,
            }
        }]}).execute()
    invalidate_cache()
    log(f"ЗАНЯТИЕ {lesson_id} ({sheet_title}): "
        f"{'подтверждено (зелёный)' if paid else 'не подтверждено (красный)'}")
    return True


def _color_to_status(r: float, g: float, b: float) -> str:
    """Любой оттенок зелёного -> paid, красного -> no, иначе не проверено.

    В Google доступны десятки оттенков, поэтому смотрим на доминирующий
    канал, а не на конкретный цвет:
      • зелёный перевешивает красный и синий  -> ✅ оплачено
      • красный перевешивает зелёный и синий  -> ❌ отклонено
      • чёрный/белый/серый/синий и прочее     -> ⚪️ не проверено
    """
    # серый/чёрный/белый — все каналы близки
    hi, lo = max(r, g, b), min(r, g, b)
    if hi - lo < 0.12:
        return ""
    if g > r and g > b:
        return "paid"
    if r > g and r > b:
        return "no"
    return ""


LessonFlags = dict[tuple[str, str, str], str]


def lesson_payment_status(lesson: dict, flags: LessonFlags) -> str:
    """Статус записи из исходной книги и листа."""
    return flags.get(
        (lesson.get("spreadsheet_id", ""), lesson.get("sheet_title", ""),
         str(lesson.get("id") or "").strip()),
        "",
    )


def lesson_flags() -> LessonFlags:
    """Цвет суммы (F) по ключу (книга, лист, ID занятия).

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

    out: LessonFlags = {}
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
            fg = fg or {}
            r = float(fg.get("red", 0))
            g = float(fg.get("green", 0))
            b = float(fg.get("blue", 0))
            out[(sh_id, title, lid)] = _color_to_status(r, g, b)

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


_IDS_CACHE: dict = {"ts": 0.0, "ids": []}
_IDS_TTL = 60.0


def _ids_from_all() -> list[str]:
    """Все ID из листов занятий + «Чеки Репет» (пакетное чтение).

    Результат кэшируется: кнопка «подтвердить» вызывает это дважды подряд,
    и без кэша каждое нажатие делало лишнее полное чтение всех таблиц.
    """
    if _IDS_CACHE["ids"] and _time.time() - _IDS_CACHE["ts"] < _IDS_TTL:
        return _IDS_CACHE["ids"]
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
    _IDS_CACHE["ts"] = _time.time()
    _IDS_CACHE["ids"] = out
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


def _fill_missing_lesson_ids(sh_id: str, title: str, series: str, first_row: int) -> int:
    """Присваивать ID пустым строкам по той же схеме, что при записи из бота."""
    with _ID_LOCK:
        rows = get_values(sh_id, f"'{title}'!A1:L", fresh=True)
        pending = [(n, row) for n, row in enumerate(rows, start=1)
                   if n >= first_row and len(row) >= 4 and not _cell(row, 11)
                   and (_cell(row, 2) or _cell(row, 3))]
        if not pending:
            return 0
        used = set(_fresh_series_ids(sh_id, title, series in ("M", "V")))
        added = 0
        for n, row in pending:
            day = _parse_day(row[1]) or dt.date.today()
            candidate = next_record_id(series + "L", day, used)
            used.add(candidate)
            try:
                update_values(sh_id, f"'{title}'!L{n}", [[candidate]])
            except Exception as error:  # noqa: BLE001
                log(f"Не удалось заполнить ID занятия в строке {n}: {type(error).__name__}")
                continue
            added += 1
        if added:
            invalidate_cache()
        return added


def ensure_personal_ids() -> int:
    """Заполнить пустые ID занятий в личных таблицах."""
    import workspaces

    added = 0
    for tutor, book in workspaces.WORKSPACES.items():
        try:
            added += _fill_missing_lesson_ids(
                book, workspaces.WORKSPACE_LESSONS_SHEET,
                workspaces.record_owner_prefix(tutor), first_row=2,
            )
        except Exception as error:  # noqa: BLE001
            log(f"Не удалось заполнить ID в личной таблице: {type(error).__name__}")
    return added


def ensure_checks_ids() -> int:
    """Заполнить пустые ID в «Чеках», пропуская защищённый пример."""
    cfg = load_config()["sheets"]
    return _fill_missing_lesson_ids(cfg["recept"], cfg["recept_checks_sheet"], "A", first_row=3)


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


def read_lessons_for_save(tutor: str) -> list[dict]:
    """Свежие строки из места записи и архива, если чек мог быть перенесён."""
    cfg = load_config()["sheets"]
    target = _target(tutor)
    sources = [target]
    if target[0] == cfg["recept"]:
        sources.append((cfg["history"], cfg["history_sheet"]))
    lessons = []
    for book, title in dict.fromkeys(sources):
        quoted = title.replace("'", "''")
        values = get_values(book, f"'{quoted}'!A1:L", fresh=True)
        lessons.extend(_rows_from(book, title, values=values))
    return lessons


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

    archive_sources = (
        (hist_id, cfg["sheets"]["history_sheet"]),
        (cfg["sheets"]["recept"], cfg["sheets"]["recept_checks_sheet"]),
    )
    seen: set[tuple] = set()
    unique: list[dict] = []
    for les in lessons:
        # Дедуп по ID: две разные строки с одинаковым содержимым
        # (реальный дубль или два занятия с той же суммой) — это разные
        # занятия, их нельзя склеивать. Без ID — по содержимому.
        lid = str(les.get("id") or "").strip()
        source = (les.get("spreadsheet_id"), les.get("sheet_title"))
        if lid.startswith("AL") and source in archive_sources:
            dedup_key = ("archived-check", lid, les["дата"], norm_name(les["репетитор"]),
                         norm_name(les["ученик"]), norm_name(les.get("родитель", "")),
                         les["сумма"])
        elif lid:
            dedup_key = (source, lid)
        else:
            dedup_key = (les["репетитор"], les["дата"],
                         norm_name(les["ученик"]), les["сумма"],
                         str(les.get("время", "")))
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
    existing = get_values(sh_id, f"'{sheet_title}'!A{row}:L{row}")
    if not existing or _cell(existing[0], 11) != str(lesson_id):
        return False
    old = existing[0]

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
    log(f"Занятие изменено в строке {row}")
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
    log(f"Занятие удалено из строки {row}")
    invalidate_cache()
    return True


def change_tutor_lesson(target: dict, tutor: str, *, amount: int | None = None,
                        delete: bool = False) -> bool:
    """Меняет только собственную запись в книге, из которой она была прочитана."""
    from sheets_util import clear_values

    lid = str(target.get("id") or "")
    if not lid or not tutor or target.get("репетитор") != tutor:
        return False
    if delete == (amount is not None):
        raise ValueError("Требуется одно действие с записью")
    spreadsheet = target["spreadsheet_id"]
    sheet = "'" + target["sheet_title"].replace("'", "''") + "'"
    with _ID_LOCK:
        rows = get_values(spreadsheet, f"{sheet}!A1:L", fresh=True)
        matches = [n for n, row in enumerate(rows[1:], start=2)
                   if _cell(row, 11) == lid and norm_name(_cell(row, 2)) == tutor]
        if len(matches) != 1:
            return False
        row = matches[0]
        if delete:
            clear_values(spreadsheet, f"{sheet}!A{row}:L{row}")
        else:
            update_values(spreadsheet, f"{sheet}!F{row}", [[amount]])
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
