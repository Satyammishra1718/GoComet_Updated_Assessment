"""The six classes, plus conflict and cannot_determine. All money in Decimal, rounded only where the glossary says.

Each detector returns internal items:
    key, class, verdict, amount (USD Decimal) or range, deadline, evidence [src], missing_doc, remedy,
    auto (inputs to the unattended-action test), learned (feedback item that changed it), feat (confidence inputs).
"""
from __future__ import annotations

import math
import os
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal

from . import rates as R
from .learn import Feedback
from .util import ZERO, add_years, days, month_key, quarter_bounds, quarter_of, r2, to_date

OUTSIDE_QS = {"TAX", "DUTY", "LATE"}
PER_SHIPMENT = {"DG", "DRYICE", "DLV", "CUSTOMS"}


def explain(*a):
    if os.environ.get("NOVA_EXPLAIN"):
        print("  [explain]", *a, file=sys.stderr)


def new_item(key, cls, **kw) -> dict:
    it = {"key": key, "class": cls, "verdict": "finding", "amount": None, "range": None, "deadline": None,
          "evidence": [], "missing_doc": None, "remedy": None, "learned": None,
          "auto": {"pickup_ok": True, "terms_level": 1, "facts_level": 3, "missing": False, "lanes": [],
                   "recipient_ok": True, "ocr": False},
          "feat": {}}
    it.update(kw)
    return it


def add_ev(it: dict, *srcs):
    for s in srcs:
        if s and s.get("quote") and not any(e["doc"] == s["doc"] and e["page"] == s["page"] and e["quote"] == s["quote"]
                                            for e in it["evidence"]):
            it["evidence"].append(s)


def to_usd(world, bill: dict, amount: Decimal) -> tuple[Decimal | None, dict | None]:
    """Convert once per finding with Annex F for the month of the invoice date."""
    if bill["currency"] in (None, "USD"):
        return r2(amount), None
    fx = world.fx(bill["contract_id"], month_key(bill["issue"])) if bill["issue"] else None
    if not fx:
        return None, None
    return r2(amount / fx["rate"]), fx["src"]


def missing_fx(world, bill: dict) -> dict:
    cid = bill["contract_id"]
    t = (world.contacts(cid, "treasury") or [None])[0]
    holder = {"party": world.carrier_party(cid)}
    if t:
        holder.update(person=t["name"], role=t["title"])
    name = world.contracts.get(cid, {}).get("fx_update_name") or "Annex F update"
    return {"name": f"{name} {month_key(bill['issue'])}" if bill["issue"] else name, "holder": holder}


def disputed_on(items: list[dict], inv_id: str) -> Decimal:
    return sum((i["amount_local"] for i in items if i.get("invoice_id") == inv_id and i["verdict"] == "finding"
                and i.get("amount_local") is not None and i["class"] in ("overcharge", "duplicate",
                                                                           "waived_but_billed")), ZERO)


# ======================================================================================================================
# Overcharge (+ conflict, cannot_determine)
# ======================================================================================================================
def _ship_weight(world, awb: str, lines: list[dict]):
    billed = next((l["qty"] for l in lines if l["code"] == "FRT" and l.get("qty") is not None), None)
    if billed is None:
        billed = next((l["qty"] for l in lines if (l.get("unit") or "").startswith("kg") and l.get("qty")), None)
    aw = world.awbs.get(awb)
    printed = aw.get("chargeable") if aw else None
    if billed is not None and printed is not None and billed > printed:
        return printed, billed, aw["sources"].get("chargeable")
    return billed, billed, None


def _dg_facts(world, awb: str) -> tuple[bool | None, bool | None, list[dict]]:
    """-> (has dangerous goods other than dry ice, has dry ice, sources). None when nothing is held."""
    dg = world.dgds.get(awb)
    aw = world.awbs.get(awb)
    if dg and dg["items"]:
        other = any(i["un"] != "1845" for i in dg["items"])
        ice = any(i["un"] == "1845" for i in dg["items"])
        return other, ice, [i["src"] for i in dg["items"]]
    if aw and aw.get("un"):
        return any(u != "1845" for u in aw["un"]), "1845" in aw["un"], []
    return None, None, []


