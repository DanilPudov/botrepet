"""
Телеграм-бот для репетиторов: заполнение занятий в лист «Чеки».

Запуск:  python bot.py          (из папки бота)
Токен и права — в config.json.
"""
from __future__ import annotations

import datetime as dt
import os
from types import SimpleNamespace
import re
import html
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Часовой пояс и кодировка — работают даже без переменных окружения
# (на хостингах бесплатных тарифов env-переменные часто недоступны).
try:
    os.environ.setdefault("TZ", "Europe/Moscow")
    import time as _tz_time
    if hasattr(_tz_time, "tzset"):
        _tz_time.tzset()
except Exception:  # noqa: BLE001
    pass
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass
# пакеты лежат рядом, чтобы не требовать установки в систему
if (BASE_DIR / "pkgs").exists():
    sys.path.insert(0, str(BASE_DIR / "pkgs"))

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    LinkPreviewOptions,
)
from telegram.constants import ParseMode
from telegram.error import InvalidToken
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import checks_writer
import workspaces as _ws
import sheets_util
import students as students_module
import reviews as reviews_module
import referrals as referrals_module
import suggestions as suggestions_module
import notes as notes_module
import faq as faq_module
import links as links_module
import schedule as schedule_module
import activity as activity_module
from sheets_util import (
    load_config,
    names_match,
    save_config,
    load_state,
    save_state,
    log,
    parse_amount,
    norm_name,
    tutor_names_from_checks,
    fmt_date_ru,
)


# --------------------------------------------------------------------------- #
# Папка данных (config.json / state.json / bot.log)
# --------------------------------------------------------------------------- #
def apply_bot_home(home: str | Path | None = None) -> Path | None:
    """Направить config.json/state.json/bot.log в другую папку.

    По умолчанию файлы лежат рядом с bot.py. Если задана переменная окружения
    DSH_BOT_HOME (или передан аргумент *home*), пути переключаются на неё —
    так тесты не трогают боевые config.json/state.json.
    Возвращает выбранную папку или None, если переопределения нет.
    """
    raw = str(home) if home is not None else os.environ.get("DSH_BOT_HOME", "")
    if not raw:
        return None
    folder = Path(raw).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    sheets_util.CONFIG_PATH = folder / "config.json"
    sheets_util.STATE_PATH = folder / "state.json"
    sheets_util.LOG_PATH = folder / "bot.log"
    return folder


# применяем DSH_BOT_HOME, если он задан (при обычном запуске — no-op)
apply_bot_home()

# --------------------------------------------------------------------------- #
# Доступ
# --------------------------------------------------------------------------- #
TUTOR, OWNER = "tutor", "owner"


def role_of(user_id: int) -> str | None:
    """Кто этот пользователь: владелец (owner), репетитор (tutor) или None.

    Имя жёстко привязано к Telegram ID: посторонний не может выбрать чужое имя
    и записывать от него. Новый репетитор добавляется только организатором.
    """
    state = load_state()
    cfg = load_config()
    if user_id in {int(x) for x in cfg.get("owner_ids", [])}:
        return OWNER
    # «Данил 2» — обычный репетитор (без админ-панели)
    if _ws.is_test_account(user_id):
        return TUTOR
    ids = {int(t["id"]): t for t in state.get("tutors", [])}
    if user_id in ids:
        return OWNER if ids[user_id].get("role") == "owner" else TUTOR
    # Максим и Вика — доступ всегда, даже если state.json на сервере старый
    try:
        if any(int(tid) == int(user_id) for tid, _ in _ws.WORKSPACE_TUTORS):
            return TUTOR
    except Exception:  # noqa: BLE001
        pass
    access = cfg.get("access", "ids")
    if access == "anyone":  # только для отладки, по умолчанию выключено
        return TUTOR
    if access == "invite_code" and user_id in state.get("invited", []):
        return TUTOR
    return None


def display_name(user_id: int) -> str | None:
    """Как обращаться к репетитору: его «имя» из настроек или первое слово ФИО."""
    state = load_state()
    for t in state.get("tutors", []):
        if int(t["id"]) == user_id:
            call = (t.get("call_name") or "").strip()
            if call:
                return call
            full = (t.get("name") or "").strip()
            return full.split()[0] if full else None
    return None


def set_call_name(user_id: int, call_name: str) -> None:
    """Сохранить, как называть репетитора."""
    state = load_state()
    for t in state.get("tutors", []):
        if int(t["id"]) == user_id:
            t["call_name"] = call_name.strip()
            break
    save_state(state)


def tutor_names() -> list[str]:
    """Имена, которыми может подписаться репетитор: из «Чеков» + из state."""
    names = tutor_names_from_checks()
    for t in load_state().get("tutors", []):
        if t.get("name") and t["name"] not in names:
            names.append(t["name"])
    return names


def view_as_name(user_id: int) -> str | None:
    """Режим организатора: за какого репетитора он сейчас смотрит."""
    return (load_state().get("view_as") or {}).get(str(user_id))


def _full_name(user_id: int) -> str | None:
    """Имя для журнала. У аккаунта «Данил 2» — своя подпись."""
    try:
        if _ws.is_test_account(user_id):
            return "Данил 2"
    except Exception:  # noqa: BLE001
        pass
    for t in load_state().get("tutors", []):
        if int(t["id"]) == user_id:
            name = (t.get("name") or "").strip()
            return name or None
    return None


def name_by_id(user_id: int) -> str | None:
    va = view_as_name(user_id)
    if va:
        _ws.set_active(va)
        return va
    if _ws.is_test_account(user_id):
        # без выбора ещё никого — имя появится после выбора
        return None
    for t in load_state().get("tutors", []):
        if int(t["id"]) == user_id:
            nm = t.get("name")
            _ws.set_active(nm or "")
            return nm
    try:
        for tid, nm in _ws.WORKSPACE_TUTORS:
            if int(tid) == int(user_id):
                _ws.set_active(nm)
                return nm
    except Exception:  # noqa: BLE001
        pass
    return None


# --------------------------------------------------------------------------- #
# Клавиатуры
# --------------------------------------------------------------------------- #
def kb(rows: list[list[tuple[str, str]]], home: bool = True) -> InlineKeyboardMarkup:
    """Клавиатура из пар (текст, callback).

    home=True (по умолчанию) добавляет внизу «🏠 Домой», если её ещё нет —
    чтобы из любого раздела можно было выйти на главный экран.
    """
    rows = [list(r) for r in rows]
    if home:
        texts = [t for r in rows for t, _ in r]
        datas = [d for r in rows for _, d in r]
        if not any(t.startswith("🏠") for t in texts) and "menu" not in datas:
            rows.append([("🏠 Домой", "menu")])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text, callback_data=data) for text, data in row]
        for row in rows
    ])


def kb_yes_no(prefix: str, yes="Да", no="Нет") -> InlineKeyboardMarkup:
    return kb([[ (yes, f"{prefix}:yes"), (no, f"{prefix}:no") ]])




PRESET_AMOUNTS = [[("1 300", "amount:1300"), ("1 500", "amount:1500")],
                  [("1 700", "amount:1700"), ("2 000", "amount:2000")],
                  [("2 500", "amount:2500"), ("3 000", "amount:3000")]]


def kb_amount() -> InlineKeyboardMarkup:
    return kb(PRESET_AMOUNTS + [[("Ввести своё", "amount:custom")]])


def kb_after_first() -> InlineKeyboardMarkup:
    return kb([[("Продолжаем заниматься", "will:yes"), ("Пока нет", "will:no")]])


def kb_cancel() -> InlineKeyboardMarkup:
    return kb([[("Отменить", "cancel")]])


# --------------------------------------------------------------------------- #
# Главное меню
# --------------------------------------------------------------------------- #
MAIN_TEXT = "Выберите действие:"


def _week_btn(user_id: int) -> str:
    """«Текущая неделя» для семьи, «Текущие 2 недели» для остальных."""
    tutor = name_by_id(user_id) or ""
    return "📊 Текущая неделя" if _ws.is_family(tutor) else "📊 Текущие 2 недели"


def kb_main(user_id: int) -> InlineKeyboardMarkup:
    return kb(home=False, rows=[
        [("📝 Записать занятие", "new")],
        [(_week_btn(user_id), "mine"), ("📅 Расписание", "sched:today")],
        [("📝 Заметки", "notes")],
        [("☰ Меню", "more")],
    ])


def _past_btn(user_id: int) -> str:
    tutor = name_by_id(user_id) or ""
    return "📆 Прошлая неделя" if _ws.is_family(tutor) else "📆 Предыдущие 2 недели"


def kb_more(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [("👥 Мои ученики", "students")],
        [("⭐ Отзывы", "reviews"), ("🎁 Рефералы", "referrals")],
        [(_past_btn(user_id), "pastweek")],
        [("💡 Предложить улучшение", "suggest")],
        [("✏️ Изменить имя", "rename"), ("❓ FAQ", "faq")],
        [("🎨 Цвет моего имени", "tcolor")],
    ]
    if role_of(user_id) == OWNER:
        rows.append([("🔄 Сменить репетитора", "view_mode")])
        if not view_as_name(user_id):
            rows.append([("📜 Последние логи", "logs")])
            rows.append([("⚙️ Управление", "admin")])
    elif _ws.is_test_account(user_id):
        rows.append([("🔄 Сменить репетитора", "view_mode")])
    rows.append([("⬅️ Назад", "nav:back")])
    return kb(rows)


async def show_main(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    text = MAIN_TEXT
    va = view_as_name(update.effective_user.id)
    if va and not _ws.is_test_account(update.effective_user.id):
        text = f"🔎 Режим просмотра: {va}\n\n" + text
    markup = kb_main(update.effective_user.id)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
        )


