import copy
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from fimex_monitor import fetch as fx
from fimex_monitor import main as m
from fimex_monitor.config import Config
from fimex_monitor.store import Cycle, Store
from tools import make_stage3_sample as stage3

from .synthetic import DEMO, SMALL, write_pricelist

T0 = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)  # 10:00 в Дубае


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class FakeNotifier:
    def __init__(self):
        self.sent: list[str] = []
        self.fail = False

    def send(self, text):
        if self.fail:
            return False
        self.sent.append(text)
        return True


class FakeFetcher:
    def __init__(self):
        self.queue = []
        self.calls = []

    def push(self, item):
        self.queue.append(item)

    def __call__(self):
        self.calls.append(1)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return fx.FetchResult(item, 200, 0.1)


@pytest.fixture
def env(tmp_path):
    class Env:
        clock = Clock()
        notifier = FakeNotifier()
        fetcher = FakeFetcher()
        cfg = Config(data_dir=tmp_path / "data", fimex_jwt="J", daily_report_hour=None)
        store = Store(tmp_path / "data" / "state.db")

        def monitor(self, **kw):
            return m.Monitor(self.cfg, self.store, self.notifier, fetcher=self.fetcher, clock=self.clock, **kw)

        def book(self, sheets, name="b.xlsx"):
            return write_pricelist(tmp_path / name, sheets).read_bytes()

        def cycle(self, item):
            self.fetcher.push(item)
            return self.monitor().live_cycle()

    e = Env()
    yield e
    e.store.close()


def with_price(sheets, title, region, price):
    out = copy.deepcopy(sheets)
    for blocks in out.values():
        for i, (t, rows) in enumerate(blocks):
            if t == title:
                blocks[i] = (t, [(a, b, c, price if c == region else d, q) for a, b, c, d, q in rows])
    return out


def cycles(store, source="live"):
    return store.recent_cycles(source)


# --- расписание --------------------------------------------------------------------------

def mk(status, minutes_ago, kind=None, retry=None):
    return Cycle(0, "live", T0 - timedelta(minutes=minutes_ago), status, kind, None, retry, None, None)


@pytest.mark.parametrize("history, expected_min", [
    ([], None),
    ([mk("ok", 0)], 30),
    ([mk("error", 0, "timeout"), mk("ok", 30)], 30),  # одна ошибка — по расписанию
    ([mk("error", 0, "timeout"), mk("error", 30, "timeout"), mk("ok", 60)], 60),
    ([mk("error", 0, "server")] * 3 + [mk("ok", 200)], 120),
    ([mk("error", 0, "server")] * 6, 120),  # потолок
    ([mk("ok", 0), mk("error", 30, "server"), mk("error", 60, "server")], 30),  # после успеха — снова 30
    ([mk("error", 0, "auth"), mk("ok", 30)], 120),  # 401/403 — раз в 2 часа
    ([mk("error", 0, "rate_limited", retry=3 * 3600)], 180),  # Retry-After длиннее расписания
    ([mk("error", 0, "rate_limited", retry=60)], 30),
])
def test_next_attempt_at(history, expected_min):
    nxt = m.next_attempt_at(history, 30)
    assert (None if nxt is None else (nxt - T0) / timedelta(minutes=1)) == expected_min


def test_poll_interval_floor_cannot_be_bypassed():
    from fimex_monitor.config import load_config
    assert load_config({"POLL_INTERVAL_MIN": "1"}).poll_interval_min == 15
    assert load_config({"POLL_INTERVAL_MIN": "45"}).poll_interval_min == 45


# --- цикл ----------------------------------------------------------------------------------