def _storage_days(world, awb: str, free: int) -> tuple[int | None, list[dict]]:
    arr = world.event(awb, ("ARRIVED_FACILITY",))
    rel = world.event(awb, ("DELIVERED", "COLLECTED_BY_CONSIGNEE", "COLLECTED"))
    if not arr or not rel or not arr["date"] or not rel["date"]:
        return None, []
    return max(0, (rel["date"] - arr["date"]).days - free), [arr["src"], rel["src"]]


def price_shipment(world, fb, bill, awb, lines, key) -> dict:
    """Billed vs correct for every line of one shipment on one bill."""
    cid = bill["contract_id"]
    c = world.contracts[cid]
    lane = next((l["lane"] for l in lines if l.get("lane")), None) or (world.awbs.get(awb) or {}).get("lane")
    line_date = next((l["date"] for l in lines if l.get("date")), None)
    pickup, psrc_kind, psrc = world.pickup(awb, line_date)
    weight, billed_w, wsrc = _ship_weight(world, awb, lines)
    out = {"lane": lane, "pickup": pickup, "pickup_kind": psrc_kind, "rows": [], "status": "ok", "evidence": [],
           "learned": None, "terms_level": 1}
    if psrc:
        out["evidence"].append(psrc)
    res = R.resolve(world, fb, cid, lane, weight, pickup, key) if any(l["code"] == "FRT" for l in lines) else None
    if res and res["status"] in ("missing", "conflict"):
        out["status"] = res["status"]
        out["res"] = res
    elif res and res["status"] == "ok":
        out["learned"] = res.get("learned")
        out["terms_level"] = res.get("level", 1)
    fuel, fuel_src = R.fuel_pct(world, cid, pickup)
    scen = {}
    if res and res["status"] == "conflict":
        scen = {"a": res["a"], "b": res["b"]}
    elif res and res["status"] == "ok":
        scen = {"x": res}

    def freight(r):
        amt = r2(r["rate"] * weight) if weight is not None else None
        if amt is not None and r.get("min") is not None and amt < r["min"]:
            amt = r["min"]
        return amt
    seen_ship = defaultdict(int)
    for l in lines:
        code = l["code"]
        billed = l["amount"]
        corr = {}
        ev = []
        if code == "FRT" and scen and weight is not None:
            for k, r in scen.items():
                corr[k] = freight(r)
            ev = list(next(iter(scen.values()))["srcs"])
            if wsrc:
                ev.append(wsrc)
        elif code == "FUEL" and scen and fuel is not None and weight is not None:
            for k, r in scen.items():
                base = sum((freight(r) for x in lines if x["code"] == "FRT"), ZERO)
                corr[k] = r2(base * fuel / 100)
            ev = [fuel_src]
        elif code in c["fees"] or code in ("SEC", "PEAK"):
            fee = c["fees"].get(code)
            if not fee:
                continue
            basis = fee["basis"]
            val = None
            if "kg" in basis:
                if weight is not None:
                    val = r2(fee["fee"] * weight)
                    if fee.get("months") and pickup and pickup.month not in fee["months"]:
                        val = ZERO
            elif "storage day" in basis or "per day" in basis:
                n, evs = _storage_days(world, awb, c.get("storage_free") or 0)
                if n is not None:
                    val = r2(fee["fee"] * n)
                    ev += evs
                    if c["sources"].get("storage"):
                        ev.append(c["sources"]["storage"])
            elif "month" in basis:
                val = r2(fee["fee"] * (l.get("qty") or 1))
            else:                                                 # per shipment
                seen_ship[code] += 1
                val = fee["fee"] if seen_ship[code] == 1 else ZERO
                if code == "DG":
                    other, ice, evs = _dg_facts(world, awb)
                    if other is False and ice:
                        val = ZERO
                        ev += evs + [c["sources"].get("dryice")]
                elif code == "DRYICE":
                    other, ice, evs = _dg_facts(world, awb)
                    if other:
                        val = ZERO
                        ev += evs + [c["sources"].get("mixed")]
                elif code == "DLV":
                    if world.event(awb, ("COLLECTED_BY_CONSIGNEE",)) and not world.event(awb, ("DELIVERED",)):
                        val = ZERO
                        ev.append(c["sources"].get("delivery"))
                elif code == "CUSTOMS":
                    hold = world.event(awb, ("CUSTOMS_HOLD",))
                    if hold and hold.get("reason") and "CARRIER" in hold["reason"].upper():
                        val = ZERO
                        ev += [hold["src"], c["sources"].get("customs")]
            if val is None:
                continue
            corr = {k: val for k in (scen or {"x": None})}
            ev = [fee["src"]] + ev
        else:
            continue
        if not corr:
            continue
        out["rows"].append({"line": l, "billed": billed, "correct": corr, "ev": [e for e in ev if e]})
    return out