# --------------------------------------------------------------------------- #
# /start
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    role = role_of(user.id)
    if role is None:
        # пишем в лог: организатору видно, кто просит доступ
        log(f"Запрос доступа: id={user.id} "
            f"имя={user.first_name or ''} @{user.username or ''}")
        await update.message.reply_text(
            "🔒 Нет доступа.\n\nОбратитесь к организатору, чтобы вас добавили. "
            "Сообщите ему свой ID: `" + str(user.id) + "`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if _ws.is_test_account(user.id) and not view_as_name(user.id):
        await show_owner_picker(update, context, edit=False)
        return
    if role == OWNER and not view_as_name(user.id):
        await show_owner_picker(update, context, edit=False)
        return

    # Первый запуск: спрашиваем, как обращаться (только для репетиторов)
    if role == TUTOR and not has_call_name(user.id):
        await ask_call_name(update, context, edit=False)
        return
    await show_main(update, context)


def has_call_name(user_id: int) -> bool:
    """Выбрано ли «как обращаться» (организатору не требуется)."""
    for t in load_state().get("tutors", []):
        if int(t["id"]) == user_id:
            return bool((t.get("call_name") or "").strip())
    return True


def _view_allowed(user_id: int) -> list[str] | None:
    """Каких репетиторов может смотреть этот аккаунт (None — всех)."""
    if _ws.is_test_account(user_id):
        return [norm_name(x) for x in _ws.TEST_ACCOUNT_TUTORS]
    st = load_state()
    for t in st.get("tutors", []):
        try:
            if int(t["id"]) != int(user_id):
                continue
        except (KeyError, ValueError, TypeError):
            continue
        allowed = t.get("can_view")
        if allowed:
            return [norm_name(x) for x in allowed]
    return None


async def show_owner_picker(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            edit: bool = True) -> None:
    """Организатор выбирает, за какого репетитора зайти."""
    user = update.effective_user
    names: list[str] = []
    only = _view_allowed(user.id)
    for t in load_state().get("tutors", []):
        nm = (t.get("name") or "").strip()
        if not nm or nm in names:      # у Ильхама два ID — показываем один раз
            continue
        if only is not None and norm_name(nm) not in only:
            continue
        names.append(nm)
    rows = [[(n, f"view:{i}")] for i, n in enumerate(names)]
    if _ws.is_test_account(user.id):
        text = "Кем будем работать?"
    else:
        rows.append([("👤 Мой профиль", "view:me")])
        text = ("👋 Вы организатор.\n\n"
                "За кого зайти, чтобы посмотреть его бота?")
    markup = kb(rows)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def ask_call_name(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        edit: bool = True) -> None:
    user = update.effective_user
    state = load_state()
    state.setdefault("pending", {})[str(user.id)] = {
        "шаг": "имя", "репетитор": name_by_id(user.id) or ""}
    save_state(state)
    full = name_by_id(user.id) or ""
    short = full.split()[0] if full else "имя"
    text = (f"Как к тебе обращаться?\n\nНапиши имя — например `{short}`.\n"
            "Его бот будет показывать в сообщениях. В таблицу по-прежнему "
            "пойдёт полное имя.")
    markup = kb([[("Оставить как в таблице", "name:default")],
                 [("Отменить", "cancel")]])
    if update.callback_query is not None:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        return
    msg = await _send(update, text, parse_mode=ParseMode.MARKDOWN,
                      reply_markup=markup)
    mid = getattr(msg, "message_id", None)
    if isinstance(mid, int):
        st = load_state()
        prompt = st.get("pending", {}).get(str(user.id))
        if prompt is not None:
            prompt["prompt_msg"] = {"chat": msg.chat_id, "id": mid}
            save_state(st)


# --------------------------------------------------------------------------- #
# Диалог «Записать занятие»
# --------------------------------------------------------------------------- #








async def begin_new_students(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать список учеников для уже начатого черновика (с датой)."""
    user = update.effective_user
    p = get_pending(user.id) or {}
    tutor = p.get("репетитор") or name_by_id(user.id) or ""
    roster = students_module.students_of(tutor) if tutor else []
    rows = []
    for i, s in enumerate(roster):
        label = s["ученик"] + (f" ({s['родитель']})" if s.get("родитель") else "")
        rows.append([(label[:40], f"stu:{i}")])
    rows.append([("➕ Добавить ученика", "add_student"), ("Отменить", "cancel")])
    await _say(update, f"📅 {p.get('дата', '')}\nЗа кого занятие?",
               reply_markup=kb(rows))


def _strike(text: str) -> str:
    return "".join(ch + "\u0336" for ch in text)


def _time_key(value) -> int:
    """Числовой ключ времени: '9:00' -> 540 (сортировка по-человечески)."""
    try:
        parts = str(value).replace(".", ":").split(":")
        return int(parts[0]) * 60 + (int(parts[1]) if len(parts) > 1 else 0)
    except (ValueError, TypeError):
        return 24 * 60 + 1


def _countdown(day: dt.date, time_str: str) -> str:
    """'через 3 ч', 'через 40 мин', 'сейчас' или '' если время не указано."""
    if not time_str:
        return ""
    try:
        hh, mm = map(int, str(time_str).split(":")[:2])
    except (ValueError, TypeError):
        return ""
    target = dt.datetime.combine(day, dt.time(hour=hh, minute=mm))
    delta = target - dt.datetime.now()
    minutes = int(delta.total_seconds() // 60)
    if minutes < -30:
        return ""
    if minutes < 1:
        return "сейчас"
    if minutes < 60:
        return f"через {minutes} мин"
    hours = minutes // 60
    return f"через {hours} ч"


async def _delete_notice_job(context):
    """Удалить служебное сообщение «добавлено» через пару секунд."""
    data = context.job.data or {}
    try:
        await context.bot.delete_message(chat_id=data["chat"], message_id=data["mid"])
    except Exception:  # noqa: BLE001
        pass


async def _finish_with_card(update, context, p, done_text):
    """Показать «добавлено», открыть список всех учеников, затем убрать уведомление."""
    notice = await _send(update, done_text)
    try:
        await show_students_list(update, context)
    except Exception as e:  # noqa: BLE001
        log(f"Не смог открыть список учеников: {e}")
    mid = getattr(notice, "message_id", None)
    chat = getattr(notice, "chat_id", None)
    if isinstance(mid, int):
        try:
            await context.bot.delete_message(chat_id=chat, message_id=mid)
        except Exception:  # noqa: BLE001
            pass


def _schedule_summary(tutor: str, student: str) -> str:
    """Краткое расписание ученика для шапки карточки."""
    try:
        entries = schedule_module.entries_for(tutor, student)
        if not entries:
            want = norm_name(student)
            entries = [e for e in schedule_module.entries_of(tutor)
                       if norm_name(e.get("ученик", "")) == want
                       or want.startswith(norm_name(e.get("ученик", "")) + " ")
                       or norm_name(e.get("ученик", "")).startswith(want + " ")]
    except Exception:
        return "—"
    if not entries:
        return "—"
    return ", ".join(f"{e.get('дни', '')} {e.get('время', '')}".strip()
                     for e in entries)


def normalize_link(raw: str) -> str | None:
    """Ссылка на чат: https://…, @username, номер телефона, ВК и др."""
    return links_module.normalize_link(raw)


def _student_link(s: dict, kind: str) -> str:
    url = (s.get("ссылка_ученика") if kind == "s"
           else s.get("ссылка_родителя")) or ""
    url = links_module.dialog_link(url) if url else ""
    return url


def _already_recorded(tutor: str, date_s: str, time_s: str,
                      student: str) -> bool:
    """Есть ли уже точно такая запись (репетитор+дата+время+ученик)."""
    try:
        day = dt.datetime.strptime(str(date_s), "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return False
    try:
        for x in checks_writer.read_lessons():
            if (names_match(x.get("репетитор", ""), tutor)
                    and x.get("дата") == day
                    and str(x.get("время", "")).strip() == str(time_s).strip()
                    and names_match(x.get("ученик", ""), student)):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


# Когда ботом последний раз пользовались (для фонового обновления кэша)
_LAST_ACTIVITY: dict = {"ts": 0.0}


def _touch_activity() -> None:
    """Отметить активность: кэш обновляем только когда ботом пользуются."""
    import time as _t
    _LAST_ACTIVITY["ts"] = _t.time()


def log_action(user_id: int, action: str, details: str = "") -> None:
    """Записать действие в журнал (кто, что, когда)."""
    try:
        who = _full_name(user_id) or name_by_id(user_id) or str(user_id)
        activity_module.record(who, action, details)
    except Exception:  # noqa: BLE001
        pass


def log_system(action: str, details: str = "") -> None:
    """Записать автоматическое действие бота (от имени DSH)."""
    try:
        activity_module.record_system(action, details)
    except Exception:  # noqa: BLE001
        pass


def _where(p: dict) -> dict:
    """Параметры точного поиска ученика (строка + родитель)."""
    kw: dict = {}
    row = p.get("row")
    if row:
        try:
            kw["where_row"] = int(row)
        except (TypeError, ValueError):
            pass
    if p.get("родитель"):
        kw["match_parent"] = p["родитель"]
    return kw


def _card_text(s: dict, sched: str, html_mode: bool = False) -> str:
    """Карточка ученика. Имя — кликабельная ссылка (синяя), без показа URL."""
    name = s.get("ученик", "")
    parent = s.get("родитель") or ""
    su = _student_link(s, "s")
    pu = _student_link(s, "p")
    if html_mode:
        name_s = links_module.link_html(su, name)
        parent_s = links_module.link_html(pu, parent) if parent else "—"
        return (f"🧒 <b>{name_s}</b>\n"
                f"Родитель: {parent_s}\n"
                f"Цена занятия: {s.get('тариф') + ' ₽' if s.get('тариф') else '—'}\n"
                f"Расписание: {sched}\n"
                f"Комментарий: {s.get('комментарий') or '—'}")
    return (f"🧒 *{name}*\n"
            f"Родитель: {parent or '—'}\n"
            f"Цена занятия: {s.get('тариф') + ' ₽' if s.get('тариф') else '—'}\n"
            f"Расписание: {sched}\n"
            f"Комментарий: {s.get('комментарий') or '—'}")


async def show_student_card(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            tutor: str, student: str) -> None:
    """Карточка ученика с кнопками правки."""
    user = update.effective_user
    s = students_module.find_student(
        tutor, student, parent=(get_pending(user.id) or {}).get("родитель", "")) or {
        "ученик": student, "родитель": "", "тариф": "", "комментарий": ""}
    set_pending(user.id, {"шаг": "карточка_ученика", "ученик": s["ученик"],
                          "родитель": s.get("родитель", ""),
                          "репетитор": tutor, "row": s.get("row")})
    text = _card_text(s, _schedule_summary(tutor, s["ученик"]), html_mode=True)
    await _say(update, text, parse_mode=ParseMode.HTML, reply_markup=kb([
        [("✏️ Имя ученика", "sedit:name"), ("👤 Родитель", "sedit:parent")],
        [("🔗 Ссылка ученика", "sedit:link_student")],
        [("🔗 Ссылка родителя", "sedit:link_parent")],
        [("💰 Цена занятия", "sedit:rate")],
        [("📝 Комментарий", "sedit:comment")],
        [("✏️ Изменить расписание", "sedit:sched")],
        [("🗑 Удалить ученика", "sedit:del")],
        [("⬅️ Назад", "nav:back")],
    ]))


async def show_student_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список занятий ученика построчно + добавить/изменить/удалить."""
    user = update.effective_user
    p = get_pending(user.id) or {}
    tutor = p.get("репетитор") or name_by_id(user.id) or ""
    student = p.get("ученик", "")
    entries = schedule_module.entries_for(tutor, student) if tutor and student else []
    s = students_module.find_student(
        tutor, student, parent=p.get("родитель", "")) or {
        "ученик": student, "родитель": "", "тариф": "", "комментарий": ""}
    rows = []
    for e in entries:
        label = f"{e.get('дни', '')} {e.get('время', '')}".strip()
        if schedule_module.one_time_date(str(e.get("дни", ""))):
            label = "🔁 перенос · " + label
        rows.append([(label or f"#{e['id']}", f"sch:open:{e['id']}")])
    rows.append([("➕ Добавить занятие", "sch:add")])
    rows.append([("⬅️ К ученику", "students")])
    if not student:
        # Нет выбранного ученика (например, вернулись из расписания) — не рисуем пустую анкету
        await show_main(update, context, edit=(update.callback_query is not None))
        return
    _su = _student_link(s, "s")
    _pu = _student_link(s, "p")
    _name_h = links_module.link_html(_su, s["ученик"])
    _par_h = links_module.link_html(_pu, s.get("родитель") or "") or "—"
    text = (f"🧒 <b>{_name_h}</b>\n"
            f"Родитель: {_par_h}\n"
            f"Цена занятия: {s.get('тариф') + ' ₽' if s.get('тариф') else '—'}\n"
            f"Комментарий: {s.get('комментарий') or '—'}\n\n"
            + ("⏰ <b>Занятия:</b>\n" +
               ("" if entries else "пока нет")))
    await _say(update, text, parse_mode=ParseMode.HTML, reply_markup=kb(rows))


def _students_same_name(tutor: str, name: str) -> list[dict]:
    want = norm_name(name)
    return [s for s in students_module.students_of(tutor)
            if norm_name(s.get("ученик", "")) == want]


def _parse_sched_line(line: str):
    import re as _re
    line = line.strip()
    is_remove = line[:1] in ("-", "−")
    if is_remove:
        line = line[1:].strip()
    m = _re.match(r"^(\d{1,2}[:.]\d{2})\s+(.+)$", line)
    if not m:
        return None
    t = parse_time(m.group(1))
    if not t:
        return None
    who = norm_name(m.group(2))
    student, parent = who, ""
    if "(" in who and who.endswith(")"):
        student = who[:who.index("(")].strip()
        parent = who[who.index("(") + 1:-1].strip()
    return {"time": t, "student": student, "parent": parent,
            "remove": is_remove}


async def _finish_schedule_edit(update, context, p, done_text: str) -> None:
    notice = await _send(update, done_text)
    set_pending(context._dsh_uid, None) if False else None
    set_pending(update.effective_user.id, None)
    text2, buttons = await schedule_view(update.effective_user.id, 7)
    await _send(update, text2, parse_mode=ParseMode.HTML,
                reply_markup=kb(buttons))
    mid = getattr(notice, "message_id", None)
    if isinstance(mid, int):
        try:
            await context.bot.delete_message(chat_id=notice.chat_id,
                                             message_id=mid)
        except Exception:  # noqa: BLE001
            pass


async def _process_schedule_queue(update, context, p) -> None:
    """Обработать строки добавления/удаления занятий дня."""
    user = update.effective_user
    tutor = p.get("репетитор") or name_by_id(user.id) or ""
    weekday = int(p.get("weekday", 0))
    queue = list(p.get("queue") or [])

    while queue:
        line = queue.pop(0)
        p["queue"] = queue
        item = _parse_sched_line(line)
        if not item:
            continue
        t, student, parent = item["time"], item["student"], item["parent"]

        if item["remove"]:
            for e in schedule_module.entries_of(tutor):
                if (weekday in schedule_module.parse_days(str(e.get("дни", "")))
                        and parse_time(str(e.get("время", ""))) == t
                        and norm_name(e.get("ученик", "")) == norm_name(student)):
                    schedule_module.delete_entry(e["id"])
                    p["removed"] = int(p.get("removed", 0)) + 1
                    break
            set_pending(user.id, p)
            continue

        # одинаковые имена учеников -> уточнить родителя
        same = _students_same_name(tutor, student)
        if not parent and len(same) > 1:
            p["resolve"] = {"weekday": weekday, "time": t, "student": student}
            set_pending(user.id, p)
            rows = [[(s.get("родитель") or "—", f"conf:par:{i}")]
                    for i, s in enumerate(same)]
            rows.append([("Отменить", "conf:cancel")])
            qmsg = await _send(update,
                        f"У вас несколько учеников с именем *{student}*.\n"
                        "Уточните родителя:",
                        parse_mode=ParseMode.HTML, reply_markup=kb(rows))
            mid = getattr(qmsg, "message_id", None)
            if isinstance(mid, int):
                p["resolve"]["prompt"] = {"chat": qmsg.chat_id, "id": mid}
                set_pending(user.id, p)
            return

        # слот занят другим занятием?
        slot = next((e for e in schedule_module.entries_of(tutor)
                     if weekday in schedule_module.parse_days(str(e.get("дни", "")))
                     and parse_time(str(e.get("время", ""))) == t), None)
        if slot is not None:
            if norm_name(slot.get("ученик", "")) == norm_name(student):
                set_pending(user.id, p)   # такой же — просто оставить
                continue
            p["resolve"] = {"weekday": weekday, "time": t, "student": student,
                            "parent": parent, "existing_id": slot["id"],
                            "existing": slot.get("ученик", "")}
            set_pending(user.id, p)
            qmsg = await _send(update,
                        f"⏰ На {schedule_module.DAY_NAMES[weekday]} {t} уже стоит "
                        f"*{slot.get('ученик', '')}*.\n\n"
                        f"Поставить *{student}* вместо него?",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=kb([
                            [("🔁 Только в этот раз", "conf:once")],
                            [("♾ Заменить навсегда", "conf:forever")],
                            [("Отменить", "conf:cancel")]]))
            mid = getattr(qmsg, "message_id", None)
            if isinstance(mid, int):
                p["resolve"]["prompt"] = {"chat": qmsg.chat_id, "id": mid}
                set_pending(user.id, p)
            return

        st = students_module.find_student(tutor, student) or {}
        log_action(user.id, "добавил в расписание",
                   f"{student} {t or ''}".strip())
        schedule_module.add_entry(tutor, student, parent or st.get("родитель", ""),
                                  schedule_module.DAY_NAMES[weekday], t,
                                  st.get("тариф", ""))
        p["added"] = int(p.get("added", 0)) + 1
        set_pending(user.id, p)

    done = f"✅ Добавлено: {p.get('added', 0)}, удалено: {p.get('removed', 0)}."
    await _finish_schedule_edit(update, context, p, done)


async def show_schedule_after_cancel(update, context, user_id: int) -> None:
    if update.callback_query is not None:
        await update.callback_query.answer()
    text2, buttons = await schedule_view(user_id, 7)
    await _send(update, text2, parse_mode=ParseMode.HTML,
                reply_markup=kb(buttons))


async def _apply_resolve(update, context, p, mode: str) -> None:
    """Применить решение по конфликту и продолжить очередь."""
    user = update.effective_user
    tutor = p.get("репетитор") or name_by_id(user.id) or ""
    r = p.get("resolve") or {}
    weekday = int(r.get("weekday", 0))
    t = r.get("time", "")
    student = r.get("student", "")
    st = students_module.find_student(tutor, student) or {}
    if mode == "par":
        parent = r.get("parent_chosen", "")
        p.setdefault("queue", []).insert(0, f"{t} {student} ({parent})")
        p.pop("resolve", None)
        set_pending(user.id, p)
        await _process_schedule_queue(update, context, p)
        return
    if mode == "once":
        today = dt.date.today()
        day = today + dt.timedelta(days=(weekday - today.weekday()) % 7)
        schedule_module.add_one_time(tutor, student, r.get("parent", "")
                                     or st.get("родитель", ""), day, t,
                                     st.get("тариф", ""))
        p["added"] = int(p.get("added", 0)) + 1
    elif mode == "forever":
        if str(r.get("existing_id", "")).isdigit():
            schedule_module.delete_entry(int(r["existing_id"]))
        schedule_module.add_entry(tutor, student, r.get("parent", "")
                                  or st.get("родитель", ""),
                                  schedule_module.DAY_NAMES[weekday], t,
                                  st.get("тариф", ""))
        p["added"] = int(p.get("added", 0)) + 1
    p.pop("resolve", None)
    set_pending(user.id, p)
    await _process_schedule_queue(update, context, p)


async def schedule_view(user_id: int, days: int) -> tuple[str, list[list[tuple[str, str]]]]:
    """Расписание: сегодня — по занятиям, неделя — по дням с кнопкой на день."""
    tutor = name_by_id(user_id)
    if not tutor:
        return "Сначала выберите своё имя (/start).", []

    entries = schedule_module.entries_of(tutor)
    names = schedule_module.DAY_NAMES
    _cancels = schedule_module.cancels_for(tutor) if hasattr(schedule_module, "cancels_for") else []
    _cancel_keys = {(c["дата"], schedule_module.parse_time_slot(c["время"]))
                    for c in _cancels}
    weekly: dict[int, list[dict]] = {i: [] for i in range(7)}
    one_time: list[tuple] = []
    for e in entries:
        single = schedule_module.one_time_date(str(e.get("дни", "")))
        if single is not None:
            one_time.append((single, e))
            continue
        for d in schedule_module.parse_days(str(e.get("дни", ""))):
            weekly[d].append(e)

    def esc(v) -> str:
        return html.escape(str(v or ""))

    try:
        _students = list(students_module.students_of(tutor))
    except Exception:  # noqa: BLE001
        _students = []

    def _link(url: str, label: str) -> str:
        url = (url or "").strip()
        if url.startswith("http://") or url.startswith("https://"):
            return f'<a href="{esc(url)}">{esc(label)}</a>'
        return esc(label)

    def _match_student(name: str, parent: str = "") -> dict:
        """Ученик по имени, при тёзках — по родителю (иначе без ссылок)."""
        want = norm_name(name)
        same = [s for s in _students
                if norm_name(s.get("ученик", "")) == want
                or names_match(s.get("ученик", ""), name)]
        if not same:
            return {}
        if len(same) == 1:
            return same[0]
        if parent:
            wp = norm_name(parent)
            by_par = [s for s in same
                      if norm_name(s.get("родитель", "")) == wp]
            if len(by_par) == 1:
                return by_par[0]
        return {}

    def who_html(e: dict) -> str:
        name = e.get("ученик", "")
        parent = e.get("родитель", "")
        st = _match_student(name, parent)
        out = _link(st.get("ссылка_ученика", ""), name)
        if parent:
            out += " (" + _link(st.get("ссылка_родителя", ""), parent) + ")"
        return out

    today_wd = dt.date.today().weekday()
    _today_cnt = 0
    parts = []
    rows: list[list[tuple[str, str]]] = []

    if days >= 30:
        today = dt.date.today()
        total = 0
        for offset in range(0, 31):
            day = today + dt.timedelta(days=offset)
            items = []
            for e in entries:
                single = schedule_module.one_time_date(str(e.get("дни", "")))
                if single is not None:
                    if single == day:
                        items.append(e)
                    continue
                if day.weekday() in schedule_module.parse_days(str(e.get("дни", ""))):
                    items.append(e)
            if not items:
                continue
            mark = " (сегодня)" if day == today else ""
            _cc = sum(1 for e in items
                      if (day, schedule_module.parse_time_slot(
                          str(e.get("время", "")))) in _cancel_keys
                      and schedule_module.one_time_date(
                          str(e.get("дни", ""))) is None)
            parts.append(f"*{schedule_module.DAY_NAMES[day.weekday()]} "
                         f"{day:%d.%m}*{mark} — {len(items) - _cc} зан.")
            for e in sorted(items, key=lambda x: _time_key(x.get("время", ""))):
                who = who_html(e)      # имена — кликабельные ссылки
                price = f" — {e['цена']} ₽" if e.get("цена") else ""
                _single = schedule_module.one_time_date(str(e.get("дни", "")))
                _t = str(e.get("время", ""))
                if _single is not None:
                    parts.append(f"  🔁 разовое {_t} {who}{price}")
                elif (day, schedule_module.parse_time_slot(_t)) in _cancel_keys:
                    parts.append(f"  ❌ {_t} {who} (отменено)")
                else:
                    parts.append(f"  {_t} {who}{price}")
                if (day, schedule_module.parse_time_slot(_t)) in _cancel_keys \
                        and _single is None:
                    continue
                try:
                    total += int(str(e.get("цена") or 0))
                except ValueError:
                    pass
        if not parts:
            parts.append("Занятий в этом периоде нет.")
        rows = [[("📅 Сегодня", "sched:today"), ("📅 Неделя", "sched:week")],
                [("⬅️ Назад", "nav:back")]]
        body = "\n".join(parts)
        count = sum(1 for l in parts if l.startswith("  ") and "(отменено)" not in l)
        payout = _ws.payout(total, tutor)
        head = (f"📅 *Расписание на месяц вперёд*\n"
                f"Всего занятий: *{count}* · ожидаемая сумма: *{payout} ₽* "
                f"({_ws.payout_note(tutor)})\n\n")
        text = head + body
        return text, rows
    del_buttons = []

    if days <= 1:  # сегодняшний день — сразу занятиями
        today_d = dt.date.today()
        day_items = []
        for e in weekly[today_wd]:
            day_items.append((schedule_module.parse_time_slot(str(e.get("время", ""))),
                              0, e, False))
        for e in entries:
            if schedule_module.one_time_date(str(e.get("дни", ""))) == today_d:
                day_items.append((schedule_module.parse_time_slot(str(e.get("время", ""))),
                                  1, e, True))
        day_items.sort(key=lambda t: (t[0], t[1]))
        for _k, _o, e, is_single in day_items:
            who = esc(e.get("ученик", ""))
            if e.get("родитель"):
                who += f" ({esc(e['родитель'])})"
            price = f" — {e['цена']} ₽" if e.get("цена") else ""
            tm = str(e.get("время", ""))
            if is_single:
                parts.append(f"  🔁 <b>разовое</b> {tm} {who_html(e)}{price}")
            elif (today_d, schedule_module.parse_time_slot(tm)) in _cancel_keys:
                parts.append(f"  ❌ {tm} {who_html(e)} (отменено)")
            else:
                parts.append(f"  {tm} {who_html(e)}{price}")
            rows.append([(f"{tm} {e.get('ученик', '')}"[:38], f"sch:open:{e['id']}")])
        _cnt_cancel = sum(
            1 for _k, _o, _e, _s in day_items
            if not _s and (today_d, schedule_module.parse_time_slot(
                str(_e.get("время", "")))) in _cancel_keys)
        _today_cnt = len(day_items) - _cnt_cancel
        parts.insert(0, f"<b>Сегодня — {names[today_wd]} {today_d:%d.%m}</b> — "
                        f"{_today_cnt} зан.")
        if not day_items:
            parts.append("  —")
        rows.append([(f"➕ Добавить на {names[today_wd]}", f"addw:{today_wd}")])
        rows.append([("➕ Разовое занятие", "addonce"),
                     ("❌ Отмена занятия", "cancel_lesson")])
        rows.append([("📅 Неделя", "sched:week"),
                     ("📅 Месяц вперёд", "sched:month")])
        rows.append([("⬅️ Назад", "nav:back")])
    else:  # неделя — блоки по дням + кнопка «изменить день»
        monday = dt.date.today() - dt.timedelta(days=today_wd)
        for d in range(7):
            day_date = monday + dt.timedelta(days=d)
            day_items = []
            for e in weekly[d]:
                day_items.append((schedule_module.parse_time_slot(str(e.get("время", ""))),
                                  0, e, False))
            for e in entries:
                if schedule_module.one_time_date(str(e.get("дни", ""))) == day_date:
                    day_items.append((schedule_module.parse_time_slot(str(e.get("время", ""))),
                                      1, e, True))
            day_items.sort(key=lambda t: (t[0], t[1]))
            _cancelled_today = sum(
                1 for _k, _o, _e, _s in day_items
                if not _s and (day_date, schedule_module.parse_time_slot(
                    str(_e.get("время", "")))) in _cancel_keys)
            parts.append(f"<b>{names[d]} {day_date:%d.%m}</b> — "
                         f"{len(day_items) - _cancelled_today} зан.")
            for _k, _o, e, is_single in day_items:
                who = esc(e.get("ученик", ""))
                if e.get("родитель"):
                    who += f" ({esc(e['родитель'])})"
                price = f" — {e['цена']} ₽" if e.get("цена") else ""
                tm = str(e.get("время", ""))
                if is_single:
                    parts.append(f"  🔁 <b>разовое</b> {tm} {who_html(e)}{price}")
                elif (day_date, schedule_module.parse_time_slot(tm)) in _cancel_keys:
                    parts.append(f"  ❌ {tm} {who_html(e)} (отменено)")
                else:
                    parts.append(f"  {tm} {who_html(e)}{price}")
            if not day_items:
                parts.append("  —")
        rows = [[(f"✏️ {names[i]}", f"schday:{i}") for i in range(4)],
                [(f"✏️ {names[i]}", f"schday:{i}") for i in range(4, 7)]]
        rows.append([("📅 Сегодня", "sched:today"),
                     ("📅 Месяц вперёд", "sched:month")])
        rows.append([("➕ Разовое занятие", "addonce"),
                     ("❌ Отмена занятия", "cancel_lesson")])
        rows.append([("⬅️ Назад", "nav:back")])

    iso_week = dt.date.today().isocalendar()[1]
    parity = "чётная" if iso_week % 2 == 0 else "нечётная"
    title = ("📅 <b>Расписание на сегодня</b>" if days <= 1
             else "📅 <b>Расписание по дням недели</b>")
    week_count = 0
    week_sum = 0
    for d in range(7):
        day_date = monday_wd = (dt.date.today() - dt.timedelta(days=today_wd)
                                + dt.timedelta(days=d)) if today_wd is not None else None
        for e in weekly[d]:
            if (day_date, schedule_module.parse_time_slot(
                    str(e.get("время", "")))) in _cancel_keys:
                continue
            week_count += 1
            try:
                week_sum += int(str(e.get("цена") or 0))
            except ValueError:
                pass
    week_payout = _ws.payout(week_sum, tutor)
    if days <= 1:
        text = (title + f"\n*Сейчас {parity} неделя* ({iso_week}-я)\n"
                + "\n"
                + "\n".join(parts))
    else:
        text = (title + f"\n*Сейчас {parity} неделя* ({iso_week}-я)\n"
                + f"Занятий на неделю: <b>{week_count}</b> · ожидаемая сумма: "
                + f"<b>{week_payout} ₽</b> (без учёта комиссии)\n\n"
                + "\n".join(parts))
    text = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", text)
    text = text.replace("*", "")
    return text, rows + del_buttons


async def begin_new(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = True):
    """Старт записи занятия: кнопки со списком учеников репетитора."""
    state = load_state()
    user = update.effective_user
    tutor_name = name_by_id(user.id)
    if not tutor_name:
        await show_main(update, context, edit=bool(update.callback_query))
        return

    students = students_module.students_of(tutor_name)
    state.setdefault("pending", {})[str(user.id)] = {
        "репетитор": tutor_name,
        "шаг": "ученик",
        "первое": "нет",
        "как_прошло": "Отлично",
        "будут": "Да",
        "комментарий": "",
        "комиссия": "",
    }
    save_state(state)
    text = (f"👤 {display_name(user.id) or tutor_name}, "
            "за кого записываем занятие?")
    rows = []
    for i, s in enumerate(students):
        label = s["ученик"] + (f" ({s['родитель']})" if s.get("родитель") else "")
        rows.append([(label[:40], f"stu:{i}")])
    rows.append([("➕ Добавить ученика", "add_student"), ("Отменить", "cancel")])
    await _say(update, text, reply_markup=kb(rows))

async def manual_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запись с чужого аккаунта: спрашиваем имя репетитора (если ещё не спрашивали)."""
    user = update.effective_user
    state = load_state()
    saved = (state.get("manual_name") or {}).get(str(user.id)) or ""
    if saved:
        await manual_show_students(update, context, saved)
        return
    state.setdefault("pending", {})[str(user.id)] = {"шаг": "ручное_имя", "чужой": True}
    save_state(state)
    await _say(update,
               "✍️ Запись с другого аккаунта.\n\n"
               "Напишите имя и фамилию репетитора, от чьего имени вносите занятие.",
               reply_markup=kb([[("Отменить", "cancel")]]))


async def manual_show_students(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               tutor_input: str) -> None:
    """Показать учеников выбранного репетитора для ручной записи."""
    user = update.effective_user
    canon = next((n for n in tutor_names() if norm_name(n) == norm_name(tutor_input)),
                 norm_name(tutor_input))
    p = {
        "репетитор": canon,
        "шаг": "ученик",
        "чужой": True,
        "первое": "нет",
        "как_прошло": "Отлично",
        "будут": "Да",
        "комментарий": "",
        "комиссия": "",
    }
    set_pending(user.id, p)
    roster = students_module.students_of(canon)
    rows = []
    for i, s in enumerate(roster):
        label = s["ученик"] + (f" ({s['родитель']})" if s.get("родитель") else "")
        rows.append([(label[:40], f"stu:{i}")])
    rows.append([("➕ Добавить ученика", "add_student"), ("Отменить", "cancel")])
    await _say(update,
               f"✏️ Запись от имени *{canon}* (чужой ID)\n\nЗа кого занятие?",
               parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))


def tutor_of(p: dict, user) -> str:
    return p.get("репетитор") or name_by_id(user.id) or ""


def get_pending(user_id: int) -> dict:
    return load_state().get("pending", {}).get(str(user_id), {})


def set_pending(user_id: int, data: dict | None):
    state = load_state()
    pend = state.setdefault("pending", {})
    if data is None:
        pend.pop(str(user_id), None)
    else:
        pend[str(user_id)] = data
    save_state(state)


async def _cleanup_tracked(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           keep_id: int | None = None) -> None:
    """Удалить все ранее отправленные ботом сообщения-подсказки.

    keep_id — сообщение, которое сейчас редактируется (его не трогаем).
    """
    user = update.effective_user
    if user is None:
        return
    st = load_state()
    ids = (st.get("bot_msgs") or {}).get(str(user.id), [])
    if not ids:
        return
    left: list[int] = []
    for mid in ids:
        if keep_id is not None and mid == keep_id:
            left.append(mid)
            continue
        try:
            await context.bot.delete_message(chat_id=user.id, message_id=mid)
        except Exception:  # noqa: BLE001 — старое/уже удалено
            pass
    st.setdefault("bot_msgs", {})[str(user.id)] = left
    save_state(st)


async def _cleanup_extras(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          keep_id: int | None = None) -> None:
    """Удалить «лишние» сообщения бота (например, вторая часть месяца).

    keep_id — сообщение, на кнопке которого сейчас нажали: его НЕ удаляем,
    иначе бот не сможет его отредактировать и экран «пропадёт».
    """
    user = update.effective_user
    st = load_state()
    extras = (st.get("extra_msgs") or {}).pop(str(user.id), [])
    if extras:
        save_state(st)
    for item in extras:
        if keep_id is not None and item.get("id") == keep_id:
            continue
        try:
            await context.bot.delete_message(chat_id=item["chat"],
                                             message_id=item["id"])
        except Exception:  # noqa: BLE001
            pass


NAV_SCREENS = (
    "menu", "more", "students", "mine", "mine:edit", "pastweek", "notes",
    "reviews", "referrals", "help", "rename", "suggest", "admin",
    "sched:", "schday:", "addw:", "sch:list", "sch:open:", "student:",
    "mact:", "cancel_lesson", "cxd:", "view_mode", "view:",
)

_NAV_PENDING: dict[int, str] = {}


def _nav_is_screen(data: str) -> bool:
    return any(data == s or data.startswith(s) for s in NAV_SCREENS)


def _nav_state(user_id: int) -> dict:
    nav = (load_state().get("nav") or {}).get(str(user_id))
    if not isinstance(nav, dict):
        nav = {"stack": [], "current": "menu"}
    return nav


def _nav_save(user_id: int, nav: dict) -> None:
    st = load_state()
    st.setdefault("nav", {})[str(user_id)] = nav
    save_state(st)


def _nav_push(user_id: int, data: str) -> None:
    """Запомнить переход на новый экран (для кнопки «Назад»)."""
    if not _nav_is_screen(data):
        return
    nav = _nav_state(user_id)
    if data == "menu":
        # «Домой» — сбрасываем историю
        nav["stack"] = []
        nav["current"] = "menu"
        _nav_save(user_id, nav)
        return
    if nav.get("current") != data:
        stack = nav.get("stack") or []
        stack.append(nav.get("current") or "menu")
        nav["stack"] = stack[-20:]
        nav["current"] = data
        _nav_save(user_id, nav)


def _nav_pop(user_id: int) -> str:
    """Вернуть предыдущий экран (по умолчанию — главное меню)."""
    nav = _nav_state(user_id)
    stack = nav.get("stack") or []
    target = stack.pop() if stack else "menu"
    nav["stack"] = stack
    nav["current"] = target
    _nav_save(user_id, nav)
    return target


async def _nav_go(update: Update, context: ContextTypes.DEFAULT_TYPE,
                  target: str) -> None:
    """Открыть экран, не трогая объект callback (он неизменяемый)."""
    user = update.effective_user
    _NAV_PENDING[user.id] = target
    try:
        await handle_callback(update, context)
    except Exception as e:  # noqa: BLE001
        log(f"Навигация назад на «{target}» не удалась: {e}")
        try:
            await show_main(update, context, edit=True)
        except Exception:  # noqa: BLE001
            pass
    finally:
        _NAV_PENDING.pop(user.id, None)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    data = _NAV_PENDING.pop(user.id, None) or (q.data or "")
    if data == "nav:back":
        await _nav_go(update, context, _nav_pop(user.id))
        return
    _nav_push(user.id, data)
    if data not in ("noop",):
        keep = getattr(q.message, "message_id", None)
        try:
            await _cleanup_extras(update, context, keep_id=keep)
        except Exception:  # noqa: BLE001
            pass
        try:
            await _cleanup_tracked(update, context, keep_id=keep)
        except Exception:  # noqa: BLE001
            pass
    role = role_of(user.id)
    try:
        await _maybe_send_note(context.bot, user.id)
    except Exception:  # noqa: BLE001
        pass
    if role is None:
        await q.answer("Нет доступа", show_alert=True)
        return

    if data == "cancel":
        set_pending(user.id, None)
        await q.answer("Отменено")
        await show_main(update, context, edit=True)
        return

    if data in ("manual:start", "manual:again"):
        await q.answer("Нет доступа", show_alert=True)
        return

    if data.startswith("sug:"):
        parts = data.split(":")
        action = parts[1]
        row = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        if role_of(user.id) != OWNER:
            await q.answer("Только организатор", show_alert=True)
            return
        item = suggestions_module.get_suggestion(row) if row else None
        if not item:
            await q.answer("Предложение не найдено", show_alert=True)
            return
        accepted = action == "ok"
        suggestions_module.set_status(
            row,
            suggestions_module.STATUS_ACCEPTED if accepted
            else suggestions_module.STATUS_REJECTED,
            green=False)
        await q.answer("Принято" if accepted else "Отклонено")
        status_txt = "✅ Принято" if accepted else "❌ Отклонено"
        await q.edit_message_text(
            f"{status_txt}\n\n{item.get('текст', '')[:400]}",
            reply_markup=kb([[("⬅️ Назад", "nav:back")]]))
        # сообщить автору: первые 10 слов + «...» и кнопка «ок»
        author = norm_name(item.get("репетитор", ""))
        words = str(item.get("текст", "")).split()
        short = " ".join(words[:10]) + ("..." if len(words) > 10 else "")
        verdict = "✅ принято" if accepted else "❌ отклонено"
        for t in load_state().get("tutors", []):
            if norm_name(t.get("name", "")) == author:
                try:
                    await context.bot.send_message(
                        chat_id=int(t["id"]),
                        text=(f"Ваше предложение {verdict}:\n\n{short}"),
                        reply_markup=kb([[("ок", "dismiss")]]))
                except Exception as e:  # noqa: BLE001
                    log(f"Не смог уведомить автора {t['id']}: {e}")
                break
        return

    if data == "cleanall":
        await q.answer()
        try:
            await q.message.delete()
        except Exception:  # noqa: BLE001
            pass
        deleted = 0
        st = load_state()
        hist = (st.get("bot_msgs") or {}).get(str(user.id), [])
        for mid in hist[-100:]:
            try:
                await context.bot.delete_message(chat_id=user.id, message_id=mid)
                deleted += 1
            except Exception:  # noqa: BLE001
                pass
        (st.setdefault("bot_msgs", {}))[str(user.id)] = []
        save_state(st)
        m = await context.bot.send_message(
            chat_id=user.id,
            text=f"🧹 Удалено сообщений: {deleted} (за 48 ч).\n"
                 "Telegram не даёт боту удалять сообщения старше 48 часов.",
            reply_markup=kb([[("🏠 Домой", "menu")]]))
        return

    if data == "dismiss":
        await q.answer()
        try:
            await q.message.delete()
        except Exception:  # noqa: BLE001
            try:
                await q.edit_message_text("✔")
            except Exception:  # noqa: BLE001
                pass
        return

    if data == "suggest":
        await q.answer()
        tutor = name_by_id(user.id) or display_name(user.id) or ""
        p = {"шаг": "предложение", "репетитор": tutor,
             "чужой": False}
        set_pending(user.id, p)
        await q.edit_message_text(
            "💡 Напишите, что улучшить в боте или добавить. "
            "Ваше предложение попадёт организатору.",
            reply_markup=kb([[("Отменить", "menu")]]))
        return

    if data == "new":
        await q.answer()
        await begin_new(update, context)
        return

    # --- «Изменить имя» / оставить как в таблице
    if data == "rename":
        await q.answer()
        await ask_call_name(update, context, edit=True)
        return

    if data == "name:default":
        await q.answer()
        user_full = name_by_id(user.id) or ""
        set_call_name(user.id, user_full.split()[0] if user_full else "Репетитор")
        set_pending(user.id, None)
        await q.edit_message_text("Готово! Выберите действие:", reply_markup=kb_main(user.id))
        return


    # --- статические разделы меню: работают и без начатого черновика.
    # Раньше они стояли после проверки «нет pending» и были недостижимы из меню. ---
    # --- напоминание о занятии по расписанию
    if data.startswith("remind:"):
        parts = data.split(":")
        action, rid = parts[1], parts[2]
        date_s = parts[3] if len(parts) > 3 else ""
        entries = {e["id"]: e for e in schedule_module.all_entries()}
        entry = entries.get(int(rid)) if rid.isdigit() else None
        if not entry:
            await q.answer("Запись расписания не найдена", show_alert=True)
            return
        if action == "ok":
            if role_of(user.id) != OWNER:
                await q.answer("Оплату подтверждает организатор",
                               show_alert=True)
                return
            await q.answer()
            if _already_recorded(entry["репетитор"], date_s,
                                 entry["время"], entry["ученик"]):
                await q.edit_message_text(
                    f"ℹ️ Это занятие уже записано ({date_s}).")
                return
            try:
                checks_writer.append_check({
                    "время": entry["время"], "дата": date_s,
                    "репетитор": entry["репетитор"], "ученик": entry["ученик"],
                    "родитель": entry.get("родитель", ""),
                    "сумма": entry["цена"], "комментарий": "по расписанию",
                    "первое": "нет", "как_прошло": "Отлично", "будут": "Да",
                    "комиссия": "", "оплачено": True,
                })
                schedule_module.update_last_date(entry["id"],
                                                 dt.datetime.strptime(date_s, "%d.%m.%Y").date())
                log_system("записал занятие по расписанию",
                           f"{entry.get('репетитор','')} — "
                           f"{entry.get('ученик','')} ({date_s})")
                log_action(user.id, "подтвердил оплату",
                           f"{entry.get('репетитор','')} — {entry.get('ученик','')} "
                           f"{entry.get('цена','')} ₽ ({date_s})")
                await q.edit_message_text(
                    f"✅ Оплата подтверждена, запись добавлена ({date_s}).")
                try:
                    import asyncio as _asyncio
                    await _asyncio.sleep(1)
                    if q.message is not None:
                        await q.message.delete()
                except Exception:  # noqa: BLE001
                    pass
            except Exception as e:  # noqa: BLE001
                log(f"Ошибка записи из напоминания: {e}")
                await q.edit_message_text(f"❌ Не удалось записать: {e}")
            return
        if action == "was":
            if role_of(user.id) == OWNER:
                await q.answer("Вы владелец — используйте «Подтвердить оплату»",
                               show_alert=True)
                return
            await q.answer()
            if _already_recorded(entry["репетитор"], date_s,
                                 entry["время"], entry["ученик"]):
                await q.edit_message_text(
                    f"ℹ️ Это занятие уже записано ({date_s}).")
                return
            try:
                checks_writer.append_check({
                    "время": entry["время"], "дата": date_s,
                    "репетитор": entry["репетитор"], "ученик": entry["ученик"],
                    "родитель": entry.get("родитель", ""),
                    "сумма": entry["цена"], "комментарий": "по расписанию",
                    "первое": "нет", "как_прошло": "Отлично", "будут": "Да",
                    "комиссия": "", "оплачено": False,
                })
                schedule_module.update_last_date(
                    entry["id"],
                    dt.datetime.strptime(date_s, "%d.%m.%Y").date())
                await q.edit_message_text(
                    f"✅ Занятие записано ({date_s}).\n"
                    "Оплата подтвердится при сверке с выпиской.")
            except Exception as e:  # noqa: BLE001
                log(f"Ошибка записи «занятие было»: {e}")
                await q.edit_message_text(f"❌ Не удалось записать: {e}")
            return

        if action == "no":
            await q.answer()
            await q.edit_message_text("❌ Отменено, запись не добавлена.")
            return
        if action == "move":
            await q.answer()
            p = get_pending(user.id) or {}
            p.update({"шаг": "перенос_время", "remind_id": entry["id"],
                      "remind_date": date_s})
            set_pending(user.id, p)
            await q.edit_message_text(
                "🔁 Напишите новое время занятия, например `19:00`.",
                parse_mode=ParseMode.MARKDOWN)
            return

    if data.startswith("view:"):
        await q.answer()
        arg = data.split(":", 1)[1]
        st = load_state()
        if arg == "me":
            (st.get("view_as") or {}).pop(str(user.id), None)
            save_state(st)
            await show_main(update, context, edit=True)
            return
        only = _view_allowed(user.id)
        names: list[str] = []
        for t in st.get("tutors", []):
            nm = (t.get("name") or "").strip()
            if not nm or nm in names:
                continue
            if only is not None and norm_name(nm) not in only:
                continue
            names.append(nm)
        idx = int(arg) if arg.isdigit() else -1
        if 0 <= idx < len(names):
            st.setdefault("view_as", {})[str(user.id)] = names[idx]
            save_state(st)
            await show_main(update, context, edit=True)
        else:
            await show_owner_picker(update, context)
        return

    if data == "view_mode":
        await q.answer()
        await show_owner_picker(update, context)
        return

    if data == "tcolor":
        await q.answer()
        p = get_pending(user.id) or {}
        p["шаг"] = "цвет_репетитора"
        p["репетитор"] = name_by_id(user.id) or ""
        set_pending(user.id, p)
        try:
            cur = checks_writer.get_tutor_color(p["репетитор"])
        except Exception:  # noqa: BLE001
            cur = None
        cur_txt = ("\nСейчас: задан." if cur else "")
        await q.edit_message_text(
            "🎨 <b>Цвет имени репетитора</b>\n\n"
            "Этот цвет будет у вашего имени в таблице занятий — "
            "и в новых записях, и в уже существующих." + cur_txt + "\n\n"
            "Напишите цвет названием или HEX-кодом:\n"
            "• <code>красный</code>, <code>синий</code>, <code>зелёный</code>\n"
            "• <code>#FF0000</code> или <code>FF0000</code>\n\n"
            "Названия: белый, чёрный, красный, зелёный, синий, голубой, "
            "жёлтый, оранжевый, фиолетовый, розовый, серый.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb([[("Отменить", "menu")]]))
        return

    if data == "pastweek":
        await q.answer()
        await show_past_week(update, context)
        return

    if data.startswith("pay:") or data.startswith("unpay:"):
        if role_of(user.id) != OWNER:
            await q.answer("Оплату подтверждает организатор", show_alert=True)
            return

    if data.startswith("pay:"):
        lid = data.split(":", 1)[1]
        try:
            checks_writer.set_lesson_paid(lid, True)
        except Exception as e:  # noqa: BLE001
            await q.answer(f"Ошибка: {e}", show_alert=True)
            return
        await q.answer("✅ Подтверждено")
        await show_past_week(update, context)
        return

    if data.startswith("unpay:"):
        lid = data.split(":", 1)[1]
        try:
            checks_writer.set_lesson_paid(lid, False)
        except Exception as e:  # noqa: BLE001
            await q.answer(f"Ошибка: {e}", show_alert=True)
            return
        await q.answer("↩️ Снято подтверждение")
        await show_past_week(update, context)
        return

    if data == "notes":
        await q.answer()
        tutor_nm = name_by_id(user.id) or ""
        try:
            note = notes_module.get_note(tutor_nm) if tutor_nm else ""
        except Exception as e:  # noqa: BLE001
            log(f"Не смог прочитать заметку: {e}")
            note = ""
        text = "📝 *Заметки*\n\n" + (note if note else "_Пока пусто._")
        rows = [[("✏️ Изменить", "notes:edit")]]
        if note:
            rows.append([("🗑 Очистить", "notes:clear")])
        rows.append([("🏠 На главный экран", "menu")])
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=kb(rows))
        return

    if data == "notes:edit":
        await q.answer()
        try:
            await _delete_prev(update, context)
        except Exception:  # noqa: BLE001
            pass
        try:
            await _cleanup_tracked(update, context)
        except Exception:  # noqa: BLE001
            pass
        p = get_pending(user.id) or {}
        p["шаг"] = "заметка"
        p["репетитор"] = name_by_id(user.id) or ""
        set_pending(user.id, p)
        await q.edit_message_text(
            "📝 Напишите заметку — любой текст, который нужно не забыть.",
            reply_markup=kb([[("Отменить", "notes")]]))
        return

    if data == "notes:clear":
        await q.answer()
        tutor_nm = name_by_id(user.id) or ""
        try:
            notes_module.clear_note(tutor_nm) if tutor_nm else None
        except Exception as e:  # noqa: BLE001
            log(f"Не смог очистить заметку: {e}")
        await q.edit_message_text("📝 Заметка очищена.",
                                  reply_markup=kb([[("🏠 На главный экран", "menu")]]))
        return

    if data == "more":
        await q.answer()
        await q.edit_message_text("☰ Меню — выберите раздел:",
                                  reply_markup=kb_more(user.id))
        return

    if data in ("menu", "faq", "help"):
        await q.answer()
        if data in ("faq", "help"):
            await q.answer()
            await q.edit_message_text(_faq_text(), parse_mode=ParseMode.HTML,
                                      reply_markup=kb([[("⬅️ Назад", "nav:back")]]))
            return
        await show_main(update, context, edit=True)
        return

    if data == "mine":
        await q.answer()
        await show_mine_edit(update, context)
        return

    if data == "mine:edit":
        await q.answer()
        await show_mine_records(update, context)
        return

    if data.startswith("mact:"):
        lid = data.split(":", 1)[1]
        try:
            lessons = checks_writer.read_lessons()
        except Exception as e:  # noqa: BLE001
            await q.answer(f"Ошибка чтения: {e}", show_alert=True)
            return
        target = next((x for x in lessons
                       if str(x.get("id") or "") == lid), None)
        if not target:
            await q.answer("Запись не найдена", show_alert=True)
            await show_mine_records(update, context)
            return
        await q.answer()
        await q.edit_message_text(
            f"{target['дата']:%d.%m} {target['ученик']} — {target['сумма']} ₽\n\n"
            "Что сделать с записью?",
            reply_markup=kb([
                [("✏️ Изменить сумму", f"msum:{lid}")],
                [("🔁 Перенести", f"mres:{lid}")],
                [("🗑 Удалить", f"mdel:{lid}")],
                [("⬅️ Назад", "mine:edit")],
            ]))
        return

    if data.startswith("msum:"):
        await q.answer()
        lid = data.split(":", 1)[1]
        p = {"шаг": "минумма", "edit_id": lid,
             "репетитор": name_by_id(user.id) or ""}
        set_pending(user.id, p)
        await q.edit_message_text(
            "Напишите новую сумму для этой записи (числом).",
            reply_markup=kb([[("Отменить", "mine")]]))
        return

    if data.startswith("mres:"):
        await q.answer()
        lid = data.split(":", 1)[1]
        target = next((x for x in checks_writer.read_lessons()
                       if str(x.get("id") or "") == lid), None)
        if not target:
            await q.edit_message_text("Запись не найдена.",
                                      reply_markup=kb([[("⬅️ Назад", "mine:edit")]]))
            return
        p = {"шаг": "перенос_дата", "edit_id": lid,
             "репетитор": target["репетитор"], "ученик": target["ученик"],
             "сумма": str(target["сумма"])}
        set_pending(user.id, p)
        await q.edit_message_text(
            "На какую дату и время перенести?\n\nНапишите, например:\n\n"
            "12.11 18:00\n\nЗанятие добавится в расписание один раз "
            "и будет помечено как перенесённое.",
            reply_markup=kb([[("Отменить", "mine")]]))
        return

    if data.startswith("mdel:yes:"):
        await q.answer()
        lid = data.split(":", 2)[2]
        try:
            ok = checks_writer.delete_lesson(lid)
        except Exception as e:  # noqa: BLE001
            log(f"Ошибка удаления записи: {e}")
            await q.edit_message_text(f"❌ Не удалось удалить: {e}",
                                      reply_markup=kb([[("⬅️ Назад", "mine:edit")]]))
            return
        if ok:
            log_action(user.id, "удалил запись", f"id {lid}")
            await q.edit_message_text("🗑 Запись удалена.",
                                      reply_markup=kb([[("🏠 На главный экран", "menu")]]))
        else:
            await q.edit_message_text(
                "⚠️ Запись уже удалена или не найдена.",
                reply_markup=kb([[("🏠 На главный экран", "menu")]]))
        return

    if data.startswith("mdel:"):
        await q.answer()
        lid = data.split(":", 1)[1]
        await q.edit_message_text(
            "🗑 Удалить эту запись из таблицы?",
            reply_markup=kb([[("Да, удалить", f"mdel:yes:{lid}")],
                             [("Отмена", "mine")]]))
        return

    if data == "admin":
        await q.answer()
        if role != OWNER:
            await q.edit_message_text("Раздел только для организатора.",
                                      reply_markup=kb([[("⬅️ Назад", "nav:back")]]))
            return
        st = load_state()
        n = len(st.get("tutors", []))
        cfg = load_config()
        text = (
            "⚙️ *Управление*\n\n"
            f"Репетиторов с доступом: *{n}*\n"
            f"Режим доступа: `{cfg.get('access')}`\n"
            f"Твой id: `{user.id}`\n\n"
            "• /add\\_tutor `<id> <Имя>` — выдать доступ\n"
            "• /list\\_tutors — кто подключён\n"
            "• /remove\\_tutor `<id>` — забрать доступ\n"
            "• /access `anyone|list|invite_code` — режим\n"
            "• /report `<id>` — сводка (или без id — по всем)\n"
        )
        await q.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("📜 Посмотреть логи", "logs")],
                             [("⬅️ Назад", "nav:back")]]))
        return

    # --- журнал действий (только организатор)
    if data == "logs" or data.startswith("logs:"):
        await q.answer()
        if role_of(user.id) != OWNER:
            await q.edit_message_text("Раздел только для организатора.",
                                      reply_markup=kb([[("⬅️ Назад", "nav:back")]]))
            return
        parts = data.split(":")
        day_offset = 0
        page = 0
        if len(parts) >= 3:
            try:
                day_offset = max(0, int(parts[1]))
                page = max(0, int(parts[2]))
            except ValueError:
                day_offset, page = 0, 0
        await show_logs(update, context, day_offset, page)
        return

    # --- разделы меню: доступны и без начатого черновика ---
    if data == "students":
        await q.answer()
        await show_students_list(update, context)
        return

    # --- карточка ученика
    if data.startswith("student:"):
        await q.answer()
        idx = int(data.split(":", 1)[1])
        tutor = name_by_id(user.id) or ""
        roster = students_module.students_of(tutor) if tutor else []
        if idx >= len(roster):
            await q.edit_message_text("Список устарел.",
                                      reply_markup=kb([[("⬅️ Назад", "students")]]))
            return
        s = roster[idx]
        p = {"шаг": "карточка_ученика", "ученик": s["ученик"],
             "репетитор": tutor, "индекс": idx, "row": s.get("row")}
        set_pending(user.id, p)
        text = _card_text(s, _schedule_summary(tutor, s["ученик"]), html_mode=True)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML,
                                  link_preview_options=LinkPreviewOptions(is_disabled=True),
                                  reply_markup=kb([
            [("✏️ Имя ученика", "sedit:name"), ("👤 Родитель", "sedit:parent")],
            [("🔗 Ссылка ученика", "sedit:link_student")],
            [("🔗 Ссылка родителя", "sedit:link_parent")],
            [("💰 Цена занятия", "sedit:rate")],
            [("📝 Комментарий", "sedit:comment")],
            [("✏️ Изменить расписание", "sedit:sched")],
            [("🗑 Удалить ученика", "sedit:del")],
            [("⬅️ Назад", "nav:back")],
        ]))
        return

    if data.startswith("sedit:"):
        await q.answer()
        p = get_pending(user.id)
        if not p or not p.get("ученик"):
            await q.edit_message_text("Откройте ученика заново.",
                                      reply_markup=kb([[("⬅️ Назад", "students")]]))
            return
        what = data.split(":", 1)[1]
        if what == "del":
            await q.edit_message_text(
                f"❗ Удалить ученика *{p['ученик']}*?\n"
                "Его цена, комментарий и расписание тоже исчезнут.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb([[("🗑 Да, удалить", "del:yes")],
                                 [("Отмена", "del:no")]]))
            return
        if what == "comment":
            p["шаг"] = "правка_comment"
            set_pending(user.id, p)
            await q.edit_message_text(
                "Напишите комментарий: класс, предмет, пометки.\n"
                "Например: `7 класс, математика, платит раз в месяц`",
                reply_markup=kb([[("Отменить", "students")]]))
            return
        if what == "sched":
            p["шаг"] = "расписание"
            set_pending(user.id, p)
            try:
                await show_student_schedule(update, context)
            except Exception as e:  # noqa: BLE001
                log(f"Ошибка открытия расписания: {e}")
                await q.edit_message_text(
                    f"Не удалось открыть расписание: {e}",
                    reply_markup=kb([[("⬅️ Назад", "students")]]))
            return
        if what in ("link_student", "link_parent"):
            p["шаг"] = f"правка_{what}"
            set_pending(user.id, p)
            await q.edit_message_text(
                ("🔗 Ссылка на чат с учеником.\n\nМожно отправить:\n"
                 "• номер телефона — надёжнее всего (username может измениться)\n"
                 "• https://t.me/username или @username\n"
                 "• ссылку на ВК: vk.com/username\n"
                 "• любой другой мессенджер или сайт (wa.me, viber, ok.ru, …)\n"
                 "• номер телефона\n\n"
                 "«-» — убрать ссылку."
                 if what == "link_student" else
                 "🔗 Ссылка на чат с родителем.\n\nМожно отправить:\n"
                 "• https://t.me/username или @username\n"
                 "• ссылку на ВК: vk.com/username\n"
                 "• любой другой мессенджер (wa.me, viber, …)\n"
                 "• номер телефона\n\n"
                 "«-» — убрать ссылку."),
                reply_markup=kb([[("Отменить", "students")]]))
            return
        p["шаг"] = f"правка_{what}"
        set_pending(user.id, p)
        prompts = {"name": "Напишите новое имя ученика.",
                   "parent": "Напишите имя родителя.",
                   "rate": "Напишите цену занятия числом, например `1700`."}
        await q.edit_message_text(
            prompts.get(what, "Напишите значение."),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("Отменить", "students")]]))
        return

    # --- «Отзывы»
    if data == "reviews":
        await q.answer()
        tutor = name_by_id(user.id) or ""
        items = reviews_module.reviews_of(tutor) if tutor else []
        total = sum(r["начислено"] for r in items)
        if items:
            lines = [f"• {r['дата']} — {r['ученик'] or '—'} — +{r['начислено']} ₽"
                     for r in items]
            text = (f"⭐ *Отзывы* — {len(items)}, начислено {total} ₽\n\n"
                    + "\n".join(lines))
        else:
            text = ("⭐ *Отзывы*\n\nПока нет ни одного отзыва.\n"
                    "За каждый отзыв начисляется *400 ₽*.")
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=kb([[("➕ Добавить отзыв", "review:add")],
                                                   [("⬅️ Назад", "nav:back")]]))
        return

    if data == "review:add":
        await q.answer()
        tutor = name_by_id(user.id) or ""
        roster = students_module.students_of(tutor) if tutor else []
        p = {"шаг": "отзыв_ученик", "репетитор": tutor}
        set_pending(user.id, p)
        rows = [[(s["ученик"], f"review:stu:{i}")] for i, s in enumerate(roster)]
        rows.append([("Без ученика", "review:stu:none"),
                     ("⬅️ Назад", "reviews")])
        await q.edit_message_text(
            "За кого отзыв?", reply_markup=kb(rows))
        return

    if data.startswith("review:stu:"):
        await q.answer()
        p = get_pending(user.id) or {}
        arg = data.split(":", 2)[2]
        tutor = p.get("репетитор") or name_by_id(user.id) or ""
        if arg == "none":
            p["отзыв_ученик"] = ""
        else:
            roster = students_module.students_of(tutor) if tutor else []
            idx = int(arg)
            p["отзыв_ученик"] = roster[idx]["ученик"] if idx < len(roster) else ""
        p["шаг"] = "отзыв_текст"
        set_pending(user.id, p)
        await q.edit_message_text(
            "Напишите сам отзыв или ссылку на него (можно коротко).",
            reply_markup=kb([[("Отменить", "reviews")]]))
        return

    # --- «Реферальная программа»
    if data == "referrals":
        await q.answer()
        tutor = name_by_id(user.id) or ""
        items = referrals_module.referrals_of(tutor) if tutor else []
        if items:
            lines = [f"• {r['дата']} — {r['кого']} — {r['статус'] or '—'}"
                     for r in items]
            text = ("🎁 *Реферальная программа*\n\n"
                    "Приведите репетитора — получите *500 ₽*.\n\n"
                    + "\n".join(lines))
        else:
            text = ("🎁 *Реферальная программа*\n\n"
                    "Приведите репетитора — получите *500 ₽*.\n\nПока никого нет.")
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=kb([[("➕ Указать репетитора", "ref:add")],
                                                   [("⬅️ Назад", "nav:back")]]))
        return

    if data == "ref:add":
        await q.answer()
        tutor = name_by_id(user.id) or ""
        p = {"шаг": "реферал_имя", "репетитор": tutor}
        set_pending(user.id, p)
        await q.edit_message_text(
            "Напишите имя репетитора, которого вы привели (имя и фамилию).",
            reply_markup=kb([[("Отменить", "referrals")]]))
        return

    # --- «Расписание»
    if data.startswith("conf:"):
        await q.answer()
        p = get_pending(user.id) or {}
        if not p.get("resolve"):
            await q.edit_message_text("Черновик потерян, начните заново.",
                                      reply_markup=kb([[("Расписание", "sched:week")]]))
            return
        pm = p["resolve"].get("prompt")
        if pm:
            try:
                await context.bot.delete_message(chat_id=pm["chat"], message_id=pm["id"])
            except Exception:  # noqa: BLE001
                pass
        what = data.split(":", 1)[1]
        if what == "cancel":
            set_pending(user.id, None)
            await show_schedule_after_cancel(update, context, user.id)
            return
        if what.startswith("par:"):
            idx = int(what.split(":")[1])
            same = _students_same_name(p.get("репетитор") or name_by_id(user.id) or "",
                                       p["resolve"].get("student", ""))
            p["resolve"]["parent_chosen"] = same[idx].get("родитель", "") if idx < len(same) else ""
            set_pending(user.id, p)
            await _apply_resolve(update, context, p, "par")
            return
        await _apply_resolve(update, context, p, what)
        return

    if data.startswith("schday:"):
        await q.answer()
        d = int(data.split(":", 1)[1])
        tutor = name_by_id(user.id) or ""
        _today = dt.date.today()
        _day = _today + dt.timedelta(days=(d - _today.weekday()) % 7)
        entries = [e for e in schedule_module.entries_of(tutor)
                   if d in schedule_module.parse_days(str(e.get("дни", "")))]
        entries += [e for e in schedule_module.entries_of(tutor)
                    if schedule_module.one_time_date(str(e.get("дни", ""))) == _day]
        entries.sort(key=lambda x: _time_key(x.get("время", "")))
        rows = []
        for e in entries:
            label = f"{e.get('время', '')} {e.get('ученик', '')}"
            if e.get("родитель"):
                label += f" ({e['родитель']})"
            rows.append([(label[:44], f"sch:open:{e['id']}")])
        rows.append([(f"➕ Добавить на {schedule_module.DAY_NAMES[d]}", f"addw:{d}")])
        rows.append([("⬅️ К неделе", "sched:week")])
        text = (f"✏️ *{schedule_module.DAY_NAMES[d]}*\n\n"
                + ("Нажмите на занятие, чтобы изменить или удалить."
                   if entries else "Занятий нет. Добавьте первое."))
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=kb(rows))
        return

    if data.startswith("uncancel:"):
        await q.answer()
        parts = data.split(":")
        day = dt.datetime.strptime(parts[1], "%Y-%m-%d").date()
        t = ":".join(parts[2:]) if len(parts) > 2 else ""
        tutor = name_by_id(user.id) or ""
        try:
            ok = schedule_module.remove_cancel(tutor, day, t)
        except Exception as e:  # noqa: BLE001
            await q.answer(f"Ошибка: {e}", show_alert=True)
            return
        text2, buttons = await schedule_view(user.id, 7)
        note = "↩️ Отмена снята — занятие снова в расписании." if ok else \
               "⚠️ Отмена не найдена."
        await q.edit_message_text(note + "\n\n" + text2,
                                  parse_mode=ParseMode.HTML,
                                  reply_markup=kb(buttons),
                                  link_preview_options=LinkPreviewOptions(is_disabled=True))
        return

    if data == "cancel_lesson":
        await q.answer()
        tutor = name_by_id(user.id) or ""
        today = dt.date.today()
        entries = schedule_module.entries_of(tutor)
        rows = []
        seen_days = set()
        for off in range(0, 15):
            day = today + dt.timedelta(days=off)
            cnt = 0
            for e in entries:
                single = schedule_module.one_time_date(str(e.get("дни", "")))
                same_day = (single == day) if single else (
                    day.weekday() in schedule_module.parse_days(str(e.get("дни", ""))))
                if same_day:
                    cnt += 1
            if cnt and day not in seen_days:
                seen_days.add(day)
                rows.append([(f"{schedule_module.DAY_NAMES[day.weekday()]} "
                              f"{day:%d.%m} — {cnt} зан.",
                              f"cxd:{day:%Y-%m-%d}")])
        if not seen_days:
            await q.edit_message_text(
                "⚠️ На ближайшие 15 дней занятий нет — отменять нечего.",
                reply_markup=kb([[("⬅️ Назад", "nav:back")]]))
            return
        rows.append([("⬅️ Назад", "nav:back")])
        await q.edit_message_text(
            "❌ *Отмена занятия*\n\nВыберите день:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
        return

    if data.startswith("cxd:"):
        await q.answer()
        day = dt.datetime.strptime(data.split(":", 1)[1], "%Y-%m-%d").date()
        tutor = name_by_id(user.id) or ""
        rows = []
        _cans = {(c["дата"], schedule_module.parse_time_slot(c["время"]))
                 for c in schedule_module.cancels_for(tutor)}
        for e in schedule_module.entries_of(tutor):
            single = schedule_module.one_time_date(str(e.get("дни", "")))
            same_day = (single == day) if single else (
                day.weekday() in schedule_module.parse_days(str(e.get("дни", ""))))
            if not same_day:
                continue
            tm = str(e.get("время", ""))
            by_key = (day, schedule_module.parse_time_slot(tm)) in _cans
            if by_key:
                rows.append([(f"↩️ Вернуть {tm} {e.get('ученик', '')}",
                              f"uncancel:{day:%Y-%m-%d}:{tm}")])
            else:
                label = f"{tm} {e.get('ученик', '')}"
                rows.append([(label[:44], f"cx:{day:%Y-%m-%d}:{tm}")])
        rows.append([("⬅️ К дням", "cancel_lesson")])
        await q.edit_message_text(
            f"❌ *{schedule_module.DAY_NAMES[day.weekday()]} {day:%d.%m}*\n\n"
            "Выберите занятие:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
        return

    if data.startswith("cx:"):
        await q.answer()
        parts = data.split(":")
        day = dt.datetime.strptime(parts[1], "%Y-%m-%d").date()
        t = ":".join(parts[2:]) if len(parts) > 2 else ""
        tutor = name_by_id(user.id) or ""
        student = ""
        target = None
        for e in schedule_module.entries_of(tutor):
            single = schedule_module.one_time_date(str(e.get("дни", "")))
            same_day = (single == day) if single else (
                day.weekday() in schedule_module.parse_days(str(e.get("дни", ""))))
            if same_day and schedule_module.parse_time_slot(str(e.get("время", ""))) == \
                    schedule_module.parse_time_slot(t):
                student = e.get("ученик", "")
                target = e
                break
        if target is not None:
            if schedule_module.one_time_date(str(target.get("дни", ""))) is not None:
                schedule_module.delete_entry(target["id"])
                msg = (f"🗑 Разовое занятие удалено полностью: "
                       f"{schedule_module.DAY_NAMES[day.weekday()]} "
                       f"{day:%d.%m} {t} {student}.")
            else:
                schedule_module.add_cancel(tutor, student, day, t)
                log_action(user.id, "отменил занятие",
                           f"{student} {day:%d.%m} {t}")
                msg = (f"❌ Отменено только на {schedule_module.DAY_NAMES[day.weekday()]} "
                       f"{day:%d.%m} {t} {student}.\n"
                       "На следующей неделе занятие останется как обычно.")
        else:
            msg = ("⚠️ Занятие не найдено — вероятно, его уже удалили "
                   "или изменили время. Список обновлён.")
            text2, buttons = await schedule_view(user.id, 7)
            await q.edit_message_text(msg + "\n\n" + text2,
                                      parse_mode=ParseMode.HTML,
                                      reply_markup=kb(buttons),
                                      link_preview_options=LinkPreviewOptions(is_disabled=True))
            return
        text2, buttons = await schedule_view(user.id, 7)
        await q.edit_message_text(msg + "\n\n" + text2,
                                  parse_mode=ParseMode.HTML,
                                  reply_markup=kb(buttons),
                                  link_preview_options=LinkPreviewOptions(is_disabled=True))
        return

    if data == "addonce":
        await q.answer()
        p = {"шаг": "разовое", "репетитор": name_by_id(user.id) or ""}
        set_pending(user.id, p)
        await q.edit_message_text(
            "➕ Разовое занятие (например, перенос).\n\n"
            "Напишите дату, время и ученика:\n\n12.11 18:00 Полина",
            reply_markup=kb([[("Отменить", "sched:week")]]))
        return

    if data.startswith("addw:"):
        await q.answer()
        d = int(data.split(":", 1)[1])
        p = {"шаг": "добавить_в_день", "weekday": d,
             "репетитор": name_by_id(user.id) or ""}
        set_pending(user.id, p)
        existing = [e for e in schedule_module.entries_of(
                        name_by_id(user.id) or "")
                    if d in schedule_module.parse_days(str(e.get("дни", "")))]
        existing.sort(key=lambda x: _time_key(x.get("время", "")))
        now_lines = "\n".join(
            f"  {e.get('время', '')} {e.get('ученик', '')}"
            for e in existing) or "  —"
        msg = await q.edit_message_text(
            f"*{schedule_module.DAY_NAMES[d]}* — сейчас в этот день:\n"
            f"{now_lines}\n\n"
            "Напишите время и ученика. Можно сразу несколько строк:\n\n"
            "17:00 Полина\n18:30 Ксения\n\n"
            "Если поставить минус — занятие будет убрано:\n\n-18:30 Ксения",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("Отменить", "sched:week")]]))
        mid = getattr(msg, "message_id", None)
        if isinstance(mid, int):
            st = load_state()
            pend = st.get("pending", {}).get(str(user.id))
            if pend is not None:
                pend["addw_msg"] = {"chat": msg.chat_id, "id": mid}
                save_state(st)
        return

    if data.startswith("sdel:"):
        await q.answer()
        eid = data.split(":", 1)[1]
        if eid.isdigit():
            schedule_module.delete_entry(int(eid))
        text, buttons = await schedule_view(user.id, 7)
        buttons = buttons + [[("🏠 На главный экран", "menu")]]
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=kb(buttons))
        return

    if data.startswith("sched:"):
        await q.answer()
        mode = data.split(":", 1)[1]
        days = {"today": 1, "week": 7, "month": 30}.get(mode, 1)
        text, days_buttons = await schedule_view(user.id, days)
        text = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", text)
        text = text.replace("*", "")
        markup = kb(days_buttons)
        if len(text) <= 3500:
            await q.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True))
            return
        chunks, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > 3500:
                chunks.append(cur)
                cur = ""
            cur += line + "\n"
        chunks.append(cur)
        await q.edit_message_text(chunks[0], parse_mode=ParseMode.HTML,
                                  link_preview_options=LinkPreviewOptions(is_disabled=True))
        extra = []
        for i, part in enumerate(chunks[1:], start=1):
            m = await _send(update, part, parse_mode=ParseMode.HTML,
                            reply_markup=markup if i == len(chunks) - 1 else None)
            try:
                extra.append({"chat": m.chat_id, "id": m.message_id})
            except Exception:  # noqa: BLE001
                pass
        st = load_state()
        st.setdefault("extra_msgs", {})[str(user.id)] = extra
        save_state(st)
        return

    if data.startswith("add:"):
        await q.answer()
        d = data.split(":", 1)[1]
        state = load_state()
        state.setdefault("pending", {})[str(user.id)] = {
            "репетитор": name_by_id(user.id) or "",
            "шаг": "ученик", "дата": d,
            "первое": "нет", "как_прошло": "Отлично", "будут": "Да",
            "комментарий": "", "комиссия": "",
        }
        save_state(state)
        await begin_new_students(update, context)
        return

    # --- подтверждение удаления
    if data == "del:yes":
        await q.answer()
        p = get_pending(user.id) or {}
        if p.get("ученик"):
            students_module.delete_student(p.get("репетитор") or name_by_id(user.id) or "",
                                           p["ученик"])
            log_action(user.id, "удалил ученика",
                       f"{p.get('ученик','')} "
                       f"({p.get('репетитор') or name_by_id(user.id) or ''})")
        set_pending(user.id, None)
        await show_main(update, context, edit=True)
        await _send(update, "🗑 Ученик удалён.",
                    reply_markup=kb([[("🏠 На главный экран", "menu")]]))
        return

    if data == "del:no":
        await q.answer()
        p = get_pending(user.id) or {}
        await show_student_card(update, context,
                                p.get("репетитор") or name_by_id(user.id) or "",
                                p.get("ученик", ""))
        return

    # --- дубль ученика
    if data == "dup:open":
        await q.answer()
        p = get_pending(user.id) or {}
        set_pending(user.id, None)
        await show_student_card(update, context,
                                p.get("репетитор") or name_by_id(user.id) or "",
                                p.get("ученик", ""))
        return

    if data == "dup:rename":
        await q.answer()
        p = get_pending(user.id) or {}
        p["шаг"] = "дубль_имя"
        set_pending(user.id, p)
        await q.edit_message_text(
            "Напишите другое имя для нового ученика "
            "(например, добавьте фамилию или цифру).",
            reply_markup=kb([[("Отмена", "cancel")]]))
        return

    # --- новый ученик: кнопки
    if data.startswith("n:price:"):
        await q.answer()
        p = get_pending(user.id) or {}
        val = data.split(":")[2]
        if val != "skip":
            students_module.set_rate(tutor_of(p, user), p.get("ученик", ""), val)
            p["тариф"] = val
        p["шаг"] = "н_коммент"
        set_pending(user.id, p)
        await q.edit_message_text(
            "Комментарий: класс, предмет, пометки?",
            reply_markup=kb([[("Пропустить", "n:comment:skip")]]))
        return

    if data == "n:comment:skip":
        await q.answer()
        p = get_pending(user.id) or {}
        p["шаг"] = "н_расписание"
        set_pending(user.id, p)
        await q.edit_message_text(
            "Расписание: напишите занятия строками:\n\n"
            "Пн 17:00\n"
            "Ср 19:00\n\n"
            "или нажмите «Позже».",
            reply_markup=kb([[("Позже", "n:sched:skip")]]))
        return

    if data == "n:sched:skip":
        await q.answer()
        p = get_pending(user.id) or {}
        p["шаг"] = "карточка_ученика"
        set_pending(user.id, p)
        await show_student_schedule(update, context)
        return

    if data == "noop":
        await q.answer()
        return

    # --- расписание конкретного ученика
    if data == "sch:list":
        await q.answer()
        await show_student_schedule(update, context)
        return

    if data == "sch:add":
        await q.answer()
        p = get_pending(user.id) or {}
        p["шаг"] = "расписание_добавить"
        set_pending(user.id, p)
        await q.edit_message_text(
            "Напишите занятия — каждое с новой строки:\n"
            "Пн 17:00\nСр 19:00\n\nМожно сразу несколько строк.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("⬅️ Назад", "sch:list")]]))
        return

    if data.startswith("sch:open:"):
        await q.answer()
        rid = data.split(":")[2]
        entry = schedule_module.get_entry(int(rid)) if rid.isdigit() else None
        if not entry:
            await show_student_schedule(update, context)
            return
        who = entry.get("ученик", "")
        if entry.get("родитель"):
            who += f" ({entry['родитель']})"
        price = f"\n💰 {entry['цена']} ₽" if entry.get("цена") else ""
        _sname = entry.get("ученик", "")
        await q.edit_message_text(
            f"⏰ <b>{entry.get('дни', '')} {entry.get('время', '')}</b>\n"
            f"🧒 {who}{price}\n\nЧто сделать?",
            parse_mode=ParseMode.HTML,
            reply_markup=kb([
                [("🧒 Карточка ученика", f"sch:card:{rid}")],
                [("✏️ Изменить день и время", f"sch:edit:{rid}")],
                [("🗑 Удалить занятие", f"sch:del:{rid}")],
                [("⬅️ Назад", "nav:back")],
            ]))
        return

    if data.startswith("sch:card:"):
        await q.answer()
        rid = data.split(":")[2]
        entry = schedule_module.get_entry(int(rid)) if rid.isdigit() else None
        if not entry:
            await q.answer("Занятие не найдено", show_alert=True)
            return
        tutor = name_by_id(user.id) or ""
        await show_student_card(update, context, tutor, entry.get("ученик", ""))
        return

    if data.startswith("sch:del:"):
        await q.answer()
        rid = data.split(":")[2]
        if rid.isdigit():
            schedule_module.delete_entry(int(rid))
        await show_student_schedule(update, context)
        return

    if data.startswith("sch:edit:"):
        await q.answer()
        rid = data.split(":")[2]
        p = get_pending(user.id) or {}
        p["шаг"] = "расписание_правка"
        p["edit_id"] = rid
        set_pending(user.id, p)
        await q.edit_message_text(
            "Напишите новое день и время, например Ср 18:30.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("⬅️ Назад", "sch:list")]]))
        return

    # --- добавить ученика (доступно и без начатого черновика)
    if data == "add_student2":
        await q.answer()
        p = {"репетитор": name_by_id(user.id) or "", "режим": "ученики"}
        await ask_new_student(update, context, p, edit=True)
        return

    if data == "add_student":
        await q.answer()
        existing = get_pending(user.id)
        p = existing or {"репетитор": name_by_id(user.id) or "",
                         "режим": "ученики"}
        p.setdefault("репетитор", name_by_id(user.id) or "")
        await ask_new_student(update, context, p, edit=True)
        return

    p = get_pending(user.id)
    if not p:
        await q.answer("Начните заново", show_alert=True)
        await show_main(update, context, edit=True)
        return

    # --- выбор ученика из списка
    if data.startswith("stu:"):
        await q.answer()
        idx = int(data.split(":", 1)[1])
        tutor = p.get("репетитор") or name_by_id(user.id) or ""
        roster = students_module.students_of(tutor) if tutor else []
        if idx >= len(roster):
            await q.edit_message_text(
                "Список устарел, начните заново.",
                reply_markup=kb([[("Записать занятие", "new")], [("Меню", "menu")]]))
            return
        st = roster[idx]
        p["ученик"] = st["ученик"]
        p["родитель"] = st.get("родитель", "")
        p["тариф"] = st.get("тариф", "")
        if p.get("дата"):
            await ask_amount(update, context, p)
        else:
            await ask_date(update, context, p)
        return

    # --- сумма
    if data == "rate:use":
        await q.answer()
        p["сумма"] = str(p.get("тариф", "")).replace(" ", "")
        await ask_time(update, context, p)
        return

    if data == "rate:change":
        await q.answer()
        p["шаг"] = "тариф_текст"
        set_pending(user.id, p)
        await q.edit_message_text(
            "Напишите новую цену занятия для этого ученика (числом).\n"
            "Она сохранится в списке учеников.",
            reply_markup=kb([[("Отменить", "cancel")]]))
        return

    if data.startswith("amount:"):
        await q.answer()
        arg = data.split(":", 1)[1]
        if arg == "custom":
            p["шаг"] = "сумма_текст"
            set_pending(user.id, p)
            await q.edit_message_text("Напишите сумму числом, например: `1700`",
                                      reply_markup=kb([[("Отменить", "cancel")]]))
            return
        p["сумма"] = arg.replace(" ", "")
        # цену запоминаем за учеником (если её ещё не было)
        if not p.get("тариф") and p.get("ученик") and p.get("репетитор"):
            try:
                students_module.set_rate(p["репетитор"], p["ученик"], p["сумма"])
                p["тариф"] = p["сумма"]
            except Exception as e:  # noqa: BLE001
                log(f"Не смог сохранить цену ученика: {e}")
        await ask_time(update, context, p)
        return

    # --- дата занятия
    if data.startswith("date:"):
        await q.answer()
        arg = data.split(":", 1)[1]
        if arg == "back":
            await ask_date(update, context, p)
            return
        if arg == "cal":
            import datetime as _d
            await q.edit_message_text("📅 Выберите дату:",
                                      reply_markup=calendar_kb(_d.date.today().year,
                                                               _d.date.today().month))
            return
        if arg == "today":
            d = dt.date.today()
        elif arg == "yesterday":
            d = dt.date.today() - dt.timedelta(days=1)
        else:
            try:
                d = dt.datetime.strptime(arg, "%d.%m.%Y").date()
            except ValueError:
                await q.answer("Не понял дату", show_alert=True)
                return
        p["дата"] = d.strftime("%d.%m.%Y")
        set_pending(user.id, p)
        await ask_amount(update, context, p)
        return

    if data.startswith("d:"):
        await q.answer()
        y, m = data.split(":", 1)[1].split("-")
        await q.edit_message_text("📅 Выберите дату:",
                                  reply_markup=calendar_kb(int(y), int(m)))
        return

    if data.startswith("dd:"):
        await q.answer()
        try:
            d = dt.datetime.strptime(data.split(":", 1)[1], "%Y-%m-%d").date()
        except ValueError:
            return
        p["дата"] = d.strftime("%d.%m.%Y")
        set_pending(user.id, p)
        await ask_amount(update, context, p)
        return

    if data == "date:back":
        await q.answer()
        await ask_date(update, context, p)
        return

    # --- время
    if data == "time:now":
        await q.answer()
        p["время"] = checks_writer.now_hhmm()
        await show_confirm(update, context, p)
        return

    if data == "time:skip":
        await q.answer()
        await q.edit_message_text(
            "⚠️ *Вы пропускаете время занятия.*\n\n"
            "Чек нужно заполнять *после оплаты*, и время желательно указывать — "
            "по нему проще найти платёж в выписке.\n\n"
            "Точно пропустить время?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("Да, пропустить", "time:skip_confirm")],
                             [("Нет, укажу время", "time:enter")]]))
        return

    if data == "time:skip_confirm":
        await q.answer()
        p["время"] = ""
        await show_confirm(update, context, p)
        return

    if data == "time:enter":
        await q.answer()
        p["шаг"] = "время"
        set_pending(user.id, p)
        await q.edit_message_text(
            "⏰ Выберите время или введите своё:",
            reply_markup=kb(TIME_SLOTS + [[("⌨️ Ввести вручную", "time:manual")],
                                          [("Отменить", "cancel")]]))
        return

    if data.startswith("time:slot:"):
        await q.answer()
        p["время"] = data.split(":", 2)[2]
        await show_confirm(update, context, p)
        return

    if data == "time:manual":
        await q.answer()
        p["шаг"] = "время_текст"
        set_pending(user.id, p)
        await q.edit_message_text(
            "Напишите время в формате `ЧЧ:ММ`, например `17:30`.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb([[("Отменить", "cancel")]]))
        return

    # --- первое ли занятие
    if data.startswith("first:"):
        await q.answer()
        yes = data.endswith("yes")
        p["первое"] = "Да" if yes else "нет"
        if yes:
            p["шаг"] = "комиссия"
            set_pending(user.id, p)
            await q.edit_message_text(
                "Сколько комиссии за первое занятие? (можно 0)",
                reply_markup=kb([[("0", "commission:0")], [("Отменить", "cancel")]]))
        else:
            p["комиссия"] = ""
            await show_confirm(update, context, p)
        return

    if data.startswith("commission:"):
        await q.answer()
        p["комиссия"] = data.split(":", 1)[1]
        await show_confirm(update, context, p)
        return

    # --- как прошло
    if data.startswith("with:"):
        await q.answer()
        p["как_прошло"] = data.split(":", 1)[1]
        await show_confirm(update, context, p)
        return

    # --- будут заниматься
    if data.startswith("will:"):
        await q.answer()
        p["будут"] = "Да" if data.endswith("yes") else "нет"
        await show_confirm(update, context, p)
        return

    # --- точечная правка полей из окна подтверждения
    if data == "edit:first":
        await q.answer()
        await q.edit_message_text("Первое занятие с этим учеником?",
                                  reply_markup=kb_yes_no("first"))
        return
    if data == "edit:with":
        await q.answer()
        await q.edit_message_text("Как прошло занятие?",
                                  reply_markup=kb([[("Отлично", "with:Отлично")],
                                                   [("Нормально", "with:Нормально")],
                                                   [("Плохо", "with:Плохо")]]))
        return
    if data == "edit:will":
        await q.answer()
        await q.edit_message_text("Ученик будет заниматься дальше?",
                                  reply_markup=kb_after_first())
        return
    if data == "edit:comment":
        await q.answer()
        p["шаг"] = "комментарий"
        set_pending(user.id, p)
        await q.edit_message_text(
            "Комментарий? Например «отправили за 2 занятия».",
            reply_markup=kb([[("Пропустить", "comment:skip")],
                             [("Отменить", "cancel")]]))
        return
    if data == "comment:skip":
        await q.answer()
        p["комментарий"] = ""
        await show_confirm(update, context, p)
        return

    # --- сохранение
    if data in ("save", "save:force"):
        await q.answer()
        p = get_pending(user.id) or {}
        if data == "save:force":
            p["force_dup"] = True
            set_pending(user.id, p)
        # проверка дублей: то же занятие (дата, ученик, сумма, время)
        if not p.get("force_dup"):
            try:
                existing = [
                    x for x in checks_writer.read_lessons()
                    if norm_name(x["репетитор"]) == norm_name(p.get("репетитор", ""))
                    and x["дата"] == dt.datetime.strptime(
                        str(p.get("дата", "")), "%d.%m.%Y").date()
                    and norm_name(x["ученик"]) == norm_name(p.get("ученик", ""))
                    and x["сумма"] == parse_amount(p.get("сумма", ""))
                ]
            except Exception:  # noqa: BLE001
                existing = []
            same = [x for x in existing
                    if str(x.get("время", "")) == str(p.get("время", ""))]
            if same:
                p["force_dup"] = False
                set_pending(user.id, p)
                await q.edit_message_text(
                    f"⚠️ Похоже, такое занятие уже записано:\n\n"
                    f"{p.get('дата')} {p.get('время')} {p.get('ученик')} — "
                    f"{p.get('сумма')} ₽\n\n"
                    "Это действительно ещё одно занятие?",
                    reply_markup=kb([
                        [("✅ Записать всё равно", "save:force")],
                        [("Отмена", "cancel")]]))
                return
        if p.get("чужой"):
            comm = (p.get("комментарий") or "").strip()
            p["комментарий"] = (comm + " " if comm else "") + "(чужой ID)"
        try:
            row = checks_writer.append_check(p)
        except Exception as e:  # noqa: BLE001
            log(f"Ошибка записи: {e}")
            await q.edit_message_text(
                "❌ Не удалось записать в таблицу. Попробуйте ещё раз или напишите организатору.\n"
                f"{e}",
                reply_markup=kb([[("Попробовать снова", "save")], [("Отменить", "cancel")]]))
            return
        st = load_state()
        st.setdefault("last_seen_tutor", {})[str(user.id)] = {
            "ученик": p.get("ученик"), "сумма": p.get("сумма")}
        save_state(st)
        set_pending(user.id, None)
        log_action(user.id, "записал занятие",
                   f"{p.get('репетитор','')} — {p.get('ученик','')} "
                   f"{p.get('сумма','')} ₽ ({p.get('дата','')} "
                   f"{p.get('время','') or 'без времени'})")
        await notify_owner(context, p, row)
        tm = f" {p['время']}" if p.get("время") else ", без времени"
        where = checks_writer.target_name(p.get('репетитор') or '')
        note = ""
        try:
            others = [
                x for x in checks_writer.read_lessons()
                if norm_name(x["репетитор"]) == norm_name(p.get("репетитор", ""))
                and norm_name(x["ученик"]) == norm_name(p.get("ученик", ""))
                and x["дата"] == dt.datetime.strptime(
                    str(p.get("дата", "")), "%d.%m.%Y").date()
                and str(x.get("время", "")) != str(p.get("время", ""))
            ]
            if others:
                times = ", ".join(sorted({str(x.get("время", "")) for x in others}))
                note = (f"\n\nℹ️ В этот день у ученика уже есть занятие: {times}. "
                        "Если это дубль — удалите лишнее в «Текущих 2 неделях».")
        except Exception:  # noqa: BLE001
            pass
        await q.edit_message_text(
            f"✅ Записано в таблицу, строка {row} ({where}).\n\n"
            f"{p['репетитор']} — {p['ученик']} — {p['сумма']} ₽ ({p['дата']}{tm})"
            + note,
            reply_markup=kb([[("Записать ещё", "new")],
                             [("Меню", "menu")]]))
        return

    if data == "redo":
        await q.answer()
        set_pending(user.id, None)
        await begin_new(update, context)
        return

    # --- «Мои ученики»
        await q.answer()
        days = 7 if data == "sched:week" else 30
        await q.edit_message_text(await schedule_text(user.id, days),
                                  parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=kb([[("За неделю", "sched:week"),
                                                    ("За месяц", "sched:month")],
                                                   [("⬅️ Назад", "nav:back")]]))
        return

    await q.answer()


# --------------------------------------------------------------------------- #
# Шаги диалога записи
# --------------------------------------------------------------------------- #
def _save_msg_id(user_id: int, chat_id: int, message_id: int) -> None:
    if not isinstance(message_id, int):
        return
    state = load_state()
    state.setdefault("msg", {})[str(user_id)] = {"chat": chat_id, "id": message_id}
    save_state(state)


async def _delete_prev(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удалить предыдущее рабочее сообщение бота (важные не отслеживаются)."""
    user = update.effective_user
    data = (load_state().get("msg") or {}).get(str(user.id))
    if not data:
        return
    try:
        await context.bot.delete_message(chat_id=data["chat"], message_id=data["id"])
    except Exception:  # noqa: BLE001 — уже удалено/старое
        pass
    state = load_state()
    (state.get("msg") or {}).pop(str(user.id), None)
    save_state(state)


async def _delete_user_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Best-effort удаление сообщения пользователя.

    В личных чатах Telegram не разрешает ботам удалять сообщения пользователя —
    тогда тихо пропускаем. В группах (если бот админ) сработает.
    """
    msg = getattr(update, "message", None)
    if msg is None or getattr(msg, "message_id", None) is None:
        return
    try:
        await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
    except Exception:  # noqa: BLE001
        pass


def _remember_msg(user_id: int, message_id) -> None:
    """Запомнить id сообщения бота (для «удалить все сообщения»)."""
    if not isinstance(message_id, int):
        return
    try:
        st = load_state()
        lst = st.setdefault("bot_msgs", {}).setdefault(str(user_id), [])
        lst.append(message_id)
        if len(lst) > 150:
            del lst[:-120]
        save_state(st)
    except Exception:  # noqa: BLE001
        pass


async def _send(update: Update, text: str, *, track: bool = True, **kwargs):
    """Ответить текстом и запомнить сообщение (чтобы удалить на следующем шаге)."""
    kwargs.setdefault("link_preview_options",
                      LinkPreviewOptions(is_disabled=True))
    msg = await update.effective_message.reply_text(text, **kwargs)
    if track and msg is not None:
        try:
            _save_msg_id(update.effective_user.id, msg.chat_id, msg.message_id)
            _remember_msg(update.effective_user.id, msg.message_id)
        except Exception:  # noqa: BLE001
            pass
    return msg


def with_home(markup):
    """Добавить кнопку «🏠 Домой» внизу клавиатуры, если её ещё нет."""
    if markup is None:
        return None
    rows = [list(r) for r in markup.inline_keyboard]
    texts = [b.text for row in rows for b in row]
    if any(t.startswith("🏠") for t in texts):
        return markup
    rows.append([InlineKeyboardButton("🏠 Домой", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


async def _say(update: Update, text: str, reply_markup=None,
               parse_mode: str | None = None) -> None:
    """Отправить шаг диалога: отредактировать сообщение с кнопкой или ответить текстом."""
    if update.callback_query is not None:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=parse_mode, reply_markup=with_home(reply_markup),
                link_preview_options=LinkPreviewOptions(is_disabled=True))
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "Message is not modified" in msg:
                return
            if "Message to edit not found" in msg or "no text in the message" in msg:
                # сообщение удалено или уже сменилось — шлём новое
                await _send(update, text, parse_mode=parse_mode,
                            reply_markup=with_home(reply_markup))
                return
            raise
    else:
        await _send(update, text, parse_mode=parse_mode,
                    reply_markup=with_home(reply_markup))


def parse_time(text: str) -> str | None:
    """'17:30' / '1730' / '17.30' / '17' -> 'HH:MM', иначе None."""
    s = str(text).strip().replace(".", ":").replace(" ", "")
    if not s:
        return None
    if ":" in s:
        parts = s.split(":")
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        hh, mm = int(parts[0]), int(parts[1])
    elif s.isdigit():
        if len(s) <= 2:
            hh, mm = int(s), 0
        elif len(s) == 3:
            hh, mm = int(s[0]), int(s[1:])
        elif len(s) == 4:
            hh, mm = int(s[:2]), int(s[2:])
        else:
            return None
    else:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{hh:02d}:{mm:02d}"


TIME_SLOTS = [[("10:00", "time:slot:10:00"), ("12:00", "time:slot:12:00")],
              [("15:00", "time:slot:15:00"), ("17:00", "time:slot:17:00")],
              [("18:00", "time:slot:18:00"), ("19:00", "time:slot:19:00")]]


def calendar_kb(year: int, month: int) -> InlineKeyboardMarkup:
    """Календарь кнопками: дни месяца + стрелки переключения месяца."""
    import calendar as _cal
    import datetime as _d
    weeks = _cal.Calendar(firstweekday=0).monthdayscalendar(year, month)
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)

    MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь",
              "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
    rows = [[(f"‹ {MONTHS[prev_m - 1]}", f"d:{prev_y}-{prev_m:02d}"),
             (f"{MONTHS[month - 1]}", "noop"),
             (f"{MONTHS[next_m - 1]} ›", f"d:{next_y}-{next_m:02d}")]]
    rows.append([("Пн", "noop"), ("Вт", "noop"), ("Ср", "noop"),
                 ("Чт", "noop"), ("Пт", "noop"), ("Сб", "noop"), ("Вс", "noop")])
    for week in weeks:
        row = []
        for day in week:
            if day == 0:
                row.append((" ", "noop"))
            else:
                row.append((str(day), f"dd:{year:04d}-{month:02d}-{day:02d}"))
        rows.append(row)
    today = _d.date.today()
    rows.append([("Сегодня", f"date:{today:%d.%m.%Y}"),
                 ("⬅️ Назад", "date:back")])
    return kb(rows)


async def ask_date(update: Update, context: ContextTypes.DEFAULT_TYPE, p: dict):
    """Шаг выбора даты занятия."""
    user = update.effective_user
    p["шаг"] = "дата"
    set_pending(user.id, p)
    import datetime as _d
    today = _d.date.today()
    yest = today - _d.timedelta(days=1)
    before = today - _d.timedelta(days=2)
    await _say(update, "📅 Когда было занятие?",
               reply_markup=kb([
                   [(f"Сегодня {today:%d.%m}", f"date:{today:%d.%m.%Y}"),
                    (f"Вчера {yest:%d.%m}", f"date:{yest:%d.%m.%Y}")],
                   [(f"Позавчера {before:%d.%m}", f"date:{before:%d.%m.%Y}"),
                    ("📅 Календарь", "date:cal")],
                   [("Отменить", "cancel")]]))


async def ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE, p: dict):
    user = update.effective_user
    p["шаг"] = "сумма"
    if not p.get("дата"):
        p["дата"] = dt.date.today().strftime("%d.%m.%Y")
    set_pending(user.id, p)
    who = p["ученик"] + (f" ({p['родитель']})" if p.get("родитель") else "")
    price = str(p.get("тариф") or "").strip()
    if price:
        markup = kb([[("💰 " + price + " ₽", "rate:use")],
                     [("💸 Изменить цену", "rate:change")],
                     [("Отменить", "cancel")]])
        text = (f"🧒 *{who}*\n\nЦена занятия: *{price} ₽*.\n"
                "Записать эту сумму?")
    else:
        markup = kb(PRESET_AMOUNTS + [[("Ввести своё", "amount:custom")],
                                      [("Отменить", "cancel")]])
        text = (f"🧒 *{who}*\n\nСколько пришло за занятие?\n"
                "_Выбранная сумма запомнится как цена ученика._")
    await _say(update, text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)


def time_suggestions(count: int = 6) -> list[str]:
    """Шесть прошедших часов до текущего момента: 22, 21, 20, 19, 18, 17."""
    now = dt.datetime.now()
    out = []
    for step in range(count):
        h = now - dt.timedelta(hours=step)
        out.append(h.strftime("%H:00"))
    return out


async def ask_time(update: Update, context: ContextTypes.DEFAULT_TYPE, p: dict):
    user = update.effective_user
    p["шаг"] = "время"
    set_pending(user.id, p)
    slots = time_suggestions(6)
    rows = []
    for i in range(0, len(slots), 3):
        rows.append([(t, f"time:slot:{t}") for t in slots[i:i + 3]])
    today_s = dt.date.today().strftime("%d.%m.%Y")
    if p.get("дата", today_s) == today_s:
        now = checks_writer.now_hhmm()
        rows.append([(f"Сейчас {now}", "time:now")])
    rows.append([("Пропустить", "time:skip")])
    await _say(update,
        f"💰 Сумма: *{p['сумма']} ₽*\n\n"
        "⏰ *Время* — когда было занятие?\n"
        "_Напишите время сообщением (например 17:30) или нажмите кнопку._",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))


async def ask_first(update: Update, context: ContextTypes.DEFAULT_TYPE, p: dict):
    user = update.effective_user
    p["шаг"] = "первое"
    set_pending(user.id, p)
    await _say(update,
        "Это *первое* занятие с этим учеником?\n_По умолчанию — нет._",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb_yes_no("first"))


async def ask_how(update: Update, context: ContextTypes.DEFAULT_TYPE, p: dict):
    user = update.effective_user
    p["шаг"] = "как_прошло"
    set_pending(user.id, p)
    await _say(update,
        "Как прошло занятие?\n_По умолчанию — «Отлично»._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[("Отлично ✅", "with:Отлично")],
                         [("Нормально", "with:Нормально")],
                         [("Плохо", "with:Плохо")]]))


