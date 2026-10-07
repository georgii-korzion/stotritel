"""Этап 5 ТЗ: стабильны ли 11-значные цифровые артикулы между выгрузками.

    python tools/compare_articles.py samples/fimex-pricelist-20261007T205720Z.xlsx data-test/raw/fimex-pricelist-….xlsx

Сравниваются позиции (товар + регион), которые есть в обоих файлах. Сеть не используется.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fimex_monitor.config import Config  # noqa: E402
from fimex_monitor.parse import export_time_from_filename, parse_workbook  # noqa: E402
from fimex_monitor.rules import group  # noqa: E402

NUMERIC_11 = re.compile(r"^\d{11}$")


def articles(path: Path) -> dict:
    when = export_time_from_filename(path) or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    offers = parse_workbook(path.read_bytes(), when.astimezone(Config().tz).date()).offers
    return {pos.key: (pos, {o.article for o in pos.offers})
            for prod in group(offers).values() for pos in prod.positions}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("old", type=Path)
    ap.add_argument("new", type=Path)
    args = ap.parse_args(argv)
    old, new = articles(args.old), articles(args.new)
    common = old.keys() & new.keys()
    numeric = [k for k in common if any(NUMERIC_11.match(a) for a in old[k][1] | new[k][1])]
    same = [k for k in numeric if old[k][1] == new[k][1]]
    diff = [k for k in numeric if old[k][1] != new[k][1]]
    print(f"Позиций: в старом {len(old)}, в новом {len(new)}, общих {len(common)}")
    print(f"Общих позиций с 11-значными артикулами: {len(numeric)}")
    print(f"  артикулы совпадают: {len(same)}")
    print(f"  артикулы отличаются: {len(diff)}")
    for k in sorted(diff)[:30]:
        pos = new[k][0]
        print(f"    {pos.brand} · {pos.title} · {pos.region}: {sorted(old[k][1])} → {sorted(new[k][1])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
