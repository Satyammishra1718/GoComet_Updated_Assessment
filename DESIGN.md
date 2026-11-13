# Design

## Fact model

`ingest` stores every email and attachment as a document: doc path (`batch_01/0014.eml#INV-1.pdf`), received time
(UTC, from the Date header), sender, and page text from `grader/extract.py`, so every quote we cite is checked
against the grader's own text. Scans go through local OCR (rapidocr), with Claude vision as a fallback.
`report` re-parses the documents received by `as_of` into typed facts (`harness/nova/parse.py`): contracts (term,
periods, Annex A/C/D/E/F, Schedule 1, late-fee, rebate and credit clauses), amendments, rate notices, bills
(invoices, statements, credit notes), AWBs, DG declarations, tracking events, AP register rows, waivers,
extensions and "picked up" emails. Every fact keeps `src = {doc, page, quote}`. Facts are recognised by meaning:
labels go through each contract's Annex E aliases, and invoice lines are split by arithmetic (the quantity × rate =
amount or base × % = amount triple that adds up), not by column position. No customer, carrier, lane, person or
invoice number appears in the code.

## Conflicts

`rates.resolve` collects every rate source in force on the pickup date: MSA Annex A (by period), signed amendments
(the later one wins per lane), and rate notices. A notice counts only if it came from the carrier's Schedule 1
`rates` address, or a person accepted it in feedback. Notice vs MSA: the notice wins (clause 3.5). Amendment vs an
authorised notice with different rates is a conflict only when the contract refuses to rank them ("neither prevails
automatically"); otherwise the amendment wins. An unauthorised sender, quoted text or text aimed at an "AI" never
creates a conflict, because it is not a source. The range reprices freight and fuel under each source. An amendment
pointing to a rates annex we don't hold gives cannot_determine, addressed to the Schedule 1 `rates` contact.

## Confidence

Each class starts from how mechanical it is (expiry 0.96, a resend duplicate 0.94, a priced overcharge 0.93,
cashback 0.88, conflict 0.82, cannot_determine 0.78). Then it drops for each weakness the brief lists: pickup date not
from tracking or a carrier email (−0.10), terms resting on correspondence or feedback (−0.03), OCR'd document
(−0.08), a feedback-derived answer (−0.02), thin evidence (−0.05). On the practice set the Brier score is 0.009.
AUROC can't be measured there because every item is right, so the weights are judgement plus my eval set.

## People in the loop

Internal actions always run: expiry alerts, payment reminders, credit tracking, and escalating conflicts. Disputes,
credit claims and document requests are `draft:*` in supervised mode, and `human_review` once the outbound amounts
on one invoice or cashback key reach $5,000.00. In autonomous mode a remedy becomes `execute:*` only when all of
these hold:
- no missing document;
- the pickup date comes from tracking or a carrier email;
- no open conflict on the same contract and lane;
- the amount is under $5,000;
- the terms are level 1–2;
- the recipient comes from Schedule 1.

Anything else goes to `human_review`. No code path accepts terms, renews, pays or settles. Practice escalation rate:
4%.

## Feedback

`feedback` stores each file. `learn.py` reads the files at report time, keeping each item's batch and decision date.
`close` and `reject` stop exactly that key. `override_amount` replaces the amount. `one_off_approval` waives only
the named months on that key. `rate_accepted`, `sender_authority` and `precedence` are standing facts. A standing
fact applies only if every `applies_to` filter matches (contract, lane, person, category) and the pickup date falls
in its window, so another lane, contract or category is untouched. Items changed this way carry `learned_at`.

## New documents

Every report rebuilds the world from all documents received by `as_of`, so a backdated amendment reprices earlier
invoices automatically. `reported_<mode>.json` remembers each key's last verdict and amount. A changed answer gets a
new id with `supersedes`. A new key on an invoice we already held is marked `retro`. Keys whose window passed or
whose invoice was paid simply disappear. A new invoice layout needs no code: header labels (issued/due/payable/
settle), id labels and lines are found by meaning and arithmetic. If no line parses and a key is set, ingest asks
Claude for the lines once and caches the answer.

## Cost

The normal path makes no model calls: parsing is deterministic and OCR is local. Measured on the practice mailbox:
163 documents ingested in 21 s (OCR is most of it), report in 1 s. That is about 2 minutes and $0 per 1,000
documents. The fallbacks (a Claude Sonnet call for a scan without OCR, or an unparseable bill) cost roughly
$0.01–0.03 per page and are cached by content hash, so a re-run is free.

## What I'd do next

- Handling-margin step-downs are recognised but not priced.
- Conditional waivers are honoured only when the condition is "paid by a date".
- Late-fee risk uses the latest AP register only.
- A wider LLM fallback for wordings the patterns miss, such as waivers phrased with no "waive" verb, behind the same
  data-only prompt.
- I'm least sure about unusual feedback combinations and about layouts where quantity × rate doesn't add up.
- More evals: margin step-down, partial-month storage, multi-month compounding late fees.
