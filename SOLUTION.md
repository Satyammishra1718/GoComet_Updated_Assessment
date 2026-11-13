# My solution

## How to run it

Python 3.11 or 3.12 (the pinned `rapidocr-onnxruntime==1.4.4` has no wheels for 3.13 and later). No API key is
needed: the main path is deterministic and the OCR runs locally. If `ANTHROPIC_API_KEY` is set, two cached fallbacks
can use it (see DESIGN.md).

```
make setup        # .venv with harness/requirements.txt (nothing added)
make grade        # practice mailbox, both modes -> report_card.txt / report_card.json
make sheet        # exam mailbox -> out/exam_report.json, answer_sheet.json
make eval         # evals/cases -> evals/results.json
make validate
```

On Windows without `make` (what I used: Git Bash, `.venv/Scripts/python`, and `PYTHONUTF8=1` so the card's ✓ prints):

```
python grader/run_grader.py --harness "python harness/harness.py" --data data/scenarios --key data/scenarios/answers --mode both --out report_card.json > report_card.txt
python tools/run_mailbox.py --data data/exam --harness harness/harness.py --out out/exam_report.json --sheet answer_sheet.json
python evals/run_evals.py --harness harness/harness.py
python evals/make_mailboxes.py     # rebuilds my eval mailboxes from the worked-example MSAs
```

The code is `harness/harness.py` (the CLI) plus the `harness/nova/` package:

| Module | What it does |
|---|---|
| `reader` | Reads each email and its attachments; runs OCR on scans. |
| `parse` | Turns documents into typed facts by meaning. |
| `world` | Assembles the facts as of a date. |
| `rates` | Decides which rate applies and when sources conflict. |
| `detect` | Runs the checks for each class. |
| `learn` | Applies feedback. |
| `report` | Picks actions, confidence and ids. |

`report --explain <id>` prints the working to stderr.

## What works

- **Practice card: 99.8 / 100.** All five hard gates are clear. Detection F1 is 1.00 with 22 hits and 0 false. Amounts:
  $14,140.09 of $14,140.09 recovered. Adaptation 11/11, learning 3/3, conflicts 2/2. Actions are right in both modes
  (24/24 each), escalation is 4%, Brier is 0.009, and clean controls 16/16. The answer-sheet check on W1 passes 9/9.
- **Exam sheet:**

  | Item | Value |
  |---|---|
  | Total savings | $8,165.33 |
  | Overpayment | $3,847.70 |
  | Cashback | $4,307.34 |
  | Late fees avoided | $10.29 |
  | Conflicts | 1 (CDU-3051302, amendment vs rate notice, $0.00–61.93) |
  | Cannot determine | 1 (CDU-3051286: Annex A rev 2, from Joaquin Rathbone) |

  I checked every exam figure by hand against the documents. The scanned invoice CDI-3031-0306 is read with local OCR
  and yields a $85.46 overcharge (INR 7,438.14 at Annex F 87.04).
- **Worked examples:** all 8 reproduce their expected finding and pass on their decoys.
- **My evals: 22 of 22 pass, 20 of them mine.** Each one has its own mailbox built by `evals/make_mailboxes.py`. They
  cover:
  - an unseen invoice layout;
  - a weight on a band edge;
  - an unauthorised waiver, a waiver seen only quoted, a waiver by month, and an injection attempt;
  - a backdated amendment;
  - late-fee risk on day 14 and day 15;
  - a copy "for your records" against a paid reminder;
  - storage days and weight above the AWB;
  - $5,000+ in autonomous mode;
  - an INR invoice with no Annex F row;
  - a feedback reject;
  - a HAWB split, a partially credited rebill, an extension, and whole-number amounts.
- **Safety by construction:**
  - Text addressed to automated readers marks the email as data.
  - Authority needs the sender's own Schedule 1 address.
  - Quoted waivers never count.
  - Supervised mode can't emit `execute:*`.

## What doesn't

- Handling-margin step-down is recognised in the contract but not priced. A contract that uses it would miss those
  cashback keys.
- Conditional waivers count only when the condition is a "paid by <date>" I can check against the AP register.
  Other conditions are left as no finding.
- Late-fee risk reads the latest AP register only. An invoice that arrived after the last register is not checked.
- The Claude fallbacks (vision for scans when OCR is missing, line extraction for an unparseable bill) are written
  and guarded, but untested here: I had no API key. Without a key they are simply skipped.
- Recognition rests on patterns written from the clause and label wordings in the brief and the data. Wording that
  departs a lot, such as a waiver with no "waive" verb, can be missed. With a key, these are the places a model
  fallback would go next.

## The eval case that caught a bug

`evals/cases/whole_number_amounts.yaml`. The case began as `waiver_by_month`: a March account-fee waiver against a
monthly statement. It failed on its first run with "key missing from the report".

I traced it to two causes:
- My test statement stored amounts without a number format, so the text layer printed `360` instead of `359.64`.
  That was a fault in the test file, and I fixed it there.
- The harness also dropped every line whose amount had no decimals: `parse._solve` only accepted 2-decimal amounts.
  So a carrier whose statement prints whole numbers would lose every line, and every finding on it.

I fixed the parser to accept whole-number amounts when quantity × rate adds up exactly. I kept a dedicated case for
that layout, and checked that it fails on the old parser and passes on the new one. The practice card and the exam
sheet were unchanged by the fix.
