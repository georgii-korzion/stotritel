"""xlsx-прайс Fimex → плоский список предложений.

Модуль не ходит ни в сеть, ни в базу: его можно проверять на файле из samples/.
Формат описан в ТЗ, раздел 4.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import openpyxl

COLUMNS = 5  # A–E

# Признаки смены формата (раздел 4). Пороги фиксированные, не настройки.
MAX_BAD_ROWS_SHARE_PCT = 2  # строк не по формату больше 2 % от числа предложений
MIN_OFFERS_SHARE_OF_PREV = Decimal("0.5")  # предложений меньше половины от прошлой выгрузки

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_ETA_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\.?$")
_PRICE_RE = re.compile(r"^\d+(\.\d+)?$")
# обычный, неразрывный, узкий неразрывный и тонкий пробелы
_PRICE_SPACES = str.maketrans("", "", "    ")
_FILENAME_TS_RE = re.compile(r"(\d{8}T\d{6}Z)")


@dataclass(frozen=True)
class Offer:
    brand: str
    title: str
    article: str
    eta_raw: str
    eta_date: date | None
    region: str
    price_usd: Decimal
    qty: int
    sheet_row: int = 0  # номер строки на листе — для разбора спорных случаев


@dataclass(frozen=True)
class BadRow:
    sheet: str
    row: int
    reason: str


@dataclass
class ParseResult:
    offers: list[Offer]
    sheets: list[str]
    title_rows: int
    bad_rows: list[BadRow]
    price_from_string: int
    eta_unparsed: int
    offers_by_sheet: dict[str, int] = field(default_factory=dict)
    titles: list[tuple[str, int, str]] = field(default_factory=list)  # (лист, строка, название)


class WorkbookError(Exception):
    """Книга не открывается как xlsx."""


def _filled(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _integral(value: Decimal) -> Decimal:
    return value.quantize(Decimal(1)) if value == value.to_integral_value() else value


def parse_price(value: object) -> tuple[Decimal | None, bool]:
    """Цена из ячейки D: число или строка с пробелами-разделителями ('1 695').

    Возвращает (цена или None, пришла ли строкой).
    """
    if isinstance(value, bool) or value is None:
        return None, False
    if isinstance(value, int):
        return Decimal(value), False
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None, False
        return _integral(Decimal(str(value))), False
    if isinstance(value, Decimal):
        return (_integral(value) if value.is_finite() else None), False
    if isinstance(value, str):
        s = value.strip().translate(_PRICE_SPACES).replace(",", ".")
        if not _PRICE_RE.match(s):
            return None, True
        try:
            return _integral(Decimal(s)), True
        except InvalidOperation:
            return None, True
    return None, False


def parse_article(value: object) -> str | None:
    """Артикул всегда строкой. Числа — без дробной части, строки — как есть (ведущие нули сохраняются)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    s = str(value).strip()
    return s or None


