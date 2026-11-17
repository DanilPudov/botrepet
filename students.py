"""
Список учеников репетиторов: лист «Ученики» в таблице «История».

Колонки (A..J):
  A Репетитор | B Ученик | C Родитель | D Тариф (₽/занятие) | E Когда добавлен
  F Комментарий | G Ссылка ученика | H Ссылка родителя | I Имя отправителя
  J ID ученика (стабильный, уникальный в книге репетитора)

В колонке I может быть несколько имён через запятую — по ним ищется,
кто именно перевёл деньги (см. senders_of / find_by_sender).

ID ученика (колонка J) — надёжный идентификатор: даже если у репетитора
два ученика с одинаковым именем (например, София (Жанна) и София (Мария)),
все операции идут по ID, а не по имени. Старым строкам без ID он
назначается автоматически при первом чтении.

Каждый репетитор видит и пополняет только своих. Лист создаётся автоматически.
"""
from __future__ import annotations

import datetime as dt

import workspaces
import sheets_util
from sheets_util import load_config, log, names_match, norm_name

COLUMNS = ["Репетитор", "Ученик", "Родитель", "Тариф (занятие)",
           "Когда добавлен", "Комментарий",
           "Ссылка ученика", "Ссылка родителя", "Имя отправителя",
           "ID ученика"]

# Индексы колонок в строке листа
I_SENDER = 8          # I — Имя отправителя
I_STUDENT_ID = 9      # J — стабильный ID ученика
_LAST_COL = "J"       # последняя колонка листа
_HEADER_RANGE = f"A1:{_LAST_COL}1"


def sheet_title() -> str:
    return load_config().get("sheets", {}).get("students_sheet", "Ученики")


def ensure_sheet(tutor: str = "") -> str:
    """Создаёт лист «Ученики» при отсутствии, иначе дотягивает шапку до I."""
    from sheets_util import sheets
    sh_id = workspaces.spreadsheet_for(tutor)
    title = sheet_title()
    if title not in sheets_util.sheet_titles(sh_id):
        sheets().spreadsheets().batchUpdate(
            spreadsheetId=sh_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]}
        ).execute()
        sheets_util.update_values(sh_id, f"'{title}'!{_HEADER_RANGE}", [COLUMNS])
        sheets_util.forget_sheet_titles(sh_id)
        log(f"Создан лист «{title}»")
        return title
    # шапка могла быть старой (A..H) — дотянуть до I
    cur = sheets_util.get_values(sh_id, f"'{title}'!{_HEADER_RANGE}")
    if not cur or len(cur[0]) < len(COLUMNS):
        sheets_util.update_values(sh_id, f"'{title}'!{_HEADER_RANGE}", [COLUMNS])
    return title


def _rows(tutor: str = "") -> list[list[str]]:
    try:
        ensure_sheet(tutor)
    except Exception as e:  # noqa: BLE001 — лист создастся при записи
        log(f"Не смог подготовить лист «{sheet_title()}»: {e}")
    return sheets_util.get_values(
        workspaces.spreadsheet_for(tutor), f"'{sheet_title()}'!A1:{_LAST_COL}")


def _cell(row: list[str], idx: int) -> str:
    """Значение колонки idx (пустая строка, если колонки нет)."""
    return row[idx] if len(row) > idx else ""


def load_config_path():
    import sheets_util
    return sheets_util.CONFIG_PATH


_STUD_CACHE: dict = {}
_STUD_TTL = 60.0
_STUD_VER = [0]   # версия кэша: растёт при любом изменении


def invalidate_students_cache() -> None:
    _STUD_CACHE.clear()
    _STUD_VER[0] += 1