def test_first_run_then_drop(env):
    assert env.cycle(env.book(SMALL))
    (start,) = env.notifier.sent
    assert start.startswith("✅ Монитор запущен: 5 брендов, 17 позиций, разрыв между регионами сейчас у 2 позиций")
    assert len(env.store.load_positions()) == 17

    env.clock.advance(minutes=30)
    assert env.cycle(env.book(with_price(SMALL, "iPhone 15 128Gb Black", "🇺🇸 US", 540)))
    (alert,) = env.notifier.sent[1:]
    assert alert.startswith("<b>🔻 iPhone 15 128Gb Black · 🇺🇸 US</b>")
    assert "Было: $572 (30 мин назад)\nСтало: $540 (−$32 · −5,6 %)" in alert
    assert "2-е предложение: $592 — 🇦🇺 AE" not in alert and "2-е предложение: $592 — 🇦🇪 AE · 08 Oct · 30 шт" in alert
    pos = env.store.load_positions()[("apple", "iphone 15 128gb black", "🇺🇸 US")]
    assert pos.price == Decimal(540) and pos.seen_at == env.clock()


def test_same_file_gives_no_alerts_but_refreshes_seen_at(env):
    data = env.book(SMALL)
    env.cycle(data)
    env.clock.advance(minutes=30)
    env.cycle(data)
    assert len(env.notifier.sent) == 1
    assert all(p.seen_at == env.clock() for p in env.store.load_positions().values())


def test_fetch_down_message_once_and_recovery(env):
    env.cycle(env.book(SMALL))
    for _ in range(4):
        env.clock.advance(minutes=30)
        assert not env.cycle(fx.FetchError(fx.TIMEOUT, "таймаут"))
    down = [t for t in env.notifier.sent if t.startswith("⚠️ Fimex не отвечает")]
    assert down == ["⚠️ Fimex не отвечает: нет данных с 10:00.\nПоследняя ошибка: таймаут"]
    env.clock.advance(minutes=120)
    env.cycle(env.book(SMALL))
    assert env.notifier.sent[-1] == "✅ Данные от Fimex снова идут."
    assert sum(t.startswith("✅ Данные") for t in env.notifier.sent) == 1


def test_error_does_not_touch_state(env):
    env.cycle(env.book(SMALL))
    before = env.store.load_positions()
    env.clock.advance(minutes=30)
    env.cycle(fx.FetchError(fx.NOT_XLSX, "ответ не xlsx"))
    assert env.store.load_positions() == before
    assert cycles(env.store)[0].status == "error" and cycles(env.store)[0].error_kind == fx.NOT_XLSX


def test_auth_error_message_once_and_rare_retries(env):
    env.cycle(env.book(SMALL))
    env.clock.advance(minutes=30)
    env.cycle(fx.FetchError(fx.AUTH, "Fimex отклонил токен: HTTP 401", 401))
    env.clock.advance(minutes=120)
    env.cycle(fx.FetchError(fx.AUTH, "Fimex отклонил токен: HTTP 401", 401))
    auth = [t for t in env.notifier.sent if t.startswith("⛔")]
    assert len(auth) == 1 and "HTTP 401" in auth[0]
    assert m.next_attempt_at(cycles(env.store), 30) == env.clock() + timedelta(minutes=120)
    assert not any(t.startswith("⚠️ Fimex не отвечает") for t in env.notifier.sent)


def test_retry_after_is_respected(env):
    env.cycle(fx.FetchError(fx.RATE_LIMITED, "429", 429, retry_after_s=5400))
    assert m.next_attempt_at(cycles(env.store), 30) == env.clock() + timedelta(minutes=90)


def test_format_change_message_once(env):
    env.cycle(env.book(SMALL))
    broken = {"Apple": [("raw", ("X", "08 Oct", "🇺🇸 US", "n/a", 1))] * 3 + SMALL["Apple"]}
    for _ in range(2):
        env.clock.advance(minutes=30)
        assert not env.cycle(env.book(broken, "broken.xlsx"))
    fmt = [t for t in env.notifier.sent if t.startswith("⚠️ Похоже, формат")]
    assert len(fmt) == 1
    assert cycles(env.store)[0].error_kind == m.FORMAT
    assert len(env.store.load_positions()) == 17


