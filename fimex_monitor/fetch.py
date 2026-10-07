"""Один GET к Fimex и проверка ответа (раздел 3 ТЗ). Без повторов: повтор — только по расписанию."""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

URL = "https://api.fimex.ae/app-api/v1/catalog/fetch-pricelist"
PARAMS = {"percent": "0", "excel": "1"}
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CONNECT_TIMEOUT_S = 10
TOTAL_TIMEOUT_S = 90
MAX_BODY_BYTES = 20 * 1024 * 1024

# Виды ошибок цикла
AUTH = "auth"  # 401/403 — токен отклонён
RATE_LIMITED = "rate_limited"  # 429
SERVER = "server"  # 5xx
HTTP = "http"  # прочие статусы
TIMEOUT = "timeout"
NETWORK = "network"
NOT_XLSX = "not_xlsx"  # 200, но тело не xlsx


class FetchError(Exception):
    def __init__(self, kind: str, message: str, status: int | None = None, retry_after_s: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class FetchResult:
    content: bytes
    status: int
    elapsed_s: float


def build_headers(jwt: str, app_access: str = "", send_app_access: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {jwt}", "Accept": XLSX_MIME}
    if send_app_access and app_access:
        headers["x-app-access"] = app_access
    return headers


def parse_retry_after(value: str | None, now: datetime | None = None) -> int | None:
    """Retry-After: секунды или HTTP-дата → секунды."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0, int((when - now).total_seconds() + 0.999))


def make_client() -> httpx.Client:
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT_S, read=TOTAL_TIMEOUT_S, write=CONNECT_TIMEOUT_S, pool=CONNECT_TIMEOUT_S)
    return httpx.Client(timeout=timeout, follow_redirects=False)


def fetch_pricelist(
    jwt: str,
    app_access: str = "",
    send_app_access: bool = False,
    client: httpx.Client | None = None,
) -> FetchResult:
    if not jwt:
        raise FetchError(AUTH, "FIMEX_JWT не задан")
    own = client is None
    client = client or make_client()
    started = time.monotonic()
    deadline = started + TOTAL_TIMEOUT_S
    try:
        with client.stream("GET", URL, params=PARAMS, headers=build_headers(jwt, app_access, send_app_access)) as r:
            status = r.status_code
            if status != 200:
                retry_after = parse_retry_after(r.headers.get("Retry-After"))
                if status in (401, 403):
                    raise FetchError(AUTH, f"Fimex отклонил токен: HTTP {status}", status, retry_after)
                if status == 429:
                    raise FetchError(RATE_LIMITED, "Fimex: слишком много запросов (HTTP 429)", status, retry_after)
                if status >= 500:
                    raise FetchError(SERVER, f"Fimex: ошибка сервера HTTP {status}", status, retry_after)
                raise FetchError(HTTP, f"Fimex: неожиданный ответ HTTP {status}", status, retry_after)
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    raise FetchError(NOT_XLSX, f"ответ больше {MAX_BODY_BYTES // 1024 // 1024} МБ", status)
                if time.monotonic() > deadline:
                    raise FetchError(TIMEOUT, f"ответ не уложился в {TOTAL_TIMEOUT_S} с", status)
                chunks.append(chunk)
            content = b"".join(chunks)
    except FetchError:
        raise
    except httpx.TimeoutException as exc:
        raise FetchError(TIMEOUT, f"таймаут: {type(exc).__name__}") from None
    except httpx.HTTPError as exc:
        raise FetchError(NETWORK, f"сеть: {type(exc).__name__}") from None
    finally:
        if own:
            client.close()

    if not content.startswith(b"PK"):
        ctype = r.headers.get("Content-Type", "?")
        raise FetchError(NOT_XLSX, f"ответ не xlsx (Content-Type: {ctype}, {len(content)} байт)", status)
    return FetchResult(content, status, time.monotonic() - started)


def jwt_expiry(token: str) -> datetime | None:
    """Время истечения из payload JWT. Подпись не проверяется — нужен только exp."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromtimestamp(int(data["exp"]), timezone.utc)
    except (IndexError, ValueError, KeyError, TypeError, binascii.Error, OverflowError, OSError):
        return None