def students_of(tutor: str) -> list[dict]:
    """Ученики репетитора: [{"ученик","родитель","тариф","добавлен",... ,"row"}]."""
    import time as _t
    cache_key = (str(load_config_path()), norm_name(tutor), _STUD_VER[0])
    hit = _STUD_CACHE.get(cache_key)
    if hit and _t.time() - hit[0] < _STUD_TTL:
        return hit[1]
    out: list[dict] = []
    seen: set[tuple] = set()
    for n, row in enumerate(_rows(tutor), start=1):
        if n == 1 or len(row) < 2 or not norm_name(row[0]):
            continue
        if not names_match(row[0], tutor):
            continue
        key = (norm_name(row[1]),
               norm_name(row[2]) if len(row) > 2 and row[2] else "")
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "ученик": row[1],
            "родитель": row[2] if len(row) > 2 else "",
            "тариф": row[3] if len(row) > 3 else "",
            "добавлен": row[4] if len(row) > 4 else "",
            "комментарий": row[5] if len(row) > 5 else "",
            "ссылка_ученика": row[6] if len(row) > 6 else "",
            "ссылка_родителя": row[7] if len(row) > 7 else "",
            "отправитель": _cell(row, I_SENDER),
            "id": _cell(row, I_STUDENT_ID),
            "row": n,
        })
    _STUD_CACHE[cache_key] = (_t.time(), out)
    return out


def student_id_prefix(tutor: str) -> str:
    """Префикс ID ученика, уникальный для репетитора.

    Данил → D, Максим → M, Вика → V, остальные → первая буква имени
    плюс часть telegram-id. Это исключает совпадение ID между разными
    репетиторами даже при одинаковых порядковых номерах.
    """
    import re as _re
    name = workspaces.canon_tutor(tutor) or str(tutor or "")
    first = ""
    for ch in name:
        if ch.isalpha():
            first = ch.upper()
            break
    # кириллическая буква المح might не подойти для ID — translit
    trans = {"Д": "D", "М": "M", "В": "V", "И": "I", "Р": "R",
             "Б": "B", "Н": "N", "А": "A", "В": "V", "С": "S", "Т": "T"}
    letter = trans.get(first, first)
    if not _re.match(r"^[A-Z]$", letter):
        letter = "T"
    # для не-семьи добавляем хвост telegram-id, чтобы префиксы не бились
    if name in ("Данил Пудов", "Максим Пудов", "Виктория Пудова"):
        return letter
    tail = ""
    try:
        import sheets_util as _su
        for t in _su.load_state().get("tutors", []):
            if norm_name(t.get("name", "")) == norm_name(name):
                tail = str(t.get("id", ""))[-3:]
                break
    except Exception:  # noqa: BLE001
        pass
    return letter + (tail or "X")


def renumber_student_ids(tutor: str = "") -> int:
    """Перенумеровать ID всех учеников листа. Возвращает число строк.

    Префикс берётся по репетитору из колонки A КАЖДОЙ строки, а не из
    аргумента: в общей книге лежат ученики всех репетиторов, и раньше
    из-за этого ID получали чужой префикс и могли совпасть.

    Нумерация внутри префикса идёт подряд, поэтому ID уникальны в книге.
    Запись — одним вызовом на весь диапазон (экономим квоту Google).
    """
    sh_id = workspaces.spreadsheet_for(tutor)
    try:
        rows = _rows(tutor)
    except Exception as e:  # noqa: BLE001
        log(f"Не смог перенумеровать ID учеников: {e}")
        return 0
    if len(rows) < 2:
        return 0
    data = [(n, r) for n, r in enumerate(rows, start=1)
            if n > 1 and any(str(x).strip() for x in r)]
    if not data:
        return 0
    counters: dict[str, int] = {}
    new_ids: list[str] = []
    for _n, r in data:
        row_tutor = str(_cell(r, 0)).strip() or tutor
        prefix = student_id_prefix(row_tutor)
        counters[prefix] = counters.get(prefix, 0) + 1
        new_ids.append(f"{prefix}{counters[prefix]}")
    if len(set(new_ids)) != len(new_ids):
        log("УЧЕНИКИ: ID не уникальны — перенумерация отменена")
        return 0
    first, last = data[0][0], data[-1][0]
    if last - first + 1 == len(data):
        sheets_util.update_values(
            sh_id, f"'{sheet_title()}'!J{first}:J{last}",
            [[x] for x in new_ids])
    else:
        for (n, _r), nid in zip(data, new_ids):
            sheets_util.update_values(
                sh_id, f"'{sheet_title()}'!J{n}", [[nid]])
    invalidate_students_cache()
    log(f"УЧЕНИКИ: ID перенумерованы ({len(new_ids)}) в {sh_id}")
    return len(new_ids)


