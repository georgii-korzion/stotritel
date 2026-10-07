import base64
import json
from datetime import datetime, timezone

import httpx
import pytest

from fimex_monitor import fetch as fx


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_request_shape_and_success():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        return httpx.Response(200, content=b"PK\x03\x04rest")

    res = fx.fetch_pricelist("JWT", "APPACCESS", False, client=client(handler))
    assert res.content.startswith(b"PK") and res.status == 200
    assert seen["url"] == "https://api.fimex.ae/app-api/v1/catalog/fetch-pricelist?percent=0&excel=1"
    assert seen["headers"]["authorization"] == "Bearer JWT"
    assert seen["headers"]["accept"] == fx.XLSX_MIME
    assert "x-app-access" not in seen["headers"]  # по умолчанию не отправляем


def test_app_access_flag():
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        return httpx.Response(200, content=b"PK..")

    fx.fetch_pricelist("JWT", "APPACCESS", True, client=client(handler))
    assert seen["headers"]["x-app-access"] == "APPACCESS"


@pytest.mark.parametrize("status, kind", [
    (401, fx.AUTH), (403, fx.AUTH), (429, fx.RATE_LIMITED), (500, fx.SERVER), (503, fx.SERVER), (404, fx.HTTP),
])
def test_http_errors(status, kind):
    with pytest.raises(fx.FetchError) as e:
        fx.fetch_pricelist("JWT", client=client(lambda r: httpx.Response(status, headers={"Retry-After": "120"})))
    assert e.value.kind == kind and e.value.status == status and e.value.retry_after_s == 120


def test_not_xlsx():
    with pytest.raises(fx.FetchError) as e:
        fx.fetch_pricelist("JWT", client=client(lambda r: httpx.Response(200, json={"error": "x"})))
    assert e.value.kind == fx.NOT_XLSX


def test_network_and_timeout():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(fx.FetchError) as e:
        fx.fetch_pricelist("JWT", client=client(timeout))
    assert e.value.kind == fx.TIMEOUT
    with pytest.raises(fx.FetchError) as e:
        fx.fetch_pricelist("JWT", client=client(refused))
    assert e.value.kind == fx.NETWORK


def test_token_not_in_error_messages():
    for status in (401, 429, 500):
        with pytest.raises(fx.FetchError) as e:
            fx.fetch_pricelist("SECRET-JWT", client=client(lambda r, s=status: httpx.Response(s)))
        assert "SECRET-JWT" not in str(e.value)


def test_retry_after_parsing():
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    assert fx.parse_retry_after("90", now) == 90
    assert fx.parse_retry_after("Thu, 08 Oct 2026 12:05:00 GMT", now) == 300
    assert fx.parse_retry_after("garbage", now) is None
    assert fx.parse_retry_after(None, now) is None


def make_jwt(payload):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'HS256'})}.{enc(payload)}.signature"


def test_jwt_expiry():
    exp = int(datetime(2027, 9, 23, tzinfo=timezone.utc).timestamp())
    assert fx.jwt_expiry(make_jwt({"exp": exp, "sub": "1"})) == datetime(2027, 9, 23, tzinfo=timezone.utc)
    assert fx.jwt_expiry("not-a-jwt") is None
    assert fx.jwt_expiry(make_jwt({"sub": "1"})) is None
    assert fx.jwt_expiry("") is None
