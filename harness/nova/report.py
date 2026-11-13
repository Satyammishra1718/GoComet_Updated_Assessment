"""Assemble report.json: feedback, actions per mode, confidence, stable ids, supersedes / retro / learned_at.

Actions (GLOSSARY "Actions" and "The two modes"):
- internal actions always run: expiry -> internal:alert, late-fee risk -> internal:payment_reminder, an earned credit
  not yet due -> internal:track_credit, a conflict -> escalate;
- outbound remedies (dispute, claim_credit, request_document) are draft:* in supervised mode, human_review at
  $5,000.00 or more summed over every outbound finding on the same invoice / cashback key;
- in autonomous mode an outbound remedy becomes execute:* only when every check of the unattended-action test passes.
Nothing here ever accepts terms, confirms a renewal, pays or settles: those actions don't exist in this code.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from . import detect
from .learn import Feedback
from .util import ZERO, iso, money, month_key, norm, to_date
from .world import World, load_docs

THRESHOLD = Decimal("5000.00")
INVOICE_CLASSES = ("overcharge", "duplicate", "waived_but_billed", "late_fee_not_owed")


def keystr(k: tuple) -> str:
    if k[0] == "cashback":
        return f"cashback:{k[1]}/{k[2]}/{k[3]}"
    return f"{k[0]}:{k[1]}"


# ---- feedback ---------------------------------------------------------------------------------------------------------
def apply_feedback(world: World, fb: Feedback, items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        stop = fb.stops(it["key"])
        if stop:
            detect.explain("feedback stops", it["key"], stop.get("item_id"))
            continue
        ov = fb.override(it["key"])
        if ov:
            amt, src = ov
            it.update(verdict="finding", amount=amt, range=None, learned=src,
                      remedy=it["remedy"] if it["remedy"] in ("dispute", "claim_credit") else "dispute")
        out.append(it)
    have = {i["key"] for i in out}
    # one-off waiver approvals: the waiver covers the named months on that key only
    for it in fb.items:
        k = it["_key"]
        if k[0] != "waived_but_billed" or it.get("decision") != "one_off_approval":
            continue
        bill = world.invoice(k[1])
        if not bill:
            continue
        f = it.get("fact") or {}
        months = set(((f.get("value") or {}).get("months")) or [])
        person = ((f.get("subject") or {}).get("person") or "").lower()
        codes = {w["code"] for w in world.waivers if w.get("code") and
                 (w["from_name"].lower() == person or (w.get("author") or "").lower() == person)}
        lines = [l for l in bill["lines"] if (not codes or l["code"] in codes) and
                 ((l.get("month_ref") in months) or (l.get("date") and month_key(l["date"]) in months))]
        if not lines:
            continue
        amt = sum((l["amount"] for l in lines), ZERO)
        usd, _ = detect.to_usd(world, bill, amt)
        if not usd:
            continue
        new = detect.new_item(k, "waived_but_billed", invoice_id=k[1], amount=usd, amount_local=amt,
                              remedy="dispute", learned=it)
        detect.add_ev(new, *[l["src"] for l in lines])
        new["auto"]["terms_level"] = 2
        out = [x for x in out if x["key"] != k] + [new]
        have.add(k)
    # override_amount on a key the detectors didn't raise
    for it in fb.items:
        k = it["_key"]
        if it.get("decision") == "override_amount" and k not in have and k[0] in INVOICE_CLASSES \
                and world.invoice(k[1]):
            ov = fb.override(k)
            new = detect.new_item(k, k[0], invoice_id=k[1], amount=ov[0], remedy="dispute", learned=it)
            b = world.invoice(k[1])
            detect.add_ev(new, b["lines"][0]["src"] if b["lines"] else None)
            out.append(new)
    return out


# ---- actions ----------------------------------------------------------------------------------------------------------
def choose_actions(world: World, items: list[dict], mode: str) -> None:
    outbound_sum: dict[tuple, Decimal] = defaultdict(lambda: ZERO)
    for it in items:
        if it["verdict"] == "finding" and it["remedy"] in ("dispute", "claim_credit"):
            k = ("inv", it["invoice_id"]) if it.get("invoice_id") else it["key"]
            outbound_sum[k] += it["amount"] or ZERO
    conflict_lanes = {ln for it in items if it["verdict"] == "conflict" for ln in it["auto"]["lanes"]}
    for it in items:
        rem = it["remedy"]
        if it["verdict"] == "conflict":
            it["action"] = "escalate"
            continue
        if rem in ("alert", "payment_reminder", "track_credit"):
            it["action"] = f"internal:{rem}"
            continue
        if rem not in ("dispute", "claim_credit", "request_document"):
            it["action"] = "none"
            continue
        k = ("inv", it["invoice_id"]) if it.get("invoice_id") else it["key"]
        big = outbound_sum[k] >= THRESHOLD
        if mode == "supervised":
            it["action"] = "human_review" if big else f"draft:{rem}"
            continue
        a = it["auto"]
        cid = it.get("contract_id") or (world.invoice(it["invoice_id"]) or {}).get("contract_id") \
            if it.get("invoice_id") else it.get("contract_id")
        if rem == "request_document":
            recipient = bool((it.get("missing_doc") or {}).get("holder", {}).get("person"))
            checks = [recipient, not big]
        else:
            recipient = bool(world.contacts(cid, "billing_disputes")) if cid in world.contracts else False
            checks = [not a["missing"], a["pickup_ok"], not (set(a["lanes"]) & conflict_lanes), not big,
                      a["terms_level"] <= 2, a["facts_level"] <= 3, recipient]
        it["action"] = f"execute:{rem}" if all(checks) else "human_review"
        it["feat"]["gate"] = checks


# ---- confidence -------------------------------------------------------------------------------------------------------
def confidence(it: dict) -> float:
    """P(verdict and amount right). Starts from how decided the class is, then subtracts for each weakness the
    README lists: completeness, authority, extraction, cross-source agreement, clause in force."""
    v, cls, a = it["verdict"], it["class"], it["auto"]
    p = {"finding": 0.93, "conflict": 0.82, "cannot_determine": 0.78}.get(v, 0.85)
    if cls == "expiry":
        p = 0.96
    elif cls == "late_fee_risk":
        p = 0.92
    elif cls == "cashback":
        p = 0.88
    elif cls == "duplicate":
        p = 0.94 if it["feat"].get("kind") == "resend" else 0.86
    if not a.get("pickup_ok", True):
        p -= 0.10
    if a.get("terms_level", 1) >= 2:
        p -= 0.03
    if a.get("ocr"):
        p -= 0.08
    if it.get("learned"):
        p -= 0.02
    if len(it["evidence"]) < 2 and v == "finding":
        p -= 0.05
    return round(max(0.05, min(0.98, p)), 3)


# ---- evidence check ---------------------------------------------------------------------------------------------------
def checked_evidence(docs_by_id: dict, ev: list[dict]) -> list[dict]:
    out = []
    for e in ev:
        q = e.get("quote") or ""
        if len("".join(q.split())) < 12:
            continue
        d = docs_by_id.get(e["doc"])
        if not d or not (1 <= e["page"] <= len(d["pages"])):
            continue
        if not d.get("scan") and norm(q) not in norm(d["pages"][e["page"] - 1]):
            continue
        out.append({"doc": e["doc"], "page": e["page"], "quote": q})
    return out


# ---- assembly ---------------------------------------------------------------------------------------------------------
def build(as_of: str, mode: str, state_dir: str) -> dict:
    d = to_date(as_of)
    docs = load_docs(state_dir)
    world = World(docs, d)
    fb = Feedback(state_dir)
    items = detect.run_all(world, fb)
    items = apply_feedback(world, fb, items)
    items = [i for i in items if not (i["verdict"] == "finding" and i["class"] != "expiry"
                                      and (i["amount"] is None or i["amount"] <= 0))]
    choose_actions(world, items, mode)
    docs_by_id = {x["doc"]: x for x in world.docs}

    state = Path(state_dir) / f"reported_{mode}.json"
    prev = json.loads(state.read_text(encoding="utf-8")) if state.is_file() else {"keys": {}, "held": [], "as_of": None}
    prev_keys = prev.get("keys", {})
    held_before = set(prev.get("held", []))
    new_keys = {}
    findings, cash, exp, risk = [], [], [], []
    for it in sorted(items, key=lambda i: keystr(i["key"])):
        ks = keystr(it["key"])
        amount = money(it["amount"]) if it["amount"] is not None and it["class"] != "expiry" else None
        rng = [money(x) for x in it["range"]] if it["range"] else None
        sig = [it["verdict"], amount, rng, iso(it["deadline"])]
        old = prev_keys.get(ks)
        rec = {"id": ks, "class": it["class"]}
        supersedes = []
        if old and old["sig"] != sig:
            rec["id"] = f"{ks}@{as_of}"
            supersedes = [old["id"]]
        elif old:
            rec["id"] = old["id"]
        new_keys[ks] = {"id": rec["id"], "sig": sig}
        for f in ("invoice_id", "contract_id", "period", "credit_type"):
            if it.get(f):
                rec[f] = it[f]
        ev = checked_evidence(docs_by_id, it["evidence"])
        rec.update({"verdict": it["verdict"], "amount_usd": amount, "amount_range": rng,
                    "deadline": iso(it["deadline"]), "action": it["action"], "confidence": confidence(it),
                    "evidence": ev, "missing_doc": it["missing_doc"], "supersedes": supersedes,
                    "learned_at": fb.learned(it["learned"]) if it.get("learned") else None})
        if not old and prev.get("as_of") and it.get("invoice_id") in held_before:
            rec["retro"] = True
        if it["verdict"] in ("finding", "conflict", "cannot_determine") and not ev:
            continue                                   # nothing we can cite: don't assert it
        if it["verdict"] == "conflict" and len(ev) < 2:
            continue
        cls = it["class"]
        if cls in INVOICE_CLASSES:
            findings.append(rec)
        elif cls == "cashback":
            cash.append(rec)
        elif cls == "expiry":
            exp.append(rec)
        elif cls == "late_fee_risk":
            risk.append(rec)
    keep = {**{k: v for k, v in prev_keys.items()}, **new_keys}
    for k in list(keep):
        if k not in new_keys:
            keep.pop(k)                                 # gone from this report: a later return is a fresh key
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"keys": keep, "held": sorted(world.bills), "as_of": as_of}, indent=1),
                     encoding="utf-8")
    return {"schema_version": "1.0", "as_of": as_of, "mode": mode, "findings": findings,
            "portfolio": {"cashback": cash, "expiry": exp, "late_fee_risk": risk}}


_ = (date, os)