def test_too_few_offers_is_format_change(env):
    env.cycle(env.book(SMALL))
    env.clock.advance(minutes=30)
    assert not env.cycle(env.book({"Meta": SMALL["Meta"]}, "few.xlsx"))
    assert "меньше половины" in env.notifier.sent[-1]


def test_broken_xlsx_is_cycle_error(env):
    env.cycle(b"PK\x03\x04broken")
    assert cycles(env.store)[0].error_kind == m.BAD_XLSX


def test_telegram_failure_alert_not_lost(env):
    env.cycle(env.book(SMALL))
    cheaper = env.book(with_price(SMALL, "iPhone 15 128Gb Black", "🇺🇸 US", 540), "cheaper.xlsx")
    env.clock.advance(minutes=30)
    env.notifier.fail = True
    env.cycle(cheaper)
    pos = env.store.load_positions()[("apple", "iphone 15 128gb black", "🇺🇸 US")]
    assert pos.price == Decimal(572)  # состояние позиции не сдвинулось
    env.clock.advance(minutes=30)
    env.notifier.fail = False
    env.cycle(cheaper)
    alerts = [t for t in env.notifier.sent if t.startswith("<b>🔻")]
    assert len(alerts) == 1 and "Было: $572" in alerts[0]
    env.clock.advance(minutes=30)
    env.cycle(cheaper)  # повторно не пишем
    assert len([t for t in env.notifier.sent if t.startswith("<b>🔻")]) == 1


def test_cooldown_after_flicker(env):
    base = env.book(SMALL)
    cheaper = env.book(with_price(SMALL, "iPhone 15 128Gb Black", "🇺🇸 US", 540), "cheaper.xlsx")
    for data in (base, cheaper, base, cheaper):
        env.cycle(data)
        env.clock.advance(minutes=30)
    assert len([t for t in env.notifier.sent if t.startswith("<b>🔻")]) == 1


def test_cap_sends_summary(env):
    env.cfg = Config(**{**env.cfg.__dict__, "max_alerts_per_cycle": 2})
    env.cycle(env.book(SMALL))
    sheets = SMALL
    for title, region, price in [("iPhone 15 128Gb Blue", "🇮🇳 IN", 500), ("DualSense Edge", "🎮", 150),
                                 ("Major V Black", "🎧", 90), ("Ray-Ban Meta Wayfarer Matte Black", "👓", 250)]:
        sheets = with_price(sheets, title, region, price)
    env.clock.advance(minutes=30)
    env.cycle(env.book(sheets, "many.xlsx"))
    alerts = [t for t in env.notifier.sent if t.startswith("<b>🔻")]
    assert len(alerts) == 2 and "iPhone 15 128Gb Blue" in alerts[0]  # самый большой спад — $97
    assert env.notifier.sent[-1].startswith("📋 Ещё 2 алерта")
    assert env.store.stats_since(T0)[2] == 4


def test_raw_files_kept_for_a_day(env):
    data = env.book(SMALL)
    env.cycle(data)
    env.clock.advance(hours=12)
    env.cycle(data)
    env.clock.advance(hours=13)
    env.cycle(data)
    names = sorted(p.name for p in (env.cfg.data_dir / "raw").glob("*.xlsx"))
    assert names == ["fimex-pricelist-20261008T180000Z.xlsx", "fimex-pricelist-20261009T070000Z.xlsx"]


def test_token_expiry_warning_once(env, monkeypatch):
    exp = T0 + timedelta(days=10)
    monkeypatch.setattr(fx, "jwt_expiry", lambda token: exp)
    env.cycle(env.book(SMALL))
    env.clock.advance(minutes=30)
    env.cycle(env.book(SMALL))
    warn = [t for t in env.notifier.sent if t.startswith("⏳")]
    assert warn == ["⏳ Токен Fimex истекает 18.10.2026 (через 10 дней). Нужен новый FIMEX_JWT."]


