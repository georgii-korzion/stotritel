import io
import zipfile
from datetime import date
from decimal import Decimal

import pytest

from fimex_monitor.parse import (
    WorkbookError,
    check_format,
    export_time_from_filename,
    parse_article,
    parse_eta,
    parse_price,
    parse_qty,
    parse_workbook,
)
from fimex_monitor.rules import group, position_key

from .synthetic import SMALL, write_pricelist

EXPORT = date(2026, 10, 8)


@pytest.fixture
def small(tmp_path):
    return parse_workbook(write_pricelist(tmp_path / "small.xlsx", SMALL), EXPORT)


def find(result, title, region=None):
    return [o for o in result.offers if o.title == title and (region is None or o.region == region)]


def test_counts(small):
    assert small.sheets == ["Apple", "Samsung", "Meta", "Sony", "Marshall"]
    assert small.offers_by_sheet == {"Apple": 10, "Samsung": 5, "Meta": 1, "Sony": 2, "Marshall": 1}
    assert len(small.offers) == 19
    assert small.title_rows == 13
    assert small.bad_rows == []
    assert small.eta_unparsed == 0
    # строкой пришли все цены от 1000: 1494, 1805, 2199, 2399, 1045, 1099, 1755, 1049
    assert small.price_from_string == 8
    assert check_format(small, None) == []


def test_string_price_with_space(small):
    (o,) = find(small, "iPhone 17 Pro Max 1Tb Blue", "🇦🇺 AU")
    assert o.price_usd == Decimal(1805) and isinstance(o.price_usd, Decimal)
    (mbp,) = find(small, "MacBook Pro 14 M4 Pro 24/1Tb Silver", "🇺🇸 US")
    assert mbp.price_usd == Decimal(2199)  # неразрывный пробел
    (ps5,) = find(small, "PlayStation 5 Pro")
    assert ps5.price_usd == Decimal(1049)  # узкий неразрывный пробел


def test_numeric_and_leading_zero_articles(small):
    s25 = {o.region: o for o in find(small, "Galaxy S25 Ultra 256Gb Black")}
    assert s25["🇦🇪 AE"].article == "37015477980"
    assert s25["🇪🇺 GL"].article == "07977974724"


def test_icon_regions_kept_as_is(small):
    assert find(small, "DualSense Edge")[0].region == "🎮"
    assert find(small, "Major V Black")[0].region == "🎧"
    assert find(small, "Ray-Ban Meta Wayfarer Matte Black")[0].region == "👓"


def test_offer_fields(small):
    o = find(small, "iPhone 15 128Gb Black", "🇺🇸 US")[0]
    assert (o.brand, o.article, o.eta_raw, o.eta_date, o.qty) == ("Apple", "MTLV3 LL/A", "08 Oct", date(2026, 10, 8), 9)
    late = find(small, "iPad mini A17 Pro 128Gb Space Gray")
    assert [x.eta_date for x in late] == [date(2026, 10, 8), date(2026, 10, 17)]


def test_samsung_two_blocks_are_one_product(small):
    products = group(small.offers)
    tab = products[("samsung", "tab s10 fe 8+ 128gb blue")]
    assert len(tab.offers) == 2
    (pos,) = tab.positions
    assert pos.price == Decimal(341)
    assert sorted(t for s, _, t in small.titles if s == "Samsung").count("Tab S10 FE 8+ 128Gb Blue") == 2


def test_position_price_is_min_row(small):
    products = group(small.offers)
    (pos,) = products[("apple", "ipad mini a17 pro 128gb space gray")].positions
    assert pos.price == Decimal(631) and pos.best.qty == 5


@pytest.mark.parametrize("value, expected, from_string", [
    (572, Decimal(572), False),
    (572.0, Decimal(572), False),
    ("1 695", Decimal(1695), True),
    ("1 695", Decimal(1695), True),
    ("1 695", Decimal(1695), True),
    ("12,50", Decimal("12.50"), True),
    ("12.5", Decimal("12.5"), True),
    (" 10 677 ", Decimal(10677), True),
    ("abc", None, True),
    ("-5", None, True),
    ("$1 695", None, True),
    (True, None, False),
    (None, None, False),
])
def test_parse_price(value, expected, from_string):
    assert parse_price(value) == (expected, from_string)


def test_parse_article_and_qty():
    assert parse_article(37015477980) == "37015477980"
    assert parse_article(3.7015477980e10) == "37015477980"
    assert parse_article(" CFI-2116-B01Y ") == "CFI-2116-B01Y"
    assert parse_article("  ") is None
    assert parse_qty(9) == 9 and parse_qty("687") == 687 and parse_qty(3.0) == 3
    assert parse_qty("x") is None and parse_qty(-1) is None and parse_qty(2.5) is None


