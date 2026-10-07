"""Контрольные числа на настоящем образце (раздел 4 ТЗ).

Образец в git не лежит (samples/ в .gitignore), поэтому без него тесты пропускаются.
На Mac Гоши, где образец есть, они обязательны.
"""

import hashlib
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from fimex_monitor.config import Config
from fimex_monitor.parse import export_time_from_filename, parse_workbook
from fimex_monitor.rules import Thresholds, find_gaps, group

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "fimex-pricelist-20261007T205720Z.xlsx"
SHA256 = "b2c300808fe93ecb2eb015f24185dcf08e57c673c20f1aa7ec51d3bebdd88a4b"

pytestmark = pytest.mark.skipif(not SAMPLE.exists(), reason="нет образца samples/fimex-pricelist-20261007T205720Z.xlsx")


@pytest.fixture(scope="module")
def parsed():
    data = SAMPLE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == SHA256
    export_date = export_time_from_filename(SAMPLE).astimezone(Config().tz).date()
    return parse_workbook(data, export_date)


def test_sheets(parsed):
    assert parsed.sheets == ["Apple", "Samsung", "Xiaomi", "OnePlus", "Meta", "Google",
                             "Honor", "HP", "Lenovo", "Sony", "Marshall"]


def test_offer_counts(parsed):
    assert len(parsed.offers) == 1285
    assert parsed.offers_by_sheet == {
        "Apple": 581, "Samsung": 399, "Xiaomi": 137, "OnePlus": 7, "Meta": 37, "Google": 41,
        "Honor": 23, "HP": 10, "Lenovo": 26, "Sony": 19, "Marshall": 5,
    }


def test_titles_products_positions(parsed):
    assert parsed.title_rows == 871
    products = group(parsed.offers)
    assert len(products) == 866
    assert sum(len(p.positions) for p in products.values()) == 1254


def test_prices(parsed):
    assert parsed.price_from_string == 413
    prices = [o.price_usd for o in parsed.offers]
    assert min(prices) == Decimal(14) and max(prices) == Decimal(10677)


def test_no_bad_rows(parsed):
    assert parsed.bad_rows == []
    assert parsed.eta_unparsed == 0


def test_regions(parsed):
    assert len({o.region for o in parsed.offers}) == 36


def test_eta_mostly_export_day(parsed):
    # 91 % строк — с датой выгрузки по Дубаю (08 Oct)
    share = Counter(o.eta_raw for o in parsed.offers)["08 Oct"] / len(parsed.offers)
    assert 0.90 <= share <= 0.92


def test_multi_region_products(parsed):
    products = group(parsed.offers)
    multi = [p for p in products.values() if len(p.positions) > 1]
    assert len(multi) == 219
    assert Counter(p.positions[0].brand for p in multi) == {"Apple": 124, "Samsung": 83, "Xiaomi": 9, "Lenovo": 2, "Google": 1}
    several_rows = [p for p in products.values() if any(len(pos.offers) > 1 for pos in p.positions)]
    assert len(several_rows) == 31


def test_samsung_duplicate_titles(parsed):
    counts = Counter(t for s, _, t in parsed.titles if s == "Samsung")
    assert sum(1 for c in counts.values() if c > 1) == 5


def test_gaps(parsed):
    assert len(find_gaps(group(parsed.offers), Thresholds())) == 41