def overcharge(world, fb: Feedback, bill: dict) -> dict | None:
    cid = bill["contract_id"]
    if cid not in world.contracts:
        return None
    c = world.contracts[cid]
    if c.get("rate_currency") and bill["currency"] and c["rate_currency"] != bill["currency"]:
        return None
    key = ("overcharge", bill["invoice_id"])
    groups = defaultdict(list)
    loose = []
    for l in bill["lines"]:
        (groups[l["awb"]] if l.get("awb") else loose).append(l)
    it = new_item(key, "overcharge", invoice_id=bill["invoice_id"])
    definite = ZERO
    lo_extra = hi_extra = ZERO
    conflict_rows = []
    missing = None
    pickups_ok = True
    terms_level = 1
    ships = [(awb, ls) for awb, ls in groups.items()]
    if loose:
        ships.append((None, loose))
    for awb, ls in ships:
        s = price_shipment(world, fb, bill, awb or "", ls, key)
        explain(bill["invoice_id"], awb, s["lane"], s["pickup"], s["pickup_kind"], s["status"],
                [(r["line"]["code"], r["billed"], r["correct"]) for r in s["rows"]])
        if s["status"] == "missing":
            missing = missing or (s, awb, ls)
            continue
        if s["status"] == "conflict":
            a = sum((r["billed"] - r["correct"]["a"] for r in s["rows"] if "a" in r["correct"]), ZERO)
            b = sum((r["billed"] - r["correct"]["b"] for r in s["rows"] if "b" in r["correct"]), ZERO)
            if max(a, b) > 0:
                lo_extra += max(ZERO, min(a, b))
                hi_extra += max(a, b)
                conflict_rows.append((s, awb))
            continue
        for r in s["rows"]:
            d = r["billed"] - next(iter(r["correct"].values()))
            if d > 0:
                definite += d
                add_ev(it, r["line"]["src"], *r["ev"])
                if r["line"]["code"] in ("FRT", "FUEL"):
                    if s["pickup_kind"] not in ("tracking", "email"):
                        pickups_ok = False
                    terms_level = max(terms_level, s["terms_level"])
                    if s["learned"]:
                        it["learned"] = s["learned"]
                    it["auto"]["lanes"].append((cid, s["lane"]))
                if s["pickup"] and r["line"]["code"] in ("FRT", "FUEL"):
                    add_ev(it, *s["evidence"])
        if s["rows"] and c["sources"].get("pricing"):
            pass
    it["auto"].update(pickup_ok=pickups_ok, terms_level=terms_level, ocr=bill.get("scan", False))
    if conflict_rows:
        s, awb = conflict_rows[0]
        lo_usd, _ = to_usd(world, bill, definite + lo_extra)
        hi_usd, _ = to_usd(world, bill, definite + hi_extra)
        if hi_usd is None:
            return None
        it.update(verdict="conflict", range=[lo_usd, hi_usd], remedy="escalate")
        frt = next((r["line"] for r in s["rows"] if r["line"]["code"] == "FRT"), None)
        res = s["res"]
        add_ev(it, frt["src"] if frt else None, res["a"]["srcs"][0], res["b"]["srcs"][0],
               c["sources"].get("unranked"), *(s["evidence"]))
        it["auto"]["lanes"].append((cid, s["lane"]))
        it["feat"]["conflict"] = True
        return it
    if definite > 0:
        usd, fxsrc = to_usd(world, bill, definite)
        if usd is None:
            it.update(verdict="cannot_determine", missing_doc=missing_fx(world, bill), remedy="request_document")
            return it
        if usd <= 0:
            return None
        it.update(amount=usd, amount_local=definite, remedy="dispute")
        add_ev(it, fxsrc, c["sources"].get("pricing"))
        return it
    if missing:
        s, awb, ls = missing
        res = s["res"]
        frt = next((l for l in ls if l["code"] == "FRT"), ls[0])
        rp = (world.contacts(cid, "rates") or [None])[0]
        holder = {"party": world.carrier_party(cid)}
        if rp:
            holder.update(person=rp["name"], role=rp["title"])
        it.update(verdict="cannot_determine", missing_doc={"name": res["name"], "holder": holder},
                  remedy="request_document")
        add_ev(it, frt["src"], res["src"], *s["evidence"])
        it["auto"]["missing"] = True
        return it
    return None


