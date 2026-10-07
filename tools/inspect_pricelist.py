"""Осмотр прайса без сети: контрольные числа из раздела 4 ТЗ, повторяющиеся названия, разрывы.

    python tools/inspect_pricelist.py samples/fimex-pricelist-20261007T205720Z.xlsx
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fimex_monitor.config import Config  # noqa: E402
from fimex_monitor.notify import money, pct  # noqa: E402
from fimex_monitor.parse import (  # noqa: E402
    check_format,
    export_time_from_filename,
    parse_workbook,
)
from fimex_monitor.rules import Thresholds, find_gaps, group, norm  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("file", type=Path)
    args = ap.parse_args(argv)

    data = args.file.read_bytes()
    when = export_time_from_filename(args.file) or datetime.fromtimestamp(args.file.stat().st_mtime, timezone.utc)
    export_date = when.astimezone(Config().tz).date()
    r = parse_workbook(data, export_date)
    products = group(r.offers)
    positions = [p for prod in products.values() for p in prod.positions]
    gaps = find_gaps(products, Thresholds())

    print(f"Файл: {args.file.name}, {len(data)} байт, sha256 {hashlib.sha256(data).hexdigest()}")
    print(f"Дата выгрузки (Дубай): {export_date}")
    print(f"Листов: {len(r.sheets)} ({', '.join(r.sheets)})")
    print(f"Предложений: {len(r.offers)} — " + ", ".join(f"{s} {n}" for s, n in r.offers_by_sheet.items()))
    print(f"Строк названий: {r.title_rows}")
    print(f"Уникальных товаров: {len(products)}")
    print(f"Позиций (товар + регион): {len(positions)}")
    print(f"Цен строкой: {r.price_from_string}")
    if r.offers:
        prices = [o.price_usd for o in r.offers]
        print(f"Цена: мин {min(prices)}, макс {max(prices)}")
    print(f"Строк не по формату: {len(r.bad_rows)}")
    for b in r.bad_rows[:20]:
        print(f"    {b.sheet}!{b.row}: {b.reason}")
    print(f"ETA не разобран: {r.eta_unparsed}")
    etas = Counter(o.eta_raw for o in r.offers)
    if etas:
        top_eta, n = etas.most_common(1)[0]
        print(f"Самый частый ETA: {top_eta} — {n * 100 / len(r.offers):.0f} % строк")
    regions = Counter(o.region for o in r.offers)
    print(f"Разных регионов: {len(regions)}: " + " ".join(f"{k}×{v}" for k, v in regions.most_common()))
    problems = check_format(r, None)
    print("Проверка формата: " + ("; ".join(problems) if problems else "ок"))

    multi = [p for p in products.values() if len(p.positions) > 1]
    by_brand = Counter(p.positions[0].brand for p in multi)
    print(f"\nТоваров с несколькими регионами: {len(multi)} (" + ", ".join(f"{b} {n}" for b, n in by_brand.items()) + ")")
    several = [p for p in products.values() if any(len(pos.offers) > 1 for pos in p.positions)]
    print(f"Товаров с несколькими строками в одном регионе: {len(several)}")

    print("\nНазвания, которые встречаются на листе в нескольких блоках (объединяются в один товар):")
    blocks = defaultdict(list)
    for sheet, row, title in r.titles:
        blocks[(sheet.strip(), norm(title))].append(row)
    dup = {k: rows for k, rows in blocks.items() if len(rows) > 1}
    if not dup:
        print("    нет")
    for (brand, tkey), rows in dup.items():
        product = products.get((norm(brand), tkey))
        title = product.positions[0].title if product else tkey
        print(f"  {brand} · {title} — блоки в строках {', '.join(map(str, rows))}")
        if product:
            for o in sorted(product.offers, key=lambda o: o.sheet_row):
                print(f"      строка {o.sheet_row}: {o.region} {money(o.price_usd)} · {o.article} · {o.eta_raw} · {o.qty} шт")

    print(f"\nРазрыв между регионами (5 % или $100): {len(gaps)} позиций")
    for g in sorted(gaps.values(), key=lambda g: -g.gap_abs)[:10]:
        p = g.position
        print(f"    {p.brand} · {p.title} · {p.region} {money(p.price)} — дешевле {g.nearest.region} "
              f"{money(g.nearest.price)} на {money(g.gap_abs)} · {pct(g.gap_pct)} %")
    return 0


if __name__ == "__main__":
    sys.exit(main())
