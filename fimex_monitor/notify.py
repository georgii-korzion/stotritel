"""Тексты сообщений и отправка в Telegram (раздел 7 ТЗ). Бот только пишет — без фреймворка."""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Iterable, Protocol
from zoneinfo import ZoneInfo

import httpx

from .rules import RULE_DROP, RULE_GAP, Alert, Evaluation, Gap

log = logging.getLogger(__name__)

TG_API = "https://api.telegram.org"
TG_LIMIT = 4096
PAUSE_BETWEEN_MESSAGES_S = 3.0
MAX_429_RETRIES = 3

REASONS = {RULE_DROP: "падение цены", RULE_GAP: "разрыв между регионами"}
MINUS = "−"


# --- форматирование ------------------------------------------------------------

def e(text: object) -> str:
    return html.escape(str(text), quote=False)


def money(value: Decimal) -> str:
    """$1 580; копейки — только если они не нулевые: $14,50."""
    q = value.quantize(Decimal("0.01"), ROUND_HALF_UP)
    sign = MINUS if q < 0 else ""
    q = abs(q)
    whole = int(q)
    cents = int((q - whole) * 100)
    s = f"{whole:,}".replace(",", " ")
    if cents:
        s += f",{cents:02d}"
    return f"{sign}${s}"


def pct(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.1"), ROUND_HALF_UP)).replace(".", ",")


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def ago(then: datetime, now: datetime) -> str:
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} ч {rest} мин назад" if rest else f"{hours} ч назад"
    days = hours // 24
    return f"{days} {plural(days, 'день', 'дня', 'дней')} назад"


def delta(prev: Decimal, now: Decimal) -> str:
    d = now - prev
    if d == 0:
        return "без изменений"
    sign = MINUS if d < 0 else "+"
    return f"{sign}{money(abs(d))} · {sign}{pct(abs(d) / prev * 100)} %"


def gap_line(gap: Gap) -> str:
    n = gap.nearest
    return f"{e(n.region)} {money(n.price)} — дешевле на {money(gap.gap_abs)} · {pct(gap.gap_pct)} %"


def format_alert(alert: Alert, now: datetime) -> str:
    pos, best = alert.position, alert.position.best
    lines = [
        f"<b>🔻 {e(pos.title)} · {e(pos.region)}</b>",
        f"{e(pos.brand)} · {e(best.article)} · {e(best.eta_raw)} · {best.qty} шт",
        "",
    ]
    if alert.prev is None:
        lines.append("Было: новая позиция")
        lines.append(f"Стало: {money(pos.price)}")
    else:
        lines.append(f"Было: {money(alert.prev.price)} ({ago(alert.prev.seen_at, now)})")
        lines.append(f"Стало: {money(pos.price)} ({delta(alert.prev.price, pos.price)})")
    s = alert.second
    if s is None:
        lines.append("2-е предложение: других предложений нет")
    else:
        lines.append(f"2-е предложение: {money(s.price_usd)} — {e(s.region)} · {e(s.eta_raw)} · {s.qty} шт")
    if alert.gap is not None:
        lines.append(f"Другой флаг: {gap_line(alert.gap)}")
    lines.append("")
    lines.append("Причина: " + " · ".join(REASONS[r] for r in alert.rules))
    return "\n".join(lines)


def _short_alert_line(alert: Alert) -> str:
    pos = alert.position
    parts = [f"• {e(pos.title)} · {e(pos.region)} — "]
    if alert.prev is None:
        parts.append(f"{money(pos.price)}, новая позиция")
    else:
        parts.append(f"{money(alert.prev.price)} → {money(pos.price)} ({delta(alert.prev.price, pos.price)})")
    if RULE_GAP in alert.rules and alert.gap is not None:
        parts.append(f"; дешевле {e(alert.gap.nearest.region)} на {money(alert.gap.gap_abs)} · {pct(alert.gap.gap_pct)} %")
    return "".join(parts)


def format_overflow(alerts: list[Alert]) -> str:
    n = len(alerts)
    head = f"📋 Ещё {n} {plural(n, 'алерт', 'алерта', 'алертов')} за этот цикл — кратко:"
    return "\n".join([head, ""] + [_short_alert_line(a) for a in alerts])


def format_start(ev: Evaluation, top: int = 10) -> str:
    brands = len({p.key[0] for p in ev.positions.values()})
    n_pos, n_gap = len(ev.positions), len(ev.gaps)
    text = (
        f"✅ Монитор запущен: {brands} {plural(brands, 'бренд', 'бренда', 'брендов')}, "
        f"{n_pos} {plural(n_pos, 'позиция', 'позиции', 'позиций')}, "
        f"разрыв между регионами сейчас у {n_gap} {plural(n_gap, 'позиции', 'позиций', 'позиций')}"
    )
    gaps = sorted(ev.gaps.values(), key=lambda g: (-g.gap_abs, g.position.title))[:top]
    if not gaps:
        return text
    lines = [text, "", f"Самые большие разрывы (топ-{len(gaps)}):"]
    for g in gaps:
        p = g.position
        lines.append(
            f"• {e(p.title)} · {e(p.region)} {money(p.price)} — дешевле {e(g.nearest.region)} "
            f"{money(g.nearest.price)} на {money(g.gap_abs)} · {pct(g.gap_pct)} %"
        )
    return "\n".join(lines)


def local_time(dt: datetime, tz: ZoneInfo, now: datetime | None = None) -> str:
    """ЧЧ:ММ, а если не сегодня — ДД.ММ ЧЧ:ММ."""
    loc = dt.astimezone(tz)
    if now is not None and loc.date() != now.astimezone(tz).date():
        return loc.strftime("%d.%m %H:%M")
    return loc.strftime("%H:%M")


