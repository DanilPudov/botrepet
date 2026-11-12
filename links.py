"""
Ссылки на мессенджеры: нормализация ввода, ссылка «в диалог», HTML для Telegram.

Репетитор присылает что угодно: «@ivan», «vk.com/ivan», «8 999 123-45-67»,
«https://wa.me/79991234567», «viber://chat?number=...». Модуль приводит это
к нормальной ссылке (normalize_link), превращает ссылку на страницу в ссылку,
открывающую диалог (dialog_link), и отдаёт безопасный HTML (link_html).

Публичный API:
  normalize_link(raw) -> str | None   "" = ссылку убрать, None = не распознано
  dialog_link(url)    -> str          ссылка, открывающая чат
  link_html(url, label) -> str        <a href=...>label</a> или просто label
  detect_messenger(url) -> str        «Telegram» / «WhatsApp» / … / «Другое»
  MESSENGERS                          список популярных в России мессенджеров
"""
from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlsplit

__all__ = [
    "MESSENGERS",
    "detect_messenger",
    "dialog_link",
    "link_html",
    "normalize_link",
]

# Популярные в России мессенджеры: название + по каким доменам узнаём.
MESSENGERS: list[dict] = [
    {"name": "Telegram", "домены": ["t.me", "telegram.me"]},
    {"name": "WhatsApp", "домены": ["wa.me", "whatsapp.com"]},
    {"name": "ВКонтакте", "домены": ["vk.com", "vk.me"]},
    {"name": "Viber", "домены": ["viber.com", "viber://"]},
    {"name": "Max", "домены": ["max.ru"]},
]

# Что пользователь пишет, когда ссылку надо стереть.
EMPTY_INPUTS = {"", "-", "—", "–", "нет", "убрать"}

# Допустимые символы телефона: цифры, «+», пробелы, скобки, дефисы.
_PHONE_CHARS = re.compile(r"^[0-9\s()+\-]+$")

# Ссылка с явной схемой, но без «//»: mailto:, tel: и т.п.
_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*:[^/\\]", re.IGNORECASE)

# Схемы мессенджеров, которые в MESSENGERS записаны доменом.
_SCHEME_NAMES = {
    "tg": "Telegram",
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "wa": "WhatsApp",
    "viber": "Viber",
    "max": "Max",
    "vk": "ВКонтакте",
}

# Хосты ВКонтакте, которые умеют превращаться в vk.me.
_VK_HOSTS = {"vk.com", "www.vk.com", "m.vk.com", "vk.me", "www.vk.me"}

# Профиль ВК: «id123» или короткое имя. Не wall-1_2 / video-3 / club7.
_VK_PROFILE = re.compile(r"^(id\d+|[A-Za-z0-9._]+)$")

# Сколько цифр считаем телефоном.
_PHONE_MIN, _PHONE_MAX = 10, 13


