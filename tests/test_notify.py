import json
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest

from fimex_monitor.notify import (
    Telegram,
    ago,
    chats_from_updates,
    format_alert,
    format_overflow,
    format_start,
    money,
    msg_daily,
    msg_token_expiring,
    pct,
    plain,
    plural,
    split_text,
)
from fimex_monitor.parse import Offer
from fimex_monitor.rules import PrevPosition, Thresholds, evaluate, position_key

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def offer(title, region, price, qty, article="ART", eta="08 Oct", brand="Apple"):
    return Offer(brand, title, article, eta, date(2026, 10, 8), region, Decimal(price), qty)


def spec_example_alert():
    t = "iPhone 17 Pro Max 1Tb Blue"
    offers = [offer(t, "🇯🇵 JP", 1494, 19, "MFYH4 J/A"), offer(t, "🇦🇺 AU", 1805, 2, "MFYX4 X/A")]
    jp, au = (position_key(o) for o in offers)
    prev = {jp: PrevPosition(Decimal(1580), NOW - timedelta(minutes=30), False),
            au: PrevPosition(Decimal(1805), NOW - timedelta(minutes=30), False)}
    (a,) = evaluate(offers, prev, {}, NOW, Thresholds(), False).alerts
    return a


def test_alert_text_matches_spec_example():
    text = format_alert(spec_example_alert(), NOW)
    assert text == (
        "<b>🔻 iPhone 17 Pro Max 1Tb Blue · 🇯🇵 JP</b>\n"
        "Apple · MFYH4 J/A · 08 Oct · 19 шт\n"
        "\n"
        "Было: $1 580 (30 мин назад)\n"
        "Стало: $1 494 (−$86 · −5,4 %)\n"
        "2-е предложение: $1 805 — 🇦🇺 AU · 08 Oct · 2 шт\n"
        "Другой флаг: 🇦🇺 AU $1 805 — дешевле на $311 · 17,2 %\n"
        "\n"
        "Причина: падение цены · разрыв между регионами"
    )


def test_alert_new_position_and_no_second_offer():
    offers = [offer("Pixel <10> & Pro", "🇺🇸 US", 900, 3), offer("Pixel <10> & Pro", "🇯🇵 JP", 1000, 1)]
    us, jp = (position_key(o) for o in offers)
    (a,) = evaluate(offers, {jp: PrevPosition(Decimal(1000), NOW, False)}, {}, NOW, Thresholds(), False).alerts
    text = format_alert(a, NOW)
    assert "Pixel &lt;10&gt; &amp; Pro" in text  # названия экранируются
    assert "Было: новая позиция\nСтало: $900\n" in text
    assert "Причина: разрыв между регионами" in text

    single = [offer("Solo", "🇺🇸 US", 900, 3)]
    key = position_key(single[0])
    (b,) = evaluate(single, {key: PrevPosition(Decimal(1000), NOW, False)}, {}, NOW, Thresholds(), False).alerts
    text = format_alert(b, NOW)
    assert "2-е предложение: других предложений нет" in text and "Другой флаг" not in text


def test_overflow_and_start_messages():
    a = spec_example_alert()
    text = format_overflow([a, a])
    assert text.startswith("📋 Ещё 2 алерта за этот цикл — кратко:")
    assert "• iPhone 17 Pro Max 1Tb Blue · 🇯🇵 JP — $1 580 → $1 494 (−$86 · −5,4 %); дешевле 🇦🇺 AU на $311 · 17,2 %" in text

    t = "iPhone 17 Pro Max 1Tb Blue"
    ev = evaluate([offer(t, "🇯🇵 JP", 1494, 19), offer(t, "🇦🇺 AU", 1805, 2), offer("X", "🎧", 10, 1, brand="Sony")],
                  {}, {}, NOW, Thresholds(), True)
    start = format_start(ev)
    assert start.startswith("✅ Монитор запущен: 2 бренда, 3 позиции, разрыв между регионами сейчас у 1 позиции")
    assert "• iPhone 17 Pro Max 1Tb Blue · 🇯🇵 JP $1 494 — дешевле 🇦🇺 AU $1 805 на $311 · 17,2 %" in start


@pytest.mark.parametrize("value, text", [
    (Decimal(1580), "$1 580"), (Decimal(14), "$14"), (Decimal("14.50"), "$14,50"),
    (Decimal(10677), "$10 677"), (Decimal(1234567), "$1 234 567"), (Decimal(-86), "−$86"),
])
def test_money(value, text):
    assert money(value) == text


