"""Этап 3 ТЗ: копия образца с вручную заниженными ценами у четырёх позиций.

    python tools/make_stage3_sample.py samples/fimex-pricelist-20261007T205720Z.xlsx

Правки:
  1. −3 %            — алерта быть не должно;
  2. −6 %            — алерт по правилу A;
  3. −$120 у позиции дороже $3000 (это меньше 5 %) — алерт по правилу A;
  4. падение меньше 5 % и меньше $100, после которого появляется разрыв с другим регионом — алерт по правилу B.

Для правок 1–3 берутся товары с одним регионом и одной строкой, чтобы правка не задела правило B.
После записи скрипт сам прогоняет оба файла через правила и проверяет, что алертов ровно три.
Сеть не используется.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fimex_monitor.config import Config  # noqa: E402
from fimex_monitor.parse import export_time_from_filename, parse_workbook  # noqa: E402
from fimex_monitor.rules import (  # noqa: E402
    Evaluation,
    Position,
    PrevPosition,
    Product,
    Thresholds,
    evaluate,
    find_gaps,
    group,
    next_state,
)

DEFAULT_TH = Thresholds()


@dataclass(frozen=True)
class Change:
    label: str
    position: Position
    new_price: Decimal
    expect_alert: bool

    @property
    def old_price(self) -> Decimal:
        return self.position.price


def _single(products: list[Product]) -> list[Position]:
    return [p.positions[0] for p in products if len(p.positions) == 1 and len(p.positions[0].offers) == 1]


def plan(offers, th: Thresholds = DEFAULT_TH) -> list[Change]:
    products = list(group(offers).values())
    used: set = set()

    def take(candidates, predicate):
        for pos in candidates:
            if pos.key[:2] not in used and predicate(pos):
                used.add(pos.key[:2])
                return pos
        raise SystemExit("В файле не нашлось подходящей позиции для одной из правок — нужен другой образец")

    def is_drop(old, new):
        d = old - new
        return d >= th.drop_abs or d / old * 100 >= th.drop_pct

    single = _single(products)
    changes = []

    p3 = take(single, lambda p: 300 <= p.price <= 3000)
    new3 = p3.price - (p3.price * Decimal("0.03")).to_integral_value()
    assert not is_drop(p3.price, new3)
    changes.append(Change("−3 % (алерта нет)", p3, new3, False))

    p6 = take(single, lambda p: 200 <= p.price <= 1500)
    new6 = p6.price - Decimal(math.ceil(p6.price * Decimal("0.06")))
    assert is_drop(p6.price, new6)
    changes.append(Change("−6 % (правило A)", p6, new6, True))

    try:
        p120 = take(single, lambda p: p.price > 3000)
    except SystemExit:  # товара с одним регионом дороже $3000 нет — подойдёт любая позиция из одной строки
        p120 = take([p for pr in products for p in pr.positions if len(p.offers) == 1], lambda p: p.price > 3000)
    changes.append(Change("−$120 у позиции дороже $3000 (правило A)", p120, p120.price - 120, True))

    gaps = find_gaps({p.key: p for p in products}, th)

    def gap_candidate(pos: Position) -> Decimal | None:
        product = next(pr for pr in products if pr.key == pos.key[:2])
        if len(product.positions) < 2 or product.positions[0] is not pos or pos.key in gaps:
            return None
        p1, p2 = pos.price, product.positions[1].price
        g = p2 - p1
        need = min(th.gap_abs - g, th.gap_pct / 100 * p2 - g)
        d = Decimal(max(1, math.ceil(need)))
        return d if not is_drop(p1, p1 - d) else None

    multi_cheapest = [p.positions[0] for p in products if len(p.positions) >= 2]
    pg = take(multi_cheapest, lambda p: gap_candidate(p) is not None)
    changes.append(Change("падение < 5 % и < $100, появился разрыв (правило B)", pg, pg.price - gap_candidate(pg), True))
    return changes


def fimex_cell(price: Decimal):
    """Как в выгрузке: число до 1000, строка с пробелом от 1000."""
    if price != price.to_integral_value():
        return float(price)
    n = int(price)
    return n if n < 1000 else f"{n:,}".replace(",", " ")


def apply_changes(src: Path, dst: Path, changes: list[Change]) -> None:
    wb = openpyxl.load_workbook(src)
    sheets = {ws.title.strip(): ws for ws in wb.worksheets}
    for ch in changes:
        best = ch.position.best
        sheets[best.brand].cell(best.sheet_row, 4).value = fimex_cell(ch.new_price)
    wb.save(dst)


def verify(src: Path, dst: Path, th: Thresholds = DEFAULT_TH) -> Evaluation:
    """Два цикла без сети и без базы: исходный файл как первый запуск, затем изменённый."""
    tz = Config().tz
    when = export_time_from_filename(src) or datetime.now(timezone.utc)
    export_date = when.astimezone(tz).date()
    now = datetime.now(timezone.utc)
    first = evaluate(parse_workbook(src.read_bytes(), export_date).offers, {}, {}, now, th, True)
    updates, _ = next_state({}, first, set(), now)
    state = {u.key: PrevPosition(u.price, u.seen_at, u.had_gap) for u in updates}
    return evaluate(parse_workbook(dst.read_bytes(), export_date).offers, state, {}, now, th, False)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("src", type=Path)
    ap.add_argument("dst", type=Path, nargs="?", help="по умолчанию samples/stage3-<имя исходного файла>")
    args = ap.parse_args(argv)
    dst = args.dst or args.src.with_name("stage3-" + args.src.name)

    tz = Config().tz
    when = export_time_from_filename(args.src)
    export_date = when.astimezone(tz).date() if when else None
    if export_date is None:
        raise SystemExit("В имени исходного файла нет времени выгрузки (…-YYYYMMDDTHHMMSSZ.xlsx)")
    offers = parse_workbook(args.src.read_bytes(), export_date).offers
    changes = plan(offers)
    apply_changes(args.src, dst, changes)

    print(f"Записан {dst}\n")
    for ch in changes:
        p, b = ch.position, ch.position.best
        print(f"{ch.label}\n    {p.brand} · {p.title} · {p.region} (лист {b.brand}, строка {b.sheet_row}): "
              f"${ch.old_price} → ${ch.new_price}")
    ev = verify(args.src, dst)
    print(f"\nПроверка правилами: алертов {len(ev.alerts)}")
    for a in ev.alerts:
        print(f"    {'+'.join(a.rules)}: {a.position.title} · {a.position.region}")
    expected = {ch.position.key for ch in changes if ch.expect_alert}
    got = {a.key for a in ev.alerts}
    if got != expected:
        print("НЕ СОВПАЛО с ожиданием (3 алерта по правкам 2–4)")
        return 1
    print("Совпадает с ожиданием: ровно три алерта, по позиции −3 % алерта нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