def msg_fetch_down(since: str, last_error: str) -> str:
    return f"⚠️ Fimex не отвечает: нет данных с {e(since)}.\nПоследняя ошибка: {e(last_error)}"


def msg_recovered() -> str:
    return "✅ Данные от Fimex снова идут."


def msg_auth_rejected(status: int | None) -> str:
    code = f" (HTTP {status})" if status else ""
    return (
        f"⛔ Fimex отклонил токен{code}. Пробую раз в 2 часа, пока не заработает.\n"
        "Нужен новый FIMEX_JWT в переменных сервиса (или включить FIMEX_SEND_APP_ACCESS=1)."
    )


def msg_format_changed(problems: list[str]) -> str:
    body = "\n".join(f"• {e(p)}" for p in problems)
    return (
        "⚠️ Похоже, формат прайса Fimex изменился — алерты не считаются, пока он не восстановится.\n"
        f"{body}\nФайл сохранён в data/raw/ для разбора."
    )


def msg_token_expiring(expires: datetime, now: datetime, tz: ZoneInfo) -> str:
    days = max(0, (expires - now).days)
    when = expires.astimezone(tz).strftime("%d.%m.%Y")
    if expires <= now:
        return f"⏳ Токен Fimex истёк {when}. Нужен новый FIMEX_JWT."
    return f"⏳ Токен Fimex истекает {when} (через {days} {plural(days, 'день', 'дня', 'дней')}). Нужен новый FIMEX_JWT."


def msg_daily(cycles: int, errors: int, alerts: int) -> str:
    return (
        f"💓 Жив: за сутки {cycles} {plural(cycles, 'цикл', 'цикла', 'циклов')}, "
        f"{errors} {plural(errors, 'ошибка', 'ошибки', 'ошибок')}, "
        f"{alerts} {plural(alerts, 'алерт', 'алерта', 'алертов')}"
    )


def split_text(text: str, limit: int = TG_LIMIT - 96) -> list[str]:
    """Разбить длинный текст по строкам, чтобы каждая часть влезла в сообщение Telegram."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # одна строка длиннее лимита — режем как есть
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    return parts


_TAG_RE = re.compile(r"</?[a-z]+>")


def plain(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text))


# --- отправка ------------------------------------------------------------------

class Notifier(Protocol):
    def send(self, text: str) -> bool: ...


class ConsoleNotifier:
    """Печатает сообщения вместо отправки — для прогона на файле."""

    def __init__(self, out: Callable[[str], None] = print):
        self.out = out
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        self.out("─" * 60 + "\n" + plain(text) + "\n")
        return True


class Telegram:
    def __init__(self, token: str, chat_id: str, thread_id: int | None = None, *,
                 client: httpx.Client | None = None, stop: threading.Event | None = None,
                 pause_s: float = PAUSE_BETWEEN_MESSAGES_S,
                 monotonic: Callable[[], float] = time.monotonic):
        self.token = token
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.client = client or httpx.Client(timeout=httpx.Timeout(30, connect=10))
        self.stop = stop or threading.Event()
        self.pause_s = pause_s
        self.monotonic = monotonic
        self._last_sent: float | None = None

    def _url(self, method: str) -> str:
        return f"{TG_API}/bot{self.token}/{method}"

    def _wait(self, seconds: float) -> bool:
        """Подождать; False — если пришёл сигнал остановки."""
        if seconds > 0:
            return not self.stop.wait(seconds)
        return not self.stop.is_set()

    def send(self, text: str) -> bool:
        return all(self._send_one(part) for part in split_text(text))

    def _send_one(self, text: str) -> bool:
        payload: dict = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if self.thread_id is not None:
            payload["message_thread_id"] = self.thread_id
        for _attempt in range(MAX_429_RETRIES + 1):
            if self._last_sent is not None:
                if not self._wait(self.pause_s - (self.monotonic() - self._last_sent)):
                    return False
            elif self.stop.is_set():
                return False
            try:
                r = self.client.post(self._url("sendMessage"), json=payload)
            except httpx.HTTPError as exc:
                log.warning("Telegram: сетевая ошибка %s", type(exc).__name__)
                return False
            finally:
                self._last_sent = self.monotonic()
            data = _json(r)
            if r.status_code == 200 and data.get("ok"):
                return True
            if r.status_code == 429:
                retry = int((data.get("parameters") or {}).get("retry_after") or 5)
                log.warning("Telegram: 429, жду %s с", retry)
                if not self._wait(retry):
                    return False
                continue
            log.warning("Telegram: HTTP %s %s", r.status_code, data.get("description", ""))
            return False
        return False

    def get_updates(self) -> list[dict]:
        r = self.client.get(self._url("getUpdates"), params={"timeout": 0})
        data = _json(r)
        if not data.get("ok"):
            raise RuntimeError(f"getUpdates: HTTP {r.status_code} {data.get('description', '')}")
        return data.get("result", [])


def _json(r: httpx.Response) -> dict:
    try:
        data = r.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def chats_from_updates(updates: Iterable[dict]) -> list[dict]:
    """Чаты, в которых бот видел сообщения или куда его добавили."""
    chats: dict[int, dict] = {}
    for u in updates:
        for key in ("message", "edited_message", "channel_post", "my_chat_member", "chat_member"):
            chat = (u.get(key) or {}).get("chat")
            if chat and "id" in chat:
                chats[chat["id"]] = chat
    return list(chats.values())