def ensure_student_ids(tutor: str = "") -> int:
    """Назначить стабильные ID всем ученикам без ID. Возвращает число новых.

    Старым строкам (записанным до появления колонки J) ID ставится
    один раз. Префикс определяется по репетитору из колонки A строки.
    """
    sh_id = workspaces.spreadsheet_for(tutor)
    try:
        rows = _rows(tutor)
    except Exception as e:  # noqa: BLE001
        log(f"Не смог назначить ID учеников: {e}")
        return 0
    if not rows:
        return 0
    # сколько уже занято под каждый префикс
    counters: dict[str, int] = {}
    for r in rows[1:]:
        sid = str(_cell(r, I_STUDENT_ID)).strip()
        if not sid:
            continue
        for pfx_len in range(1, len(sid) + 1):
            if sid[pfx_len:].isdigit():
                pfx = sid[:pfx_len]
                counters[pfx] = max(counters.get(pfx, 0), int(sid[pfx_len:]))
                break
    written = 0
    for n, r in enumerate(rows, start=1):
        if n == 1 or not any(str(x).strip() for x in r):
            continue
        if str(_cell(r, I_STUDENT_ID)).strip():
            continue
        row_tutor = str(_cell(r, 0)).strip() or tutor
        prefix = student_id_prefix(row_tutor)
        counters[prefix] = counters.get(prefix, 0) + 1
        sheets_util.update_values(
            sh_id, f"'{sheet_title()}'!J{n}", [[f"{prefix}{counters[prefix]}"]])
        written += 1
    if written:
        invalidate_students_cache()
        log(f"УЧЕНИКИ: назначено ID {written} в таблице {sh_id}")
    return written


def add_student(tutor: str, student: str, parent: str = "", rate: str = "",
                comment: str = "", student_link: str = "",
                parent_link: str = "", sender: str = "") -> int:
    """Добавляет ученика. Возвращает номер строки."""
    ensure_sheet(tutor)
    cfg = load_config()
    sh_id = workspaces.spreadsheet_for(tutor)
    rows = _rows(tutor)
    last = 1
    for n, row in enumerate(rows, start=1):
        if any(str(x).strip() for x in row):
            last = n
    row_num = last + 1
    prefix = student_id_prefix(tutor)
    used = {s.get("id") for s in students_of(tutor) if s.get("id")}
    mx = 0
    for u in used:
        u = str(u)
        tail = u[len(prefix):] if u.startswith(prefix) else ""
        if tail.isdigit():
            mx = max(mx, int(tail))
        elif u.startswith("S") and u[1:].isdigit():
            mx = max(mx, int(u[1:]))
    new_id = ""
    while True:
        mx += 1
        cand = f"{prefix}{mx}"
        if cand not in used:
            new_id = cand
            break
    sheets_util.update_values(
        sh_id, f"'{sheet_title()}'!A{row_num}:{_LAST_COL}{row_num}",
        [[tutor, student, parent, rate, dt.date.today().strftime("%d.%m.%Y"),
          comment, student_link, parent_link, sender, new_id]])
    log(f"УЧЕНИК добавлен строка {row_num}: {tutor} — {student} ({parent})")
    invalidate_students_cache()
    return row_num