async def ask_will(update: Update, context: ContextTypes.DEFAULT_TYPE, p: dict):
    user = update.effective_user
    p["шаг"] = "будут"
    set_pending(user.id, p)
    await _say(update,
        "Ученик будет заниматься дальше?\n_По умолчанию — да._",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb_after_first())


async def show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, p: dict):
    """Итог: значения по умолчанию, любое можно поправить точечно."""
    user = update.effective_user
    p["шаг"] = "подтверждение"
    set_pending(user.id, p)
    who = p["ученик"] + (f" ({p['родитель']})" if p.get("родитель") else "")
    time_str = p.get("время") or "не указано ⚠️"
    comm = p.get("комиссия")
    text = (
        "📋 *Проверьте запись:*\n\n"
        f"👤 {p['репетитор']}\n"
        f"🧒 {who}\n"
        f"📅 {p['дата']}   ⏰ {time_str}\n"
        f"💰 {p['сумма']} ₽\n\n"
        f"🆕 Первое занятие: {p.get('первое', 'нет')}"
        + (f" (комиссия {comm} ₽)" if comm else "") + "\n"
        f"🙂 Как прошло: {p.get('как_прошло', 'Отлично')}\n"
        f"📈 Будут заниматься: {p.get('будут', 'Да')}\n"
        f"💬 Комментарий: {p.get('комментарий') or '—'}"
    )
    markup = kb([
        [("✅ Всё верно, сохранить", "save")],
        [("Первое занятие", "edit:first"), ("Как прошло", "edit:with")],
        [("Будут заниматься", "edit:will"), ("Комментарий", "edit:comment")],
        [("Начать заново", "new"), ("Отменить", "cancel")],
    ])
    await _say(update, text, parse_mode=ParseMode.MARKDOWN,
                                                  reply_markup=markup)


