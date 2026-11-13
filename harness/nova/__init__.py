"""Nova freight-invoice audit harness.

Layers, in the order data flows through them:
    reader   .eml -> documents with page text (the grader's own text layer; OCR for scans)
    parse    documents -> typed facts by meaning (contracts, invoices, emails, records), each with provenance
    world    facts received by as_of -> one consistent view (latest register, invoice copies, authority)
    rates    term resolution: which rate applies to a shipment, and whether two trusted sources disagree
    detect   the six classes plus conflict / cannot_determine, all arithmetic in Decimal
    learn    feedback.json decisions, applied with strict scope
    actions  verdict -> action per mode, including the unattended-action test
    report   assemble report.json: stable ids, supersedes / retro / learned_at, confidence
"""