def find_student(tutor: str, student: str,
                 parent: str | None = None) -> dict | None:
    """Найти ученика. Сначала по стабильному ID, затем по имени+родителю.

    Если имя совпало у нескольких учеников, а родитель не указан,
    возвращаем None: лучше попросить уточнить, чем записать не тому.
    """
    roster = students_of(tutor)
    # Стабильный ID имеет приоритет: тёзки никогда не путаются.
    want_id = str(student or "").strip()
    if len(want_id) >= 2 and want_id[:1].isalpha():
        by_id = [s for s in roster if s.get("id") == want_id]
        if len(by_id) == 1:
            return by_id[0]
    want = norm_name(student)
    exact = [s for s in roster if norm_name(s["ученик"]) == want]
    if not exact:
        exact = [s for s in roster if names_match(s["ученик"], student)]
    if not exact:
        return None
    if len(exact) == 1:
        return exact[0]
    # несколько тёзок — выбираем по родителю
    if parent:
        wp = norm_name(parent)
        by_parent = [s for s in exact
                     if norm_name(s.get("родитель", "")) == wp
                     or names_match(s.get("родитель", ""), parent)]
        if len(by_parent) == 1:
            return by_parent[0]
    return None


def _find_row(tutor: str, student: str) -> int | None:
    found = find_student(tutor, student)
    return found["row"] if found else None


def _row_to_dict(row: list[str], row_num: int) -> dict:
    """Строка листа -> словарь ученика (как в students_of)."""
    return {
        "ученик": _cell(row, 1),
        "родитель": _cell(row, 2),
        "тариф": _cell(row, 3),
        "добавлен": _cell(row, 4),
        "комментарий": _cell(row, 5),
        "ссылка_ученика": _cell(row, 6),
        "ссылка_родителя": _cell(row, 7),
        "отправитель": _cell(row, 8),
        "id": _cell(row, 9),
        "row": row_num,
    }


def update_student(tutor: str, student: str, *, new_name: str | None = None,
                   parent: str | None = None, rate: str | None = None,
                   comment: str | None = None,
                   student_link: str | None = None,
                   parent_link: str | None = None,
                   sender: str | None = None,
                   where_row: int | None = None,
                   match_parent: str | None = None) -> bool:
    """Меняет имя/родителя/тариф/отправителя ученика. True, если нашёл.

    where_row — точная строка из карточки (надёжнее поиска по имени).
    match_parent — родитель для различения одинаковых имён.
    """
    if where_row:
        try:
            row = int(where_row)
        except (TypeError, ValueError):
            row = None
    else:
        row = None
    if row is None:
        found = find_student(tutor, student, parent=match_parent)
        if not found:
            return False
        row = found["row"]
    cfg = load_config()
    sh_id = workspaces.spreadsheet_for(tutor)
    # ВАЖНО: при тёзках find_student по имени вернёт None и мы затрём
    # остальные поля пустыми. Поэтому берём данные из ТОЙ ЖЕ строки.
    cur = find_student(tutor, student, parent=match_parent)
    if not cur or cur.get("row") != row:
        cur = {}
        try:
            rows = _rows(tutor)
            if 2 <= row <= len(rows):
                cur = _row_to_dict(rows[row - 1], row)
        except Exception:  # noqa: BLE001
            pass
    values = [[
        tutor,
        new_name if new_name is not None else cur.get("ученик", ""),
        parent if parent is not None else cur.get("родитель", ""),
        rate if rate is not None else cur.get("тариф", ""),
        cur.get("добавлен", ""),
        comment if comment is not None else cur.get("комментарий", ""),
        (student_link if student_link is not None
         else cur.get("ссылка_ученика", "")),
        (parent_link if parent_link is not None
         else cur.get("ссылка_родителя", "")),
        sender if sender is not None else cur.get("отправитель", ""),
        cur.get("id", ""),
    ]]
    sheets_util.update_values(
        sh_id, f"'{sheet_title()}'!A{row}:{_LAST_COL}{row}", values)
    log(f"УЧЕНИК обновлён строка {row}: {tutor} — {values[0][1]}")
    invalidate_students_cache()
    return True




