"""Цикл, расписание и команды.

Порядок цикла (раздел 8 ТЗ): скачать → проверить → разобрать → посчитать алерты →
отправить → записать новое состояние.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import logging
import signal
import sys
import threading
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from . import fetch as fx
from .config import (
    AUTH_RETRY_MIN,
    FETCH_DOWN_AFTER_ERRORS,
    MAX_BACKOFF_MIN,
    RAW_KEEP_HOURS,
    TOKEN_WARN_DAYS,
    Config,
    ConfigError,
    load_config,
)
from .notify import (
    ConsoleNotifier,
    Notifier,
    Telegram,
    chats_from_updates,
    format_alert,
    format_overflow,
    format_start,
    local_time,
    msg_auth_rejected,
    msg_daily,
    msg_fetch_down,
    msg_format_changed,
    msg_recovered,
    msg_token_expiring,
)
from .parse import (
    WorkbookError,
    check_format,
    export_time_from_filename,
    parse_workbook,
)
from .rules import Evaluation, apply_filters, evaluate, next_state, split_by_cap
from .store import Cycle, Store

log = logging.getLogger("fimex_monitor")

BAD_XLSX = "bad_xlsx"  # тело начинается с PK, но книга не открывается
FORMAT = "format"  # книга открылась, но формат, похоже, изменился
INTERNAL = "internal"  # ошибка в самом боте

OUTBOX_MAX_AGE = timedelta(hours=24)

META_INCIDENT_FETCH = "incident_fetch"
META_INCIDENT_AUTH = "incident_auth"
META_INCIDENT_FORMAT = "incident_format"
META_TOKEN_WARNED = "token_warned_exp"
META_DAILY_REPORT = "daily_report_date"
INCIDENTS = (META_INCIDENT_FETCH, META_INCIDENT_AUTH, META_INCIDENT_FORMAT)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- расписание ------------------------------------------------------------------

def consecutive_errors(cycles: list[Cycle], exclude: tuple[str, ...] = ()) -> list[Cycle]:
    """Ошибочные циклы подряд, от последнего назад до первого успешного."""
    out = []
    for c in cycles:
        if c.status == "ok":
            break
        if c.status == "error" and c.error_kind not in exclude:
            out.append(c)
        elif c.error_kind in exclude:
            break
    return out


def next_attempt_at(cycles: list[Cycle], poll_min: int) -> datetime | None:
    """Когда можно делать следующий запрос к Fimex. None — можно сейчас (попыток ещё не было).

    cycles — живые циклы, от новых к старым. Интервал после ошибок удваивается
    (30 → 60 → 120, потолок 120) и возвращается к обычному после первого успеха;
    после 401/403 — раз в 2 часа; Retry-After — не меньше него.
    """
    if not cycles:
        return None
    last = cycles[0]
    errors = 0
    for c in cycles:
        if c.status == "ok":
            break
        errors += 1
    interval = poll_min
    if errors > 1:
        interval = min(poll_min * 2 ** (errors - 1), max(MAX_BACKOFF_MIN, poll_min))
    if last.status != "ok" and last.error_kind == fx.AUTH:
        interval = max(interval, AUTH_RETRY_MIN)
    nxt = last.attempt_at + timedelta(minutes=interval)
    if last.retry_after_s:
        nxt = max(nxt, last.attempt_at + timedelta(seconds=last.retry_after_s))
    return nxt


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- монитор -----------------------------------------------------------------------

class Monitor:
    def __init__(self, cfg: Config, store: Store, notifier: Notifier, *,
                 fetcher: Callable[[], fx.FetchResult] | None = None,
                 clock: Callable[[], datetime] = utcnow,
                 stop: threading.Event | None = None,
                 sleep: Callable[[float], None] | None = None):
        self.cfg = cfg
        self.store = store
        self.notifier = notifier
        self.fetcher = fetcher or (lambda: fx.fetch_pricelist(
            cfg.fimex_jwt, cfg.fimex_app_access, cfg.fimex_send_app_access))
        self.clock = clock
        self.stop = stop or threading.Event()
        self.sleep = sleep or (lambda s: self.stop.wait(s))
        self.raw_dir = cfg.data_dir / "raw"

    # --- боевой цикл -------------------------------------------------------------

    def run_forever(self) -> None:
        if n := self.store.mark_interrupted():
            log.info("Найдено оборванных циклов: %s — считаю их ошибочными", n)
        self.check_token(self.clock())
        self.flush_outbox()
        announced: datetime | None = None
        while not self.stop.is_set():
            now = self.clock()
            report_at = self.next_daily_report(now)
            if report_at is not None and report_at <= now:
                self.send_daily_report(now)
                continue
            fetch_at = next_attempt_at(self.store.recent_cycles("live"), self.cfg.poll_interval_min) or now
            if fetch_at <= now:
                self.live_cycle()
                continue
            if announced != fetch_at:
                mins = int((fetch_at - now).total_seconds() // 60)
                log.info("Следующий запрос к Fimex в %s (через %s мин)",
                         local_time(fetch_at, self.cfg.tz, now), mins)
                announced = fetch_at
            wake = min(fetch_at, report_at) if report_at else fetch_at
            self.sleep(max(1.0, (wake - now).total_seconds()))
        log.info("Остановка по сигналу")

    def live_cycle(self) -> bool:
        """Один живой цикл. True — успешный."""
        now = self.clock()
        self.check_token(now)
        cycle_id = self.store.start_cycle(now, "live")
        try:
            log.info("Цикл #%s: запрос к Fimex", cycle_id)
            try:
                res = self.fetcher()
            except fx.FetchError as exc:
                self.fail(cycle_id, exc.kind, str(exc), http_status=exc.status, retry_after_s=exc.retry_after_s)
                return False
            log.info("Цикл #%s: получено %s КБ за %.1f с", cycle_id, len(res.content) // 1024, res.elapsed_s)
            self.save_raw(res.content, now)
            return self.process(res.content, now, cycle_id=cycle_id) is not None
        except Exception as exc:  # ошибка в самом боте — цикл ошибочный, процесс живёт дальше
            log.exception("Цикл #%s: внутренняя ошибка", cycle_id)
            if self.store.cycle_status(cycle_id) == "started":
                self.fail(cycle_id, INTERNAL, f"внутренняя ошибка бота: {type(exc).__name__}")
            return False

    def process(self, content: bytes, fetched_at: datetime, *, cycle_id: int | None,
                dry_run: bool = False) -> Evaluation | None:
        """Разобрать выгрузку, посчитать и отправить алерты, записать состояние.

        dry_run — ничего не отправлять (кроме печати) и не сохранять.
        """
        cfg = self.cfg
        now = self.clock()
        digest = sha256(content)
        export_date = fetched_at.astimezone(cfg.tz).date()

        try:
            parsed = parse_workbook(content, export_date)
        except WorkbookError as exc:
            if dry_run:
                log.error("Файл не разобран: %s", exc)
                return None
            self.fail(cycle_id, BAD_XLSX, str(exc), sha256=digest)
            return None

        last_ok = self.store.last_success()
        problems = check_format(parsed, last_ok.offers if last_ok else None)
        if problems:
            if dry_run:
                log.error("Формат файла, похоже, изменился: %s", "; ".join(problems))
                return None
            self.fail(cycle_id, FORMAT, "; ".join(problems), sha256=digest, offers=len(parsed.offers),
                      problems=problems)
            return None
        if last_ok and last_ok.sha256 == digest:
            log.info("Прайс не изменился с прошлой успешной выгрузки (sha256 %s…)", digest[:12])

        offers = apply_filters(parsed.offers, export_date, cfg.eta_max_days, cfg.min_price_usd)
        prev = self.store.load_positions()
        alerted = self.store.alerted_since(now - cfg.thresholds.cooldown)
        first_run = last_ok is None
        ev = evaluate(offers, prev, alerted, now, cfg.thresholds, first_run)
        log.info(
            "Разобрано: предложений %s (после фильтров %s), строк не по формату %s, ETA не разобран %s; "
            "позиций %s, разрывов %s; алертов %s, подавлено повтором %s%s",
            len(parsed.offers), len(offers), len(parsed.bad_rows), parsed.eta_unparsed,
            len(ev.positions), len(ev.gaps), len(ev.alerts), len(ev.suppressed),
            " — первый запуск, только база" if first_run else "",
        )

        if dry_run:
            if first_run:
                self.notifier.send(format_start(ev))
            individual, overflow = split_by_cap(ev.alerts, cfg.max_alerts_per_cycle)
            for a in individual:
                self.notifier.send(format_alert(a, now))
            if overflow:
                self.notifier.send(format_overflow(overflow))
            return ev

        self.recovered()
        unsent = self.send_alerts(ev, cycle_id, now)
        updates, clear_gap = next_state(prev, ev, unsent, now)
        self.store.save_cycle_result(
            cycle_id, self.clock(), updates, clear_gap, sha256=digest, offers=len(parsed.offers),
            positions=len(ev.positions), alerts=len(ev.alerts) - len(unsent),
            note=f"не ушло алертов: {len(unsent)}" if unsent else None,
        )
        if unsent:
            log.warning("Не ушло в Telegram алертов: %s — повторю в следующем цикле", len(unsent))
        if first_run:
            self.store.enqueue("start", format_start(ev), now)
        self.flush_outbox()
        return ev

    def send_alerts(self, ev: Evaluation, cycle_id: int | None, now: datetime) -> set:
        """Отправить алерты. Отправленным считается только то, что Telegram принял."""
        individual, overflow = split_by_cap(ev.alerts, self.cfg.max_alerts_per_cycle)
        unsent = set()
        ok = True
        for a in individual:
            ok = ok and not self.stop.is_set() and self.notifier.send(format_alert(a, now))
            if ok:
                self.store.record_alert(cycle_id, a.key, a.rules, a.price, self.clock())
            else:
                unsent.add(a.key)
        if overflow:
            ok = ok and not self.stop.is_set() and self.notifier.send(format_overflow(overflow))
            for a in overflow:
                if ok:
                    self.store.record_alert(cycle_id, a.key, a.rules, a.price, self.clock())
                else:
                    unsent.add(a.key)
        return unsent

    def fail(self, cycle_id: int | None, kind: str, message: str, *, http_status: int | None = None,
             retry_after_s: int | None = None, sha256: str | None = None, offers: int | None = None,
             problems: list[str] | None = None) -> None:
        now = self.clock()
        self.store.finish_cycle(cycle_id, now, "error", error_kind=kind, http_status=http_status,
                                retry_after_s=retry_after_s, sha256=sha256, offers=offers, note=message)
        log.warning("Цикл #%s: ошибка [%s] %s", cycle_id, kind, message)

        if kind == fx.AUTH:
            if not self.store.get(META_INCIDENT_AUTH):
                self.store.enqueue("auth", msg_auth_rejected(http_status), now)
                self.store.set(META_INCIDENT_AUTH, "1")
        elif kind == FORMAT:
            if not self.store.get(META_INCIDENT_FORMAT):
                self.store.enqueue("format", msg_format_changed(problems or [message]), now)
                self.store.set(META_INCIDENT_FORMAT, "1")
        else:
            streak = consecutive_errors(self.store.recent_cycles("live"), exclude=(fx.AUTH, FORMAT))
            if len(streak) >= FETCH_DOWN_AFTER_ERRORS and not self.store.get(META_INCIDENT_FETCH):
                last_ok = self.store.last_success("live")
                since = last_ok.attempt_at if last_ok else streak[-1].attempt_at
                self.store.enqueue("fetch_down", msg_fetch_down(local_time(since, self.cfg.tz, now), message), now)
                self.store.set(META_INCIDENT_FETCH, "1")

        nxt = next_attempt_at(self.store.recent_cycles("live"), self.cfg.poll_interval_min)
        if nxt:
            log.info("Повтор не раньше %s", local_time(nxt, self.cfg.tz, now))
        self.flush_outbox()

    def recovered(self) -> None:
        if any(self.store.get(k) for k in INCIDENTS):
            self.store.enqueue("recovered", msg_recovered(), self.clock())
            for k in INCIDENTS:
                self.store.set(k, None)
            self.flush_outbox()

    # --- служебное ---------------------------------------------------------------

    def check_token(self, now: datetime) -> None:
        exp = fx.jwt_expiry(self.cfg.fimex_jwt)
        if exp is None or exp - now > timedelta(days=TOKEN_WARN_DAYS):
            return
        marker = str(int(exp.timestamp()))
        if self.store.get(META_TOKEN_WARNED) != marker:
            self.store.enqueue("token", msg_token_expiring(exp, now, self.cfg.tz), now)
            self.store.set(META_TOKEN_WARNED, marker)

    def next_daily_report(self, now: datetime) -> datetime | None:
        """Когда отправить суточную сводку; не позже чем в течение часа после DAILY_REPORT_HOUR."""
        hour = self.cfg.daily_report_hour
        if hour is None:
            return None
        tz = self.cfg.tz
        local = now.astimezone(tz)
        today_at = datetime.combine(local.date(), time(hour), tz)
        done = self.store.get(META_DAILY_REPORT) == local.date().isoformat()
        if not done and today_at <= local < today_at + timedelta(hours=1):
            return now
        if not done and local < today_at:
            return today_at
        return datetime.combine(local.date() + timedelta(days=1), time(hour), tz)

    def send_daily_report(self, now: datetime) -> None:
        cycles, errors, alerts = self.store.stats_since(now - timedelta(hours=24))
        self.store.enqueue("daily", msg_daily(cycles, errors, alerts), now)
        self.store.set(META_DAILY_REPORT, now.astimezone(self.cfg.tz).date().isoformat())
        self.flush_outbox()

    def flush_outbox(self) -> None:
        for item in self.store.pending():
            now = self.clock()
            if now - item.created_at > OUTBOX_MAX_AGE:
                log.warning("Служебное сообщение [%s] не ушло за сутки — выбрасываю", item.kind)
                self.store.mark_dropped(item.id)
                continue
            if self.stop.is_set() or not self.notifier.send(item.text):
                break
            self.store.mark_sent(item.id, self.clock())

    def save_raw(self, content: bytes, at: datetime) -> Path:
        """Сырой xlsx — в data/raw/ на сутки, для разбора спорных алертов."""
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        path = self.raw_dir / f"fimex-pricelist-{at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}.xlsx"
        path.write_bytes(content)
        cutoff = at - timedelta(hours=RAW_KEEP_HOURS)
        for old in self.raw_dir.glob("*.xlsx"):
            when = export_time_from_filename(old) or datetime.fromtimestamp(old.stat().st_mtime, timezone.utc)
            if when < cutoff:
                old.unlink(missing_ok=True)
        return path


# --- команды ------------------------------------------------------------------------

class InstanceLock:
    """Один экземпляр на DATA_DIR: второй процесс с той же базой не стартует."""

    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "monitor.lock"
        self.fd = None

    def __enter__(self):
        self.fd = open(self.path, "w")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fd.close()
            raise SystemExit(f"С {self.path.parent} уже работает другой экземпляр монитора — выхожу.") from None
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()


class RedactingFilter(logging.Filter):
    """Подменяет секреты на *** во всех записях лога, включая трейсбеки."""

    def __init__(self):
        super().__init__()
        self.secrets: list[str] = []

    def _redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            record.msg, record.args = self._redact(record.getMessage()), None
            if record.exc_info:
                record.exc_text = self._redact(logging.Formatter().formatException(record.exc_info))
                record.exc_info = None
        return True


class TzFormatter(logging.Formatter):
    """Время в логах — в часовом поясе из настроек, а не в системном поясе контейнера."""

    tz = None

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, self.tz) if self.tz else datetime.fromtimestamp(record.created).astimezone()
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


LOG_FORMATTER = TzFormatter("%(asctime)s %(levelname)s %(message)s")


def setup_logging() -> RedactingFilter:
    redactor = RedactingFilter()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(LOG_FORMATTER)
    handler.addFilter(redactor)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # httpx пишет URL запросов на INFO, а в URL Telegram — токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return redactor


def _require(cfg: Config, *, fimex: bool = False, telegram: bool = False) -> None:
    missing = []
    if fimex and not cfg.fimex_jwt:
        missing.append("FIMEX_JWT")
    if telegram and not cfg.tg_bot_token:
        missing.append("TG_BOT_TOKEN")
    if telegram and not cfg.tg_chat_id:
        missing.append("TG_CHAT_ID")
    if missing:
        raise ConfigError("не заданы: " + ", ".join(missing))


def _telegram(cfg: Config, stop: threading.Event | None = None) -> Telegram:
    return Telegram(cfg.tg_bot_token, cfg.tg_chat_id, cfg.tg_thread_id, stop=stop)


def cmd_run(cfg: Config) -> int:
    _require(cfg, fimex=True, telegram=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with InstanceLock(cfg.data_dir):
        store = Store(cfg.data_dir / "state.db")
        log.info("Монитор запущен: DATA_DIR=%s, интервал %s мин", cfg.data_dir, cfg.poll_interval_min)
        Monitor(cfg, store, _telegram(cfg, stop), stop=stop).run_forever()
        store.close()
    return 0


def _file_time(path: Path, override: str | None) -> datetime:
    if override:
        dt = datetime.fromisoformat(override)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return export_time_from_filename(path) or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


def cmd_once(cfg: Config, file: str | None, dry_run: bool, fetched_at: str | None) -> int:
    if dry_run and not file:
        raise ConfigError("--dry-run работает только с --file: живой запрос «для проверки» не делаем")
    db = cfg.data_dir / "state.db"

    if file:
        path = Path(file)
        content, when = path.read_bytes(), _file_time(path, fetched_at)
        log.info("Файл %s, время выгрузки %s UTC, sha256 %s", path.name, f"{when:%Y-%m-%d %H:%M}", sha256(content))
        if dry_run:
            store = Store(db, readonly=True)
            ev = Monitor(cfg, store, ConsoleNotifier()).process(content, when, cycle_id=None, dry_run=True)
            store.close()
            return 0 if ev is not None else 1
        with InstanceLock(cfg.data_dir):
            store = Store(db)
            monitor = Monitor(cfg, store, ConsoleNotifier())
            cycle_id = store.start_cycle(monitor.clock(), "file")
            ev = monitor.process(content, when, cycle_id=cycle_id)
            store.close()
        return 0 if ev is not None else 1

    _require(cfg, fimex=True, telegram=True)
    with InstanceLock(cfg.data_dir):
        store = Store(db)
        store.mark_interrupted()
        now = utcnow()
        nxt = next_attempt_at(store.recent_cycles("live"), cfg.poll_interval_min)
        if nxt and nxt > now:
            log.error("Рано: следующий запрос к Fimex с этим DATA_DIR не раньше %s", local_time(nxt, cfg.tz, now))
            store.close()
            return 3
        monitor = Monitor(cfg, store, _telegram(cfg))
        ok = monitor.live_cycle()
        store.close()
    return 0 if ok else 1


def cmd_chat_id(cfg: Config) -> int:
    if not cfg.tg_bot_token:
        raise ConfigError("не задан TG_BOT_TOKEN")
    chats = chats_from_updates(_telegram(cfg).get_updates())
    if not chats:
        print("Бот пока не видел ни одного чата. Добавьте бота в группу, напишите в группе "
              "/start@имя_бота и запустите команду ещё раз.")
        return 1
    for c in chats:
        name = c.get("title") or c.get("username") or c.get("first_name") or ""
        print(f"{c['id']}\t{c.get('type', '')}\t{name}")
    return 0


def cmd_send_test(cfg: Config) -> int:
    _require(cfg, telegram=True)
    ok = _telegram(cfg).send("🧪 Тестовое сообщение от монитора Fimex: связь с группой есть.")
    print("Отправлено" if ok else "Не отправлено — подробности в логе выше")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    redactor = setup_logging()
    parser = argparse.ArgumentParser(prog="python -m fimex_monitor", description="Монитор блотов в прайсе Fimex")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="боевой цикл")
    once = sub.add_parser("once", help="один цикл и выход")
    once.add_argument("--file", help="взять прайс из файла, без запроса к Fimex; сообщения — в консоль")
    once.add_argument("--dry-run", action="store_true", help="с --file: ничего не сохранять")
    once.add_argument("--fetched-at", help="время выгрузки файла (ISO); по умолчанию — из имени файла")
    sub.add_parser("chat-id", help="показать id чатов, в которых бот видел сообщения")
    sub.add_parser("send-test", help="отправить в группу тестовое сообщение")
    args = parser.parse_args(argv)

    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        cfg = load_config()
        redactor.secrets = cfg.secrets
        LOG_FORMATTER.tz = cfg.tz
        if args.cmd == "run":
            return cmd_run(cfg)
        if args.cmd == "once":
            return cmd_once(cfg, args.file, args.dry_run, args.fetched_at)
        if args.cmd == "chat-id":
            return cmd_chat_id(cfg)
        if args.cmd == "send-test":
            return cmd_send_test(cfg)
    except ConfigError as exc:
        log.error("Настройки: %s", exc)
        return 2
    return 2