def parse_qty(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    s = str(value).strip().translate(_PRICE_SPACES)
    return int(s) if s.isdigit() else None


def parse_eta(value: object, export_date: date) -> tuple[str, date | None]:
    """ETA без года ('08 Oct') → (исходная строка, дата).

    Год подбирается так, чтобы дата была ближайшей к дате выгрузки:
    в декабре '05 Jan' — это январь следующего года.
    """
    if isinstance(value, datetime):
        return value.strftime("%d %b"), value.date()
    if isinstance(value, date):
        return value.strftime("%d %b"), value
    raw = str(value).strip()
    m = _ETA_RE.match(raw)
    if not m:
        return raw, None
    day, month = int(m.group(1)), _MONTHS.get(m.group(2).lower())
    if month is None:
        return raw, None
    candidates = []
    for year in (export_date.year - 1, export_date.year, export_date.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        return raw, None
    return raw, min(candidates, key=lambda d: abs((d - export_date).days))


def _open(data: bytes | str | Path):
    source = io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data
    try:
        return openpyxl.load_workbook(source, read_only=True, data_only=True)
    except Exception as exc:  # zipfile.BadZipFile, KeyError, InvalidFileException, ...
        raise WorkbookError(f"книга не открывается: {type(exc).__name__}") from exc


def parse_workbook(data: bytes | str | Path, export_date: date) -> ParseResult:
    wb = _open(data)
    try:
        return _parse(wb, export_date)
    except WorkbookError:
        raise
    except Exception as exc:  # битый xml внутри zip и т. п.
        raise WorkbookError(f"книга не читается: {type(exc).__name__}") from exc
    finally:
        wb.close()


def _parse(wb, export_date: date) -> ParseResult:
    offers: list[Offer] = []
    bad: list[BadRow] = []
    sheets: list[str] = []
    offers_by_sheet: dict[str, int] = {}
    titles: list[tuple[str, int, str]] = []
    title_rows = price_from_string = eta_unparsed = 0

    for ws in wb.worksheets:
        sheet = ws.title
        brand = sheet.strip()
        sheets.append(sheet)
        if hasattr(ws, "reset_dimensions"):
            # размеры листа в файле могут быть записаны неверно — читаем все строки
            ws.reset_dimensions()
        title: str | None = None

        rows = ws.iter_rows(min_row=1, min_col=1, max_col=COLUMNS, values_only=True)
        for row_no, row in enumerate(rows, start=1):
            values = tuple(row[:COLUMNS]) + (None,) * (COLUMNS - len(row))
            filled = [_filled(v) for v in values]
            count = sum(filled)

            if count == 0:  # разделитель
                continue
            if count == 1 and filled[0]:  # название товара
                title = str(values[0]).strip()
                title_rows += 1
                titles.append((sheet, row_no, title))
                continue
            if count != COLUMNS:
                bad.append(BadRow(sheet, row_no, f"заполнено ячеек: {count}"))
                continue

            a, b, c, d, e = values
            if title is None:
                bad.append(BadRow(sheet, row_no, "предложение без названия над ним"))
                continue
            article = parse_article(a)
            if not article:
                bad.append(BadRow(sheet, row_no, "пустой артикул"))
                continue
            price, from_string = parse_price(d)
            if price is None or price <= 0:
                bad.append(BadRow(sheet, row_no, "цена не разбирается или не положительна"))
                continue
            qty = parse_qty(e)
            if qty is None:
                bad.append(BadRow(sheet, row_no, "количество не разбирается"))
                continue
            region = str(c).strip()
            eta_raw, eta_date = parse_eta(b, export_date)
            if eta_date is None:
                eta_unparsed += 1
            price_from_string += from_string

            offers.append(Offer(
                brand=brand, title=title, article=article, eta_raw=eta_raw,
                eta_date=eta_date, region=region, price_usd=price, qty=qty,
                sheet_row=row_no,
            ))
            offers_by_sheet[sheet] = offers_by_sheet.get(sheet, 0) + 1

    return ParseResult(
        offers=offers, sheets=sheets, title_rows=title_rows, bad_rows=bad,
        price_from_string=price_from_string, eta_unparsed=eta_unparsed,
        offers_by_sheet=offers_by_sheet, titles=titles,
    )


def check_format(result: ParseResult, prev_offers: int | None) -> list[str]:
    """Признаки того, что формат файла изменился. Пустой список — всё в порядке."""
    problems: list[str] = []
    n = len(result.offers)
    if not result.offers_by_sheet:
        problems.append("в книге не осталось листов с предложениями")
    if len(result.bad_rows) * 100 > n * MAX_BAD_ROWS_SHARE_PCT:
        reasons: dict[str, int] = {}
        for b in result.bad_rows:
            reasons[b.reason] = reasons.get(b.reason, 0) + 1
        top = ", ".join(f"{r} — {k}" for r, k in sorted(reasons.items(), key=lambda x: -x[1])[:3])
        problems.append(
            f"строк не по формату {len(result.bad_rows)} при {n} предложениях "
            f"(больше {MAX_BAD_ROWS_SHARE_PCT} %): {top}"
        )
    if prev_offers and n < prev_offers * MIN_OFFERS_SHARE_OF_PREV:
        problems.append(f"предложений {n}, в прошлой выгрузке было {prev_offers} — меньше половины")
    return problems


def export_time_from_filename(path: str | Path) -> datetime | None:
    """Время выгрузки из имени вида fimex-pricelist-20261007T205720Z.xlsx (UTC)."""
    m = _FILENAME_TS_RE.search(Path(path).name)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
