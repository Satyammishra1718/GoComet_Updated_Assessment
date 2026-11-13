"""Documents -> typed facts, by meaning rather than by layout.

Nothing here knows a customer, carrier, lane, person or invoice number. Each parser looks for what a document *is*
(a signed agreement, an amendment, a bill, a waiver, a tracking export...) from the words and shapes any such document
carries, and returns plain dicts. Every fact keeps a `src` = {doc, page, quote} where `quote` is text copied from that
page, so evidence can always be cited.

Invoice lines are split by arithmetic, not by column position: on a line, the quantity x rate = amount (or base x
percent = amount) triple that adds up is the one that is used, so reordered or renamed columns still parse.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from .util import (DATE_RE, D, find_dates, norm, parse_date, r2, slash_order_of, squash)

# ---- shared patterns --------------------------------------------------------------------------------------------------
AWB_RE = re.compile(r"(?<![A-Za-z0-9\-])([A-Z]{2,4}\d{6,11}(?:-H\d{1,3})?)(?![A-Za-z0-9])")
LANE_RE = re.compile(r"(?<![A-Za-z0-9\-])([A-Z]{2,4}\d{1,2}-[A-Z]{2,4}\d{1,2})(?![A-Za-z0-9\-])")
CONTRACT_ID_RE = re.compile(r"\b([A-Z]{2,6}(?:-[A-Z0-9]{1,6}){2,5})\b")
NUM_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
                    r"(\s?%|\s?(?:kgs?|shpts?|shipments?|days?|months?|fees?|pcs|pieces|units?)\b)?", re.I)
CURRENCIES = ("USD", "INR", "EUR", "GBP", "SGD", "AED", "CNY", "JPY")
BAND_RE = r"(?:minimum charge|min(?:imum)?\.? charge|\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?\s*kg|\d+(?:\.\d+)?\s*\+\s*kg)"
RATE_ROW_RE = re.compile(rf"^([A-Z]{{2,4}}\d{{1,2}}-[A-Z]{{2,4}}\d{{1,2}})\s+({BAND_RE})\s*[:=]?\s*"
                         rf"(?:(?:USD|INR|EUR|GBP)\s*)?(\d[\d,]*\.\d+)((?:\s+\d[\d,]*\.\d+)*)\s*(USD|INR|EUR|GBP)?\s*$",
                         re.I)
INJECTION_RE = re.compile(r"(\bAI\b|artificial intelligence|automated (?:system|auditor|reader|agent|tool)s?|"
                          r"language model|\bLLM\b|\bassistant\b|ignore (?:all |any )?(?:previous|prior|above) "
                          r"instructions|system prompt|auditing (?:software|bot)|\bbots?\b)", re.I)

# Built-in charge vocabulary. Each contract's Annex E adds its own printed labels on top (see Aliases).
DEFAULT_ALIASES = {
    "FRT": ["air freight", "express freight", "freight charge", "airfreight", "freight"],
    "FUEL": ["fuel surcharge", "fuel adj", "fsc", "fuel"],
    "SEC": ["security surcharge", "security screening", "screening fee", "screening", "security"],
    "DG": ["dangerous goods fee", "dangerous goods", "dg handling", "hazmat handling", "dg fee", "hazmat"],
    "DRYICE": ["dry ice handling fee", "dry ice handling", "dry ice"],
    "STOR": ["warehouse storage", "storage", "demurrage"],
    "DLV": ["delivery fee", "door delivery", "final-mile delivery", "final mile delivery", "delivery"],
    "CUSTOMS": ["customs hold fee", "customs examination", "hold handling", "customs hold", "customs"],
    "ACCT": ["monthly account fee", "account maintenance", "account fee"],
    "SPROG": ["storage programme fee", "storage programme", "storage program"],
    "PPROG": ["pickup programme fee", "pickup programme", "pickup program"],
    "PEAK": ["peak season surcharge", "peak season", "pss"],
    "COST": ["carrier cost", "linehaul cost pass-through", "linehaul cost"],
    "MARGIN": ["handling margin", "forwarding margin"],
    "HDL": ["special handling", "supplementary handling"],
    "DUTY": ["import duty", "duty paid on your behalf", "duties", "duty"],
    "TAX": ["sales tax", "gst", "vat", "igst", "cgst", "sgst"],
    "LATE": ["late payment fee", "late fee", "overdue interest", "late charge", "interest on overdue"],
}


class Aliases:
    """Charge label -> charge code, longest label first. Compared without spaces (OCR drops them)."""

    def __init__(self, extra: dict[str, list[str]] | None = None):
        table: dict[str, str] = {}
        for code, names in DEFAULT_ALIASES.items():
            for n in names:
                table[squash(n)] = code
        for code, names in (extra or {}).items():
            for n in names:
                if len(squash(n)) >= 3:
                    table[squash(n)] = code
        self.items = sorted(table.items(), key=lambda kv: -len(kv[0]))

    def code_of(self, text: str) -> tuple[str | None, str | None]:
        s = squash(text)
        for label, code in self.items:
            if label in s:
                return code, label
        return None, None


def src(doc: str, page: int, quote: str) -> dict:
    return {"doc": doc, "page": page, "quote": norm(quote)}


def body_lines(rec: dict) -> tuple[list[str], list[str], str | None]:
    """Email page -> (own lines, quoted lines, quoted author). Header lines are dropped."""
    lines = rec["pages"][0].split("\n")
    try:
        start = lines.index("") + 1
    except ValueError:
        start = 0
    own, quoted, author = [], [], None
    in_quote = False
    for ln in lines[start:]:
        if ln.startswith("Attachments:"):
            continue
        m = re.match(r"^On (.+?), (.+?) wrote:\s*$", ln)
        if m or re.match(r"^-{2,}\s*(Forwarded|Original) message", ln, re.I):
            in_quote = True
            if m:
                author = m.group(2).strip()
            continue
        if ln.startswith(">"):
            quoted.append(ln.lstrip("> ").rstrip())
            continue
        if in_quote:
            fm = re.match(r"^From:\s*(.+?)\s*(?:<.*>)?\s*$", ln)
            if fm and not author:
                author = fm.group(1).strip()
            quoted.append(ln)
        else:
            own.append(ln)
    return own, quoted, author


def ids_in(text: str) -> list[str]:
    """Document numbers: tokens with letters and digits that look like ids (AB-US-2040-0001, ABI/40-41/0001)."""
    out = []
    for m in re.finditer(r"(?<![A-Za-z0-9/\-])([A-Z][A-Z0-9]*(?:[\-/][A-Z0-9]+)+)(?![A-Za-z0-9/\-])", text):
        t = m.group(1)
        if any(c.isdigit() for c in t) and not LANE_RE.fullmatch(t) and not AWB_RE.fullmatch(t):
            out.append(t)
    return out


def norm_invoice_id(s: str) -> str:
    return re.sub(r"[\s#:]", "", str(s)).upper()


# ======================================================================================================================
# Contracts and amendments
# ======================================================================================================================
def _rate_rows(text: str, doc: str, page: int) -> list[dict]:
    rows = []
    for ln in text.split("\n"):
        m = RATE_ROW_RE.match(ln.strip())
        if not m:
            continue
        band = m.group(2).lower().replace(" ", "")
        rates = [D(m.group(3))] + [D(x) for x in m.group(4).split()]
        if "min" in band:
            lo, hi, kind = None, None, "min"
        elif "+" in band:
            lo, hi, kind = D(band.split("+")[0]), None, "band"
        else:
            a, b = band.replace("kg", "").split("-")
            lo, hi, kind = D(a), D(b), "band"
        rows.append({"lane": m.group(1).upper(), "kind": kind, "lo": lo, "hi": hi, "rates": rates,
                     "currency": (m.group(5) or "").upper() or None, "src": src(doc, page, ln)})
    return rows


def _flat_pages(rec: dict) -> list[str]:
    return [norm(p) for p in rec["pages"]]


def _clause(flat_pages: list[str], pattern: str) -> tuple[re.Match | None, int]:
    for i, p in enumerate(flat_pages):
        m = re.search(pattern, p, re.I)
        if m:
            return m, i + 1
    return None, 0


def _sentence(flat: str, start: int, end: int) -> str:
    """The clause around a match, cut at the next clause number, as it appears on the page (for quotes)."""
    a = flat.rfind(". ", 0, start)
    a = 0 if a < 0 else a + 2
    m = re.search(r"\s\d{1,2}\.\d{1,2} [A-Z]|\s\d{1,2} [A-Z][a-z]+(?: [a-z]+)?\.? [A-Z]|$", flat[end:])
    b = end + (m.start() if m else 0)
    return flat[a:b].strip()


def parse_contract(rec: dict) -> dict | None:
    pages = rec["pages"]
    full = "\n".join(pages)
    if not re.search(r"Master Services Agreement|Services Agreement|Agreement No\.", full) or \
            not re.search(r"Schedule 1|Authori[sz]ed Persons", full):
        return None
    if re.search(r"^Amendment No\.", full, re.M) and not re.search(r"Agreement No\.", full):
        return None
    flat = _flat_pages(rec)
    doc = rec["doc"]
    m = re.search(r"Agreement No\.?\s*([A-Z0-9][A-Z0-9\-/]+)", full)
    cid = m.group(1) if m else None
    if not cid:
        return None
    c: dict = {"type": "contract", "contract_id": cid, "doc": doc, "signed": bool(re.search(r"Signature", full)),
               "sources": {}}

    def grab(name, pattern, conv=lambda m: m.group(1)):
        m, pg = _clause(flat, pattern)
        if m:
            c[name] = conv(m)
            c["sources"][name] = src(doc, pg, _sentence(flat[pg - 1], m.start(), m.end()))
        return m

    grab("effective", r"(?:Effective [Dd]ate\.?\s[^.]*?|takes effect on )(\d{4}-\d{2}-\d{2})",
         lambda m: date.fromisoformat(m.group(1)))
    grab("end", r"Initial term\.[^.]*?(\d{4}-\d{2}-\d{2})", lambda m: date.fromisoformat(m.group(1)))
    m, pg = _clause(flat, r"(?:Auto-?renewal|Renewal)\.\s*([^.]*\.)")
    c["auto_renew"] = False
    if m:
        txt = m.group(1)
        if re.search(r"renew", txt, re.I) and not re.search(r"does not|shall not|will not|no automatic", txt, re.I):
            c["auto_renew"] = True
            nm, npg = _clause(flat, r"(?:Auto-?renewal|Renewal)\.[^.]*\.[^.]*?(\d+)\s*days?'?\s*(?:prior |written )?notice")
            nm = nm or re.search(r"(\d+)\s*days", txt)
            if nm:
                c["notice_days"] = int(nm.group(1))
        c["sources"]["renewal"] = src(doc, pg, _sentence(flat[pg - 1], m.start(), m.end()))
    grab("pay_days", r"(?:due|payable) (?:within )?(\d+) days (?:after|from|of)", lambda m: int(m.group(1)))
    lf = grab("late_rate", r"late fee of ([\d.]+)%", lambda m: D(m.group(1)))
    if lf:
        g, _ = _clause(flat, r"(?:no fee is due if days_late <= (\d+))|(?:more than (\d+) days? (?:after|past))")
        c["grace"] = int((g.group(1) or g.group(2))) if g else 0
    m, pg = _clause(flat, r"Compounding\.\s*[^.]*\.")
    c["compound"] = bool(m and re.search(r"principal plus|added to the overdue|compound", m.group(0), re.I)
                         and not re.search(r"not compound|simple", m.group(0), re.I))
    if not m:
        m2, _ = _clause(flat, r"simple interest|does not compound|not compounded")
        c["compound"] = False if m2 else True
    grab("storage_free", r"Storage days = [^.]*?- (\d+) free days", lambda m: int(m.group(1)))
    # rebate tiers
    m, pg = _clause(flat, r"Volume rebate\.\s*(.*?)(?:\d+\.\d+ [A-Z][a-z]+\.|$)")
    if m and not re.search(r"Not applicable", m.group(1)[:40], re.I):
        tiers = [(D(a.replace(",", "")), D(b)) for a, b in
                 re.findall(r"(?:QS|spend)\s*>=?\s*(?:USD\s*)?([\d,]+(?:\.\d+)?)\s*:\s*([\d.]+)%", m.group(1))]
        if tiers:
            c["rebate_tiers"] = sorted(tiers)
            c["sources"]["rebate"] = src(doc, pg, _sentence(flat[pg - 1], m.start(), m.start() + 30))
    grab("rebate_days", r"(?:Rebate credit|rebate credit notes)[^.]*?(\d+) days after (?:the end of each )?Quarter",
         lambda m: int(m.group(1)))
    m, pg = _clause(flat, r"Annual improvement credit\.\s*([^.]*?(?:\d+\.\d+)?[^.]*?)\.(?: \d| [A-Z])")
    if m and not re.search(r"Not applicable", m.group(1)[:40], re.I):
        pm = re.search(r"([\d.]+)% of", m.group(1))
        dm = re.search(r"(\d+) days after each anniversary", m.group(1))
        if pm:
            c["improve_pct"] = D(pm.group(1))
            c["improve_days"] = int(dm.group(1)) if dm else 60
            c["sources"]["improve"] = src(doc, pg, _sentence(flat[pg - 1], m.start(), m.start() + 30))
    m, pg = _clause(flat, r"Margin step-down\.\s*([^.]*)\.")
    if m and not re.search(r"Not applicable", m.group(1), re.I):
        c["margin_step_down"] = m.group(1)        # recognised, not priced (see DESIGN.md)
    u, upg = _clause(flat, r"neither prevails automatically|does not rank correspondence")
    c["unranked_notices"] = bool(u)
    if u:
        c["sources"]["unranked"] = src(doc, upg, _sentence(flat[upg - 1], u.start(), u.end()))
    m, pg = _clause(flat, r"Pricing date\.[^.]*\.")
    if m:
        c["sources"]["pricing"] = src(doc, pg, m.group(0))
    for name, pat in (("dg", r"Dangerous goods fee\.[^.]*\."), ("dryice", r"Dry ice\.[^.]*\.[^.]*\."),
                      ("mixed", r"Mixed shipments\.[^.]*\."), ("delivery", r"Delivery\. The delivery fee[^.]*\."),
                      ("customs", r"Customs holds\.[^.]*\."), ("storage", r"Storage\. Storage days[^.]*\."),
                      ("waiver", r"Waiver authority\.[^.]*\."), ("waiver_scope", r"Scope of waiver\.[^.]*\."),
                      ("extension", r"Extension of due date\.[^.]*\."), ("late", r"Late fee\.[^.]*\."),
                      ("precedence", r"Precedence\.[^.]*\."), ("qs", r"Qualifying Spend\.[^.]*\.")):
        m, pg = _clause(flat, pat)
        if m:
            c["sources"][name] = src(doc, pg, m.group(0))

    # annexes and Schedule 1, line by line with the current section
    c.update({"periods": [], "rates": [], "fuel": {}, "fees": {}, "aliases": {}, "fx": {}, "schedule1": [],
              "rate_currency": None, "fx_na": False})
    section = None
    for pi, page in enumerate(pages, 1):
        lines = page.split("\n")
        for li, ln in enumerate(lines):
            s = ln.strip()
            hm = re.match(r"^(Annex [A-Z]|Schedule \d+)\b[:.]?\s*(.*)$", s)
            if hm and not re.match(r"^Annex [A-Z] \S+ \d+$", s):
                section = hm.group(1)
                rest = hm.group(2)
                if section == "Annex A":
                    cm = re.search(r"\((USD|INR|EUR|GBP)", rest)
                    c["rate_currency"] = cm.group(1) if cm else c["rate_currency"]
                if section == "Annex F" and re.search(r"Not applicable", rest + " " + " ".join(lines[li + 1:li + 2]),
                                                      re.I):
                    c["fx_na"] = True
                continue
            if section == "Annex A":
                pm = re.match(r"^Period (\S+): pickups (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})", s)
                if pm:
                    c["periods"].append({"name": pm.group(1), "start": date.fromisoformat(pm.group(2)),
                                         "end": date.fromisoformat(pm.group(3)), "src": src(doc, pi, s)})
                    continue
                c["rates"] += _rate_rows(s, doc, pi)
            elif section == "Annex C":
                fm = re.match(r"^(\d{4}-\d{2})\s+([\d.]+)\s*%$", s)
                if fm:
                    c["fuel"][fm.group(1)] = {"pct": D(fm.group(2)), "src": src(doc, pi, s)}
            elif section == "Annex D":
                dm = re.match(r"^([A-Z][A-Z0-9]{1,9})\s+(.+?)\s+(\d[\d,]*\.\d+)\s+(USD|INR|EUR|GBP)\s+per\s+(.+)$", s)
                if dm:
                    basis = dm.group(5).lower()
                    nxt = lines[li + 1].strip() if li + 1 < len(lines) else ""
                    months = None
                    mm = re.search(r"pickups in (\w+) to (\w+) only", basis + " " + nxt, re.I)
                    if mm:
                        from .util import MONTHS
                        a, b = MONTHS.get(mm.group(1).lower()), MONTHS.get(mm.group(2).lower())
                        if a and b:
                            months = list(range(a, b + 1)) if a <= b else list(range(a, 13)) + list(range(1, b + 1))
                    c["fees"][dm.group(1)] = {"name": dm.group(2), "fee": D(dm.group(3)), "currency": dm.group(4),
                                              "basis": basis, "months": months, "src": src(doc, pi, s)}
            elif section == "Annex E":
                em = re.match(r"^Code ([A-Z0-9]+):\s*(.+?);\s*also billed as:\s*(.+)$", s)
                if em:
                    c["aliases"][em.group(1)] = [em.group(2)] + [x.strip() for x in em.group(3).split(",")]
                    c.setdefault("alias_src", {})[em.group(1)] = src(doc, pi, s)
            elif section == "Annex F":
                xm = re.match(r"^(\d{4}-\d{2})\s+(\d[\d,]*\.\d+)$", s)
                if xm:
                    c["fx"][xm.group(1)] = {"rate": D(xm.group(2)), "src": src(doc, pi, s)}
                elif re.search(r"Annex F update|issued by Treasury", s):
                    c["fx_update_name"] = "Annex F update"
            elif section == "Schedule 1":
                sm = re.match(r"^(.+?),\s*(.+?),\s*([A-Z0-9]+(?:-[A-Z0-9]+)*):\s*(.+?)\s*<([^>]+)>\s*$", s)
                if sm:
                    cats = [x.strip().lower().replace(" ", "_") for x in sm.group(4).split(",")]
                    c["schedule1"].append({"name": sm.group(1).strip(), "title": sm.group(2).strip(),
                                           "party": sm.group(3), "categories": cats, "email": sm.group(5).lower(),
                                           "src": src(doc, pi, s)})
    if "notice_days" in c and c.get("end"):
        from .util import days
        c["notice_deadline"] = c["end"] - days(c["notice_days"])
    return c


def parse_amendment(rec: dict, known_contracts: set[str]) -> dict | None:
    full = "\n".join(rec["pages"])
    if not re.search(r"Amendment No\.|AMENDMENT|Amendment reference", full):
        return None
    if re.search(r"Agreement No\.", full) and re.search(r"Schedule 1", full):
        return None
    flat = _flat_pages(rec)
    doc = rec["doc"]
    ref = re.search(r"Amendment reference\s+(\S+)", full)
    cid = None
    for t in CONTRACT_ID_RE.findall(full):
        if t in known_contracts:
            cid = t
            break
    if not cid and ref:
        cid = ref.group(1).split("/")[0]
    eff = re.search(r"Effective (?:from|date:?)\s*(\d{4}-\d{2}-\d{2})", full)
    a = {"type": "amendment", "doc": doc, "ref": ref.group(1) if ref else doc, "contract_id": cid,
         "effective": date.fromisoformat(eff.group(1)) if eff else None,
         "signed": bool(re.search(r"Signature|Signed for and on behalf", full)), "rates": [], "rev_refs": [],
         "sources": {}}
    if eff:
        m, pg = _clause(flat, r"Effective (?:from|date:?)\s*\d{4}-\d{2}-\d{2}")
        a["sources"]["effective"] = src(doc, pg, m.group(0)) if m else None
    for pi, page in enumerate(rec["pages"], 1):
        a["rates"] += _rate_rows(page, doc, pi)
        for m in re.finditer(r"(Annex [A-Z](?: rev(?:ision)? \d+)?)\s*\(attached\)\s*applies from (\d{4}-\d{2}-\d{2}) "
                             r"to lane\(s\) ([A-Z0-9 ,\-]+?)\.", norm(page)):
            a["rev_refs"].append({"name": m.group(1), "from": date.fromisoformat(m.group(2)),
                                  "lanes": [x.strip() for x in m.group(3).split(",") if x.strip()],
                                  "src": src(doc, pi, m.group(0))})
        for m in re.finditer(r"(?:initial term|term of the Agreement|term)[^.]*?(?:is extended|extended)[^.]*?"
                             r"(?:to|until|ending on|expires on) (\d{4}-\d{2}-\d{2})", norm(page), re.I):
            a["new_end"] = date.fromisoformat(m.group(1))
            a["sources"]["new_end"] = src(doc, pi, m.group(0))
    return a


def parse_rate_annex(rec: dict) -> dict | None:
    """A standalone rates annex (e.g. 'Annex A rev 2'), held as its own document."""
    full = "\n".join(rec["pages"])
    m = re.search(r"(Annex [A-Z](?: rev(?:ision)? \d+)?)", full)
    if not m or re.search(r"Agreement No\.|Amendment reference", full):
        return None
    rows = []
    for pi, page in enumerate(rec["pages"], 1):
        rows += _rate_rows(page, rec["doc"], pi)
    if not rows:
        return None
    return {"type": "rate_annex", "doc": rec["doc"], "name": m.group(1), "rates": rows,
            "contract_ids": CONTRACT_ID_RE.findall(full)}


# ======================================================================================================================
# Bills: invoices, statements, credit notes
# ======================================================================================================================
BILL_WORDS = r"(?:tax invoice|invoice|freight bill|charges advice|statement of account|statement|debit note|bill)"


def _numbers(text: str) -> list[dict]:
    out = []
    for m in NUM_RE.finditer(text):
        raw, unit = m.group(1), (m.group(2) or "").strip().lower()
        out.append({"v": D(raw), "raw": raw, "pct": unit == "%", "unit": "" if unit == "%" else unit,
                    "dec": len(raw.split(".")[1]) if "." in raw else 0, "pos": m.start()})
    return out


def _strip_tokens(text: str, slash: str) -> tuple[str, list[str], list[str], list[date], str | None]:
    awbs = AWB_RE.findall(text)
    lanes = LANE_RE.findall(text)
    dates = [d for d, _, _ in find_dates(text, slash)]
    month_ref = None
    mr = re.search(r"(?<![\d-])(\d{4}-\d{2})(?![\d-])", DATE_RE.sub(" ", text))
    if mr:
        month_ref = mr.group(1)
    t = DATE_RE.sub(" ", text)
    t = re.sub(r"(?<![\d-])\d{4}-\d{2}(?![\d-])", " ", t)
    t = AWB_RE.sub(" ", t)
    t = LANE_RE.sub(" ", t)
    for i in ids_in(t):
        t = t.replace(i, " ")
    return t, awbs, lanes, dates, month_ref


def _solve(nums: list[dict]) -> dict | None:
    """Find quantity x rate = amount (or base x pct = amount) among the numbers of one line."""
    pcts = [n for n in nums if n["pct"]]
    vals = [n for n in nums if not n["pct"]]
    if pcts:
        for p in pcts:
            for a in reversed(vals):
                if a["dec"] not in (0, 2):
                    continue
                for b in vals:
                    if b is a:
                        continue
                    if r2(b["v"] * p["v"] / 100) == a["v"]:
                        return {"base": b["v"], "pct": p["v"], "amount": a["v"], "qty": None, "rate": None,
                                "unit": "%"}
    best = None
    for a in reversed(vals):
        if a["dec"] not in (0, 2):
            continue
        for q in vals:
            if q is a:
                continue
            for r in vals:
                if r is a or r is q:
                    continue
                if r2(q["v"] * r["v"]) != a["v"]:
                    continue
                score = (2 if q["unit"] else 0) + (1 if r["dec"] >= 2 else 0) + (1 if q["pos"] < r["pos"] else 0)
                if best is None or score > best[0]:
                    best = (score, {"qty": q["v"], "unit": q["unit"], "rate": r["v"], "amount": a["v"],
                                    "base": None, "pct": None})
        if best:
            return best[1]
    return None


def parse_line(line: str, aliases: Aliases, slash: str) -> dict | None:
    code, label = aliases.code_of(line)
    if not code:
        return None
    if re.match(r"^\s*(sub ?total|total|net charges|balance due|charges\b|amount due|tax\b)", line, re.I) and \
            code not in ("TAX",):
        return None
    t, awbs, lanes, dates, month_ref = _strip_tokens(line, slash)
    item = {"code": code, "label": label, "text": line, "awb": None, "mawb": None, "lane": lanes[0] if lanes else None,
            "date": dates[0] if dates else None, "month_ref": month_ref}
    mawb = re.search(r"MAWB\s+([A-Z]{2,4}\d{6,11})", line)
    if mawb:
        item["mawb"] = mawb.group(1)
    for a in awbs:
        if not (mawb and a == mawb.group(1)):
            item["awb"] = a
            break
    if code == "LATE":
        ref = re.search(r"(?:on|for)\s+(?:invoice\s+)?([A-Z0-9][A-Z0-9/\-]*\d)", line)
        item["late_ref"] = norm_invoice_id(ref.group(1)) if ref else None
        cur = re.search(r"\b(?:USD|INR|EUR|GBP)\s*([\d,]+\.\d{2})", line)
        principal = D(cur.group(1)) if cur else None
        nums = _numbers(t)
        pct = next((n["v"] for n in nums if n["pct"]), None)
        mo = re.search(r"(\d+)\s*months?\b", t) or re.search(r"month\s+(\d+)", t)
        cands = [n for n in nums if not n["pct"] and n["dec"] == 2 and n["v"] != principal]
        amount = None
        if principal is not None and pct is not None:
            want = r2(principal * pct / 100)
            amount = next((n["v"] for n in cands if n["v"] == want), None)
        if amount is None and cands:
            amount = cands[-1]["v"]
        item.update({"principal": principal, "pct": pct, "months": int(mo.group(1)) if mo else 1,
                     "amount": amount, "qty": None, "rate": None, "base": None, "unit": ""})
        return item if amount is not None else None
    nums = _numbers(t)
    sol = _solve(nums)
    if sol:
        item.update(sol)
        item["parsed"] = True
    else:
        twod = [n for n in nums if n["dec"] == 2 and not n["pct"]]
        if not twod:
            return None
        item.update({"amount": twod[-1]["v"], "qty": None, "rate": None, "base": None, "pct": None, "unit": "",
                     "parsed": False})
        kg = next((n for n in nums if n["unit"].startswith("kg")), None)
        if kg:
            item["qty"], item["unit"] = kg["v"], "kg"
    return item


def _header_dates(text: str, slash: str) -> tuple[date | None, date | None]:
    issue = due = None
    for ln in text.split("\n")[:25]:
        for m in DATE_RE.finditer(ln):
            d = parse_date(m.group(0), slash)
            label = ln[:m.start()].lower()
            label = label.split("|")[-1] if "|" in label else label
            label = re.split(r"\d", label)[-1] if re.search(r"[a-z]", re.split(r"\d", label)[-1]) else label
            if re.search(r"due|payable|settle|pay by|payment by", label):
                due = due or d
            elif re.search(r"date|issued|dated|statement|credit|invoice", label):
                issue = issue or d
    return issue, due


def parse_bill(rec: dict, aliases_for, contract_ids: set[str], llm_lines: list | None = None) -> dict | None:
    """Invoice, monthly statement or credit note. aliases_for(contract_id) -> Aliases."""
    pages = rec["pages"]
    if not pages or rec["kind"] == "eml":
        return None
    head = "\n".join(pages[0].split("\n")[:12])
    full = "\n".join(pages)
    if re.search(r"AIR WAYBILL|DECLARATION FOR|^AWB \| Event", full, re.M):
        return None
    is_credit = bool(re.search(r"credit note|credit memo|CREDIT NOTE", head, re.I))
    hm = re.search(rf"(?i){BILL_WORDS}\s*(?:no\.?|number|ref\.?)?\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-]*\d[A-Z0-9/\-]*)", head)
    cm = re.search(r"(?i)credit (?:note|memo)\s*(?:no\.?|number|ref\.?)?\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-]*\d[A-Z0-9/\-]*)", head)
    if is_credit and cm:
        bid = cm.group(1)
    elif hm:
        bid = hm.group(1)
    else:
        sm = re.search(rf"(?i){BILL_WORDS}\s+(\S+)", rec.get("subject", ""))
        bid = sm.group(1) if sm else None
    if not bid:
        return None
    slash = slash_order_of(full)
    issue, due = _header_dates(pages[0], slash)
    cid = None
    lm = re.search(r"(?i)(?:contract(?: reference| ref\.?| no\.?)?|service agreement|agreement(?: no\.?)?)\s*[:#]?\s*"
                   r"([A-Z0-9][A-Z0-9\-/]{4,})", full)
    if lm:
        cid = lm.group(1)
    if cid not in contract_ids:
        for k in sorted(contract_ids, key=len, reverse=True):
            if k in full or squash(k) in squash(full):
                cid = k
                break
    currency = None
    for ln in full.split("\n"):
        if re.search(r"(?i)\b(total|balance due|amount due|currency|net charges|\(.{0,3}(USD|INR))", ln):
            m = re.search(r"\b(" + "|".join(CURRENCIES) + r")\b", ln) or re.search(r"(" + "|".join(CURRENCIES) + r")",
                                                                                   ln)
            if m:
                currency = m.group(1)
                break
    if not currency:
        counts = {c: full.count(c) for c in CURRENCIES}
        currency = max(counts, key=counts.get) if any(counts.values()) else "USD"
    total = None
    for ln in full.split("\n"):
        if re.match(r"(?i)^\s*(total payable|total credit|balance due|amount due|grand total|total)(?![a-z])", ln) and \
                not re.match(r"(?i)^\s*sub", ln):
            nums = [n for n in _numbers(ln) if n["dec"] == 2]
            if nums:
                total = nums[-1]["v"]
    al = aliases_for(cid)
    lines = []
    for pi, page in enumerate(pages, 1):
        for ln in page.split("\n"):
            it = parse_line(ln, al, slash)
            if it:
                it["page"] = pi
                it["src"] = src(rec["doc"], pi, ln)
                lines.append(it)
    if not lines and llm_lines:
        for x in llm_lines:
            try:
                it = parse_line(" ".join(str(v) for v in (x.get("lane"), x.get("awb"), x.get("charge"),
                                                          x.get("quantity"), x.get("unit"), x.get("rate"),
                                                          x.get("base"), x.get("percent") and f"{x['percent']}%",
                                                          x.get("amount"), x.get("pickup")) if v), al, slash)
            except Exception:  # noqa: BLE001
                it = None
            if it:
                it["page"] = 1
                it["src"] = src(rec["doc"], 1, x.get("text") or "")
                lines.append(it)
    if not lines and total is None:
        return None
    credit = None
    if is_credit:
        tm = re.search(r"(?i)credit type\s*[:|]?\s*([a-z_ ]+?)\s*(?:\||$)", full, re.M)
        pm = re.search(r"(?i)period\s*[:|]?\s*(\d{4}-Q[1-4]|CY\d+)", full) or re.search(r"(\d{4}-Q[1-4]|\bCY\d+\b)",
                                                                                         full)
        ctype = (tm.group(1).strip().lower() if tm else "")
        if not ctype:
            ctype = "rebate" if re.search(r"(?i)rebate", full) else "improvement" if re.search(
                r"(?i)improvement", full) else "other"
        if "rebate" in ctype:
            ctype = "rebate"
        elif "improve" in ctype or "promised" in ctype:
            ctype = "improvement"
        elif "margin" in ctype:
            ctype = "margin_step_down"
        refs = [norm_invoice_id(x) for x in ids_in(full) if norm_invoice_id(x) != norm_invoice_id(bid)
                and x != cid]
        credit = {"credit_type": ctype, "period": pm.group(1) if pm else None, "refs": refs,
                  "awbs": AWB_RE.findall(full)}
        if total is None:
            nums = [n for n in _numbers(full) if n["dec"] == 2]
            total = nums[-1]["v"] if nums else None
    return {"type": "credit_note" if is_credit else "bill", "doc": rec["doc"], "email": rec["email"],
            "invoice_id": norm_invoice_id(bid), "issue": issue, "due": due, "contract_id": cid,
            "currency": currency, "total": total, "lines": lines, "credit": credit, "scan": rec.get("scan", False),
            "received": rec["received"], "statement": bool(re.search(r"(?i)statement", head))}


# ======================================================================================================================
# Records
# ======================================================================================================================
def _header_map(header: list[str], wants: dict[str, str]) -> dict[str, int]:
    out = {}
    for i, h in enumerate(header):
        hl = h.strip().lower()
        for k, pat in wants.items():
            if k not in out and re.search(pat, hl):
                out[k] = i
                break
    return out


def parse_table_records(rec: dict) -> dict | None:
    """Tracking export or AP register / payment ledger, recognised from the header row."""
    for pi, page in enumerate(rec["pages"], 1):
        rows = [r.split(" | ") for r in page.split("\n") if r.strip()]
        for hi, header in enumerate(rows[:5]):
            hm = _header_map(header, {"awb": r"awb|waybill|shipment", "event": r"event|status|milestone",
                                      "date": r"^date|event date|time", "station": r"station|location",
                                      "reason": r"reason"})
            if {"awb", "event", "date"} <= hm.keys():
                events = []
                for r in rows[hi + 1:]:
                    if len(r) <= max(hm.values()):
                        continue
                    d = parse_date(r[hm["date"]].strip()[:10]) if r[hm["date"]].strip() else None
                    events.append({"awb": r[hm["awb"]].strip(), "event": r[hm["event"]].strip().upper(), "date": d,
                                   "station": r[hm["station"]].strip() if "station" in hm else None,
                                   "reason": r[hm["reason"]].strip() if "reason" in hm else None,
                                   "src": src(rec["doc"], pi, " | ".join(r))})
                return {"type": "tracking", "doc": rec["doc"], "events": events}
            hm = _header_map(header, {"invoice": r"invoice|document|bill", "idate": r"invoice date|doc.*date",
                                      "due": r"due", "amount": r"amount|total", "status": r"status",
                                      "paid": r"paid|payment date|settled"})
            if {"invoice", "status"} <= hm.keys() and ("paid" in hm or "due" in hm):
                entries = []
                for r in rows[hi + 1:]:
                    if len(r) <= hm["status"]:
                        continue
                    def cell(k):
                        return r[hm[k]].strip() if k in hm and hm[k] < len(r) else ""
                    am = re.search(r"(" + "|".join(CURRENCIES) + r")?\s*([\d,]+\.\d{2})", cell("amount"))
                    dd = DATE_RE.search(cell("due"))
                    pd = DATE_RE.search(cell("paid"))
                    entries.append({"invoice_id": norm_invoice_id(cell("invoice")),
                                    "due": parse_date(dd.group(0)) if dd else None,
                                    "amount": D(am.group(2)) if am else None,
                                    "currency": am.group(1) if am and am.group(1) else None,
                                    "status": cell("status").lower(), "paid": parse_date(pd.group(0)) if pd else None,
                                    "src": src(rec["doc"], pi, " | ".join(r))})
                return {"type": "register", "doc": rec["doc"], "entries": entries, "received": rec["received"]}
    return None


def parse_awb(rec: dict) -> dict | None:
    full = "\n".join(rec["pages"])
    m = re.search(r"(MASTER )?AIR WAYBILL\s+([A-Z]{2,4}\d{6,11}(?:-H\d+)?)", full)
    if not m:
        return None
    out = {"type": "awb", "doc": rec["doc"], "awb": m.group(2), "master": bool(m.group(1)), "sources": {}}
    ex = re.search(r"Executed on (\S+)", full)
    if ex:
        out["date"] = parse_date(ex.group(1))
        out["sources"]["date"] = src(rec["doc"], 1, ex.group(0))
    for key, pat in (("actual", r"Actual weight ([\d.]+)\s*kg"), ("chargeable", r"Chargeable weight ([\d.]+)\s*kg"),
                     ("total_chargeable", r"Total chargeable weight ([\d.]+)\s*kg")):
        mm = re.search(pat, full)
        if mm:
            out[key] = D(mm.group(1))
            line = next((ln for ln in full.split("\n") if mm.group(0) in ln), mm.group(0))
            out["sources"][key] = src(rec["doc"], 1, line)
    dm = re.search(r"Dimensions (\d+)x(\d+)x(\d+) cm x (\d+)", full)
    if dm:
        out["dims"] = [int(dm.group(i)) for i in range(1, 5)]
    out["houses"] = {h: D(w) for h, w in re.findall(r"House (\S+?):\s*\d+ pieces?,\s*([\d.]+)\s*kg", full)}
    out["un"] = re.findall(r"UN(\d{4})", full)
    om = re.search(r"Origin (\w+) \| Destination (\w+)", full)
    if om:
        out["lane"] = f"{om.group(1)}-{om.group(2)}"
    return out


def parse_dgd(rec: dict) -> dict | None:
    full = "\n".join(rec["pages"])
    m = re.search(r"DECLARATION FOR (?:AWB )?([A-Z]{2,4}\d{6,11}(?:-H\d+)?)", full)
    if not m:
        return None
    items = []
    for pi, page in enumerate(rec["pages"], 1):
        for ln in page.split("\n"):
            um = re.search(r"UN\s?(\d{4})", ln)
            if um:
                items.append({"un": um.group(1), "refrigerant": bool(re.search(r"refrigerant", ln, re.I)),
                              "only": bool(re.search(r"refrigerant only", ln, re.I)), "src": src(rec["doc"], pi, ln)})
    return {"type": "dgd", "doc": rec["doc"], "awb": m.group(1), "items": items}


# ======================================================================================================================
# Emails
# ======================================================================================================================
def parse_email(rec: dict, contract_ids: set[str], aliases: Aliases) -> list[dict]:
    """Facts an email states in its own words. Quoted text is kept apart: it never carries authority."""
    own, quoted, qauthor = body_lines(rec)
    doc = rec["doc"]
    text = "\n".join(own)
    flat = norm(text)
    base = {"doc": doc, "from_name": rec["from_name"], "from_email": rec["from_email"], "received": rec["received"],
            "subject": rec["subject"], "injection": bool(INJECTION_RE.search(text))}
    cids = [c for c in contract_ids if c in text or c in rec["subject"]]
    facts = []
    # rate notice
    rows = []
    for ln in own:
        rows += _rate_rows(ln.strip(), doc, 1)
    if rows:
        dates = [d for d, _, _ in find_dates(flat)]
        fm = re.search(r"(?:effective|from|apply|applies)[^.]*?(\d{4}-\d{2}-\d{2})(?:[^.]*?(?:to|until|through)\s+"
                       r"(\d{4}-\d{2}-\d{2}))?", flat, re.I)
        eff_from = date.fromisoformat(fm.group(1)) if fm else (dates[0] if dates else None)
        eff_to = date.fromisoformat(fm.group(2)) if fm and fm.group(2) else None
        refl = next((ln for ln in own if re.search(r"effective|from .* (to|until)", ln, re.I) and DATE_RE.search(ln)),
                    None)
        cid = next((c for c in cids), None)
        if not cid:
            rm = re.search(r"\b([A-Z0-9\-]+)/RN-", flat)
            cid = rm.group(1) if rm else None
        facts.append({**base, "type": "rate_notice", "contract_id": cid, "rates": rows, "from": eff_from,
                      "to": eff_to, "src_dates": src(doc, 1, refl) if refl else None})
    # due-date extension
    for ln in own:
        if re.search(r"(?i)extend|falls due|new due date|due date (?:is|will be) (?:moved|changed)|now due", ln) and \
                DATE_RE.search(ln):
            ids = [norm_invoice_id(x) for x in ids_in(ln) if x not in contract_ids]
            d = [x for x, _, _ in find_dates(ln)]
            if ids and d:
                facts.append({**base, "type": "extension", "invoice_ids": ids, "new_due": d[-1],
                              "src": src(doc, 1, ln)})
    # waivers, in the sender's own words or only quoted
    def waivers(lines, quoted_flag):
        out = []
        for ln in lines:
            if not re.search(r"(?i)\bwaiv", ln):
                continue
            if re.search(r"(?i)(cannot|can't|unable to|will not|won't|not able to|do not|don't|decline to|no longer)\s+"
                         r"(?:\w+\s+){0,2}waiv|not (?:be )?waived|waiver (?:is|was) (?:refused|declined|rejected)", ln):
                continue
            code, label = aliases.code_of(ln)
            awbs = AWB_RE.findall(ln)
            months = re.findall(r"(?<![\d-])(\d{4}-\d{2})(?![\d-])", DATE_RE.sub(" ", ln))
            for mname, y in re.findall(r"\b(January|February|March|April|May|June|July|August|September|October|"
                                       r"November|December)\s+(\d{4})", ln):
                from .util import MONTHS
                months.append(f"{y}-{MONTHS[mname.lower()]:02d}")
            ctx = " ".join(lines)
            cond = bool(re.search(r"(?i)\b(provided that|provided|on condition|conditional|subject to|only if|"
                                  r"if (?:you|payment|the invoice|paid))\b", ln + " " + ctx))
            once = bool(re.search(r"(?i)\b(this once|one[- ]off|one time|this time only|only for)\b", ctx))
            out.append({**base, "type": "waiver", "code": code, "label": label, "awbs": awbs, "months": months,
                        "conditional": cond, "once": once, "quoted": quoted_flag,
                        "author": qauthor if quoted_flag else rec["from_name"], "condition_text": ctx if cond else None,
                        "src": src(doc, 1, ln), "invoice_ids": [norm_invoice_id(x) for x in ids_in(ln)]})
        return out
    facts += waivers(own, False)
    if not any(f["type"] == "waiver" for f in facts):
        facts += waivers(quoted, True)
    # picked up / milestones
    for ln in own:
        if re.search(r"(?i)picked up|collected from shipper|pickup completed", ln):
            awbs = AWB_RE.findall(ln) or AWB_RE.findall(rec["subject"])
            d = [x for x, _, _ in find_dates(ln)]
            if awbs and d:
                facts.append({**base, "type": "picked_up", "awb": awbs[0], "date": d[-1], "src": src(doc, 1, ln)})
    # resend / reminder wording, used by duplicate detection
    facts.append({**base, "type": "cover", "asks_payment": bool(re.search(
        r"(?i)process payment|please pay|payment (?:is )?(?:now )?(?:due|overdue|outstanding)|remit|kindly pay|"
        r"settle|as outstanding|overdue", text)),
        "copy_only": bool(re.search(r"(?i)\bcopy\b|for your records|duplicate copy|re-?sent for reference|"
                                    r"no action (?:is )?(?:needed|required)|do not pay", text)),
        "reminder": bool(re.search(r"(?i)\breminder\b", text)),
        "ids": [norm_invoice_id(x) for x in ids_in(rec["subject"] + " " + text) if x not in contract_ids],
        "src": src(doc, 1, next((ln for ln in own if re.search(r"(?i)payment|outstanding|copy|records", ln)),
                                own[0] if own else rec["subject"]))})
    return facts