def test_no_token_warning_when_far(env, monkeypatch):
    monkeypatch.setattr(fx, "jwt_expiry", lambda token: T0 + timedelta(days=15))
    env.cycle(env.book(SMALL))
    assert not any(t.startswith("⏳") for t in env.notifier.sent)


def test_internal_error_does_not_kill_cycle_loop(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(m, "evaluate", boom)
    assert not env.cycle(env.book(SMALL))
    assert cycles(env.store)[0].error_kind == m.INTERNAL


# --- run_forever: рестарт и расписание ---------------------------------------------------

def run_loop(env, *, max_sleeps):
    sleeps = []
    stop = threading.Event()

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= max_sleeps:
            stop.set()
        env.clock.advance(seconds=seconds)

    env.monitor(stop=stop, sleep=sleep).run_forever()
    return sleeps


def test_restart_does_not_fetch_early(env):
    env.cycle(env.book(SMALL))
    calls = len(env.fetcher.calls)
    env.clock.advance(minutes=5)  # рестарт через 5 минут после запроса
    sleeps = run_loop(env, max_sleeps=1)
    assert len(env.fetcher.calls) == calls
    assert sleeps == [25 * 60]
    starts = [t for t in env.notifier.sent if t.startswith("✅ Монитор запущен")]
    assert len(starts) == 1


def test_loop_cadence_and_backoff(env):
    data = env.book(SMALL)
    for item in [data, fx.FetchError(fx.SERVER, "503", 503), fx.FetchError(fx.SERVER, "503", 503),
                 fx.FetchError(fx.SERVER, "503", 503), data, data]:
        env.fetcher.push(item)
    times = []
    original = env.fetcher.__call__

    class Recording:
        def __call__(self_inner):
            times.append(env.clock())
            return original()

    env.fetcher = Recording()
    run_loop(env, max_sleeps=6)
    gaps = [(b - a) / timedelta(minutes=1) for a, b in pairwise(times)]
    assert gaps == [30, 30, 60, 120, 30]


def test_interrupted_cycle_counts_as_error(env):
    env.store.start_cycle(env.clock(), "live")  # процесс упал посреди запроса
    env.clock.advance(minutes=1)
    run_loop(env, max_sleeps=1)
    assert env.fetcher.calls == []  # после оборванной попытки тоже ждём интервал
    assert cycles(env.store)[0].error_kind == "interrupted"


def test_daily_report(env):
    env.cfg = Config(**{**env.cfg.__dict__, "daily_report_hour": 10})
    mon = env.monitor()
    env.clock.t = datetime(2026, 10, 8, 5, 50, tzinfo=timezone.utc)  # 09:50 в Дубае
    assert mon.next_daily_report(env.clock()) == datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
    env.clock.t = datetime(2026, 10, 8, 6, 5, tzinfo=timezone.utc)
    assert mon.next_daily_report(env.clock()) == env.clock()
    env.cycle(env.book(SMALL))
    mon.send_daily_report(env.clock())
    assert env.notifier.sent[-1] == "💓 Жив: за сутки 1 цикл, 0 ошибок, 0 алертов"
    assert mon.next_daily_report(env.clock()) == datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc)
    env.clock.t = datetime(2026, 10, 8, 8, 0, tzinfo=timezone.utc)  # пропустили час — до завтра
    assert mon.next_daily_report(env.clock()) == datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc)


def test_outbox_retries_service_messages(env):
    env.notifier.fail = True
    env.cycle(env.book(SMALL))  # стартовое сообщение не ушло
    env.notifier.fail = False
    env.clock.advance(minutes=30)
    env.cycle(env.book(SMALL))
    assert env.notifier.sent[0].startswith("✅ Монитор запущен")


# --- этап 3 на синтетике ------------------------------------------------------------------

