"""The mailbox as of a date: every fact from documents received by end of day as_of (UTC), in one place.

Also answers the questions every detector asks: who has authority for what, which contract a bill is under, when a
shipment was picked up (and from which source), what the AP register says about an invoice.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from . import parse as P
from .util import D, ZERO, to_date, utc_date


def load_docs(state_dir: str) -> list[dict]:
    f = Path(state_dir) / "docs.jsonl"
    if not f.is_file():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


class World:
    def __init__(self, docs: list[dict], as_of: date):
        self.as_of = as_of
        self.docs = [d for d in docs if utc_date(d["received"]) <= as_of]
        self.by_doc = {d["doc"]: d for d in self.docs}
        self.contracts: dict[str, dict] = {}
        self.amendments: dict[str, list[dict]] = defaultdict(list)
        self.rate_annexes: list[dict] = []
        self.bills: dict[str, list[dict]] = defaultdict(list)        # invoice_id -> copies, oldest first
        self.credits: list[dict] = []
        self.awbs: dict[str, dict] = {}
        self.dgds: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = defaultdict(list)
        self.registers: list[dict] = []
        self.notices: list[dict] = []
        self.extensions: list[dict] = []
        self.waivers: list[dict] = []
        self.pickups: dict[str, dict] = {}
        self.covers: dict[str, dict] = {}                            # email doc -> cover fact
        self._parse()

    # ---- building ----------------------------------------------------------------------------------------------------
    def _parse(self):
        for d in self.docs:
            if d["kind"] == "pdf":
                c = P.parse_contract(d)
                if c:
                    old = self.contracts.get(c["contract_id"])
                    if not old or d["received"] >= self.by_doc[old["doc"]]["received"]:
                        self.contracts[c["contract_id"]] = c
        cids = set(self.contracts)
        for d in self.docs:
            if d["kind"] == "pdf" and not any(c["doc"] == d["doc"] for c in self.contracts.values()):
                a = P.parse_amendment(d, cids)
                if a and a.get("contract_id"):
                    a["received"] = d["received"]
                    self.amendments[a["contract_id"]].append(a)
                    continue
                ra = P.parse_rate_annex(d)
                if ra:
                    self.rate_annexes.append(ra)
        all_aliases = {}
        for c in self.contracts.values():
            for k, v in c["aliases"].items():
                all_aliases.setdefault(k, [])
                all_aliases[k] += v
        self.aliases_any = P.Aliases(all_aliases)
        alias_cache = {}

        def aliases_for(cid):
            if cid not in alias_cache:
                alias_cache[cid] = P.Aliases(self.contracts[cid]["aliases"]) if cid in self.contracts \
                    else self.aliases_any
            return alias_cache[cid]
        used = {c["doc"] for c in self.contracts.values()} | {a["doc"] for v in self.amendments.values() for a in v} \
            | {r["doc"] for r in self.rate_annexes}
        for d in self.docs:
            if d["doc"] in used:
                continue
            if d["kind"] == "eml":
                for f in P.parse_email(d, cids, self.aliases_any):
                    t = f["type"]
                    if t == "rate_notice":
                        self.notices.append(f)
                    elif t == "extension":
                        self.extensions.append(f)
                    elif t == "waiver":
                        self.waivers.append(f)
                    elif t == "picked_up":
                        self.pickups.setdefault(f["awb"], f)
                    elif t == "cover":
                        self.covers[d["doc"]] = f
                continue
            if d["kind"] == "xlsx":
                t = P.parse_table_records(d)
                if t and t["type"] == "tracking":
                    for e in t["events"]:
                        self.events[e["awb"]].append(e)
                    continue
                if t and t["type"] == "register":
                    self.registers.append(t)
                    continue
            aw = P.parse_awb(d)
            if aw:
                self.awbs[aw["awb"]] = aw
                continue
            dg = P.parse_dgd(d)
            if dg:
                self.dgds[dg["awb"]] = dg
                continue
            b = P.parse_bill(d, aliases_for, cids, d.get("llm_lines"))
            if b:
                if b["type"] == "credit_note":
                    self.credits.append(b)
                else:
                    self.bills[b["invoice_id"]].append(b)
        for v in self.bills.values():
            v.sort(key=lambda b: b["received"])
        self.registers.sort(key=lambda r: r["received"])
        for v in self.events.values():
            v.sort(key=lambda e: (e["date"] or date.min))

    # ---- questions ---------------------------------------------------------------------------------------------------
    def invoice(self, inv_id: str) -> dict | None:
        v = self.bills.get(inv_id)
        return v[0] if v else None

    def register_entry(self, inv_id: str) -> dict | None:
        """The latest AP register row for an invoice."""
        for r in reversed(self.registers):
            for e in r["entries"]:
                if e["invoice_id"] == inv_id:
                    return e
        return None

    def carrier_party(self, cid: str) -> str | None:
        c = self.contracts.get(cid)
        if not c or not c["schedule1"]:
            return None
        domains = {b["email"] and self.by_doc[b["email"]]["from_email"].split("@")[-1]
                   for v in self.bills.values() for b in v if b["contract_id"] == cid}
        for e in c["schedule1"]:
            if e["email"].split("@")[-1] in domains:
                return e["party"]
        parties = []
        for e in c["schedule1"]:
            if e["party"] not in parties:
                parties.append(e["party"])
        return parties[0]

    def contacts(self, cid: str, category: str, party: str | None = None) -> list[dict]:
        c = self.contracts.get(cid)
        if not c:
            return []
        party = party or self.carrier_party(cid)
        return [e for e in c["schedule1"] if category in e["categories"] and (party is None or e["party"] == party)]

    def kam(self, cid: str) -> dict | None:
        party = self.carrier_party(cid)
        c = self.contracts.get(cid) or {"schedule1": []}
        ks = [e for e in c["schedule1"] if e["party"] == party and re.search(r"key account", e["title"], re.I)]
        return (ks or self.contacts(cid, "notices") or [None])[0]

    def schedule1_person(self, cid: str, name: str | None, email: str | None, category: str) -> dict | None:
        """The Schedule 1 entry for this sender and category, carrier side. Notices count only from listed
        addresses, so a listed name writing from another address has no authority."""
        for e in self.contacts(cid, category):
            if email and e["email"] == email.lower():
                return e
        return None

    def pickup(self, awb: str, line_date: date | None) -> tuple[date | None, str, dict | None]:
        """-> (pickup date, source kind, src). Order: tracking PICKED_UP, carrier 'picked up' email, AWB, invoice."""
        for e in self.events.get(awb, []):
            if e["event"] in ("PICKED_UP", "PICKUP", "COLLECTED") and e["date"]:
                return e["date"], "tracking", e["src"]
        if awb in self.pickups:
            p = self.pickups[awb]
            return p["date"], "email", p["src"]
        aw = self.awbs.get(awb)
        if aw and aw.get("date"):
            return aw["date"], "awb", aw["sources"].get("date")
        return line_date, "invoice", None

    def event(self, awb: str, names: tuple[str, ...]) -> dict | None:
        for e in self.events.get(awb, []):
            if e["event"] in names:
                return e
        return None

    def fx(self, cid: str, month: str) -> dict | None:
        c = self.contracts.get(cid)
        return (c or {}).get("fx", {}).get(month)

    def received(self, doc: str) -> date:
        return utc_date(self.by_doc[doc.split("#")[0]]["received"])


def chargeable_weight(aw: dict | None):
    """Clause 3.3 from the AWB: max(actual, volumetric) rounded up to the next 0.5 kg; else the printed figure."""
    if not aw:
        return None
    if aw.get("actual") is not None and aw.get("dims"):
        l, w, h, n = aw["dims"]
        vol = D(l * w * h * n) / D(6000)
        x = max(aw["actual"], vol)
        whole = (x * 2).to_integral_value(rounding="ROUND_CEILING") / 2
        return whole
    return aw.get("chargeable")


_ = ZERO, to_date