async def ask_new_student(update: Update, context: ContextTypes.DEFAULT_TYPE, p: dict,
                          edit=True):
    user = update.effective_user
    p["шаг"] = "новый_ученик"
    set_pending(user.id, p)
    text = ("Введите ученика: `Имя (Имя родителя)` — например `Ваня (Мария)`.\n"
            "Можно просто имя: `Ваня`.\n\n"
            "Он появится в вашем списке учеников.")
    markup = kb([[("Отменить", "cancel")]])
    if edit and update.callback_query:
        await _say(update,
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
    else:
        await _say(update, text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)


async def schedule_text(user_id: int, days: int) -> str:
    """Занятия репетитора за N дней: из «Занятий», за месяц — ещё и из «Истории»."""
    tutor = name_by_id(user_id)
    if not tutor:
        return "Сначала выберите своё имя (/start)."

    start = dt.date.today() - dt.timedelta(days=days)
    lessons = [x for x in checks_writer.read_lessons(include_history=days > 7)
               if x["репетитор"] == tutor and x["дата"] >= start]

    if not lessons:
        return f"📅 За последние {days} дней занятий нет."

    lessons.sort(key=lambda x: (x["дата"], _time_key(x["время"])))
    try:
        import reconcile
        ops = reconcile.load_ops(None) or []
    except Exception:  # noqa: BLE001
        ops = []

    total = 0
    out = []
    for r in lessons:
        total += r["сумма"]
        paid = ""
        for o in ops:
            try:
                if o["dt"].date() == r["дата"] and int(o["sum"]) == r["сумма"]:
                    paid = " ✅"
                    break
            except (AttributeError, ValueError):
                continue
        if not paid and ops:
            paid = " ⏳"
        tm = f" {r['время']}" if r["время"] else ""
        out.append(f"{r['дата']:%d.%m}{tm} — {r['ученик']} — {r['сумма']} ₽{paid}")
    tail = "\n\n✅ — есть в выписке, ⏳ — не нашлось" if ops else ""
    return (f"📅 *Занятия за {days} дн.*\n\n" + "\n".join(out)
            + f"\n\n*Итого: {total} ₽*" + tail)


def _faq_text() -> str:
    """Текст раздела FAQ из листа «FAQ» (вопросы + ответы)."""
    try:
        items = faq_module.rows()
    except Exception as e:  # noqa: BLE001
        log(f"FAQ: ошибка чтения: {e}")
        items = []
    if not items:
        return ("❓ <b>FAQ — частые вопросы</b>\n\n"
                "Ваших вопросов пока не было.")
    parts = ["❓ <b>FAQ — частые вопросы</b>\n"]
    for n, it in enumerate(items, 1):
        q = it["вопрос"].replace("<", "&lt;").replace(">", "&gt;")
        parts.append(f"<b>{n}. {q}</b>")
        if it["ответ"]:
            a = it["ответ"].replace("<", "&lt;").replace(">", "&gt;")
            parts.append(f"   {a}")
        else:
            parts.append("   <i>ответ готовится</i>")
        parts.append("")
    return "\n".join(parts).strip()


FAQ_TEXT = "❓ <b>FAQ — частые вопросы</b>\n\nВаших вопросов пока не было."
HELP_TEXT = FAQ_TEXT

HELP_TEXT = FAQ_TEXT


async def _delete_later(context, chat_id: int, message_id: int,
                        delay: float = 3.0) -> None:
    """Удалить сообщение через delay секунд."""
    import asyncio as _asyncio
    await _asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:  # noqa: BLE001
        pass


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фото/файлы пока не поддерживаются."""
    user = update.effective_user
    msg = getattr(update, "message", None)
    try:
        if msg is not None:
            await context.bot.delete_message(chat_id=msg.chat_id,
                                             message_id=msg.message_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        sent = await context.bot.send_message(
            chat_id=user.id, text="пока только текст(")
        await _delete_later(context, user.id, sent.message_id, 3.0)
    except Exception:  # noqa: BLE001
        pass


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    _touch_activity()
    try:
        await _maybe_send_note(context.bot, user.id)
    except Exception:  # noqa: BLE001
        pass
    text = (update.message.text or "").strip()
    try:
        for ent in (getattr(update.message, "entities", None) or []):
            if getattr(ent, "type", "") == "custom_emoji":
                log(f"CUSTOM_EMOJI id={getattr(ent, 'custom_emoji_id', '')}")
    except Exception:  # noqa: BLE001
        pass
    # журнал всех входящих сообщений — чтобы ничего не терялось
    try:
        with open(str(sheets_util.DATA_DIR / "messages.log"), "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%d.%m.%Y %H:%M:%S} | "
                    f"id={user.id} | {display_name(user.id) or ''} | {text}\n")
    except Exception:  # noqa: BLE001
        pass
    await _delete_prev(update, context)
    await _delete_user_msg(update, context)
    p = get_pending(user.id)
    if role_of(user.id) is None:
        await _send(update,
            "🔒 Нет доступа. Ваш id: `" + str(user.id) + "` — "
            "отправьте его организатору.",
            parse_mode=ParseMode.MARKDOWN)
        return

    if not p:
        await show_main(update, context)
        return

    step = p.get("шаг")

    # --- предложение по улучшению бота
    if step == "предложение":
        tutor = p.get("репетитор") or display_name(user.id) or f"ID {user.id}"
        log_action(user.id, "предложил улучшение", text[:60])
        row = suggestions_module.add_suggestion(tutor, text[:500],
                                                source=str(user.id))
        set_pending(user.id, None)
        is_owner = role_of(user.id) == OWNER
        if is_owner:
            try:
                suggestions_module.set_status(
                    row, suggestions_module.STATUS_CONFIRMED, green=False)
            except Exception as e:  # noqa: BLE001
                log(f"Не удалось отметить предложение: {e}")
            await _send(update, track=False, text=
                "✅ Ваше предложение подтверждено и принято в работу.",
                reply_markup=kb([[("Меню", "menu")]]))
            return
        # уведомляем организатора с кнопками
        cfg = load_config()
        for oid in cfg.get("owner_ids", []):
            try:
                await context.bot.send_message(
                    chat_id=int(oid),
                    text=(f"💡 *Новое предложение*\n\n"
                          f"От: {tutor}\n\n{text[:500]}"),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb([
                        [("✅ Принять", f"sug:ok:{row}")],
                        [("❌ Отклонить", f"sug:no:{row}")]]))
                log(f"Предложение {row} отправлено владельцу {oid}")
            except Exception as e:  # noqa: BLE001
                log(f"Не смог отправить предложение {oid}: {e}")
        await _send(update, track=False, text=
            "✅ Спасибо! Предложение отправлено организатору.",
            reply_markup=kb([[("Меню", "menu")]]))
        return

    # --- как обращаться к репетитору
    if step == "имя":
        pm = (p or {}).get("prompt_msg")
        if pm:
            try:
                await context.bot.delete_message(chat_id=pm["chat"],
                                                 message_id=pm["id"])
            except Exception:  # noqa: BLE001
                pass
        name = norm_name(text)[:40]
        if len(name) < 2:
            await _send(update, "Напишите имя, например: Данил")
            return
        set_call_name(user.id, name)
        set_pending(user.id, None)
        await _send(update, 
            f"Готово, {name}! Выберите действие:", reply_markup=kb_main(user.id))
        return

    # --- перенос занятия из напоминания
    if step == "перенос_время":
        t = parse_time(text)
        if not t:
            await _send(update, "Не понял время. Например: 19:00")
            return
        rid = p.get("remind_id")
        date_s = p.get("remind_date", "")
        try:
            _update_entry_time(rid, t)
        except Exception as e:  # noqa: BLE001
            log(f"Не смог обновить время расписания: {e}")
        set_pending(user.id, None)
        await _send(update, 
            f"🔁 Перенесено на {t}. Напоминание придёт через час после начала.",
            reply_markup=kb([[("Меню", "menu")]]))
        return

    # --- постоянное расписание ученика: список
    if step == "расписание":
        await show_student_schedule(update, context)
        return

    # --- добавить несколько занятий строками
    if step == "расписание_добавить":
        import re as _re
        added = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = _re.match(r"^([^\s]+)\s+(\d{1,2}[:.]\d{2}|\d{1,2})$", line)
            if not m:
                continue
            days = schedule_module.parse_days(m.group(1))
            t = parse_time(m.group(2))
            if not days or not t:
                continue
            tutor = p.get("репетитор") or name_by_id(user.id) or ""
            st = students_module.find_student(tutor, p.get("ученик", "")) or {}
            schedule_module.add_entry(
                tutor, p.get("ученик", ""), (st.get("родитель") or ""),
                schedule_module.DAY_NAMES[days[0]], t, st.get("тариф") or "")
            added += 1
        if not added:
            await _send(update, "Не понял. Пример:\nПн 17:00\nСр 19:00",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=kb([[("⬅️ Назад", "sch:list")]]))
            return
        p["шаг"] = "карточка_ученика"
        set_pending(user.id, p)
        await _finish_with_card(update, context, p,
                                f"✅ Добавлено занятий: {added}")
        return

    # --- правка одного занятия
    if step == "расписание_правка":
        import re as _re
        m = _re.match(r"^([^\s]+)\s+(\d{1,2}[:.]\d{2}|\d{1,2})$", text.strip())
        if not m:
            await _send(update, "Формат: Ср 18:30", parse_mode=ParseMode.MARKDOWN,
                        reply_markup=kb([[("⬅️ Назад", "sch:list")]]))
            return
        days = schedule_module.parse_days(m.group(1))
        t = parse_time(m.group(2))
        if not days or not t:
            await _send(update, "Не понял день или время. Пример: Ср 18:30",
                        parse_mode=ParseMode.MARKDOWN)
            return
        rid = p.get("edit_id", "")
        if str(rid).isdigit():
            schedule_module.update_entry(int(rid),
                                         schedule_module.DAY_NAMES[days[0]], t)
        p["шаг"] = "расписание"
        set_pending(user.id, p)
        await _send(update, "✅ Изменено.", reply_markup=kb([[("⬅️ К расписанию", "sch:list")]]))
        return

    # --- новая цена занятия для ученика
    if step == "тариф_текст":
        amt = parse_amount(text)
        if amt is None:
            await _send(update, "Напишите число, например 1700.")
            return
        p["тариф"] = str(amt)
        p["сумма"] = str(amt)
        if p.get("ученик") and p.get("репетитор"):
            try:
                students_module.set_rate(p["репетитор"], p["ученик"], str(amt))
            except Exception as e:  # noqa: BLE001
                log(f"Не смог сохранить цену: {e}")
        await ask_time(update, context, p)
        return

    # --- дубль: новое имя
    if step == "дубль_имя":
        new_name = norm_name(text)[:60]
        if len(new_name) < 2:
            await _send(update, "Слишком коротко. Напишите имя.")
            return
        tutor = p.get("репетитор") or name_by_id(user.id) or ""
        if students_module.find_student(tutor, new_name):
            await _send(update, "Такое имя тоже уже есть. Напишите другое.")
            return
        students_module.add_student(tutor, new_name, p.get("дубль_родитель", ""))
        log_action(user.id, "добавил ученика",
                   f"{new_name} ({p.get('дубль_родитель') or 'без родителя'})")
        p.update({"ученик": new_name, "родитель": p.get("дубль_родитель", "")})
        if p.get("режим") == "ученики":
            p.pop("режим", None)
            p["шаг"] = "н_цена"
            set_pending(user.id, p)
            await _send(update, f"✅ {new_name} добавлен.\n\nКакая цена занятия?",
                        reply_markup=kb([
                            [("1 300", "n:price:1300"), ("1 500", "n:price:1500")],
                            [("1 700", "n:price:1700"), ("2 000", "n:price:2000")],
                            [("Пропустить", "n:price:skip")]]))
            return
        p["шаг"] = "ученик"
        set_pending(user.id, p)
        await _send(update, f"✅ {new_name} добавлен.",
                    reply_markup=kb([[("⬅️ К ученикам", "students")]]))
        return

    # --- добавить/убрать занятия в конкретном дне (много строк, конфликты)
    if step == "добавить_в_день":
        p["queue"] = [l for l in text.splitlines() if l.strip()]
        set_pending(user.id, p)
        await _process_schedule_queue(update, context, p)
        return

    # --- отмена занятия на дату
    if step == "отмена_занятия":
        import re as _re
        m = _re.match(r"^(\d{1,2}[.]\d{1,2}(?:[.]\d{2,4})?)\s+(\d{1,2}[:.]\d{2})$",
                      text.strip())
        if not m:
            await _send(update, "Формат: 12.11 18:00")
            return
        rd, rt = m.group(1), m.group(2)
        if rd.count(".") == 1:
            rd += f".{dt.date.today().year}"
        try:
            d = dt.datetime.strptime(rd, "%d.%m.%Y").date()
        except ValueError:
            await _send(update, "Не понял дату. Формат: 12.11 18:00")
            return
        t = parse_time(rt)
        tutor = p.get("репетитор") or name_by_id(user.id) or ""
        student = ""
        for e in schedule_module.entries_of(tutor):
            single = schedule_module.one_time_date(str(e.get("дни", "")))
            same_day = (single == d) if single else (
                d.weekday() in schedule_module.parse_days(str(e.get("дни", ""))))
            if same_day and schedule_module.parse_time_slot(str(e.get("время", ""))) == \
                    schedule_module.parse_time_slot(t):
                student = e.get("ученик", "")
                break
        if not student:
            await _send(update, "Не нашёл занятие на эту дату и время.")
            return
        schedule_module.add_cancel(tutor, student, d, t)
        set_pending(user.id, None)
        await _send(update, f"❌ Отменено: {d:%d.%m} {t} {student}.\nНапоминание не придёт.",
                    reply_markup=kb([[("📅 Расписание", "sched:week")]]))
        return

    # --- разовое занятие
    if step == "разовое" and len(text) > 60 and ":" not in text:
        set_pending(user.id, None)
        await _send(update,
                    "Это сообщение не похоже на разовое занятие — я записал его "
                    "в журнал и вышел из режима.\n\n"
                    "Если это предложение по боту — «Остальное» → "
                    "«💡 Предложить улучшение».",
                    reply_markup=kb([[("💡 Предложить улучшение", "suggest")],
                                     [("🏠 На главный экран", "menu")]]))
        return

    if step == "разовое":
        import re as _re
        m = _re.match(r"^(\d{1,2}[.]\d{1,2}(?:[.]\d{2,4})?)\s+"
                      r"(\d{1,2}[:.]\d{2})\s+(.+)$", text.strip())
        if not m:
            await _send(update, "Формат: 12.11 18:00 Полина")
            return
        rd, rt, who = m.group(1), m.group(2), norm_name(m.group(3))
        if rd.count(".") == 1:
            rd += f".{dt.date.today().year}"
        try:
            d = dt.datetime.strptime(rd, "%d.%m.%Y").date()
        except ValueError:
            await _send(update, "Не понял дату. Формат: 12.11 18:00 Полина")
            return
        t = parse_time(rt)
        student, parent = who, ""
        if "(" in who and who.endswith(")"):
            student = who[:who.index("(")].strip()
            parent = who[who.index("(") + 1:-1].strip()
        tutor = p.get("репетитор") or name_by_id(user.id) or ""
        st = students_module.find_student(tutor, student) or {}
        # зачёркиваем чужое занятие в это же время, если есть
        for e in schedule_module.entries_of(tutor):
            single = schedule_module.one_time_date(str(e.get("дни", "")))
            same_day = (single == d) if single else (
                d.weekday() in schedule_module.parse_days(str(e.get("дни", ""))))
            if (same_day and norm_name(e.get("ученик", "")) != norm_name(student)
                    and schedule_module.parse_time_slot(str(e.get("время", ""))) ==
                    schedule_module.parse_time_slot(t)):
                schedule_module.add_cancel(tutor, e.get("ученик", ""), d, t)
        busy = []
        for _e in schedule_module.entries_of(tutor):
            _single = schedule_module.one_time_date(str(_e.get("дни", "")))
            _same = (_single == d) if _single else (
                d.weekday() in schedule_module.parse_days(str(_e.get("дни", ""))))
            if (_same and schedule_module.parse_time_slot(str(_e.get("время", ""))) ==
                    schedule_module.parse_time_slot(t)
                    and norm_name(_e.get("ученик", "")) != norm_name(student)):
                busy.append(_e.get("ученик", ""))
        if busy:
            await _send(update,
                        f"⚠️ На {d:%d.%m} {t} уже стоит: {', '.join(busy)}.\n"
                        "Разовое занятие не добавлено — выберите другое время.",
                        reply_markup=kb([[("📅 Расписание", "sched:week")]]))
            return
        schedule_module.add_one_time(tutor, student, parent or st.get("родитель", ""),
                                     d, t, st.get("тариф", ""))
        set_pending(user.id, None)
        await _send(update, f"✅ Разовое: {d:%d.%m} {t} {student}.",
                    reply_markup=kb([[("📅 Расписание", "sched:week")]]))
        return

    # --- перенос занятия (разово)
    if step == "перенос_дата":
        import re as _re
        m = _re.match(r"^(\d{1,2}[.]\d{1,2}(?:[.]\d{2,4})?)\s+"
                      r"(\d{1,2}[:.]\d{2}|\d{1,2})$", text.strip())
        if not m:
            await _send(update, "Не понял. Пример: 12.11 18:00")
            return
        raw_date, raw_time = m.group(1), m.group(2)
        if raw_date.count(".") == 1:
            raw_date += f".{dt.date.today().year}"
        try:
            d = dt.datetime.strptime(raw_date, "%d.%m.%Y").date()
        except ValueError:
            await _send(update, "Не понял дату. Пример: 12.11 18:00")
            return
        t = parse_time(raw_time)
        if not t:
            await _send(update, "Не понял время. Пример: 12.11 18:00")
            return
        st = students_module.find_student(p.get("репетитор", ""),
                                          p.get("ученик", "")) or {}
        eid = schedule_module.add_one_time(
            p.get("репетитор", ""), p.get("ученик", ""),
            st.get("родитель", ""), d, t,
            p.get("сумма") or st.get("тариф", ""))
        set_pending(user.id, None)
        await _send(update,
                    f"🔁 Перенесено на {d:%d.%m.%Y} {t}.\n"
                    f"Занятие в расписании помечено как перенесённое "
                    f"(#{eid}) и после этой даты исчезнет.",
                    reply_markup=kb([[("⬅️ " + _week_btn(user.id), "mine")]]))
        return

    # --- цвет имени репетитора (для всех его занятий)
    if step == "цвет_репетитора":
        tutor_nm = p.get("репетитор") or name_by_id(user.id) or ""
        try:
            ok, count = checks_writer.set_tutor_color(tutor_nm, text)
        except Exception as e:  # noqa: BLE001
            log(f"Ошибка цвета репетитора: {e}")
            ok, count = False, 0
        set_pending(user.id, None)
        if ok:
            await _send(update,
                        f"🎨 Цвет имени изменён. Покрашено строк: {count}.\n"
                        "Новые записи будут в этом цвете автоматически.",
                        reply_markup=kb([[("🏠 Домой", "menu")]]))
        else:
            await _send(update,
                        "Не понял цвет. Напишите название (красный, синий…) "
                        "или HEX-код вида #FF0000.",
                        reply_markup=kb([[("🏠 Домой", "menu")]]))
        return

    # --- правка суммы записи
    if step == "минумма":
        amt = parse_amount(text)
        if amt is None:
            await _send(update, "Напишите число, например 1700.")
            return
        lid = str(p.get("edit_id", ""))
        for x in checks_writer.read_lessons():
            if str(x.get("id") or "") == lid:
                rec = {"время": x["время"], "дата": x["дата"].strftime("%d.%m.%Y"),
                       "репетитор": x["репетитор"], "ученик": x["ученик"],
                       "родитель": x.get("родитель", ""),
                       "сумма": str(amt), "комментарий": x["комментарий"],
                       "первое": x["первое"], "как_прошло": x["как_прошло"],
                       "будут": x["будут"], "комиссия": x["комиссия"]}
                checks_writer.update_lesson(lid, rec)
                log_action(user.id, "изменил сумму",
                           f"{x['репетитор']} — {x['ученик']} "
                           f"→ {amt} ₽")
                break
        set_pending(user.id, None)
        await _send(update, "✅ Запись изменена.",
                    reply_markup=kb([[("⬅️ " + _week_btn(user.id), "mine")]]))
        return

    # --- новый ученик: цена
    if step == "н_цена":
        amt = parse_amount(text)
        if amt is None:
            await _send(update, "Напишите число, например 1700.")
            return
        students_module.set_rate(tutor_of(p, user), p.get("ученик", ""), str(amt))
        p.update({"тариф": str(amt), "шаг": "н_коммент"})
        set_pending(user.id, p)
        await _send(update, "Цена сохранена.\n\nКомментарий: класс, предмет, "
                            "пометки?", reply_markup=kb([[("Пропустить", "n:comment:skip")]]))
        return

    # --- новый ученик: комментарий
    if step == "н_коммент":
        students_module.update_student(tutor_of(p, user), p.get("ученик", ""),
                                       comment=text[:300])
        p.update({"шаг": "н_расписание"})
        set_pending(user.id, p)
        await _send(update, "Комментарий сохранён.\n\n"
                            "Расписание: напишите занятия строками:\n\n"
                            "Пн 17:00\n"
                            "Ср 19:00\n\n"
                            "или нажмите «Позже».",
                    reply_markup=kb([[("Позже", "n:sched:skip")]]))
        return

    # --- новый ученик: расписание
    if step == "н_расписание":
        import re as _re
        added = 0
        for line in text.splitlines():
            m = _re.match(r"^([^\s]+)\s+(\d{1,2}[:.]\d{2}|\d{1,2})$", line.strip())
            if not m:
                continue
            days = schedule_module.parse_days(m.group(1))
            t = parse_time(m.group(2))
            if not days or not t:
                continue
            schedule_module.add_entry(
                tutor_of(p, user), p.get("ученик", ""), p.get("родитель", ""),
                schedule_module.DAY_NAMES[days[0]], t, p.get("тариф", ""))
            added += 1
        p["шаг"] = "карточка_ученика"
        set_pending(user.id, p)
        done = (f"✅ Занятий добавлено: {added}" if added
                else "Хорошо, расписание заполните позже.")
        await _finish_with_card(update, context, p, done)
        return

    # --- заметка
    if step == "заметка":
        tutor_nm = p.get("репетитор") or name_by_id(user.id) or ""
        try:
            notes_module.set_note(tutor_nm, text[:2000])
            log_action(user.id, "изменил заметку", text[:60])
        except Exception as e:  # noqa: BLE001
            log(f"Не смог сохранить заметку: {e}")
            await _send(update, f"❌ Не удалось сохранить заметку: {e}")
            return
        set_pending(user.id, None)
        await _send(update, "✅ Заметка сохранена.",
                    reply_markup=kb([[("📝 Заметки", "notes")],
                                     [("🏠 На главный экран", "menu")]]))
        return

    # --- комментарий ученика
    if step == "правка_comment":
        _tut_c = p.get("репетитор") or name_by_id(user.id) or ""
        _stu_c = p.get("ученик") or ""
        log_action(user.id, "изменил комментарий",
                   f"{_tut_c} — {_stu_c}")
        tutor = p.get("репетитор") or name_by_id(user.id) or ""
        students_module.update_student(tutor, p.get("ученик", ""),
                                       comment=text[:300], **_where(p))
        set_pending(user.id, None)
        await _send(update, "✅ Комментарий сохранён.",
                    reply_markup=kb([[("⬅️ К ученикам", "students")]]))
        return

    # --- правка ссылок
    if step in ("правка_link_student", "правка_link_parent"):
        _tut_l = p.get("репетитор") or name_by_id(user.id) or ""
        _stu_l = p.get("ученик") or ""
        log_action(user.id, "изменил ссылку родителя" if
                   step == "правка_link_parent" else "изменил ссылку ученика",
                   f"{_tut_l} — {_stu_l}")
        tutor = p.get("репетитор") or name_by_id(user.id) or ""
        student = p.get("ученик") or ""
        link = text.strip()
        if link in ("-", "—", "нет", "убрать"):
            link = ""
        else:
            norm = normalize_link(link)
            if norm is None:
                await _send(update,
                            "Не понял ссылку. Можно:\n"
                            "• номер телефона (надёжнее: username может измениться)\n"
                            "• https://t.me/username или @username\n"
                            "• vk.com/username (ВК)\n"
                            "• https://wa.me/79991234567 (WhatsApp)\n"
                            "• номер телефона (+79991234567)\n"
                            "• «-», чтобы убрать")
                return
            link = norm
        row = p.get("row")
        saved = False
        if row:
            if step == "правка_link_student":
                saved = students_module.update_student_row(
                    int(row), student_link=link)
            else:
                saved = students_module.update_student_row(
                    int(row), parent_link=link)
        if not saved:
            if step == "правка_link_student":
                saved = students_module.update_student(
                    tutor, student, student_link=link, **_where(p))
            else:
                saved = students_module.update_student(
                    tutor, student, parent_link=link, **_where(p))
        set_pending(user.id, None)
        try:
            await _delete_prev(update, context)
        except Exception:  # noqa: BLE001
            pass
        if saved:
            await _send(update, "✅ Ссылка сохранена.",
                        reply_markup=kb([[("⬅️ К ученикам", "students")]]))
        else:
            await _send(update,
                        "⚠️ Не нашёл ученика в таблице «Ученики» — "
                        "возможно, он добавлен только в расписании.\n"
                        "Добавьте его в «👥 Мои ученики» и повторите.",
                        reply_markup=kb([[("👥 Мои ученики", "students")],
                                         [("🏠 Домой", "menu")]]))
        return

    # --- правка карточки ученика
    if step in ("правка_name", "правка_parent", "правка_rate"):
        tutor = p.get("репетитор") or name_by_id(user.id) or ""
        student = p.get("ученик") or ""
        if step == "правка_rate":
            amt = parse_amount(text)
            if amt is None:
                await _send(update, "Напишите число, например 1700.")
                return
            students_module.update_student(tutor, student, rate=str(amt),
                                           **_where(p))
            log_action(user.id, "изменил цену ученика",
                       f"{tutor} — {student} → {amt} ₽")
        elif step == "правка_parent":
            students_module.update_student(tutor, student,
                                           parent=norm_name(text)[:60],
                                           **_where(p))
            log_action(user.id, "изменил родителя",
                       f"{tutor} — {student} → {norm_name(text)[:60]}")
        else:
            new_name = norm_name(text)[:60]
            if len(new_name) < 2:
                await _send(update, "Слишком коротко.")
                return
            students_module.update_student(tutor, student,
                                           new_name=new_name, **_where(p))
            log_action(user.id, "переименовал ученика",
                       f"{tutor} — {student} → {new_name}")
        set_pending(user.id, None)
        await _send(update, 
            "✅ Сохранено.", reply_markup=kb([[("⬅️ К ученикам", "students")]]))
        return

    # --- текст отзыва
    if step == "отзыв_текст":
        p["отзыв"] = text[:300]
        row = reviews_module.add_review(
            p.get("репетитор") or name_by_id(user.id) or "",
            p.get("отзыв_ученик", ""), p["отзыв"])
        log_action(user.id, "оставил отзыв",
                   f"{p.get('отзыв_ученик') or 'без ученика'}: {p['отзыв'][:60]}")
        set_pending(user.id, None)
        await _send(update, track=False, text=
            f"⭐ Отзыв сохранён ({reviews_module.REWARD} ₽ начислено).",
            reply_markup=kb([[("⭐ Отзывы", "reviews")], [("Меню", "menu")]]))
        return

    # --- имя приведённого репетитора
    if step == "реферал_имя":
        referee = norm_name(text)[:80]
        if len(referee) < 3:
            await _send(update, "Напишите имя и фамилию репетитора.")
            return
        tutor = p.get("репетитор") or name_by_id(user.id) or ""
        referrals_module.add_referral(tutor, referee)
        set_pending(user.id, None)
        await _send(update, track=False, text=
            f"🎁 Записал: вы привели {referee}.\n"
            f"Вознаграждение {referrals_module.REWARD} ₽.",
            reply_markup=kb([[("🎁 Реферальная программа", "referrals")],
                             [("Меню", "menu")]]))
        return

    # --- ученик введён вручную (не было в списке)
    if step in ("новый_ученик", "ученик_вручную", "ученик"):
        raw = norm_name(text)
        if len(raw) < 2:
            await _send(update, 
                "Слишком коротко. Напишите: `Имя (Имя родителя)`",
                parse_mode=ParseMode.MARKDOWN)
            return
        # «Имя (Родитель)» или просто «Имя»
        student, parent = raw, ""
        if "(" in raw and raw.endswith(")"):
            student = raw[:raw.index("(")].strip()
            parent = raw[raw.index("(") + 1:-1].strip()
        if len(student) < 2:
            await _send(update, 
                "Имя слишком короткое. Например: `Ваня (Мария)`",
                parse_mode=ParseMode.MARKDOWN)
            return
        p["ученик"] = student
        p["родитель"] = parent
        p["тариф"] = ""
        tutor_check = p.get("репетитор") or name_by_id(user.id) or ""
        dup = students_module.find_student(tutor_check, student) if tutor_check else None
        if dup and norm_name(dup.get("родитель", "")) == norm_name(parent):
            p.update({"шаг": "дубль_ученика", "репетитор": tutor_check,
                      "дубль_родитель": parent})
            set_pending(user.id, p)
            who = student + (f" ({parent})" if parent else "")
            await _send(update,
                        f"⚠️ Ученик *{who}* уже есть в вашем списке.\n\n"
                        "Добавить ещё одного, с немного другим именем?",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=kb([
                            [("✅ Это он — открыть", "dup:open")],
                            [("✏️ Добавить с другим именем", "dup:rename")],
                            [("Отмена", "cancel")],
                        ]))
            return
        # запоминаем ученика в списке (если ещё нет)
        tutor = p.get("репетитор") or name_by_id(user.id) or ""
        if tutor and not students_module.find_student(tutor, student):
            try:
                students_module.add_student(tutor, student, parent)
            except Exception as e:  # noqa: BLE001
                log(f"Не смог сохранить ученика в список: {e}")
        if p.get("режим") == "ученики":
            p.pop("режим", None)
            p.update({"ученик": student, "родитель": parent,
                      "репетитор": tutor, "шаг": "н_цена"})
            set_pending(user.id, p)
            await _send(update, f"✅ {student} добавлен.\n\n"
                                "Какая цена занятия?",
                        reply_markup=kb([
                            [("1 300", "n:price:1300"), ("1 500", "n:price:1500")],
                            [("1 700", "n:price:1700"), ("2 000", "n:price:2000")],
                            [("Пропустить", "n:price:skip")]]))
            return
        if p.get("дата"):
            await ask_amount(update, context, p)
        else:
            await ask_date(update, context, p)
        return

    # --- сумма текстом
    if step == "сумма_текст":
        amt = parse_amount(text)
        if amt is None:
            await _send(update, "Не понял сумму. Напишите число, например 1700.")
            return
        p["сумма"] = str(amt)
        await ask_time(update, context, p)
        return

    # --- время текстом
    if step == "время_текст":
        t = parse_time(text)
        if t is None:
            await _send(update, 
                "Не понял время. Напишите в формате `ЧЧ:ММ`, например `17:30`.",
                parse_mode=ParseMode.MARKDOWN)
            return
        p["время"] = t
        await ask_first(update, context, p)
        return

    # --- комиссия
    if step == "комиссия":
        amt = parse_amount(text)
        if amt is None and text.strip() != "0":
            await _send(update, "Напишите число (можно 0).")
            return
        p["комиссия"] = str(amt or 0)
        await ask_how(update, context, p)
        return

    # --- комментарий
    if step == "комментарий":
        p["комментарий"] = text[:200]
        await show_confirm(update, context, p)
        return

    # --- ученик ещё не выбран (шаг кнопочный)
    if step == "ученик":
        await _send(update, 
            "Выберите ученика кнопкой выше или нажмите «➕ Добавить ученика».",
            reply_markup=kb([[("Записать занятие", "new")], [("Меню", "menu")]]))
        return

    await _send(update, 
        "На этом шаге нужно выбрать вариант кнопкой выше (или начать заново: /start).")


# --------------------------------------------------------------------------- #
# Отчёты
# --------------------------------------------------------------------------- #
LOGS_PER_PAGE = 10


async def show_logs(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    day_offset: int = 0, page: int = 0) -> None:
    """Журнал действий: кто, что и когда — постранично."""
    items = activity_module.entries(day_offset)
    label = activity_module.day_label(day_offset)
    if not items:
        text = (f"📜 <b>Журнал действий</b>\n{label}\n\n"
                "Записей нет.")
        rows = [[("📅 Вчера", f"logs:{day_offset + 1}:0")] if day_offset == 0
                else [("📅 Сегодня", "logs:0:0")],
                [("⬅️ Назад", "admin")]]
        await _say(update, text, parse_mode=ParseMode.HTML, reply_markup=kb(rows))
        return

    pages = (len(items) + LOGS_PER_PAGE - 1) // LOGS_PER_PAGE
    page = min(max(0, page), pages - 1)
    chunk = items[page * LOGS_PER_PAGE:(page + 1) * LOGS_PER_PAGE]

    lines = []
    for e in chunk:
        ts = str(e.get("ts", ""))[11:16]          # ЧЧ:ММ
        who = str(e.get("user", "")).strip()
        act = str(e.get("action", "")).strip()
        det = str(e.get("details", "")).strip()
        line = f"<b>{ts}</b> {html.escape(who)} — {html.escape(act)}"
        if det:
            line += f": {html.escape(det)}"
        lines.append(line)

    text = (f"📜 <b>Журнал действий</b> — {label}\n"
            f"Всего записей: {len(items)} · стр. {page + 1}/{pages}\n\n"
            + "\n".join(lines))

    nav: list[list[tuple[str, str]]] = []
    prev_btn = ("◀️ Раньше", f"logs:{day_offset}:{page - 1}")
    next_btn = ("Позже ▶️", f"logs:{day_offset}:{page + 1}")
    if pages > 1:
        if page > 0:
            nav.append([prev_btn, next_btn] if page < pages - 1 else [prev_btn])
        else:
            nav.append([next_btn])
    day_row = ([("📅 Вчера", f"logs:{day_offset + 1}:0")] if day_offset == 0
               else [("📅 Сегодня", "logs:0:0"),
                     ("📅 Раньше", f"logs:{day_offset + 1}:0")])
    nav.append(day_row)
    nav.append([("⬅️ Назад", "admin")])
    await _say(update, text, parse_mode=ParseMode.HTML, reply_markup=kb(nav))


async def show_students_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раздел «Мои ученики» — кнопки по ученикам."""
    user = update.effective_user
    tutor = name_by_id(user.id) or ""
    roster = students_module.students_of(tutor) if tutor else []
    rows: list[list[tuple[str, str]]] = []
    for i, s in enumerate(roster):
        label = s["ученик"] + (f" — {s['родитель']}" if s.get("родитель") else "")
        price = f" · {s['тариф']} ₽" if s.get("тариф") else ""
        rows.append([(label + price, f"student:{i}")])
    if roster:
        lines = []
        for s in roster:
            su = links_module.dialog_link(s.get("ссылка_ученика", "") or "")
            pu = links_module.dialog_link(s.get("ссылка_родителя", "") or "")
            nm = links_module.link_html(su, s["ученик"])
            par = s.get("родитель") or ""
            par_h = links_module.link_html(pu, par) if par else "—"
            price = f" · {s['тариф']} ₽" if s.get("тариф") else ""
            lines.append(f"• {nm} ({par_h}){price}")
        text = ("👥 <b>Мои ученики</b>\n\n" + "\n".join(lines)
                + "\n\nНажмите на ученика ниже, чтобы изменить данные.")
    else:
        text = "👥 <b>Мои ученики</b>\n\nПока пусто. Добавьте первого."
    rows.append([("➕ Добавить ученика", "add_student2"), ("⬅️ Назад", "nav:back")])
    await _say(update, text, parse_mode=ParseMode.HTML, reply_markup=kb(rows))


async def show_mine_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Текущая неделя: список занятий + кнопка «Изменить»."""
    user = update.effective_user
    tutor = name_by_id(user.id) or ""
    today = dt.date.today()
    week_start = today - dt.timedelta(days=today.weekday())
    week_end = week_start + dt.timedelta(days=_ws.week_span(tutor) - 1)
    try:
        all_lessons = checks_writer.read_lessons(with_checks=True)
    except Exception:  # noqa: BLE001
        all_lessons = checks_writer.read_lessons()
    try:
        flags = checks_writer.lesson_flags()
    except Exception:  # noqa: BLE001
        flags = {}
    lessons = [x for x in all_lessons
               if names_match(x["репетитор"], tutor)
               and week_start <= x["дата"] <= week_end]
    lessons.sort(key=lambda x: (x["дата"], _time_key(x["время"])))
    parts = []
    by_day: dict[dt.date, list[dict]] = {}
    for x in lessons:
        by_day.setdefault(x["дата"], []).append(x)
    for day in sorted(by_day):
        _t = dt.date.today()
        if day == _t:
            title = f"сегодня {day:%d.%m}"
        elif day == _t - dt.timedelta(days=1):
            title = f"вчера {day:%d.%m}"
        elif day == _t - dt.timedelta(days=2):
            title = f"позавчера {day:%d.%m}"
        else:
            title = f"{day:%d.%m}"
        parts.append(f"<b>{title}</b>")
        for x in by_day[day]:
            tm = f" {x['время']}" if x["время"] else ""
            stud = students_module.find_student(
            tutor, x["ученик"],
            parent=x.get("родитель") or "") or {}
            par = (stud.get("родитель") or x.get("родитель") or "").strip()
            su = links_module.dialog_link(stud.get("ссылка_ученика", "") or "")
            pu = links_module.dialog_link(stud.get("ссылка_родителя", "") or "")
            who = links_module.link_html(su, x["ученик"])
            if par:
                who += " (" + links_module.link_html(pu, par) + ")"
            st = flags.get(str(x.get("id") or ""), "")
            mark = "✅ " if st == "paid" else ("❌ " if st == "no" else "⚪️ ")
            parts.append(f"  {mark}{tm} {who} — {x['сумма']} ₽")
    total = sum(x["сумма"] for x in lessons)
    _span = _ws.week_span(tutor)
    _title = "Текущая неделя" if _span == 7 else "Текущие 2 недели"
    head = (f"<b>{_title}</b> "
            f"({week_start:%d.%m}–{week_end:%d.%m})\n\n")
    payout = _ws.payout(total, tutor)
    _tp = sum(1 for x in lessons if flags.get(str(x.get("id") or ""), "") == "paid")
    _tn = sum(1 for x in lessons if flags.get(str(x.get("id") or ""), "") == "no")
    _tu = len(lessons) - _tp - _tn
    _total_line = f"\n<b>Итого — {payout} ₽</b>"
    if not _ws.is_family(tutor):
        _total_line += f" ({_ws.payout_note(tutor)}, без учёта комиссии за ученика)"
    text = (head + "\n".join(parts)
            + f"\n\n✅ оплачено: {_tp} · ❌ отклонено: {_tn} · ⚪️ не проверено: {_tu}"
            + _total_line) if parts else (
        f"<b>{_title}</b> ({week_start:%d.%m}–{week_end:%d.%m})\n\n"
        "Записей пока нет.")
    rows = []
    if lessons:
        rows.append([("✏️ Изменить", "mine:edit")])
    rows.append([("⬅️ Назад", "nav:back")])
    await _say(update, text, parse_mode=ParseMode.HTML, reply_markup=kb(rows))


WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


async def show_past_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Предыдущая неделя/2 недели: ✅ подтверждённые, ❌ нет (по цвету суммы)."""
    user = update.effective_user
    tutor = name_by_id(user.id) or ""
    _span = _ws.week_span(tutor)
    _title = "Прошлая неделя" if _span == 7 else "Предыдущие 2 недели"
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday() + _span)
    sunday = monday + dt.timedelta(days=_span - 1)
    try:
        all_lessons = checks_writer.read_lessons(include_history=True)
    except Exception:  # noqa: BLE001
        all_lessons = checks_writer.read_lessons()
    lessons = [x for x in all_lessons
               if names_match(x["репетитор"], tutor)
               and monday <= x["дата"] <= sunday]
    lessons.sort(key=lambda x: (x["дата"], _time_key(x["время"])))
    try:
        flags = checks_writer.lesson_flags()
    except Exception:  # noqa: BLE001
        flags = {}
    parts = []
    rows = []
    paid_sum = no_sum = _unknown_sum = 0
    for x in lessons:
        lid = str(x.get("id") or "")
        st = flags.get(lid, "")
        mark = "✅" if st == "paid" else ("❌" if st == "no" else "⚪️")
        stud = students_module.find_student(
            tutor, x["ученик"],
            parent=x.get("родитель") or "") or {}
        par = (stud.get("родитель") or x.get("родитель") or "").strip()
        su = links_module.dialog_link(stud.get("ссылка_ученика", "") or "")
        pu = links_module.dialog_link(stud.get("ссылка_родителя", "") or "")
        who = links_module.link_html(su, x["ученик"])
        if par:
            who += " (" + links_module.link_html(pu, par) + ")"
        parts.append(f"{mark} {WEEKDAYS_RU[x['дата'].weekday()]} "
                     f"{x['дата']:%d.%m} {x['время']} {who} — "
                     f"{x['сумма']} ₽")
        if st == "paid":
            paid_sum += x["сумма"]
        elif st == "no":
            no_sum += x["сумма"]
        else:
            _unknown_sum += x["сумма"]
    if not parts:
        text = (f"📆 <b>{_title}</b> ({monday:%d.%m}–{sunday:%d.%m})\n\n"
                "Занятий нет.")
    else:
        _total = paid_sum + no_sum + _unknown_sum
        _payout = _ws.payout(_total, tutor)
        text = (f"📆 <b>{_title}</b> ({monday:%d.%m}–{sunday:%d.%m})\n\n"
                + "\n".join(parts)
                + f"\n\n✅ подтверждено: <b>{paid_sum} ₽</b>"
                + f"\n❌ не подтверждено: <b>{no_sum} ₽</b>"
                + f"\n\n<b>Итого — {_payout} ₽</b>"
              + ("" if _ws.is_family(tutor) else
                 f" ({_ws.payout_note(tutor)})"))
    rows.append([("⬅️ Назад", "nav:back")])
    await _say(update, text, parse_mode=ParseMode.HTML, reply_markup=kb(rows))


