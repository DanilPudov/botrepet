"""
Список учеников репетиторов: лист «Ученики» в таблице «История».

Колонки (A..I):
  A Репетитор | B Ученик | C Родитель | D Тариф (₽/занятие) | E Когда добавлен
  F Комментарий | G Ссылка ученика | H Ссылка родителя | I Имя отправителя

В колонке I может быть несколько имён через запятую — по ним ищется,
кто именно перевёл деньги (см. senders_of / find_by_sender).

Каждый репетитор видит и пополняет только своих. Лист создаётся автоматически.
"""
from __future__ import annotations

import datetime as dt

import workspaces
import sheets_util
from sheets_util import load_config, log, names_match, norm_name

COLUMNS = ["Репетитор", "Ученик", "Родитель", "Тариф (занятие)",
           "Когда добавлен", "Комментарий",
           "Ссылка ученика", "Ссылка родителя", "Имя отправителя"]

# Индексы колонок в строке листа
I_SENDER = 8          # I — Имя отправителя
_LAST_COL = "I"       # последняя колонка листа
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
            "row": n,
        })
    _STUD_CACHE[cache_key] = (_t.time(), out)
    return out


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
    sheets_util.update_values(
        sh_id, f"'{sheet_title()}'!A{row_num}:{_LAST_COL}{row_num}",
        [[tutor, student, parent, rate, dt.date.today().strftime("%d.%m.%Y"),
          comment, student_link, parent_link, sender]])
    log(f"УЧЕНИК добавлен строка {row_num}: {tutor} — {student} ({parent})")
    invalidate_students_cache()
    return row_num


def find_student(tutor: str, student: str,
                 parent: str | None = None) -> dict | None:
    """Найти ученика. При одинаковых именах — различать по родителю.

    Если имя совпало у нескольких учеников, а родитель не указан,
    возвращаем None: лучше попросить уточнить, чем записать не тому.
    """
    roster = students_of(tutor)
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
    cur = find_student(tutor, student) or {}
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
    ]]
    sheets_util.update_values(
        sh_id, f"'{sheet_title()}'!A{row}:{_LAST_COL}{row}", values)
    log(f"УЧЕНИК обновлён по строке {row}: {name}")
    invalidate_students_cache()
    return True


def set_rate(tutor: str, student: str, rate: str) -> bool:
    invalidate_students_cache()
    return update_student(tutor, student, rate=str(rate))


def delete_student(tutor: str, student: str) -> bool:
    """Очищает строку ученика (A..I), остальные строки не сдвигаются."""
    row = _find_row(tutor, student)
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
