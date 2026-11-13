# AI use

## Tools

- Claude Code (Claude Opus 5.5) in the Claude desktop app, for the whole of this version:
  - reading the brief, glossary, grader and data;
  - writing the plan;
  - writing `harness/` (the `nova` package) and the eval builder and cases;
  - running the practice grader, exam sheet and evals;
  - drafting SOLUTION.md and DESIGN.md.
- No model is called at run time on the normal path. The harness has optional Claude Sonnet fallbacks (scan
  transcription, line extraction), which are cached and off without a key.

## How you worked with them

1. I asked Claude to read the take-home repo and write a detailed plan, then to build everything locally from that
   plan.
2. Claude read every practice and exam document (dumped through `grader/extract.py`) and worked out each practice
   scenario against its answer file.
3. It then wrote the reader, parser, world model, rate resolution, detectors, feedback, actions and report, testing
   against the practice grader after each step.

What was checked, and how:
- **Exam sheet.** It has no answers, so each exam figure was computed by hand from the documents before trusting the
  harness. That covers each overcharge, the OCR'd scan, the Q3 rebate, the conflict range and the late-fee risk.
- **Eval set.** It was written to probe generalisation (new layouts and wordings, edge dates, injection, feedback)
  rather than the practice files. One case caught a real parser bug (see SOLUTION.md).
- **Two claims checked and dropped.** A first "bug caught" claim did not hold up: the old pattern still passed
  through a subject-line fallback, so it was dropped. A hard-coded-answer scan was also done: no practice ids, names
  or lanes appear in `harness/`.

Where it went wrong:
- A first regex for the contract effective date missed "Effective date. This Agreement takes effect on…".
- An invoice-id pattern missed "Tax Invoice No: X".
- The parser dropped whole-number amounts.
- An eval spreadsheet lacked number formats.

All four were caught by probing the parsed facts or by eval cases, and fixed.

## Written without AI

None of the code. I directed the work and reviewed and ran the results.

## Prompt log

My prompts in the Claude Code session, verbatim:

1. "Im given this assesment from GoComet end and help me create a detailed plan of it and write that plan in a md
   file and genrtae it for mr in my donwloads folders : https://github.com/gocomet-india/nova-takehome"
2. "okay start building locallyl evytuhing on this same md file and dont need to ask me evceything and not
   necassrily it will take 16 hours if it takes lesser than do it in lesser too and after u r done tell me ill push
   eveything to my github remote repo"

The full session transcript (Claude's reasoning, tool calls and outputs) is in `ai_log/claude-code-session.zip`,
the app's export of this session. I scanned it for API keys and found none, so nothing was trimmed.