def update_student_row(row: int, tutor: str = "", **fields) -> bool:
    """Обновить ученика по номеру строки (когда имена совпадают)."""
    cfg = load_config()
    sh_id = workspaces.spreadsheet_for(tutor)
    try:
        rows = _rows(tutor)
    except Exception:  # noqa: BLE001
        return False
    if row < 2 or row > len(rows):
        return False
    cur_row = rows[row - 1]
    name = fields.get("new_name") if fields.get("new_name") is not None \
        else _cell(cur_row, 1)
    parent = fields.get("parent") if fields.get("parent") is not None \
        else _cell(cur_row, 2)
    values = [[
        _cell(cur_row, 0),
        name,
        parent,
        fields["rate"] if fields.get("rate") is not None else _cell(cur_row, 3),
        _cell(cur_row, 4),
        fields["comment"] if fields.get("comment") is not None else _cell(cur_row, 5),
        fields["student_link"] if fields.get("student_link") is not None else _cell(cur_row, 6),
        fields["parent_link"] if fields.get("parent_link") is not None else _cell(cur_row, 7),
        fields["sender"] if fields.get("sender") is not None else _cell(cur_row, 8),
        _cell(cur_row, I_STUDENT_ID),
    ]]
    sheets_util.update_values(
        sh_id, f"'{sheet_title()}'!A{row}:{_LAST_COL}{row}", values)
    log(f"УЧЕНИК обновлён по строке {row}: {name}")
    invalidate_students_cache()
    return True


def set_rate(tutor: str, student: str, rate: str,
             parent: str | None = None,
             where_row: int | None = None) -> bool:
    """Сохранить цену. parent/where_row обязательны при тёзках."""
    invalidate_students_cache()
    return update_student(tutor, student, rate=str(rate),
                          match_parent=parent, where_row=where_row)


def delete_student(tutor: str, student: str, where_row: int | None = None,
                   match_parent: str | None = None) -> bool:
    """Очищает строку ученика (A..I), остальные строки не сдвигаются.

    where_row — точная строка из карточки (нужно при тёзках).
    match_parent — родитель для различения одинаковых имён.
    """
    row = None
    if where_row:
        try:
            row = int(where_row)
        except (TypeError, ValueError):
            row = None
    if row is None:
        found = find_student(tutor, student, parent=match_parent)
        row = found["row"] if found else None
    if row is None:
        return False
    cfg = load_config()
    sh_id = workspaces.spreadsheet_for(tutor)
    sheets_util.clear_values(
        sh_id, f"'{sheet_title()}'!A{row}:{_LAST_COL}{row}")
    log(f"УЧЕНИК удалён строка {row}: {tutor} — {student}")
    invalidate_students_cache()
    return True


# ---------------------------------------------------------------- отправители
def sender_names(raw: str) -> list[str]:
    """Разбирает «Имя отправителя» на нормализованные имена (через запятую)."""
    out: list[str] = []
    for part in str(raw or "").split(","):
        name = norm_name(part)
        if name and name not in out:
            out.append(name)
    return out


def senders_of(tutor: str) -> dict[str, str]:
    """{нормализованное имя отправителя: имя ученика} для учеников репетитора."""
    out: dict[str, str] = {}
    for s in students_of(tutor):
        student = norm_name(s.get("ученик", ""))
        for name in sender_names(s.get("отправитель", "")):
            out.setdefault(name, student)
    return out


def _match_len(a: str, b: str) -> int:
    """Длина более короткой строки, если одна входит в другую, иначе 0."""
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) if short and short in long else 0


def find_by_sender(tutor: str, sender: str) -> dict | None:
    """Ищет ученика по имени отправителя платежа.

    Сначала точное совпадение norm_name, затем вхождение одной строки в другую
    (для строк длиной от 3 символов), при нескольких кандидатах — самое длинное.
    """
    want = norm_name(sender)
    if not want:
        return None
    roster = students_of(tutor)

    for s in roster:                       # точное совпадение
        if want in sender_names(s.get("отправитель", "")):
            return s

    best: dict | None = None
    best_len = 2                          # порог: длина >= 3
    for s in roster:
        for name in sender_names(s.get("отправитель", "")):
            got = _match_len(want, name)
            if got > best_len:
                best, best_len = s, got
    return best