def test_stage3_on_synthetic(tmp_path, env):
    src = write_pricelist(tmp_path / "fimex-pricelist-20261007T205720Z.xlsx", DEMO)
    dst = tmp_path / "stage3-fimex-pricelist-20261007T205720Z.xlsx"
    assert stage3.main([str(src), str(dst)]) == 0

    env.cycle(src.read_bytes())
    env.clock.advance(minutes=30)
    env.cycle(dst.read_bytes())
    alerts = [t for t in env.notifier.sent if t.startswith("<b>🔻")]
    assert len(alerts) == 3
    assert not any("iPhone 15 128Gb Blue" in t for t in alerts)  # −3 %


# --- команды ------------------------------------------------------------------------------

def test_once_file_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    src = write_pricelist(tmp_path / "fimex-pricelist-20261007T205720Z.xlsx", SMALL)
    assert m.main(["once", "--file", str(src), "--dry-run"]) == 0
    assert not (tmp_path / "d" / "state.db").exists()
    assert "Монитор запущен" in capsys.readouterr().out

    assert m.main(["once", "--file", str(src)]) == 0  # сохраняет базу, печатает в консоль
    db = tmp_path / "d" / "state.db"
    size = sqlite3.connect(db).execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    changed = write_pricelist(tmp_path / "x-20261007T205720Z.xlsx", with_price(SMALL, "iPhone 15 128Gb Black", "🇺🇸 US", 540))
    capsys.readouterr()
    assert m.main(["once", "--file", str(changed), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "🔻 iPhone 15 128Gb Black · 🇺🇸 US" in out
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == size
    assert con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
    assert con.execute("SELECT last_price FROM positions WHERE title='iPhone 15 128Gb Black' AND region='🇺🇸 US'").fetchone() == ("572",)


def test_dry_run_requires_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert m.main(["once", "--dry-run"]) == 2


def test_once_live_refuses_too_early(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k, v in {"DATA_DIR": str(tmp_path / "d"), "FIMEX_JWT": "J", "TG_BOT_TOKEN": "T", "TG_CHAT_ID": "-1"}.items():
        monkeypatch.setenv(k, v)
    store = Store(tmp_path / "d" / "state.db")
    store.start_cycle(datetime.now(timezone.utc) - timedelta(minutes=10), "live")
    store.close()
    monkeypatch.setattr(fx, "fetch_pricelist", lambda *a, **k: pytest.fail("запроса к Fimex быть не должно"))
    assert m.main(["once"]) == 3


def test_single_instance_lock(tmp_path):
    with m.InstanceLock(tmp_path):
        with pytest.raises(SystemExit):
            with m.InstanceLock(tmp_path):
                pass


def test_secrets_are_redacted_in_logs(capsys):
    redactor = m.setup_logging()
    redactor.secrets = ["SUPERSECRET"]
    log = logging.getLogger("fimex_monitor.test")
    log.warning("url https://api.telegram.org/botSUPERSECRET/sendMessage")
    try:
        raise ValueError("token SUPERSECRET leaked")
    except ValueError:
        log.exception("ошибка")
    out = capsys.readouterr().out
    assert "SUPERSECRET" not in out and "bot***/sendMessage" in out and "token *** leaked" in out


def test_store_readonly_does_not_create_db(tmp_path):
    s = Store(tmp_path / "none" / "state.db", readonly=True)
    assert s.load_positions() == {}
    s.close()
    assert not (tmp_path / "none").exists()


def test_docker_and_ignore_files_exclude_secrets():
    root = Path(__file__).resolve().parent.parent
    for name in (".gitignore", ".dockerignore", ".railwayignore"):
        lines = (root / name).read_text().split()
        assert any(x in lines for x in (".env",)), name
        assert any(x.lstrip("/").startswith("samples") for x in lines), name
        assert any(x.lstrip("/").startswith("data") for x in lines), name
    dockerfile = (root / "Dockerfile").read_text()
    assert "USER" not in dockerfile and "PYTHONUNBUFFERED=1" in dockerfile
    assert "COPY . " not in dockerfile