# --------------------------------------------------------------------------- #
# Нормализация пользовательского ввода
# --------------------------------------------------------------------------- #
def _as_phone(s: str) -> str | None:
    """«8 999 123-45-67» -> «+79991234567». None, если это не телефон."""
    if not s or not _PHONE_CHARS.match(s):
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    if not (_PHONE_MIN <= len(digits) <= _PHONE_MAX):
        return None
    if s.startswith("+"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("8"):
        return "+7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return "+" + digits
    return "+" + digits


def _looks_like_domain(s: str) -> bool:
    """«vk.com/ivan», «wa.me/79991234567» — домен без схемы."""
    if not s or " " in s or "\n" in s or "\t" in s:
        return False
    host = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in host:                      # это e-mail, а не ссылка
        return False
    if "." not in host or host.startswith(".") or host.endswith("."):
        return False
    tld = host.rsplit(".", 1)[1]
    return len(tld) >= 2 and tld.replace("-", "").isalnum()


def normalize_link(raw: str) -> str | None:
    """Приводит ввод пользователя к ссылке.

    ""/«-»/«—»/«нет»/«убрать» -> "" (ссылку убрать)
    есть «://»                -> как есть
    «@name»                   -> https://t.me/name (пустое имя -> None)
    «vk.com/ivan»             -> https://vk.com/ivan
    «8 999 123-45-67»         -> https://t.me/+79991234567
    иначе                     -> None (не распознано)
    """
    s = (raw or "").strip()
    if s.lower() in EMPTY_INPUTS:
        return ""
    if "://" in s:                       # tg://, viber://, whatsapp://, max:// …
        return s
    if _SCHEME.match(s) and " " not in s:  # mailto:, tel: и прочие схемы
        return s
    if s.startswith("@"):
        name = s[1:].replace(" ", "").strip()
        return f"https://t.me/{name}" if name else None
    phone = _as_phone(s)
    if phone is not None:
        return f"https://t.me/{phone}"
    if _looks_like_domain(s):
        return "https://" + s
    return None


# --------------------------------------------------------------------------- #
# Ссылка на страницу -> ссылка на диалог
# --------------------------------------------------------------------------- #
def _whatsapp_number(parts) -> str:
    """Достаёт номер из whatsapp.com-ссылки. Пустая строка, если номера нет."""
    for key in ("phone", "number"):
        for value in parse_qs(parts.query).get(key, []):
            digits = "".join(ch for ch in value if ch.isdigit())
            if _PHONE_MIN <= len(digits) <= _PHONE_MAX:
                return digits
    for seg in parts.path.split("/"):
        seg = seg.strip()
        if seg.isdigit() and _PHONE_MIN <= len(seg) <= _PHONE_MAX:
            return seg
    return ""


def dialog_link(url: str) -> str:
    """Ссылка на СТРАНИЦУ -> ссылка, открывающая ДИАЛОГ.

    vk.com/ivan   -> vk.me/ivan
    vk.com/id123  -> vk.me/id123
    wa.me/…       -> без изменений
    whatsapp.com/send?phone=7999… -> https://wa.me/7999…
    t.me, max.ru, viber://, всё остальное -> без изменений
    """
    s = (url or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low.startswith("viber://"):
        return s
    # разбираем и «vk.com/ivan», и «https://vk.com/ivan»
    candidate = s if "://" in low else "https://" + s
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        scheme = (parts.scheme or "").lower()
    except ValueError:                   # битый URL — отдаём как есть
        return s
    if scheme not in ("http", "https", ""):
        return s
    tail = ""
    if parts.query:
        tail += "?" + parts.query
    if parts.fragment:
        tail += "#" + parts.fragment

    if host in _VK_HOSTS and host.endswith("vk.com"):
        segs = [seg for seg in parts.path.split("/") if seg]
        # vk.me будем работать только для профиля: «id123» или «username».
        # wall-1_2, video-3 — это контент, а не человек.
        if len(segs) == 1 and _VK_PROFILE.match(segs[0]):
            return f"https://vk.me/{segs[0]}{tail}"
        return s

    if host == "wa.me" or host.endswith(".wa.me"):
        return s

    if host == "whatsapp.com" or host.endswith(".whatsapp.com"):
        number = _whatsapp_number(parts)
        return f"https://wa.me/{number}" if number else s

    return s


# --------------------------------------------------------------------------- #
# HTML для Telegram
# --------------------------------------------------------------------------- #
def link_html(url: str, label: str) -> str:
    """<a href="url">label</a> для http(s)-ссылки, иначе — только текст.

    Пустой label -> пустая строка (показывать нечего).
    """
    text = "" if label is None else str(label)
    if not text:
        return ""
    u = (url or "").strip()
    low = u.lower()
    escaped = html.escape(text)
    if low.startswith("http://") or low.startswith("https://"):
        return f'<a href="{html.escape(u, quote=True)}">{escaped}</a>'
    return escaped


# --------------------------------------------------------------------------- #
# Определение мессенджера по домену
# --------------------------------------------------------------------------- #
def detect_messenger(url: str) -> str:
    """«Telegram» / «WhatsApp» / «ВКонтакте» / «Viber» / «Max» / «Другое»."""
    low = (url or "").strip().lower()
    if not low:
        return "Другое"
    if low.startswith("@"):
        return "Telegram"
    # своя схема мессенджера: tg://, whatsapp://, viber://, max://
    head = low.split("://", 1)
    if len(head) == 2 and head[0] not in ("http", "https"):
        scheme = head[0]
        if scheme in _SCHEME_NAMES:
            return _SCHEME_NAMES[scheme]
        for messenger in MESSENGERS:
            for domain in messenger["домены"]:
                if domain.endswith("://") and domain[:-3] == scheme:
                    return messenger["name"]
    # домен ищем в хосте, а не где попало: «vk.com» в пути — не ВК
    candidate = low if "://" in low else "https://" + low
    try:
        host = (urlsplit(candidate).hostname or "").lower()
    except ValueError:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return "Другое"
    for messenger in MESSENGERS:
        for domain in messenger["домены"]:
            if domain.endswith("://"):
                continue
            if host == domain or host.endswith("." + domain):
                return messenger["name"]
    return "Другое"
