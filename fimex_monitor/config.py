"""Настройки из окружения (на Mac — из .env, на Railway — из переменных сервиса)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .rules import Thresholds

log = logging.getLogger(__name__)

# Жёсткие ограничения (раздел 2 ТЗ) — настройками не обходятся.
MIN_POLL_INTERVAL_MIN = 15
MAX_BACKOFF_MIN = 120
AUTH_RETRY_MIN = 120
FETCH_DOWN_AFTER_ERRORS = 3
TOKEN_WARN_DAYS = 14
RAW_KEEP_HOURS = 24


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    fimex_jwt: str = field(default="", repr=False)
    fimex_app_access: str = field(default="", repr=False)
    fimex_send_app_access: bool = False
    tg_bot_token: str = field(default="", repr=False)
    tg_chat_id: str = ""
    tg_thread_id: int | None = None
    poll_interval_min: int = 30
    drop_pct: Decimal = Decimal(5)
    drop_abs_usd: Decimal = Decimal(100)
    gap_pct: Decimal = Decimal(5)
    gap_abs_usd: Decimal = Decimal(100)
    eta_max_days: int | None = None
    min_price_usd: Decimal = Decimal(0)
    baseline_max_age_hours: float = 24
    alert_cooldown_hours: float = 12
    max_alerts_per_cycle: int = 15
    daily_report_hour: int | None = 10
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("Asia/Dubai"))
    data_dir: Path = Path("data")

    @property
    def thresholds(self) -> Thresholds:
        return Thresholds(
            drop_pct=self.drop_pct,
            drop_abs=self.drop_abs_usd,
            gap_pct=self.gap_pct,
            gap_abs=self.gap_abs_usd,
            baseline_max_age=timedelta(hours=self.baseline_max_age_hours),
            cooldown=timedelta(hours=self.alert_cooldown_hours),
        )

    @property
    def secrets(self) -> list[str]:
        """Значения, которые нельзя показывать в логах."""
        return [s for s in (self.fimex_jwt, self.fimex_app_access, self.tg_bot_token) if s]

    @property
    def telegram_ready(self) -> bool:
        return bool(self.tg_bot_token and self.tg_chat_id)


def _str(env: Mapping[str, str], key: str) -> str:
    return (env.get(key) or "").strip()


def _int(env, key, default: int | None, *, empty_means_none: bool = False) -> int | None:
    if key not in env:
        return default
    raw = _str(env, key)
    if raw == "":
        return None if empty_means_none else default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{key}: ожидается целое число, получено {raw!r}") from None


def _dec(env, key, default: Decimal) -> Decimal:
    raw = _str(env, key).replace(",", ".")
    if raw == "":
        return default
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ConfigError(f"{key}: ожидается число, получено {raw!r}") from None
    if not value.is_finite() or value < 0:
        raise ConfigError(f"{key}: ожидается неотрицательное число, получено {raw!r}")
    return value


def _float(env, key, default: float) -> float:
    return float(_dec(env, key, Decimal(str(default))))


def _bool(env, key, default: bool) -> bool:
    raw = _str(env, key).lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on", "да")


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env

    poll = _int(env, "POLL_INTERVAL_MIN", 30)
    if poll < MIN_POLL_INTERVAL_MIN:
        log.warning(
            "POLL_INTERVAL_MIN=%s меньше нижнего предела, используется %s мин",
            poll, MIN_POLL_INTERVAL_MIN,
        )
        poll = MIN_POLL_INTERVAL_MIN

    tz_name = _str(env, "TZ") or "Asia/Dubai"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"TZ: неизвестный часовой пояс {tz_name!r}") from None

    report_hour = _int(env, "DAILY_REPORT_HOUR", 10, empty_means_none=True)
    if report_hour is not None and not 0 <= report_hour <= 23:
        raise ConfigError(f"DAILY_REPORT_HOUR: ожидается час 0–23, получено {report_hour}")

    eta_max_days = _int(env, "ETA_MAX_DAYS", None, empty_means_none=True)
    if eta_max_days is not None and eta_max_days < 0:
        raise ConfigError("ETA_MAX_DAYS: ожидается неотрицательное число")

    max_alerts = _int(env, "MAX_ALERTS_PER_CYCLE", 15)
    if max_alerts < 1:
        raise ConfigError("MAX_ALERTS_PER_CYCLE: ожидается число от 1")

    thread = _int(env, "TG_THREAD_ID", None, empty_means_none=True)

    return Config(
        fimex_jwt=_str(env, "FIMEX_JWT"),
        fimex_app_access=_str(env, "FIMEX_APP_ACCESS"),
        fimex_send_app_access=_bool(env, "FIMEX_SEND_APP_ACCESS", False),
        tg_bot_token=_str(env, "TG_BOT_TOKEN"),
        tg_chat_id=_str(env, "TG_CHAT_ID"),
        tg_thread_id=thread,
        poll_interval_min=poll,
        drop_pct=_dec(env, "DROP_PCT", Decimal(5)),
        drop_abs_usd=_dec(env, "DROP_ABS_USD", Decimal(100)),
        gap_pct=_dec(env, "GAP_PCT", Decimal(5)),
        gap_abs_usd=_dec(env, "GAP_ABS_USD", Decimal(100)),
        eta_max_days=eta_max_days,
        min_price_usd=_dec(env, "MIN_PRICE_USD", Decimal(0)),
        baseline_max_age_hours=_float(env, "BASELINE_MAX_AGE_HOURS", 24),
        alert_cooldown_hours=_float(env, "ALERT_COOLDOWN_HOURS", 12),
        max_alerts_per_cycle=max_alerts,
        daily_report_hour=report_hour,
        tz=tz,
        data_dir=Path(_str(env, "DATA_DIR") or "data"),
    )
