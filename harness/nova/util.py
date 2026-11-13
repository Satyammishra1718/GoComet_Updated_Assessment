"""Small shared helpers: money, rounding, dates, text normalisation."""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
ZERO = Decimal("0")

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})


def D(x) -> Decimal:
    """Decimal from a printed number ("1,234.56", "1 234.56", 12, "12.5%")."""
    if isinstance(x, Decimal):
        return x
    s = str(x).strip().replace(",", "").replace("%", "").replace(" ", "")
    try:
        return Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"not a number: {x!r}") from e


def r2(x: Decimal) -> Decimal:
    """Round half up to cents, ties away from zero (the only rounding the glossary allows)."""
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def money(x) -> str:
    return f"{r2(D(x)):.2f}"


def norm(s: str) -> str:
    return " ".join(str(s).split())


def squash(s: str) -> str:
    """Lower case, no spaces: compares labels the way OCR prints them ("Deliveryfee" == "Delivery fee")."""
    return re.sub(r"\s+", "", str(s)).lower()


# ---- dates ------------------------------------------------------------------------------------------------------------
ISO = r"\d{4}-\d{2}-\d{2}"
SLASH = r"\d{1,2}/\d{1,2}/\d{4}"
DMY_TEXT = r"\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]* \d{4}"
MDY_TEXT = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]* \d{1,2},? \d{4}"
DATE_RE = re.compile(rf"(?<![\d/])(?:{ISO}|{SLASH}|{DMY_TEXT}|{MDY_TEXT})(?![\d/])", re.I)


def parse_date(s: str, slash_order: str = "DMY") -> date | None:
    s = s.strip().rstrip(",.")
    try:
        if re.fullmatch(ISO, s):
            return date.fromisoformat(s)
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            d_, mo = (a, b) if slash_order == "DMY" else (b, a)
            return date(y, mo, d_)
        m = re.fullmatch(r"(\d{1,2}) ([A-Za-z]+) (\d{4})", s)
        if m:
            return date(int(m.group(3)), MONTHS[m.group(2).lower()[:3] if m.group(2).lower()[:4] != "sept" else "sep"],
                        int(m.group(1)))
        m = re.fullmatch(r"([A-Za-z]+) (\d{1,2}),? (\d{4})", s)
        if m:
            return date(int(m.group(3)), MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
    except (ValueError, KeyError):
        return None
    return None


def slash_order_of(text: str) -> str:
    """Decide dd/mm vs mm/dd for one document from the dates it prints."""
    dmy = mdy = 0
    for m in re.finditer(r"(?<![\d/])(\d{1,2})/(\d{1,2})/(\d{4})", text):
        a, b = int(m.group(1)), int(m.group(2))
        dmy += a > 12
        mdy += b > 12
    return "MDY" if mdy > dmy else "DMY"


def find_dates(text: str, slash_order: str = "DMY") -> list[tuple[date, int, int]]:
    out = []
    for m in DATE_RE.finditer(text):
        d = parse_date(m.group(0), slash_order)
        if d:
            out.append((d, m.start(), m.end()))
    return out


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def to_date(s) -> date | None:
    if s is None:
        return None
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def quarter_of(d: date) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def quarter_bounds(q: str) -> tuple[date, date]:
    y, n = int(q[:4]), int(q[-1])
    start = date(y, 3 * (n - 1) + 1, 1)
    end_month = 3 * n
    end = date(y, end_month, calendar.monthrange(y, end_month)[1])
    return start, end


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:            # 29 Feb
        return d.replace(year=d.year + n, day=28)


def days(n: int) -> timedelta:
    return timedelta(days=n)


def utc_date(ts: str) -> date:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