@pytest.mark.parametrize("raw, export, expected", [
    ("08 Oct", date(2026, 10, 8), date(2026, 10, 8)),
    ("17 Oct", date(2026, 10, 8), date(2026, 10, 17)),
    ("05 Jan", date(2026, 12, 28), date(2027, 1, 5)),  # в декабре январь — следующего года
    ("28 Dec", date(2027, 1, 3), date(2026, 12, 28)),
    ("29 Feb", date(2027, 10, 1), date(2028, 2, 29)),
    ("soon", date(2026, 10, 8), None),
])
def test_parse_eta(raw, export, expected):
    assert parse_eta(raw, export) == (raw, expected)


def test_bad_rows_are_counted_not_guessed(tmp_path):
    sheets = {
        "Apple": [
            ("raw", ("ORPHAN", "08 Oct", "🇺🇸 US", 100, 1)),  # предложение без названия
            ("iPhone 15 128Gb Black", [("MTLV3 LL/A", "08 Oct", "🇺🇸 US", 572, 9)]),
            ("raw", ("X", "08 Oct", None, None, None)),  # 2 ячейки
            ("raw", ("X", "08 Oct", "🇺🇸 US", "n/a", 1)),  # цена не разбирается
            ("raw", ("X", "08 Oct", "🇺🇸 US", 0, 1)),  # цена не положительна
            ("raw", (None, None, "🇺🇸 US", None, None)),  # одна ячейка, но не в A
        ],
    }
    result = parse_workbook(write_pricelist(tmp_path / "bad.xlsx", sheets), EXPORT)
    assert len(result.offers) == 1
    assert [b.reason for b in result.bad_rows] == [
        "предложение без названия над ним",
        "заполнено ячеек: 2",
        "цена не разбирается или не положительна",
        "цена не разбирается или не положительна",
        "заполнено ячеек: 1",
    ]
    assert check_format(result, None)  # 5 плохих строк на 1 предложение — формат изменился


def test_check_format_thresholds(small):
    from fimex_monitor.parse import BadRow
    n = len(small.offers)  # 19: 2 % — это 0,38 строки
    assert check_format(small, prev_offers=38) == []  # ровно половина — ещё норма
    assert check_format(small, prev_offers=39)  # меньше половины
    small.bad_rows = [BadRow("Apple", 1, "заполнено ячеек: 3")]
    assert len(small.bad_rows) * 100 > n * 2
    assert check_format(small, None)


def test_check_format_no_sheets(tmp_path):
    result = parse_workbook(write_pricelist(tmp_path / "empty.xlsx", {"Apple": []}), EXPORT)
    assert check_format(result, None) == ["в книге не осталось листов с предложениями"]


def test_two_percent_bad_rows_is_still_ok(tmp_path):
    rows = [(f"A{i}", "08 Oct", "🇺🇸 US", 100 + i, 1) for i in range(100)]
    sheets = {"Apple": [("Item", rows), ("raw", ("X", "Y", None, None, None)), ("raw", ("X", "Y", None, None, None))]}
    result = parse_workbook(write_pricelist(tmp_path / "two.xlsx", sheets), EXPORT)
    assert len(result.offers) == 100 and len(result.bad_rows) == 2
    assert check_format(result, None) == []


def test_not_a_workbook():
    with pytest.raises(WorkbookError):
        parse_workbook(b"<html>login</html>", EXPORT)
    with pytest.raises(WorkbookError):
        parse_workbook(b"PK\x03\x04garbage", EXPORT)


def test_wrong_dimension_in_file_still_reads_all_rows(tmp_path):
    """Если в файле неверно записан размер листа, read_only-режим не должен терять строки."""
    src = write_pricelist(tmp_path / "src.xlsx", SMALL)
    buf = io.BytesIO()
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.startswith("xl/worksheets/sheet"):
                text = data.decode()
                start = text.index("<dimension ref=")
                end = text.index("/>", start) + 2
                data = (text[:start] + '<dimension ref="A1"/>' + text[end:]).encode()
            zout.writestr(item, data)
    result = parse_workbook(buf.getvalue(), EXPORT)
    assert len(result.offers) == 19


def test_export_time_from_filename():
    dt = export_time_from_filename("samples/fimex-pricelist-20261007T205720Z.xlsx")
    assert dt.isoformat() == "2026-10-07T20:57:20+00:00"
    assert export_time_from_filename("stage3-fimex-pricelist-20261007T205720Z.xlsx") == dt
    assert export_time_from_filename("x.xlsx") is None


def test_position_key_uses_normalized_title(small):
    o = find(small, "iPhone 15 128Gb Black", "🇺🇸 US")[0]
    assert position_key(o) == ("apple", "iphone 15 128gb black", "🇺🇸 US")