async def show_mine_records(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список записей кнопками: нажали запись -> выбрать действие."""
    user = update.effective_user
    tutor = name_by_id(user.id) or ""
    today = dt.date.today()
    week_start = today - dt.timedelta(days=today.weekday())
    week_end = week_start + dt.timedelta(days=_ws.week_span(tutor) - 1)
    lessons = [x for x in checks_writer.read_lessons()
               if x["репетитор"] == tutor
               and week_start <= x["дата"] <= week_end]
    lessons.sort(key=lambda x: (x["дата"], _time_key(x["время"])))
    rows = []
    lines: list[str] = []
    for x in lessons:
        lid = str(x.get("id") or "")
        stud = students_module.find_student(
            tutor, x["ученик"],
            parent=x.get("родитель") or "") or {}
        par = (stud.get("родитель") or "").strip()
        su = links_module.dialog_link(stud.get("ссылка_ученика", "") or "")
        pu = links_module.dialog_link(stud.get("ссылка_родителя", "") or "")
        nm = links_module.link_html(su, x["ученик"])
        par_h = (" (" + links_module.link_html(pu, par) + ")") if par else ""
        lines.append(f"• {x['дата']:%d.%m} {nm}{par_h} — {x['сумма']} ₽")
        label = (f"{x['дата']:%d.%m} {x['ученик']}"
                 + (f" ({par})" if par else "")
                 + f" — {x['сумма']} ₽")
        rows.append([(label[:40], f"mact:{lid}" if lid else "noop")])
    rows.append([("⬅️ Назад", "mine")])
    text = ("<b>Изменить запись</b>\n\n" + "\n".join(lines)
            + "\n\nНажмите на запись ниже:") if lines else "Записей нет."
    await _say(update, text, parse_mode=ParseMode.HTML, reply_markup=kb(rows))


async def mine_report(user_id: int) -> str:
    """Занятия репетитора за текущую неделю (из таблицы)."""
    tutor = name_by_id(user_id)
    if not tutor:
        return "Сначала выберите своё имя (/start)."
    _today = dt.date.today()
    start = _today - dt.timedelta(days=_today.weekday())
    _span = _ws.week_span(tutor)
    end = start + dt.timedelta(days=_span - 1)
    _title = "Текущая неделя" if _span == 7 else "Текущие 2 недели"
    lessons = [x for x in checks_writer.read_lessons()
               if x["репетитор"] == tutor and start <= x["дата"] <= end]
    if not lessons:
        return (f"{_title} ({start:%d.%m}–{end:%d.%m}): "
                "записей не найдено.")
    lessons.sort(key=lambda x: (x["дата"], _time_key(x["время"])))
    total = sum(x["сумма"] for x in lessons)
    lines = []
    for x in lessons:
        tm = f" {x['время']}" if x["время"] else ""
        comm = f" ({x['комментарий']})" if x["комментарий"] else ""
        lines.append(f"{x['дата']:%d.%m}{tm} — {x['ученик']} — {x['сумма']} ₽{comm}")
    _tail = f"\n\nИтого: *{_ws.payout(total, tutor)} ₽*"
    if not _ws.is_family(tutor):
        _tail += f" ({_ws.payout_note(tutor)}, без учёта комиссии)"
    return (f"*{_title}* ({start:%d.%m}–{end:%d.%m}) "
            f"— {display_name(user_id) or tutor}\n\n"
            + "\n".join(lines) + _tail)


async def notify_owner(context: ContextTypes.DEFAULT_TYPE, p: dict, row: int):
    cfg = load_config()
    chat_id = cfg.get("notification_chat_id")
    if not chat_id:
        return
    try:
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=f"🆕 Новая запись (строка {row})\n{p['репетитор']} — {p['ученик']} — "
                 f"{p['сумма']} ₽ ({p['дата']} {p['время']})")
    except Exception as e:  # noqa: BLE001
        log(f"Не смог уведомить владельца: {e}")


# --------------------------------------------------------------------------- #
# Админ-команды
# --------------------------------------------------------------------------- #
async def add_tutor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        log_action(update.effective_user.id, "выдал доступ",
                   " ".join(context.args))
    if role_of(update.effective_user.id) != OWNER:
        await update.message.reply_text("Только для организатора.")
        return
    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Использование: /add_tutor <telegram_id> <Имя Фамилия>")
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id должен состоять из цифр.")
        return
    name = " ".join(context.args[1:]).strip() or str(tid)
    state = load_state()
    state.setdefault("tutors", [])
    state["tutors"] = [t for t in state["tutors"] if int(t["id"]) != tid]
    state["tutors"].append({"id": tid, "name": name})
    save_state(state)
    await update.message.reply_text(f"✅ {name} теперь может заполнять занятия (id {tid}).")


async def list_tutors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if role_of(update.effective_user.id) != OWNER:
        await update.message.reply_text("Только для организатора.")
        return
    state = load_state()
    if not state.get("tutors"):
        await update.message.reply_text("Пока никто не подключён.")
        return
    lines = [f"{t['id']} — {t['name']}" + (" *(организатор)*" if t.get("role") == "owner" else "")
             for t in state["tutors"]]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def remove_tutor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        log_action(update.effective_user.id, "забрал доступ",
                   " ".join(context.args))
    if role_of(update.effective_user.id) != OWNER:
        await update.message.reply_text("Только для организатора.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /remove_tutor <telegram_id>")
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id должен состоять из цифр.")
        return
    state = load_state()
    before = len(state.get("tutors", []))
    state["tutors"] = [t for t in state.get("tutors", []) if int(t["id"]) != tid]
    save_state(state)
    await update.message.reply_text("Удалён." if len(state["tutors"]) < before else "Не найден.")


async def set_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if role_of(update.effective_user.id) != OWNER:
        await update.message.reply_text("Только для организатора.")
        return
    if not context.args or context.args[0] not in ("anyone", "list", "invite_code"):
        await update.message.reply_text("Использование: /access anyone|list|invite_code")
        return
    cfg = load_config()
    cfg["access"] = context.args[0]
    save_config(cfg)
    await update.message.reply_text(f"Режим доступа: {cfg['access']}")


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if role_of(update.effective_user.id) != OWNER:
        await update.message.reply_text("Только для организатора.")
        return
    await update.message.reply_text(await week_report(), parse_mode=ParseMode.MARKDOWN)


async def week_report() -> str:
    lessons = checks_writer.read_lessons()
    today = dt.date.today()
    start = today - dt.timedelta(days=today.weekday())  # понедельник
    per: dict[str, list[int]] = {}
    total = 0
    for x in lessons:
        if not (start <= x["дата"] <= today):
            continue
        per.setdefault(x["репетитор"], []).append(x["сумма"])
        total += x["сумма"]
    if not per:
        return "На этой неделе записей нет."
    lines_out = [f"*{k}*: {len(v)} зан. — {sum(v)} ₽"
                 for k, v in sorted(per.items(), key=lambda p: -sum(p[1]))]
    return (f"*Итоги недели* ({start:%d.%m} — {today:%d.%m})\n\n"
            + "\n".join(lines_out) + f"\n\nВсего: *{total} ₽*")
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    for t in state.get("tutors", []):
        if t.get("role") == "owner":
            continue
        try:
            await context.bot.send_message(
                int(t["id"]),
                "⏰ Привет! Не забудь(те) внести прошедшие занятия за неделю — бот запишет всё в таблицу.",
                reply_markup=kb([[("📝 Записать занятие", "new")]]),
            )
        except Exception as e:  # noqa: BLE001
            log(f"Напоминание {t.get('name')} не доставлено: {e}")


def _update_entry_time(entry_id: int, time_str: str) -> None:
    from sheets_util import get_values, update_values
    cfg = load_config()
    sh_id = cfg["sheets"]["history"]
    rows = get_values(sh_id, "'Расписание'!A1:I")
    for n, row in enumerate(rows, start=1):
        if n == 1 or not row:
            continue
        if str(row[0]).strip() == str(entry_id):
            update_values(sh_id, f"'Расписание'!F{n}", [[time_str]])
            return


async def send_lesson_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Через час после начала: полузаполненный чек с кнопками."""
    data = context.job.data or {}
    entry = data.get("entry") or {}
    date_s = data.get("date", "")
    if not entry:
        return
    key = data.get("key")
    if key:
        st = load_state()
        st.setdefault("reminded", {})[key] = True
        if len(st["reminded"]) > 300:
            st["reminded"] = dict(list(st["reminded"].items())[-200:])
        save_state(st)
    who = entry.get("ученик", "")
    if entry.get("родитель"):
        who += f" ({entry['родитель']})"
    _cfg0 = load_config()
    _chat0 = int(entry.get("telegram_id") or 0) or data.get("chat_id")
    _owner0 = int(_chat0) in [int(x) for x in _cfg0.get("owner_ids", [])]
    text = (f"⏰ Занятие прошло?\n\n"
            f"🧒 {who}\n"
            f"📅 {date_s}  ⏰ {entry.get('время', '')}\n"
            f"💰 {entry.get('цена', '')} ₽\n\n"
            + ("Подтвердите оплату или перенесите:"
               if _owner0 else "Отметьте, что занятие было, или перенесите:"))
    chat = int(entry.get("telegram_id") or 0) or data.get("chat_id")
    cfg = load_config()
    is_owner_chat = int(chat) in [int(x) for x in cfg.get("owner_ids", [])]
    if is_owner_chat:
        first_row = [("✅ Подтвердить оплату",
                      f"remind:ok:{entry['id']}:{date_s}")]
    else:
        first_row = [("✅ Занятие было",
                      f"remind:was:{entry['id']}:{date_s}")]
    try:
        await context.bot.send_message(
            chat_id=chat,
            text=text,
            reply_markup=kb([
                first_row,
                [("🔁 Перенос", f"remind:move:{entry['id']}:{date_s}"),
                 ("❌ Отмена", f"remind:no:{entry['id']}:{date_s}")],
            ]))
    except Exception as e:  # noqa: BLE001
        log(f"Напоминание не доставлено: {e}")


async def reschedule_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Разослать напоминания на ближайшие занятия (включая разовые)."""
    now = dt.datetime.now()
    jq = context.job_queue
    state = load_state()
    reminded = state.setdefault("reminded", {})
    for entry in schedule_module.all_entries():
        day = schedule_module.next_occurrence(entry)
        if not day:
            continue
        key = f"{entry['репетитор']}-{entry['id']}-{day:%Y%m%d}"
        if reminded.get(key):
            continue
        try:
            if jq.get_jobs_by_name(f"remind-{key}"):
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            hh, mm = map(int, str(entry.get("время", "")).split(":")[:2])
        except (ValueError, TypeError):
            continue
        if hasattr(schedule_module, "is_cancelled") and schedule_module.is_cancelled(
                entry.get("репетитор", ""), day, str(entry.get("время", ""))):
            continue
        fire_at = dt.datetime.combine(day, dt.time(hh, mm)) + dt.timedelta(hours=1)
        if fire_at <= now:
            _single = schedule_module.one_time_date(str(entry.get("дни", "")))
            if _single is not None or day == dt.date.today():
                # занятие уже прошло (сегодня или разовое) — напомним сейчас
                fire_at = now + dt.timedelta(minutes=1)
            else:
                fire_at = dt.datetime.combine(day + dt.timedelta(days=7),
                                              dt.time(hh, mm)) + dt.timedelta(hours=1)
                day = day + dt.timedelta(days=7)
        chat_id = _chat_id_by_tutor(entry["репетитор"])
        if not chat_id:
            continue
        jq.run_once(
            send_lesson_reminder, when=fire_at - now,
            data={"entry": entry, "date": day.strftime("%d.%m.%Y"),
                  "chat_id": chat_id, "key": key},
            name=f"remind-{key}")
        log(f"Напоминание запланировано: {entry.get('ученик')} "
            f"{day:%d.%m} {entry.get('время')} -> в {fire_at:%d.%m %H:%M}")


def _chat_id_by_tutor(tutor: str) -> int | None:
    for t in load_state().get("tutors", []):
        if norm_name(t.get("name", "")) == norm_name(tutor):
            return int(t["id"])
    return None


def _note_brief(note: str) -> str:
    """Краткий текст уведомления из файла заметки."""
    lines = [ln.strip() for ln in note.split("\n") if ln.strip()]
    if not lines:
        return ""
    head = lines[0]
    bullets = [ln for ln in lines[1:]
               if ln[:1].isdigit() or ln.startswith(("•", "-"))]
    if not bullets:
        return head
    return head + "\n\n" + "\n".join(
        b if len(b) <= 90 else b[:87] + "..." for b in bullets[:5])


async def notify_update(context_or_app) -> None:
    """Запомнить уведомление об обновлении, но НЕ рассылать сразу.

    Рассылка идёт, когда репетитор сам нажмёт любую кнопку или напишет —
    чтобы не отвлекать людей без повода.
    """
    import hashlib

    candidates = [sheets_util.DATA_DIR / "update_note.txt",
                  sheets_util.BASE_DIR / "update_note.txt"]
    note_path = next((p for p in candidates if p.exists()), None)
    if note_path is None:
        return
    try:
        note = note_path.read_text(encoding="utf-8").strip()
    except Exception as e:  # noqa: BLE001
        log(f"Не прочитал update_note.txt: {e}")
        return
    if not note:
        try:
            note_path.unlink()
        except OSError:
            pass
        return

    digest = hashlib.sha1(note.encode("utf-8")).hexdigest()[:16]
    marker = sheets_util.DATA_DIR / "update_note.sent"
    try:
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == digest:
            log("Уведомление об обновлении уже разослано ранее")
            try:
                note_path.unlink()
            except OSError:
                pass
            return
    except OSError:
        pass

    brief = _note_brief(note)
    if not brief:
        return
    st = load_state()
    # не повторять то же самое: если текст не изменился — не шлём
    prev = (st.get("last_note_text") or "").strip()
    if prev == brief.strip():
        log("Уведомление не изменилось — не отправляю повторно")
        try:
            note_path.unlink()
        except OSError:
            pass
        return
    st["last_note_text"] = brief.strip()
    st["pending_note"] = {"text": brief, "digest": digest, "sent": []}
    save_state(st)
    log("Уведомление об обновлении отложено до действия пользователя")
    try:
        note_path.unlink()
    except OSError:
        pass


async def _maybe_send_note(bot_obj, user_id) -> None:
    """Отправить отложенное уведомление этому пользователю (один раз)."""
    try:
        st = load_state()
        note = st.get("pending_note") or {}
        text = (note.get("text") or "").strip()
        if not text:
            return
        sent = [str(x) for x in (note.get("sent") or [])]
        uid = str(user_id)
        if uid in sent:
            return
        cfg = load_config()
        # уведомления об обновлении — только главному владельцу
        allowed: set[str] = set()
        for oid in (cfg.get("note_recipients")
                    or [cfg.get("owner_ids", [793935902])[0]]):
            try:
                allowed.add(str(int(oid)))
            except (ValueError, TypeError):
                continue
        if uid not in allowed:
            return
        await bot_obj.send_message(
            chat_id=int(user_id), text=text,
            reply_markup=kb([[("Отлично", "dismiss")]], home=False))
        sent.append(uid)
        note["sent"] = sent
        st["pending_note"] = note
        save_state(st)
        log(f"Уведомление об обновлении отправлено {user_id}")
        if allowed <= set(sent):
            st.pop("pending_note", None)
            save_state(st)
            try:
                (sheets_util.DATA_DIR / "update_note.sent").write_text(
                    note.get("digest", ""), encoding="utf-8")
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001
        log(f"Не смог отправить отложенное уведомление: {e}")


async def _ensure_accounts() -> None:
    """Зарегистрировать Максима и Вику, если их нет в state."""
    try:
        added = _ws.ensure_tutors(load_state, save_state)
        if added:
            log(f"Зарегистрированы аккаунты: {', '.join(added)}")
    except Exception as e:  # noqa: BLE001
        log(f"Не удалось зарегистрировать аккаунты: {e}")


async def _warm_cache(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Заранее прочитать таблицы в фоне — первый экран открывается сразу."""
    import asyncio as _aio

    def _load() -> None:
        try:
            checks_writer.ensure_checks_ids()
        except Exception as e:  # noqa: BLE001
            log(f"ID в «Чеках» не проставлены: {e}")
        try:
            checks_writer.read_lessons(include_history=True)
            checks_writer.lesson_flags()
            students_module.students_of("Данил Пудов")
        except Exception as e:  # noqa: BLE001
            log(f"Прогрев кэша не удался: {e}")

    try:
        await _aio.to_thread(_load)
        log("Кэш прогрет")
    except Exception as e:  # noqa: BLE001
        log(f"Прогрев кэша не удался: {e}")


async def refresh_cache_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обновить кэш в фоне — только если ботом недавно пользовались.

    Google даёт 60 чтений в минуту на проект. Постоянное обновление
    каждую минуту съедало квоту, и пользователи получали ошибку 429.
    """
    import asyncio as _aio
    import time as _t

    if _t.time() - _LAST_ACTIVITY.get("ts", 0) > 600:
        return          # 10 минут тишины — кэш не трогаем

    def _refresh() -> None:
        try:
            checks_writer.read_lessons(include_history=True)
            checks_writer.lesson_flags()
        except Exception as e:  # noqa: BLE001
            log(f"Фоновое обновление кэша не удалось: {e}")

    try:
        await _aio.to_thread(_refresh)
    except Exception as e:  # noqa: BLE001
        log(f"Фоновое обновление кэша не удалось: {e}")


async def check_suggestions_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в час: проверить таблицу предложений и напомнить о новых."""
    try:
        pend = suggestions_module.pending_suggestions()
    except Exception as e:  # noqa: BLE001
        log(f"Проверка предложений не удалась: {e}")
        return
    if not pend:
        return
    cfg = load_config()
    st = load_state()
    # уже отправленные строки не повторяем
    shown = set(str(x) for x in (st.get("suggestions_shown") or []))
    fresh = [x for x in pend if str(x["row"]) not in shown]
    if not fresh:
        return
    for oid in cfg.get("note_recipients") or cfg.get("owner_ids", []):
        for x in fresh[:10]:
            try:
                await context.bot.send_message(
                    chat_id=int(oid),
                    text=(f"💡 <b>Предложение</b> (строка {x['row']})\n"
                          f"От: {x['репетитор']}\n\n"
                          f"{str(x['текст'])[:400]}"),
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb([
                        [("✅ Принять", f"sug:ok:{x['row']}")],
                        [("❌ Отклонить", f"sug:no:{x['row']}")]]))
            except Exception as e:  # noqa: BLE001
                log(f"Не смог отправить предложение {oid}: {e}")
    shown |= {str(x["row"]) for x in fresh}
    st["suggestions_shown"] = sorted(shown)[-200:]
    save_state(st)
    log(f"Проверка предложений: новых {len(fresh)}")


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("new", "Записать занятие"),
        BotCommand("mine", "Текущие занятия"),
        BotCommand("faq", "FAQ"),
        BotCommand("help", "FAQ"),
    ])
    await _ensure_accounts()
    try:
        await notify_update(app)
    except Exception as e:  # noqa: BLE001
        log(f"Ошибка уведомления об обновлении: {e}")
    try:
        await check_suggestions_job(SimpleNamespace(bot=app.bot))
    except Exception as e:  # noqa: BLE001
        log(f"Ошибка отчёта по предложениям: {e}")


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if role_of(update.effective_user.id) is None:
        await update.message.reply_text("Нет доступа.")
        return
    await begin_new(update, context, edit=False)


