"""Mailbox reader: every .eml and its attachments become documents with page text.

The text layer is the grader's own (grader/extract.py), so every quote we cite is checked against exactly the text we
read. A PDF with no text layer (a scan) is read with local OCR (rapidocr), or with Claude vision when OCR is not
installed and a key is set. Scan quotes can't be checked by the grader, but the page number still must be right.
"""
from __future__ import annotations

import sys
from datetime import timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "grader"))
import extract  # noqa: E402

from . import llm  # noqa: E402


def _email_meta(msg) -> dict:
    name, addr = parseaddr(str(msg["From"]))
    dt = parsedate_to_datetime(str(msg["Date"]))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    to = [parseaddr(x)[1].lower() for x in str(msg["To"] or "").split(",") if x.strip()]
    cc = [parseaddr(x)[1].lower() for x in str(msg["Cc"] or "").split(",") if x.strip()]
    return {"from_name": name.strip(), "from_email": addr.lower(), "to": to, "cc": cc,
            "subject": str(msg["Subject"] or ""),
            "received": dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


# ---- OCR --------------------------------------------------------------------------------------------------------------
_ENGINE = None


def _rapid():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415
        _ENGINE = RapidOCR()
    return _ENGINE


def _ocr_page_rapid(page) -> str:
    import numpy as np  # noqa: PLC0415
    pix = page.get_pixmap(dpi=200)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    if pix.n == 4:
        img = img[:, :, :3]
    res, _ = _rapid()(img)
    boxes = sorted(res or [], key=lambda r: ((r[0][0][1] + r[0][2][1]) / 2, r[0][0][0]))
    lines: list[list] = []
    for b in boxes:
        y = (b[0][0][1] + b[0][2][1]) / 2
        h = abs(b[0][2][1] - b[0][0][1]) or 10
        if lines and abs(lines[-1][0] - y) < max(8.0, 0.5 * h):
            lines[-1][1].append(b)
        else:
            lines.append([y, [b]])
    return "\n".join(" ".join(x[1] for x in sorted(bs, key=lambda r: r[0][0][0])) for _, bs in lines)


def ocr_pdf(data: bytes, cache_key: str, state_dir: str) -> tuple[list[str], str]:
    """-> (pages, engine). Local OCR first; Claude vision as the fallback."""
    import pymupdf  # noqa: PLC0415
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        try:
            return [_ocr_page_rapid(p) for p in doc], "rapidocr"
        except Exception as e:  # noqa: BLE001  (OCR missing or failing: try vision)
            print(f"rapidocr unavailable ({e}); trying vision", file=sys.stderr)
        pages = []
        for i, p in enumerate(doc):
            png = p.get_pixmap(dpi=170).tobytes("png")
            pages.append(llm.transcribe_image(png, f"{cache_key}#p{i + 1}", state_dir) or "")
        return pages, "claude-vision" if any(pages) else "none"


# ---- one batch --------------------------------------------------------------------------------------------------------
def read_batch(batch_dir: Path, state_dir: str, known: set[str]) -> list[dict]:
    """Every document in the batch not seen before. One record per email and per attachment."""
    out = []
    for eml in sorted(batch_dir.glob("*.eml")):
        rel = f"{batch_dir.name}/{eml.name}"
        data = eml.read_bytes()
        msg = extract.parse_eml(data)
        meta = _email_meta(msg)
        if rel not in known:
            out.append({"doc": rel, "email": rel, "kind": "eml", "name": eml.name, "batch": batch_dir.name,
                        "pages": extract.pages_of(data, "eml"), "scan": False, **meta})
        for name, att in extract.attachments(data):
            doc = f"{rel}#{name}"
            if doc in known:
                continue
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            rec = {"doc": doc, "email": rel, "kind": ext, "name": name, "batch": batch_dir.name, "scan": False,
                   **meta}
            try:
                if ext == "pdf" and extract.pdf_is_image_only(att):
                    rec["pages"], rec["ocr"] = ocr_pdf(att, doc, state_dir)
                    rec["scan"] = True
                elif ext in ("pdf", "xlsx"):
                    rec["pages"] = extract.pages_of(att, ext)
                else:
                    continue
            except Exception as e:  # noqa: BLE001  (a broken attachment must not stop the batch)
                print(f"skipping {doc}: {e}", file=sys.stderr)
                continue
            out.append(rec)
    return out
