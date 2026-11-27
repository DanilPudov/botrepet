"""
Общие утилиты бота репетиторов: конфиг, состояние, доступ к Google Sheets.
Всё хранится рядом со скриптом — бот можно перенести на другое место/хостинг,
поменяв только пути в config.json.
"""
from __future__ import annotations

import json
import logging
import re
import datetime as dt
import threading
from http.client import IncompleteRead
from pathlib import Path

import os as _os

BASE_DIR = Path(__file__).resolve().parent


def _data_dir() -> Path:
    """Папка для изменяемых файлов: DATA_DIR (хостинг) или папка бота."""
    env = (_os.environ.get("DATA_DIR") or "").strip()
    if env:
        try:
            p = Path(env)
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            pass
    # Bothost: постоянное хранилище по умолчанию
    if Path("/app/data").is_dir():
        return Path("/app/data")
    return BASE_DIR


DATA_DIR = _data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "bot.log"

_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# Конфиг и состояние
# --------------------------------------------------------------------------- #
def _ensure_config() -> None:
    """Если в DATA_DIR нет config.json — скопировать из папки бота."""
    if CONFIG_PATH.exists():
        return
    src = BASE_DIR / "config.json"
    if src.exists() and src != CONFIG_PATH:
        try:
            CONFIG_PATH.write_text(src.read_text(encoding="utf-8"),
                                   encoding="utf-8")
        except OSError:
            pass


def load_config() -> dict:
    with _lock:
        _ensure_config()
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    with _lock:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def load_state() -> dict:
    with _lock:
        if not STATE_PATH.exists():
            return {"version": 1, "tutors": [], "pending": {}, "last_seen_tutor": {}}
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    with _lock:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)


_LOG_SECRET = re.compile(
    r"(?:(?<!\d)\d{7,12}:[A-Za-z0-9_-]{25,}|gh[pousr]_[A-Za-z0-9_]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|ya29\.[A-Za-z0-9_-]+)"
)


class _SecretMaskingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _LOG_SECRET.sub("[скрыто]", super().format(record))