async def cmd_mine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if role_of(update.effective_user.id) is None:
        await update.message.reply_text("Нет доступа.")
        return
    await update.message.reply_text(await mine_report(update.effective_user.id),
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML,
                                    reply_markup=kb([[("Начать", "new")], [("Меню", "menu")]]))


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #
def print_token_rejected():
    """Понятное сообщение, если Telegram отклонил токен (InvalidToken)."""
    print("=" * 60)
    print("Telegram отклонил токен бота (InvalidToken).")
    print("Проверьте токен у @BotFather: /mybots -> ваш бот -> API Token.")
    print("Вставьте его в config.json -> \"telegram_token\"")
    print("или запустите:  python bot.py --set-token=123456:AAA...")
    print("=" * 60)


_LOCK_HANDLE = None


_IGNORED_ERRORS = (
    "Message is not modified",
    "Message to edit not found",
    "no text in the message",
    "Query is too old",
    "query id is invalid",
    "Conflict: terminated by other getUpdates",
    "Message can't be deleted",
    "message is too long",
)


async def _on_error(update, context) -> None:
    """Не засорять лог простынями: краткая запись о настоящих ошибках."""
    err = getattr(context, "error", None)
    text = str(err or "")
    for known in _IGNORED_ERRORS:
        if known in text:
            # Telegram-служебные ситуации — молча
            return
    if err is None:
        return
    try:
        user_id = getattr(update, "effective_user", None)
        who = user_id.id if user_id else "?"
    except Exception:  # noqa: BLE001
        who = "?"
    log(f"ОШИБКА у {who}: {type(err).__name__}: {text[:300]}")