def test_pct_plural_ago():
    assert pct(Decimal("5.44")) == "5,4" and pct(Decimal("17.23")) == "17,2" and pct(Decimal(5)) == "5,0"
    assert [plural(n, "позиция", "позиции", "позиций") for n in (1, 2, 5, 11, 21, 22, 112)] == [
        "позиция", "позиции", "позиций", "позиций", "позиция", "позиции", "позиций"]
    assert ago(NOW - timedelta(minutes=30), NOW) == "30 мин назад"
    assert ago(NOW - timedelta(minutes=90), NOW) == "1 ч 30 мин назад"
    assert ago(NOW - timedelta(hours=2), NOW) == "2 ч назад"
    assert ago(NOW, NOW) == "только что"


def test_service_messages():
    assert msg_daily(48, 1, 3) == "💓 Жив: за сутки 48 циклов, 1 ошибка, 3 алерта"
    tz = ZoneInfo("Asia/Dubai")
    exp = datetime(2027, 9, 23, 10, 0, tzinfo=timezone.utc)
    assert msg_token_expiring(exp, exp - timedelta(days=14), tz) == \
        "⏳ Токен Fimex истекает 23.09.2027 (через 14 дней). Нужен новый FIMEX_JWT."


def test_split_text():
    text = "\n".join(f"line {i:04d} " + "x" * 50 for i in range(200))
    parts = split_text(text, limit=1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "\n".join(parts) == text
    assert split_text("short") == ["short"]


def test_plain():
    assert plain("<b>A &amp; B</b>") == "A & B"


# --- Telegram --------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make_tg(handler, thread_id=None, stop=None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    stop = stop or threading.Event()
    tg = Telegram("123:SECRET", "-1001234567890", thread_id, client=client, stop=stop, pause_s=0)
    return tg


def test_telegram_send_payload():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {}})

    assert make_tg(handler, thread_id=7).send("<b>hi</b>")
    path, body = seen[0]
    assert path == "/bot123:SECRET/sendMessage"
    assert body["chat_id"] == "-1001234567890" and body["parse_mode"] == "HTML"
    assert body["message_thread_id"] == 7 and body["text"] == "<b>hi</b>"


def test_telegram_429_waits_retry_after(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 2}})
        return httpx.Response(200, json={"ok": True})

    stop = threading.Event()
    waits = []
    monkeypatch.setattr(stop, "wait", lambda s=None: waits.append(s) or False)
    assert make_tg(handler, stop=stop).send("x")
    assert len(calls) == 2 and 2 in waits


def test_telegram_group_upgraded_to_supergroup():
    chats = []

    def handler(request):
        chat = json.loads(request.content)["chat_id"]
        chats.append(chat)
        if chat == "-1001234567890":
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: group chat was upgraded to a supergroup chat",
                                             "parameters": {"migrate_to_chat_id": -1009876543210}})
        return httpx.Response(200, json={"ok": True})

    tg = make_tg(handler)
    assert tg.send("a") and tg.send("b")
    assert chats == ["-1001234567890", "-1009876543210", "-1009876543210"]


def test_telegram_error_returns_false():
    assert not make_tg(lambda r: httpx.Response(400, json={"ok": False, "description": "chat not found"})).send("x")

    def boom(request):
        raise httpx.ConnectError("no route")

    assert not make_tg(boom).send("x")


def test_telegram_pause_between_messages(monkeypatch):
    stop = threading.Event()
    waits = []
    monkeypatch.setattr(stop, "wait", lambda s=None: waits.append(s) or False)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})))
    clock = FakeClock()
    tg = Telegram("t", "c", client=client, stop=stop, pause_s=3, monotonic=clock)
    tg.send("a")
    clock.t = 1.0
    tg.send("b")
    assert waits == [2.0]


def test_chats_from_updates():
    updates = [
        {"message": {"chat": {"id": -100123, "type": "supergroup", "title": "Блоты"}}},
        {"my_chat_member": {"chat": {"id": -555, "type": "group", "title": "Old"}}},
        {"message": {"chat": {"id": -100123, "type": "supergroup", "title": "Блоты"}}},
    ]
    assert [c["id"] for c in chats_from_updates(updates)] == [-100123, -555]