def configure_logging() -> None:
    """Маскировать секреты в сообщениях библиотек, включая трассировки."""
    handler = logging.StreamHandler()
    handler.setFormatter(_SecretMaskingFormatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(handlers=[handler], level=logging.WARNING, force=True)


RECORD_ID_LOCK = threading.RLock()


def next_record_id(prefix: str, day: dt.date, existing) -> str:
    """Выдать уникальный номер для двух букв и календарной даты."""
    stem = f"{prefix}{day:%d%m}"
    used = {str(value).strip().upper() for value in existing}
    for number in range(1, 100):
        if stem + str(number) in used or stem + f"{number:02d}" in used:
            continue
        return stem + f"{number:02d}"
    raise ValueError(f"Для {stem} уже заняты все 99 номеров.")


def legacy_record_id(record_id, kind: str) -> str:
    """Старый ID, сохранённый в конце префиксного ID при переносе."""
    value = str(record_id or "").strip()
    match = re.fullmatch(r"[ADMV]([LPSC])-[A-Z0-9]+-[A-Z0-9]+-([A-Za-z0-9]+)", value)
    if match and match.group(1) == kind:
        return match.group(2)
    return value


def log(msg: str) -> None:
    msg = _LOG_SECRET.sub("[скрыто]", msg)
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Google Sheets
# --------------------------------------------------------------------------- #
# Транспорт httplib2 нельзя разделять между основным и фоновыми потоками.
_sheets_clients = threading.local()


def sheets():
    """Клиент Google Sheets API для текущего потока."""
    if getattr(_sheets_clients, "client", None) is None:
        import sys

        # пакеты могут лежать рядом в ./pkgs (локальный запуск без установки)
        pkg_dir = BASE_DIR / "pkgs"
        if pkg_dir.exists() and str(pkg_dir) not in sys.path:
            sys.path.insert(0, str(pkg_dir))

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        cfg = load_config()
        import base64 as _b64
        import json as _json

        env_json = (_os.environ.get("GOOGLE_KEY_JSON") or "").strip()
        env_b64 = (_os.environ.get("GOOGLE_KEY_B64") or "").strip()
        if env_json or env_b64:
            try:
                raw = env_json or _b64.b64decode(env_b64).decode("utf-8")
                info = _json.loads(raw)
                creds = service_account.Credentials.from_service_account_info(
                    info,
                    scopes=["https://www.googleapis.com/auth/spreadsheets"])
                _sheets_clients.client = build("sheets", "v4", credentials=creds,
                                       cache_discovery=False,
                                       static_discovery=False)
                return _RetryProxy(_sheets_clients.client)
            except Exception as e:  # noqa: BLE001
                log(f"Ключ Google из переменной окружения не подошёл: {e}")

        key_path = str(cfg.get("service_account_key") or "")
        if key_path and not Path(key_path).is_absolute():
            for base in (DATA_DIR, BASE_DIR):
                cand = base / key_path
                if cand.exists():
                    key_path = str(cand)
                    break
        if not Path(key_path or "").exists():
            raise FileNotFoundError(
                f"Не найден ключ Google: {key_path or 'service_account.json'}. "
                "Загрузите service_account.json в /app/data "
                "или задайте переменную GOOGLE_KEY_JSON / GOOGLE_KEY_B64.")
        creds = service_account.Credentials.from_service_account_file(
            key_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        # static_discovery=False: не требует локальных discovery-документов
        # (экономит ~107 МБ диска на сервере)
        _sheets_clients.client = build("sheets", "v4", credentials=creds,
                               cache_discovery=False,
                               static_discovery=False)
    return _RetryProxy(_sheets_clients.client)


def _is_retryable(e: Exception) -> bool:
    """Повторяем при квоте (429) и при сетевых/SSL-обрывах."""
    msg = str(e)
    low = msg.lower()
    if "429" in msg or "quota" in low:
        return True
    for token in ("ssl", "record layer", "length_mismatch", "length mismatch",
                  "connection reset", "connection aborted", "timed out",
                  "timeout", "broken pipe", "remote end closed",
                  "temporarily unavailable", "502", "503", "504"):
        if token in low:
            return True
    return False


class _RetryProxy:
    """Обёртка клиента Google: любой .execute() повторяется при 429 и SSL-обрывах.

    Так повтор работает для ВСЕХ запросов сразу — и чтения, и записи,
    и запросы структуры книги, — а не только для тех, что обёрнуты вручную.
    """
    __slots__ = ("_obj",)

    def __init__(self, obj):
        object.__setattr__(self, "_obj", obj)

    def __getattr__(self, name):
        attr = getattr(object.__getattribute__(self, "_obj"), name)
        if not callable(attr):
            return attr

        def _call(*a, **kw):
            res = attr(*a, **kw)
            if isinstance(res, (dict, list, str, int, float, bool)) or res is None:
                return res
            return _RetryProxy(res)

        return _call

    def execute(self, *a, **kw):
        obj = object.__getattribute__(self, "_obj")
        return _retry_429(lambda: obj.execute(*a, **kw))


def _exec(request):
    """Выполнить запрос Google с повтором на 429 и обрывы соединения."""
    return _retry_429(lambda: request.execute())


def _drop_client() -> None:
    """Сбросить клиент Google: следующая попытка создаст новое соединение."""
    _sheets_clients.client = None


def _retry_429(func, *args, retry_incomplete_read=False, **kwargs):
    """Повтор при квоте Google (429) и при обрывах соединения."""
    import time as _t
    last = None
    for attempt in range(4):
        try:
            return func(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            incomplete_read = retry_incomplete_read and isinstance(e, IncompleteRead)
            if not incomplete_read and not _is_retryable(e):
                raise
            last = e
            # SSL/сеть: соединение испорчено — пересоздаём клиент
            if "429" not in str(e) and "quota" not in str(e).lower():
                _drop_client()
            if attempt < 3:
                _t.sleep(0.6 * (attempt + 1))
    raise last


# Кэш прочитанных диапазонов: один экран бота делает 5-9 одинаковых
# чтений одних и тех же листов. TTL короткий, при записи кэш сбрасывается.
_VALUES_CACHE: dict = {}
_VALUES_TTL = 60.0


# Кэш списка листов книги: без него каждый экран запрашивал структуру
# таблицы заново и упирался в квоту Google (60 чтений в минуту).
_SHEETS_TITLES: dict[str, tuple[float, list[str]]] = {}
_SHEETS_TTL = 300.0


def sheet_titles(sh_id: str, refresh: bool = False, *, strict: bool = False) -> list[str]:
    """Названия листов книги (кэш 5 минут)."""
    import time as _t
    now = _t.time()
    if not refresh:
        hit = _SHEETS_TITLES.get(sh_id)
        if hit and now - hit[0] < _SHEETS_TTL:
            return hit[1]
    try:
        meta = _exec(sheets().spreadsheets().get(spreadsheetId=sh_id))
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    except Exception:
        if strict:
            raise
        hit = _SHEETS_TITLES.get(sh_id)
        return hit[1] if hit else []
    _SHEETS_TITLES[sh_id] = (now, titles)
    return titles


def forget_sheet_titles(sh_id: str) -> None:
    _SHEETS_TITLES.pop(sh_id, None)


def invalidate_values_cache() -> None:
    _VALUES_CACHE.clear()


def get_values(spreadsheet_id: str, range_: str, *, fresh: bool = False) -> list[list[str]]:
    import time as _t
    key = (spreadsheet_id, range_)
    hit = _VALUES_CACHE.get(key)
    if not fresh and hit is not None and _t.time() - hit[0] < _VALUES_TTL:
        return hit[1]
    resp = _retry_429(
        lambda: (sheets()
                 .spreadsheets()
                 .values()
                 .get(spreadsheetId=spreadsheet_id, range=range_)
                 .execute()),
        retry_incomplete_read=True,
    )
    out = resp.get("values", [])
    _VALUES_CACHE[key] = (_t.time(), out)
    return out


_BATCH_CACHE: dict = {}


def get_values_batch(spreadsheet_id: str,
                     ranges: list[str]) -> list[list[list[str]]]:
    """Несколько диапазонов одной таблицы за ОДИН запрос к Google."""
    import time as _t
    key = (spreadsheet_id, tuple(ranges))
    hit = _BATCH_CACHE.get(key)
    if hit is not None and _t.time() - hit[0] < _VALUES_TTL:
        return hit[1]
    resp = _retry_429(
        lambda: (sheets()
                 .spreadsheets()
                 .values()
                 .batchGet(spreadsheetId=spreadsheet_id,
                           ranges=list(ranges))
                 .execute()),
        retry_incomplete_read=True,
    )
    out = [vr.get("values", []) for vr in resp.get("valueRanges", [])]
    _BATCH_CACHE[key] = (_t.time(), out)
    return out


def update_values(spreadsheet_id: str, range_: str, values: list[list], *,
                  value_input_option: str = "USER_ENTERED") -> dict:
    """Точная запись диапазона. НИКОГДА не используем append (см. памятку)."""
    body = {"values": values, "majorDimension": "ROWS"}
    invalidate_values_cache()
    _BATCH_CACHE.clear()
    return _retry_429(
        lambda: (sheets()
                 .spreadsheets()
                 .values()
                 .update(
                     spreadsheetId=spreadsheet_id,
                     range=range_,
                     valueInputOption=value_input_option,
                     body=body,
                 )
                 .execute()))


def update_values_batch(spreadsheet_id: str, data: list[dict]) -> dict:
    """Записать связанные диапазоны одной операцией."""
    invalidate_values_cache()
    _BATCH_CACHE.clear()
    return _retry_429(lambda: sheets().spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data}).execute())


def clear_values(spreadsheet_id: str, range_: str) -> None:
    invalidate_values_cache()
    _BATCH_CACHE.clear()
    sheets().spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=range_
    ).execute()