def _single_instance() -> None:
    """Не дать запустить второй экземпляр бота.

    На Linux/Docker используем flock: блокировка снимается автоматически,
    когда процесс завершается. Это важно в контейнере, где бот = PID 1
    и проверка os.kill(1, 0) всегда «видит живой процесс».
    """
    global _LOCK_HANDLE
    import os
    from pathlib import Path as _P
    lock = str(_P(str(sheets_util.CONFIG_PATH)).parent / "bot.lock")
    try:
        fh = open(lock, "a+", encoding="utf-8")
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _LOCK_HANDLE = fh          # держим открытым, пока жив процесс
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
            return
        except ImportError:
            fh.close()                 # Windows — ниже старая проверка
        except OSError:
            fh.close()
            print("Бот уже запущен. Второй экземпляр не нужен.")
            sys.exit(0)
    except Exception:  # noqa: BLE001
        pass
    # --- запасной путь (Windows) ---
    try:
        if os.path.exists(lock):
            with open(lock, "r", encoding="utf-8") as f:
                pid = int((f.read() or "0").strip() or 0)
            if pid and pid != os.getpid():
                try:
                    os.kill(pid, 0)
                    print("Бот уже запущен (pid %d). Второй экземпляр не нужен." % pid)
                    sys.exit(0)
                except OSError:
                    pass
        with open(lock, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:  # noqa: BLE001
        pass


def main():
    _single_instance()
    # токен можно задать аргументом: python bot.py --set-token=123:AAA
    set_token = None
    for a in sys.argv[1:]:
        if a.startswith("--set-token="):
            set_token = a.split("=", 1)[1].strip()

    cfg = load_config()
    token = (set_token
             or os.environ.get("BOT_TOKEN")
             or os.environ.get("TELEGRAM_BOT_TOKEN")
             or cfg.get("telegram_token") or "").strip()
    if not token:
        print("=" * 60)
        print("В config.json не заполнен \"telegram_token\".")
        print("Получите токен у @BotFather (/newbot) и вставьте его")
        print("в config.json -> \"telegram_token\": \"123456:AAA...\"")
        print("или запустите:  python bot.py --set-token=123456:AAA...")
        print("=" * 60)
        sys.exit(2)  # отдельный код: start_bot.bat не будет перезапускать

    if set_token and set_token != cfg.get("telegram_token"):
        cfg["telegram_token"] = set_token
        save_config(cfg)
        print("Токен сохранён в config.json")

    try:
        app = Application.builder().token(token).post_init(post_init).build()
    except InvalidToken:
        print_token_rejected()
        sys.exit(3)  # отдельный код: неверный токен, автоперезапуск не поможет

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("mine", cmd_mine))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add_tutor", add_tutor))
    app.add_handler(CommandHandler("list_tutors", list_tutors))
    app.add_handler(CommandHandler("remove_tutor", remove_tutor))
    app.add_handler(CommandHandler("access", set_access))
    app.add_handler(CommandHandler("report", report))

    app.add_error_handler(_on_error)
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.Document.ALL
        | filters.VOICE | filters.AUDIO, handle_photo))

    rem = cfg.get("reminder") or {}
    if rem.get("enabled", True):
        if app.job_queue is None:
            # APScheduler (python-telegram-bot[job-queue]) не установлен —
            # напоминания недоступны, но бот должен работать
            log("Напоминание выключено: не установлен APScheduler "
                '(pip install "python-telegram-bot[job-queue]").')
        else:
            try:
                wd = int(rem.get("weekday", 0))
                hh, mm = map(int, str(rem.get("time", "19:00")).split(":"))
                reminder_time = dt.time(hour=hh, minute=mm)
            except (TypeError, ValueError):
                log("Напоминание выключено: некорректные weekday/time в config.json.")
            else:
                app.job_queue.run_daily(
                    send_reminder, time=reminder_time,
                    days=tuple(range(7)) if wd < 0 else (wd,))

    if app.job_queue is not None:
        app.job_queue.run_once(reschedule_reminders, when=5)
        app.job_queue.run_repeating(reschedule_reminders, interval=3600, first=60)
        app.job_queue.run_daily(reschedule_reminders, time=dt.time(hour=0, minute=10))
        app.job_queue.run_repeating(check_suggestions_job, interval=3600, first=90)
        app.job_queue.run_once(_warm_cache, when=1)
        # кэш живёт 60 с — обновляем каждые 45 с, чтобы он не остывал
        app.job_queue.run_repeating(refresh_cache_job, interval=180, first=120)

    import hashlib as _hl
    try:
        _h = _hl.sha1()
        for _name in sorted(("bot.py", "sheets_util.py", "checks_writer.py",
                             "students.py", "schedule.py", "notes.py",
                             "workspaces.py", "activity.py")):
            _f = sheets_util.BASE_DIR / _name
            if _f.exists():
                _h.update(_f.read_bytes())
        _ver = _h.hexdigest()[:8]
    except Exception:  # noqa: BLE001
        _ver = "?"
    log(f"Бот запущен. Сборка {_ver}. Режим доступа: {cfg.get('access')}")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except InvalidToken:
        print_token_rejected()
        sys.exit(3)


if __name__ == "__main__":
    main()
