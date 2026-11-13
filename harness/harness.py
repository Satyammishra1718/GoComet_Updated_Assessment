"""Nova freight-invoice audit harness. The grader calls exactly these three commands, each in a fresh process:

    python harness.py ingest <batch_dir> --state-dir <dir>
    python harness.py report --as-of YYYY-MM-DD [--mode supervised|autonomous] [--state-dir <dir>] > report.json
    python harness.py feedback <feedback.json> --state-dir <dir>

- ingest:   reads every .eml in the batch (body, PDF and XLSX attachments; OCR for scans) and appends the documents,
            with their received time, to <state-dir>/docs.jsonl. Model calls (optional) happen here and are cached.
- report:   rebuilds the world from documents received by --as-of, runs the detectors, applies feedback, chooses
            actions for the mode, and prints report.json. Pure computation: no network.
- feedback: stores the person's decisions in <state-dir>/feedback/, with the batch they came after.

The package lives in harness/nova/ (see nova/__init__.py for the layers).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_STATE = ".state"


def ingest(batch_dir: str, state_dir: str) -> None:
    from nova import llm, parse, reader  # noqa: PLC0415
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    f = state / "docs.jsonl"
    known = set()
    if f.is_file():
        known = {json.loads(x)["doc"] for x in f.read_text(encoding="utf-8").splitlines() if x.strip()}
    new = reader.read_batch(Path(batch_dir), state_dir, known)
    if llm.available():
        # a bill whose lines the deterministic reader can't split: ask the model for its lines (cached)
        al = parse.Aliases()
        for d in new:
            if d["kind"] != "pdf":
                continue
            b = parse.parse_bill(d, lambda _c: al, set())
            if b and b["type"] == "bill" and not b["lines"] and b["total"]:
                lines = llm.extract_lines("\n".join(d["pages"]), d["doc"], state_dir)
                if lines:
                    d["llm_lines"] = lines
    with f.open("a", encoding="utf-8") as fh:
        for d in new:
            fh.write(json.dumps(d) + "\n")
    print(f"ingested {len(new)} document(s) from {batch_dir}", file=sys.stderr)


def report(as_of: str, mode: str, state_dir: str) -> dict:
    from nova.report import build  # noqa: PLC0415
    return build(as_of, mode, state_dir)


def feedback(feedback_file: str, state_dir: str) -> None:
    dest = Path(state_dir) / "feedback"
    dest.mkdir(parents=True, exist_ok=True)
    data = json.loads(Path(feedback_file).read_text(encoding="utf-8"))
    n = len(list(dest.glob("*.json")))
    name = f"{n + 1:03d}_{Path(feedback_file).name}"
    shutil.copyfile(feedback_file, dest / name)
    print(f"stored {len(data.get('items') or [])} feedback item(s) as {name}", file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness.py", description="Freight invoice audit harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="read one batch folder into the state dir")
    p.add_argument("batch_dir")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("report", help="print report.json for a date")
    p.add_argument("--as-of", required=True, help="YYYY-MM-DD; only documents received by then count")
    p.add_argument("--mode", choices=["supervised", "autonomous"], default="supervised")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p.add_argument("--explain", help="debug: print the working for one key (e.g. INV-2040-0001) to stderr")
    p = sub.add_parser("feedback", help="take in a person's decisions (feedback.json)")
    p.add_argument("feedback_file")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "ingest":
            ingest(a.batch_dir, a.state_dir)
        elif a.cmd == "report":
            if a.explain:
                import os  # noqa: PLC0415
                os.environ["NOVA_EXPLAIN"] = a.explain
            json.dump(report(a.as_of, a.mode, a.state_dir), sys.stdout, indent=1)
            sys.stdout.write("\n")
        else:
            feedback(a.feedback_file, a.state_dir)
    except NotImplementedError as e:
        print(f"harness.py {a.cmd}: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
