"""Правила алертов: прошлое состояние + предложения → алерты и новое состояние.

Чистые функции: ни сети, ни базы. Разделы 5 и 6 ТЗ.

Допущения из раздела 12 ТЗ, каждое — в одном месте:
1. алерт только на падение цены — `_is_drop`;
2. разрыв считается от ближайшего по цене другого региона и пишется только
   о дешёвой стороне, при появлении и по вине самой позиции — `find_gaps`, `_gap_alert`;
3. ETA любой — фильтр `ETA_MAX_DAYS` выключен по умолчанию (`apply_filters`);
4. «второе предложение» — самая дешёвая другая строка товара в любом регионе — `second_offer`;
5. сравнение с ценой прошлого цикла, а не с максимумом за день — `PrevPosition.price`;
6. количество на алерт не влияет — нигде не участвует в правилах;
7. блоки с одинаковым названием на листе — один товар — `product_key`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable, Mapping

from .parse import Offer

RULE_DROP = "A"
RULE_GAP = "B"

ProductKey = tuple[str, str]  # (бренд, название) в нормализованном виде
PositionKey = tuple[str, str, str]  # (бренд, название, регион)


@dataclass(frozen=True)
class Thresholds:
    drop_pct: Decimal = Decimal(5)
    drop_abs: Decimal = Decimal(100)
    gap_pct: Decimal = Decimal(5)
    gap_abs: Decimal = Decimal(100)
    baseline_max_age: timedelta = timedelta(hours=24)
    cooldown: timedelta = timedelta(hours=12)


def norm(text: str) -> str:
    """Ключ для названия: края обрезаны, пробелы схлопнуты, регистр сложен."""
    return " ".join(text.split()).casefold()


def product_key(offer: Offer) -> ProductKey:
    return norm(offer.brand), norm(offer.title)


def position_key(offer: Offer) -> PositionKey:
    return norm(offer.brand), norm(offer.title), offer.region


def _offer_rank(offer: Offer) -> tuple[Decimal, int]:
    # дешевле — лучше; при равной цене — где больше штук
    return offer.price_usd, -offer.qty


@dataclass
class Position:
    key: PositionKey
    brand: str
    title: str
    region: str
    offers: list[Offer]

    @property
    def best(self) -> Offer:
        return min(self.offers, key=_offer_rank)

    @property
    def price(self) -> Decimal:
        return self.best.price_usd


@dataclass
class Product:
    key: ProductKey
    offers: list[Offer]
    positions: list[Position]  # по возрастанию цены


@dataclass(frozen=True)
class Gap:
    """Разрыв у самого дешёвого региона товара относительно ближайшего по цене."""
    position: Position
    nearest: Position
    gap_abs: Decimal
    gap_pct: Decimal


@dataclass(frozen=True)
class PrevPosition:
    price: Decimal  # последняя известная цена
    seen_at: datetime  # когда её видели
    had_gap: bool  # был ли разрыв по правилу B в прошлом цикле


@dataclass
class Alert:
    position: Position
    rules: tuple[str, ...]
    prev: PrevPosition | None  # None — позиция новая (или база устарела и прошлой цены нет)
    gap: Gap | None  # текущий разрыв позиции — для строки «Другой флаг»
    second: Offer | None
    strength: Decimal  # падение или разрыв в долларах — для сортировки и потолка

    @property
    def key(self) -> PositionKey:
        return self.position.key

    @property
    def price(self) -> Decimal:
        return self.position.price


@dataclass
class Evaluation:
    first_run: bool
    products: dict[ProductKey, Product]
    positions: dict[PositionKey, Position]
    gaps: dict[PositionKey, Gap]
    alerts: list[Alert]  # от сильного к слабому
    suppressed: list[tuple[PositionKey, str]] = field(default_factory=list)


@dataclass(frozen=True)
class PositionUpdate:
    key: PositionKey
    brand: str
    title: str
    region: str
    price: Decimal
    seen_at: datetime
    had_gap: bool


def apply_filters(
    offers: Iterable[Offer],
    export_date: date,
    eta_max_days: int | None,
    min_price: Decimal,
) -> list[Offer]:
    """ETA_MAX_DAYS и MIN_PRICE_USD — до группировки (раздел 5)."""
    out = []
    latest = export_date + timedelta(days=eta_max_days) if eta_max_days is not None else None
    for o in offers:
        if o.price_usd < min_price:
            continue
        if latest is not None and (o.eta_date is None or o.eta_date > latest):
            continue
        out.append(o)
    return out


def group(offers: Iterable[Offer]) -> dict[ProductKey, Product]:
    products: dict[ProductKey, Product] = {}
    positions: dict[PositionKey, Position] = {}
    for o in offers:
        pk = product_key(o)
        product = products.get(pk)
        if product is None:
            product = products[pk] = Product(pk, [], [])
        product.offers.append(o)
        key = position_key(o)
        pos = positions.get(key)
        if pos is None:
            pos = positions[key] = Position(key, o.brand, o.title, o.region, [])
            product.positions.append(pos)
        pos.offers.append(o)
    for product in products.values():
        product.positions.sort(key=lambda p: _offer_rank(p.best))
    return products


def find_gaps(products: Mapping[ProductKey, Product], th: Thresholds) -> dict[PositionKey, Gap]:
    gaps: dict[PositionKey, Gap] = {}
    for product in products.values():
        if len(product.positions) < 2:
            continue
        cheapest, nearest = product.positions[0], product.positions[1]
        p1, p2 = cheapest.price, nearest.price
        gap_abs = p2 - p1
        if gap_abs <= 0:
            continue
        gap_pct = gap_abs / p2 * 100
        if gap_abs >= th.gap_abs or gap_pct >= th.gap_pct:
            gaps[cheapest.key] = Gap(cheapest, nearest, gap_abs, gap_pct)
    return gaps


def second_offer(product: Product, position: Position) -> Offer | None:
    """Самая дешёвая строка товара, кроме лучшей строки самой позиции; регион любой."""
    best = position.best
    rest = [o for o in product.offers if o is not best]
    return min(rest, key=_offer_rank) if rest else None


def _is_drop(prev: Decimal, now: Decimal, th: Thresholds) -> bool:
    drop_abs = prev - now
    if drop_abs <= 0:
        return False  # рост и неизменная цена — не алерт
    return drop_abs >= th.drop_abs or drop_abs / prev * 100 >= th.drop_pct


def _gap_alert(gap: Gap | None, prev: PrevPosition | None, fresh: bool, now_price: Decimal) -> bool:
    """Правило B: разрыв есть сейчас, его не было в прошлом цикле, и он возник из-за самой позиции."""
    if gap is None:
        return False
    if prev is not None and prev.had_gap:
        return False
    return not fresh or now_price < prev.price


def evaluate(
    offers: Iterable[Offer],
    prev: Mapping[PositionKey, PrevPosition],
    alerted: Mapping[tuple[PositionKey, str], Decimal],
    now: datetime,
    th: Thresholds,
    first_run: bool,
) -> Evaluation:
    """Посчитать алерты по текущей выгрузке.

    prev    — последнее известное состояние позиций;
    alerted — минимальная цена, о которой уже писали по (позиция, правило) за окно cooldown.
    """
    products = group(offers)
    positions = {p.key: p for prod in products.values() for p in prod.positions}
    gaps = find_gaps(products, th)
    ev = Evaluation(first_run, products, positions, gaps, [])
    if first_run:
        return ev

    for product in products.values():
        for pos in product.positions:
            p = prev.get(pos.key)
            fresh = p is not None and now - p.seen_at <= th.baseline_max_age
            gap = gaps.get(pos.key)
            rules: list[str] = []
            if fresh and _is_drop(p.price, pos.price, th):
                rules.append(RULE_DROP)
            if _gap_alert(gap, p, fresh, pos.price):
                rules.append(RULE_GAP)
            for rule in list(rules):
                already = alerted.get((pos.key, rule))
                if already is not None and pos.price >= already:
                    rules.remove(rule)
                    ev.suppressed.append((pos.key, rule))
            if not rules:
                continue
            strength = max(
                (p.price - pos.price) if RULE_DROP in rules else Decimal(0),
                gap.gap_abs if RULE_GAP in rules else Decimal(0),
            )
            ev.alerts.append(Alert(
                position=pos, rules=tuple(rules), prev=p if fresh else None, gap=gap,
                second=second_offer(product, pos), strength=strength,
            ))

    ev.alerts.sort(key=lambda a: (-a.strength, a.position.brand, a.position.title, a.position.region))
    return ev


def split_by_cap(alerts: list[Alert], cap: int) -> tuple[list[Alert], list[Alert]]:
    """Самые сильные — отдельными сообщениями, остальные — одним сводным."""
    if len(alerts) <= cap:
        return alerts, []
    return alerts[:cap], alerts[cap:]


def next_state(
    prev: Mapping[PositionKey, PrevPosition],
    ev: Evaluation,
    unsent: set[PositionKey],
    now: datetime,
) -> tuple[list[PositionUpdate], list[PositionKey]]:
    """Новое состояние после цикла.

    Позиции, чей алерт не ушёл в Telegram, не трогаем: в следующем цикле
    сравнение повторится и алерт не потеряется. У пропавших позиций
    последняя цена остаётся, а флаг разрыва сбрасывается — в этом цикле их не было.
    """
    updates = [
        PositionUpdate(key, pos.brand, pos.title, pos.region, pos.price, now, key in ev.gaps)
        for key, pos in ev.positions.items()
        if key not in unsent
    ]
    clear_gap = [k for k, p in prev.items() if p.had_gap and k not in ev.positions]
    return updates, clear_gap