# ======================================================================================================================
# Duplicates
# ======================================================================================================================
def duplicates(world, fb, bills: list[dict]) -> list[dict]:
    items = []
    # (a) the same invoice number sent again, asking for payment of a copy the ledger shows paid or scheduled
    for inv_id, copies in world.bills.items():
        first = copies[0]
        for cp in copies[1:]:
            cover = world.covers.get(cp["email"], {})
            if not cover or not cover.get("asks_payment") or cover.get("copy_only"):
                continue
            reg = world.register_entry(inv_id)
            if not reg or reg["status"] not in ("paid", "scheduled", "approved", "in payment run"):
                continue
            resent = to_date(cp["received"])
            if reg["status"] == "paid" and reg["paid"] and reg["paid"] > resent:
                continue
            usd, fxsrc = to_usd(world, first, first["total"] or ZERO)
            if not usd:
                continue
            it = new_item(("duplicate", inv_id), "duplicate", invoice_id=inv_id, amount=usd,
                          amount_local=first["total"], remedy="dispute")
            add_ev(it, reg["src"], cover.get("src"), first["lines"][0]["src"] if first["lines"] else None, fxsrc)
            it["feat"]["kind"] = "resend"
            items.append(it)
            break
    # (b) one charge on two invoice numbers: AWB + charge code + amount (labels mapped through Annex E)
    seen: dict[tuple, tuple[dict, dict]] = {}
    hits: dict[str, list] = defaultdict(list)
    for b in sorted(bills, key=lambda b: (b["issue"] or date.min, b["received"])):
        for l in b["lines"]:
            if l["code"] in ("LATE", "TAX") or l.get("amount") is None:
                continue
            if l.get("awb"):
                k = (b["contract_id"], l["awb"], l["code"], l["amount"])
            elif l.get("month_ref"):
                k = (b["contract_id"], "month:" + l["month_ref"], l["code"], l["amount"])
            else:
                continue
            if k in seen and seen[k][0]["invoice_id"] != b["invoice_id"]:
                hits[b["invoice_id"]].append((l, seen[k][0], seen[k][1]))
            else:
                seen.setdefault(k, (b, l))
    for inv_id, hs in hits.items():
        b = world.invoice(inv_id)
        overlap = sum((l["amount"] for l, _, _ in hs), ZERO)
        earlier = {e["invoice_id"] for _, e, _ in hs}
        credited = sum((cn["total"] or ZERO for cn in world.credits
                        if cn["credit"]["credit_type"] not in ("rebate", "improvement")
                        and (set(cn["credit"]["refs"]) & (earlier | {inv_id})
                             or set(cn["credit"]["awbs"]) & {l["awb"] for l, _, _ in hs if l.get("awb")})), ZERO)
        net = overlap - credited
        if net <= 0:
            continue
        usd, fxsrc = to_usd(world, b, net)
        if not usd:
            continue
        it = new_item(("duplicate", inv_id), "duplicate", invoice_id=inv_id, amount=usd, amount_local=net,
                      remedy="dispute")
        for l, e, el in hs:
            add_ev(it, l["src"], el["src"])
        c = world.contracts.get(b["contract_id"])
        if c:
            for l, _, _ in hs:
                add_ev(it, (c.get("alias_src") or {}).get(l["code"]))
        add_ev(it, fxsrc)
        it["feat"]["kind"] = "rebill"
        if not any(i["key"] == it["key"] for i in items):
            items.append(it)
    return items