# --------------------------------------------------------------------------- #
# Вспомогательное
# --------------------------------------------------------------------------- #
WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_RU = [
    "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]


def fmt_date_ru(d: dt.date) -> str:
    return f"{WEEKDAYS_RU[d.weekday()]}, {d.day} {MONTHS_RU[d.month - 1]}"


def parse_amount(text: str) -> int | None:
    """Положительная сумма в рублях с обычными разделителями разрядов."""
    value = re.sub(r"\s*[₽р]\s*$", "", str(text or "").strip(), flags=re.IGNORECASE)
    value = re.sub(r"[\t \u00a0\u202f]+", " ", value).strip()
    if len(value) > 64:
        return None
    if " " in value:
        if not re.fullmatch(r"\+?[0-9]{1,3}(?: [0-9]{3})+(?:[.,][0-9]+)?", value):
            return None
        value = value.replace(" ", "")
    if not re.fullmatch(r"\+?[0-9]+(?:[.,][0-9]+)*", value):
        return None

    value = value.lstrip("+")
    dot = value.rfind(".")
    comma = value.rfind(",")
    if dot >= 0 and comma >= 0:
        decimal = "." if dot > comma else ","
        grouping = "," if decimal == "." else "."
        whole, fraction = value.rsplit(decimal, 1)
        if not re.fullmatch(r"[0-9]{1,3}(?:" + re.escape(grouping) + r"[0-9]{3})+", whole):
            return None
        value = whole.replace(grouping, "") + "." + fraction
    elif dot >= 0 or comma >= 0:
        separator = "." if dot >= 0 else ","
        groups = value.split(separator)
        if (len(groups[0]) <= 3 and int(groups[0]) > 0
                and all(len(group) == 3 for group in groups[1:])):
            value = "".join(groups)
        elif len(groups) == 2:
            value = ".".join(groups)
        else:
            return None

    from decimal import Decimal

    amount = Decimal(value)
    if not 0 < amount <= 1_000_000:
        return None
    rounded = round(amount)
    return rounded if rounded > 0 else None


def _name_words(name: str) -> list[str]:
    """Слова имени без пунктуации/регистра, только буквы."""
    s = norm_name(name)
    s = s.replace(".", " ").replace(",", " ").replace("-", " ")
    return [w for w in s.split() if len(w) >= 2]


def _fold(name: str) -> str:
    """Имя для сравнения: нижний регистр, ё → е."""
    return norm_name(name).lower().replace("\u0451", "\u0435")


def names_match(a: str, b: str) -> bool:
    """Совпадают ли имена людей, даже если порядок слов разный.

    «Рома Жлобич» == «Жлобич Рома», «Пётр» == «Петр».
    """
    na, nb = _fold(a), _fold(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    wa, wb = _name_words(_fold(a)), _name_words(_fold(b))
    if not wa or not wb:
        return False
    if set(wa) == set(wb):
        return True
    # каждое слово одного имени должно найти пару в другом
    def _pair(words1, words2):
        for w1 in words1:
            ok = False
            for w2 in words2:
                if w1 == w2 or (len(w1) >= 3 and len(w2) >= 3
                                and (w1.startswith(w2) or w2.startswith(w1))):
                    ok = True
                    break
            if not ok:
                return False
        return True
    if len(wa) == len(wb) and _pair(wa, wb) and _pair(wb, wa):
        return True
    # одно имя — часть другого (например «Рома» и «Рома Жлобич»)
    short, long_ = (wa, wb) if len(wa) < len(wb) else (wb, wa)
    if len(short) == 1 and _pair(short, long_):
        return True
    return False

def norm_name(name: str) -> str:
    return " ".join(str(name).replace("\u00a0", " ").split()).strip()


def tutor_names_from_checks() -> list[str]:
    """Уникальные имена репетиторов из листа «Чеки» (в порядке первого появления)."""
    cfg = load_config()
    sh_id = cfg["sheets"]["recept"]
    title = cfg["sheets"]["recept_checks_sheet"]
    rows = get_values(sh_id, f"'{title}'!A2:C")
    seen: list[str] = []
    for row in rows:
        if len(row) < 3:
            continue
        name = norm_name(row[2])
        if name and name not in seen:
            seen.append(name)
    return seen
