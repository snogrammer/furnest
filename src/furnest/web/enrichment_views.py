"""What enrichment found, shaped for the property page: the price comparison table, the
price history chart (server-rendered SVG, no JS) and the risk flags."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from markupsafe import Markup, escape
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.models import EnrichmentResult, RiskFlag
from furnest.db.models.enrichment import ALTERNATIVE
from furnest.db.property_store import PropertyPricePoint
from furnest.db.session import session_scope
from furnest.domain import Money

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class ComparisonRow:
    source: str
    title: str
    url: str
    price: Money | None
    # Signed difference from this property's price, as "+$100.00"; None if unknown.
    difference: str | None
    cheaper: bool | None
    score_pct: int


@dataclass(frozen=True)
class RiskItem:
    kind: str
    severity: str
    summary: str


@dataclass(frozen=True)
class EnrichmentView:
    alternatives: list[ComparisonRow]
    risks: list[RiskItem]


def _difference(minor: object, currency: str | None) -> str | None:
    if not isinstance(minor, int) or isinstance(minor, bool) or currency is None or minor == 0:
        return None
    try:
        amount = Money(amount_minor=abs(minor), currency=currency)
    except ValueError:
        return None
    return f"{'+' if minor > 0 else '-'}{amount.format()}"


def load_enrichment(factory: sessionmaker[Session], property_id: int) -> EnrichmentView:
    with session_scope(factory) as session:
        results = session.scalars(
            select(EnrichmentResult)
            .where(
                EnrichmentResult.property_id == property_id,
                EnrichmentResult.kind == ALTERNATIVE,
            )
            .order_by(EnrichmentResult.score.desc(), EnrichmentResult.id)
        ).all()
        flags = session.scalars(select(RiskFlag).where(RiskFlag.property_id == property_id)).all()
        alternatives = []
        for r in results:
            price = None
            if r.monthly_price_minor is not None and r.currency is not None:
                try:
                    price = Money(amount_minor=r.monthly_price_minor, currency=r.currency)
                except ValueError:
                    price = None
            alternatives.append(
                ComparisonRow(
                    source=r.source,
                    title=r.title,
                    url=r.url,
                    price=price,
                    difference=_difference(r.data.get("price_difference_minor"), r.currency),
                    cheaper=r.cheaper,
                    score_pct=round(r.score * 100),
                )
            )
        risks = sorted(
            (RiskItem(f.kind, f.severity, f.summary) for f in flags),
            key=lambda i: (_SEVERITY_RANK.get(i.severity, 3), i.kind),
        )
    return EnrichmentView(alternatives=alternatives, risks=risks)


def top_severity(risks: list[RiskItem]) -> str | None:
    if not risks:
        return None
    return min((r.severity for r in risks), key=lambda s: _SEVERITY_RANK.get(s, 3))


_W, _H = 640, 240
_LEFT, _RIGHT, _TOP, _BOTTOM = 72, 16, 16, 36


def price_chart(points: list[PropertyPricePoint]) -> Markup | None:
    """A step-line SVG of the monthly price over time (``points`` oldest first), in
    the currency of the latest price. None with fewer than two comparable points."""
    if not points:
        return None
    currency = points[-1].price.monthly.currency
    monthly = [(p.observed_at, p.price.monthly) for p in points]
    series = [(at, m) for at, m in monthly if m.currency == currency]
    if len(series) < 2:
        return None
    t0 = series[0][0].timestamp()
    span = series[-1][0].timestamp() - t0
    lo = min(m.amount_minor for _, m in series)
    hi = max(m.amount_minor for _, m in series)
    vspan = hi - lo
    pw, ph = _W - _LEFT - _RIGHT, _H - _TOP - _BOTTOM

    def x(at: float) -> float:
        return _LEFT + (pw / 2 if span == 0 else (at - t0) / span * pw)

    def y(minor: int) -> float:
        return _TOP + (ph / 2 if vspan == 0 else (hi - minor) / vspan * ph)

    coords = [(x(at.timestamp()), y(m.amount_minor)) for at, m in series]
    # Step line: a listing's price holds until the next change.
    path = f"M{coords[0][0]:.1f},{coords[0][1]:.1f}"
    for (_, py), (cx, cy) in pairwise(coords):
        path += f" H{cx:.1f}"
        if cy != py:
            path += f" V{cy:.1f}"
    first, last = series[0], series[-1]
    low, high = Money(amount_minor=lo, currency=currency), Money(amount_minor=hi, currency=currency)
    desc = (
        f"Monthly price from {first[1].format()} on {first[0]:%Y-%m-%d} "
        f"to {last[1].format()} on {last[0]:%Y-%m-%d}; "
        f"lowest {low.format()}, highest {high.format()}."
    )
    parts = [
        f'<svg class="price-chart" viewBox="0 0 {_W} {_H}" role="img" '
        f'aria-labelledby="price-chart-title price-chart-desc" xmlns="http://www.w3.org/2000/svg">',
        '<title id="price-chart-title">Price history</title>',
        f'<desc id="price-chart-desc">{escape(desc)}</desc>',
        f'<line class="axis" x1="{_LEFT}" y1="{_TOP + ph}" x2="{_W - _RIGHT}" y2="{_TOP + ph}"/>',
        f'<line class="axis" x1="{_LEFT}" y1="{_TOP}" x2="{_LEFT}" y2="{_TOP + ph}"/>',
    ]
    for minor in (hi, lo) if vspan else (hi,):
        ypos = y(minor)
        label = Money(amount_minor=minor, currency=currency).format()
        parts.append(
            f'<text class="tick" x="{_LEFT - 6}" y="{ypos + 4:.1f}" text-anchor="end">'
            f"{escape(label)}</text>"
        )
    for at, anchor, xpos in (
        (first[0], "start", _LEFT),
        (last[0], "end", _W - _RIGHT),
    ):
        parts.append(
            f'<text class="tick" x="{xpos}" y="{_H - 12}" text-anchor="{anchor}">'
            f"{at:%Y-%m-%d}</text>"
        )
    parts.append(f'<path class="line" d="{path}" fill="none"/>')
    for (cx, cy), (at, m) in zip(coords, series, strict=True):
        parts.append(
            f'<circle class="dot" cx="{cx:.1f}" cy="{cy:.1f}" r="3.5">'
            f"<title>{escape(m.format())}/mo on {at:%Y-%m-%d}</title></circle>"
        )
    parts.append("</svg>")
    # Every dynamic value above is escaped or numeric.
    return Markup("".join(parts))  # noqa: S704