# ======================================================================================================================
# Waived but billed
# ======================================================================================================================
def _condition_met(world, w: dict, bill: dict) -> bool | None:
    t = w.get("condition_text") or ""
    from .util import find_dates
    ds = [d for d, _, _ in find_dates(t)]
    reg = world.register_entry(bill["invoice_id"])
    if ds and reg is not None:
        return bool(reg.get("paid") and reg["paid"] <= ds[-1])
    return None


def waived(world, fb: Feedback, bill: dict) -> dict | None:
    cid = bill["contract_id"]
    if cid not in world.contracts:
        return None
    c = world.contracts[cid]
    key = ("waived_but_billed", bill["invoice_id"])
    matched, evid, learned, quoted_hit = [], [], None, None
    for w in world.waivers:
        if w.get("injection") or not w.get("code"):
            continue
        lines = [l for l in bill["lines"] if l["code"] == w["code"] and (
            (l.get("awb") and l["awb"] in w["awbs"]) or (l.get("month_ref") and l["month_ref"] in w["months"]))]
        if not lines:
            continue
        on = next((l.get("date") for l in lines if l.get("date")), None)
        if w["quoted"]:
            author_ok = any(e["name"].lower() == (w.get("author") or "").lower()
                            for e in world.contacts(cid, "waivers"))
            if author_ok:
                quoted_hit = (w, lines)
            continue
        auth = world.schedule1_person(cid, w["from_name"], w["from_email"], "waivers")
        grant = None if auth else fb.authority(cid, w["from_name"], "waivers", on or to_date(w["received"]))
        if not (auth or grant):
            continue
        if w["conditional"] and _condition_met(world, w, bill) is not True:
            continue
        matched += [l for l in lines if l not in matched]
        evid += [w["src"]] + ([auth["src"]] if auth else [])
        learned = learned or grant
    if matched:
        amt = sum((l["amount"] for l in matched), ZERO)
        usd, fxsrc = to_usd(world, bill, amt)
        if not usd:
            return None
        it = new_item(key, "waived_but_billed", invoice_id=bill["invoice_id"], amount=usd, amount_local=amt,
                      remedy="dispute", learned=learned)
        add_ev(it, *[l["src"] for l in matched], *evid, c["sources"].get("waiver"), fxsrc)
        if learned:
            it["auto"]["terms_level"] = 2
        return it
    if quoted_hit:
        w, lines = quoted_hit
        k = world.kam(cid)
        holder = {"party": world.carrier_party(cid)}
        if k:
            holder.update(person=k["name"], role=k["title"])
        it = new_item(key, "waived_but_billed", invoice_id=bill["invoice_id"], verdict="cannot_determine",
                      missing_doc={"name": f"original waiver email from {w.get('author')}", "holder": holder},
                      remedy="request_document")
        add_ev(it, w["src"], *[l["src"] for l in lines])
        it["auto"]["missing"] = True
        return it
    return None


# ======================================================================================================================
# Late fees
# ======================================================================================================================
def due_date(world, cid: str, inv_id: str) -> tuple[date | None, list[dict], dict | None]:
    """The due date, extended if the carrier's Schedule 1 payment_terms person extended it."""
    inv = world.invoice(inv_id)
    reg = world.register_entry(inv_id)
    due = (reg or {}).get("due") or (inv or {}).get("due")
    ev = [reg["src"]] if reg else []
    learned = None
    for e in world.extensions:
        if inv_id not in e["invoice_ids"] or e.get("injection"):
            continue
        auth = world.schedule1_person(cid, e["from_name"], e["from_email"], "payment_terms")
        grant = None if auth else world_fb_auth(e, cid)
        if auth or grant:
            due = e["new_due"]
            ev.append(e["src"])
            learned = learned or grant
    return due, ev, learned


_FB: Feedback | None = None


def world_fb_auth(e, cid):
    return _FB.authority(cid, e["from_name"], "payment_terms", to_date(e["received"])) if _FB else None


def late_fee_owed(c: dict, principal: Decimal, days_late: int) -> Decimal:
    if days_late <= (c.get("grace") or 0):
        return ZERO
    n = math.ceil(days_late / 30)
    rate = c["late_rate"]
    base, total = principal, ZERO
    for _ in range(n):
        fee = r2(rate / 100 * base)
        total += fee
        if c.get("compound", True):
            base += fee
    return total


