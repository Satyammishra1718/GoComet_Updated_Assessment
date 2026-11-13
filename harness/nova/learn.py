"""A person's decisions (feedback.json), kept with their source and applied with strict scope.

- A `standing` fact applies to a later key only if every filter in `applies_to` matches and the date that matters
  (pickup or document date) is inside valid_from..valid_to. Same person, other category: no. Same lane, other
  contract: no.
- A `one_off` decision changes its own key and nothing else.
- Every item a decision changed carries learned_at = {feedback_item_id, batch}.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .parse import norm_invoice_id
from .util import D, to_date


def _key(fk: dict) -> tuple:
    if fk.get("class") == "cashback" or "credit_type" in fk:
        return ("cashback", fk.get("contract_id"), fk.get("period"), fk.get("credit_type"))
    if fk.get("class") == "expiry":
        return ("expiry", fk.get("contract_id"))
    return (fk.get("class"), norm_invoice_id(fk.get("invoice_id", "")))


def _same_person(a: str | None, b: str | None) -> bool:
    return bool(a and b and " ".join(a.lower().split()) == " ".join(b.lower().split()))


class Feedback:
    def __init__(self, state_dir: str):
        self.items: list[dict] = []
        d = Path(state_dir) / "feedback"
        for f in sorted(d.glob("*.json")) if d.is_dir() else []:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for it in data.get("items") or []:
                if not isinstance(it, dict):
                    continue
                it = dict(it)
                it["_batch"] = data.get("after_batch") or f.stem
                it["_decided"] = to_date(data.get("decided_at"))
                it["_key"] = _key(it.get("finding_key") or {})
                self.items.append(it)

    def learned(self, it: dict) -> dict:
        return {"feedback_item_id": str(it.get("item_id")), "batch": str(it["_batch"])}

    # ---- per key ------------------------------------------------------------------------------------------------------
    def for_key(self, key: tuple) -> list[dict]:
        return [it for it in self.items if it["_key"] == key]

    # ---- standing facts ----------------------------------------------------------------------------------------------
    @staticmethod
    def _in_window(it: dict, on: date | None) -> bool:
        sc = it.get("scope") or {}
        a, b = to_date(sc.get("valid_from")), to_date(sc.get("valid_to"))
        if on is None:
            return True
        return (a is None or on >= a) and (b is None or on <= b)

    def accepted_notice(self, cid: str, lane: str, notice: dict, pickup: date | None) -> dict | None:
        """A rate notice a person accepted for this contract and lane (fact rate_accepted)."""
        for it in self.items:
            f = it.get("fact") or {}
            if f.get("type") != "rate_accepted":
                continue
            ap = (it.get("scope") or {}).get("applies_to") or {}
            subj = f.get("subject") or {}
            if (ap.get("contract_id") or subj.get("contract_id")) != cid:
                continue
            if (ap.get("lane") or subj.get("lane")) not in (None, lane):
                continue
            v = f.get("value") or {}
            if v.get("sent_by") and not _same_person(v["sent_by"], notice["from_name"]):
                continue
            if v.get("sent_on") and str(v["sent_on"]) != notice["received"][:10]:
                continue
            if not self._in_window(it, pickup):
                continue
            return it
        return None

    def authority(self, cid: str, person: str, category: str, on: date | None) -> dict | None:
        """confirm + sender_authority: the person has that category on that contract from valid_from."""
        for it in self.items:
            f = it.get("fact") or {}
            if f.get("type") != "sender_authority" or it.get("decision") not in ("confirm", None):
                continue
            ap = (it.get("scope") or {}).get("applies_to") or {}
            cats = [c.lower() for c in ((f.get("value") or {}).get("categories") or [])]
            if ap.get("category"):
                cats = [ap["category"].lower()] if ap["category"].lower() in cats or not cats else []
            if category not in cats:
                continue
            if ap.get("contract_id") and ap["contract_id"] != cid:
                continue
            who = ap.get("person") or (f.get("subject") or {}).get("person")
            if not _same_person(who, person):
                continue
            if (it.get("scope") or {}).get("kind") == "one_off":
                continue
            if not self._in_window(it, on):
                continue
            return it
        return None

    def precedence(self, cid: str, lane: str, on: date | None, key: tuple | None) -> dict | None:
        for it in self.items:
            f = it.get("fact") or {}
            if f.get("type") != "precedence":
                continue
            ap = (it.get("scope") or {}).get("applies_to") or {}
            subj = f.get("subject") or {}
            if (ap.get("contract_id") or subj.get("contract_id")) != cid:
                continue
            if (ap.get("lane") or subj.get("lane")) not in (None, lane):
                continue
            if it["_key"] == key or self._in_window(it, on):
                return it
        return None

    def override(self, key: tuple) -> tuple[object, dict] | None:
        for it in self.for_key(key):
            if it.get("decision") == "override_amount":
                amt = ((it.get("fact") or {}).get("value") or {}).get("amount_usd")
                if amt is not None:
                    return D(amt), it
        return None

    def stops(self, key: tuple) -> dict | None:
        """reject, or close (dispute closed / rate accepted): that key is not raised again."""
        for it in self.for_key(key):
            if it.get("decision") in ("reject", "close"):
                return it
        return None

    def one_off_waiver(self, key: tuple) -> dict | None:
        for it in self.for_key(key):
            f = it.get("fact") or {}
            if it.get("decision") == "one_off_approval" and f.get("type") == "waiver_scope":
                return it
        return None
