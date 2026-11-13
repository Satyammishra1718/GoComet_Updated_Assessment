"""Which freight rate applies to one shipment, and how sure we are.

Sources, strongest first (GLOSSARY "Which document wins"):
    signed amendment (later effective date wins for the lanes it changes) > MSA Annex A
    a rate notice from the carrier's Schedule 1 `rates` person (clause 3.5) changes Annex A from its date
    a notice from anyone else has no authority, unless a person accepted it in feedback (rate_accepted)
An amendment and an authorised notice that disagree, both in force, are a conflict when the contract refuses to rank
them (clause 13.3 style); otherwise the amendment wins. An amendment that points to a rates annex we don't hold
("Annex A rev 2 (attached) applies from ...") makes the rate unknowable: cannot_determine.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from .learn import Feedback
from .util import D


def _band(rows: list[dict], lane: str, weight: Decimal | None) -> tuple[dict | None, dict | None]:
    lane_rows = [r for r in rows if r["lane"] == lane]
    mn = next((r for r in lane_rows if r["kind"] == "min"), None)
    if weight is None:
        return None, mn
    bands = sorted((r for r in lane_rows if r["kind"] == "band"), key=lambda r: r["lo"])
    pick = None
    for r in bands:
        if weight >= r["lo"] and (r["hi"] is None or weight <= r["hi"]):
            pick = r
    if pick is None:                     # between printed bands (e.g. 44.7 kg): the band whose floor is below
        for r in bands:
            if weight >= r["lo"]:
                pick = r
    return pick, mn


def _period_index(c: dict, pickup: date | None) -> int:
    ps = c.get("periods") or []
    if not ps or pickup is None:
        return 0
    for i, p in enumerate(ps):
        if p["start"] <= pickup <= p["end"]:
            return i
    return 0 if pickup < ps[0]["start"] else len(ps) - 1


def _col(row: dict, idx: int) -> Decimal:
    return row["rates"][min(idx, len(row["rates"]) - 1)]


def candidate(rows: list[dict], lane: str, weight, idx: int = 0) -> dict | None:
    b, m = _band(rows, lane, weight)
    if not b:
        return None
    return {"rate": _col(b, idx), "min": _col(m, idx) if m else None, "srcs": [b["src"]] + ([m["src"]] if m else [])}


def resolve(world, fb: Feedback, cid: str, lane: str, weight, pickup: date | None, key: tuple | None = None) -> dict:
    """-> {status: ok|conflict|missing|none, rate, min, srcs, level, kind, learned, alt, missing}."""
    c = world.contracts.get(cid)
    if not c or not lane:
        return {"status": "none"}
    idx = _period_index(c, pickup)
    cur = candidate(c["rates"], lane, weight, idx)
    if cur:
        psrc = c["periods"][idx]["src"] if c.get("periods") else None
        cur.update(kind="msa", level=1, learned=None, srcs=cur["srcs"] + ([psrc] if psrc else []))
    # signed amendments in force on the pickup date, oldest first: each replaces the lanes it names
    for a in sorted(world.amendments.get(cid, []), key=lambda a: a["effective"] or date.min):
        if not a["signed"] or (a["effective"] and pickup and pickup < a["effective"]):
            continue
        got = candidate(a["rates"], lane, weight)
        if got:
            got.update(kind="amendment", level=1, learned=None, ref=a["ref"],
                       srcs=got["srcs"] + ([a["sources"]["effective"]] if a["sources"].get("effective") else []))
            cur = got
        for rr in a["rev_refs"]:
            if lane in rr["lanes"] and pickup and pickup >= rr["from"]:
                held = next((x for x in world.rate_annexes if x["name"].lower() == rr["name"].lower()
                             and (not x["contract_ids"] or cid in x["contract_ids"])), None)
                got = candidate(held["rates"], lane, weight) if held else None
                if got:
                    got.update(kind="amendment", level=1, learned=None, ref=rr["name"], srcs=got["srcs"] + [rr["src"]])
                    cur = got
                else:
                    cur = {"status": "missing", "name": rr["name"], "src": rr["src"], "kind": "missing"}
    if cur is None:
        return {"status": "none"}
    if cur.get("status") == "missing":
        return cur
    # rate notices in force on the pickup date
    notices = []
    for n in world.notices:
        if n["contract_id"] not in (cid, None) or n.get("injection"):
            continue
        if not any(r["lane"] == lane for r in n["rates"]):
            continue
        if pickup and ((n["from"] and pickup < n["from"]) or (n["to"] and pickup > n["to"])):
            continue
        got = candidate(n["rates"], lane, weight)
        if not got:
            continue
        auth = world.schedule1_person(cid, n["from_name"], n["from_email"], "rates")
        acc = fb.accepted_notice(cid, lane, n, pickup)
        grant = None if auth or acc else fb.authority(cid, n["from_name"], "rates", pickup)
        if not (auth or acc or grant):
            continue
        got.update(kind="notice", level=2, learned=acc or grant, accepted=bool(acc), notice=n,
                   srcs=got["srcs"] + ([n["src_dates"]] if n.get("src_dates") else []))
        notices.append(got)
    if not notices:
        cur["status"] = "ok"
        return cur
    n = notices[-1]
    if cur["kind"] == "msa" or n.get("accepted"):
        n["status"] = "ok"
        if cur["kind"] == "amendment" and n.get("accepted"):
            n["over"] = cur
        return n
    # amendment vs authorised notice, both in force
    if n["rate"] == cur["rate"] and n["min"] == cur["min"]:
        cur["status"] = "ok"
        return cur
    pref = fb.precedence(cid, lane, pickup, key)
    if pref:
        use = str(((pref.get("fact") or {}).get("value") or {}).get("use", "")).lower()
        win = n if "notice" in use or "correspondence" in use else cur
        win = dict(win, status="ok", learned=pref)
        return win
    if world.contracts[cid].get("unranked_notices"):
        return {"status": "conflict", "a": cur, "b": n, "srcs": cur["srcs"] + n["srcs"]}
    cur["status"] = "ok"
    return cur


def fuel_pct(world, cid: str, pickup: date | None) -> tuple[Decimal | None, dict | None]:
    c = world.contracts.get(cid)
    if not c or not pickup:
        return None, None
    f = c["fuel"].get(f"{pickup.year:04d}-{pickup.month:02d}")
    return (f["pct"], f["src"]) if f else (None, None)


_ = D