def late_fee_not_owed(world, fb, bill: dict, items: list[dict]) -> dict | None:
    cid = bill["contract_id"]
    c = world.contracts.get(cid)
    if not c or c.get("late_rate") is None:
        return None
    wrong = ZERO
    it = new_item(("late_fee_not_owed", bill["invoice_id"]), "late_fee_not_owed", invoice_id=bill["invoice_id"],
                  remedy="dispute")
    for l in bill["lines"]:
        if l["code"] != "LATE" or not l.get("late_ref"):
            continue
        ref = l["late_ref"]
        reg = world.register_entry(ref)
        if not reg or not reg.get("paid"):
            continue
        due, ev, learned = due_date(world, cid, ref)
        if not due:
            continue
        refinv = world.invoice(ref)
        principal = (reg.get("amount") or (refinv or {}).get("total") or l.get("principal") or ZERO)
        principal -= disputed_on(items, ref)
        owed = late_fee_owed(c, principal, (reg["paid"] - due).days)
        explain(bill["invoice_id"], "late fee on", ref, "due", due, "paid", reg["paid"], "owed", owed, "billed",
                l["amount"])
        if l["amount"] > owed:
            wrong += l["amount"] - owed
            add_ev(it, l["src"], *ev, c["sources"].get("extension") if len(ev) > 1 else None,
                   c["sources"].get("late"))
            it["learned"] = it["learned"] or learned
    if wrong <= 0:
        return None
    usd, fxsrc = to_usd(world, bill, wrong)
    if not usd:
        return None
    it.update(amount=usd, amount_local=wrong)
    add_ev(it, fxsrc)
    return it


def late_fee_risk(world, fb, items: list[dict]) -> list[dict]:
    out = []
    if not world.registers:
        return out
    reg = world.registers[-1]
    for e in reg["entries"]:
        if e["status"] in ("paid", "scheduled", "cancelled", "credited", "void") or e.get("paid"):
            continue
        inv = world.invoice(e["invoice_id"])
        if not inv:
            continue
        cid = inv["contract_id"]
        c = world.contracts.get(cid)
        if not c or c.get("late_rate") is None:
            continue
        due, ev, learned = due_date(world, cid, e["invoice_id"])
        if not due or not (0 < (due - world.as_of).days <= 14):
            continue
        principal = (e.get("amount") or inv["total"] or ZERO) - disputed_on(items, e["invoice_id"])
        fee = r2(c["late_rate"] / 100 * principal)
        usd, fxsrc = to_usd(world, inv, fee)
        if usd is None:
            continue
        it = new_item(("late_fee_risk", e["invoice_id"]), "late_fee_risk", invoice_id=e["invoice_id"], amount=usd,
                      deadline=due, remedy="payment_reminder", learned=learned)
        add_ev(it, *ev, c["sources"].get("late"), fxsrc)
        out.append(it)
    return out


# ======================================================================================================================
# Cashback and credits
# ======================================================================================================================
def _qs(world, cid: str, start: date, end: date) -> tuple[Decimal | None, list[dict]]:
    total, ev = ZERO, []
    for inv_id, copies in world.bills.items():
        b = copies[0]
        if b["contract_id"] != cid or not b["issue"] or not (start <= b["issue"] <= end):
            continue
        q = sum((l["amount"] for l in b["lines"] if l["code"] not in OUTSIDE_QS and l.get("amount") is not None), ZERO)
        usd, _ = to_usd(world, b, q)
        if usd is None:
            return None, ev
        total += usd
        if b["lines"]:
            ev.append(b["lines"][0]["src"])
    for cn in world.credits:
        if cn["contract_id"] != cid or cn["credit"]["credit_type"] in ("rebate", "improvement", "margin_step_down"):
            continue
        if cn["issue"] and start <= cn["issue"] <= end and cn["total"]:
            usd, _ = to_usd(world, cn, cn["total"])
            total -= usd or ZERO
    return total, ev


def _credited(world, cid: str, ctype: str, period: str) -> tuple[Decimal, list[dict]]:
    amt, ev = ZERO, []
    for cn in world.credits:
        if cn["contract_id"] == cid and cn["credit"]["credit_type"] == ctype and cn["credit"]["period"] == period:
            usd, _ = to_usd(world, cn, cn["total"] or ZERO)
            amt += usd or ZERO
            if cn["lines"]:
                ev.append(cn["lines"][0]["src"])
    return amt, ev


def cashback(world, fb) -> list[dict]:
    out = []
    for cid, c in world.contracts.items():
        eff = c.get("effective")
        if not eff:
            continue
        if c.get("rebate_tiers"):
            q = quarter_of(eff)
            while True:
                start, end = quarter_bounds(q)
                if end >= world.as_of:
                    break
                qs, ev = _qs(world, cid, max(start, eff), end)
                if qs is not None:
                    pct = ZERO
                    for thr, p in c["rebate_tiers"]:
                        if qs >= thr:
                            pct = p
                    earned = r2(pct / 100 * qs)
                    paid, cev = _credited(world, cid, "rebate", q)
                    owed = earned - paid
                    explain(cid, q, "QS", qs, "pct", pct, "earned", earned, "credited", paid)
                    if pct > 0 and owed > 0:
                        due = end + days(c.get("rebate_days") or 45)
                        it = new_item(("cashback", cid, q, "rebate"), "cashback", contract_id=cid, period=q,
                                      credit_type="rebate", amount=r2(owed),
                                      remedy="claim_credit" if world.as_of > due else "track_credit")
                        add_ev(it, c["sources"].get("rebate"), c["sources"].get("qs"), *ev[:3], *cev)
                        out.append(it)
                y, n = int(q[:4]), int(q[-1])
                q = f"{y + (n == 4)}-Q{1 if n == 4 else n + 1}"
        if c.get("improve_pct"):
            n = 1
            while True:
                start = add_years(eff, n - 1)
                end = add_years(eff, n) - days(1)
                if end >= world.as_of:
                    break
                qs, ev = _qs(world, cid, start, end)
                if qs is not None:
                    earned = r2(c["improve_pct"] / 100 * qs)
                    paid, cev = _credited(world, cid, "improvement", f"CY{n}")
                    owed = earned - paid
                    explain(cid, f"CY{n}", "QS", qs, "earned", earned, "credited", paid)
                    if owed > 0:
                        due = end + days(1 + (c.get("improve_days") or 60))
                        it = new_item(("cashback", cid, f"CY{n}", "promised_credit"), "cashback", contract_id=cid,
                                      period=f"CY{n}", credit_type="promised_credit", amount=r2(owed),
                                      remedy="claim_credit" if world.as_of > due else "track_credit")
                        add_ev(it, c["sources"].get("improve"), c["sources"].get("qs"), *ev[:3], *cev)
                        out.append(it)
                n += 1
    return out


# ======================================================================================================================
# Expiry
# ======================================================================================================================
def expiry(world) -> list[dict]:
    out = []
    for cid, c in world.contracts.items():
        end, ev = c.get("end"), [c["sources"].get("end")]
        for a in world.amendments.get(cid, []):
            if a.get("new_end") and a["signed"]:
                end, ev = a["new_end"], [a["sources"]["new_end"]]
        if not end:
            continue
        op = end
        if c.get("auto_renew") and c.get("notice_days"):
            op = end - days(c["notice_days"])
            ev.append(c["sources"].get("renewal"))
        if 0 <= (op - world.as_of).days <= 60:
            it = new_item(("expiry", cid), "expiry", contract_id=cid, deadline=op, remedy="alert")
            add_ev(it, *ev, c["sources"].get("renewal"))
            out.append(it)
    return out


def run_all(world, fb: Feedback) -> list[dict]:
    global _FB
    _FB = fb
    items = []
    bills = [v[0] for v in world.bills.values()]
    for b in bills:
        for fn in (overcharge, waived):
            it = fn(world, fb, b)
            if it:
                items.append(it)
    items += duplicates(world, fb, bills)
    for b in bills:
        it = late_fee_not_owed(world, fb, b, items)
        if it:
            items.append(it)
    items += late_fee_risk(world, fb, items)
    items += cashback(world, fb)
    items += expiry(world)
    return items


_ = (Decimal, sys)
