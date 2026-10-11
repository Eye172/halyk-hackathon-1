# One covenant, with its supporting evidence

This is a deliberately small excerpt of the **public Halyk AI Challenge dataset**:
one scenario (`P1`), one covenant (`6.1`), **two PDF documents and four ledger rows**.
It contains no hidden-stage documents and is not the complete benchmark.

| File | Purpose |
| --- | --- |
| [Loan agreement](documents/8d878af064f2.pdf) | The borrower's covenant and threshold |
| [Auditor's notes](documents/896b7933db48.pdf) | Reporting period and transaction treatment |
| [Ledger excerpt](master_ledger_2025.csv) | Three calculation inputs and one excluded transaction |
| [Calculation plan](fact_pack.json) | Formula, input IDs, document references and rationale |
| [Answer template](submission_template.json) | One empty output cell |
| [Expected answer](expected_answer.json) | One reference result from the public challenge |
| [Provenance](provenance.json) | Scope and SHA-256 checksums |

The original PDFs and selected CSV field values are retained unchanged. Templates
and the fact pack are reduced to this one covenant. These are challenge materials,
not evidence of a live customer deployment. No additional redistribution license
is claimed for the organizer's materials.

## Follow the calculation

The capital-intensity covenant requires:

```text
CapEx / (operating costs + terminal land lease) <= 0.42
1,842,006.44 / (3,104,882.61 + 918,443.27) = 0.4578317777...
```

Amounts are taken as expense magnitudes by the existing engine. The auditor's
notes exclude `TXN-P1-0045` from the 2025 calculation; it remains in the excerpt so
the reader can inspect the difference between a ledger entry and an eligible input.
The verdict is made before display rounding. The result is `BREACH`, displayed as
`0.46`. A breach does not automatically make an arbitrary transaction valid evidence;
this reference cell has `evidence_txn_id: null`.

## Reproduce from the source repository

From the root of [Eye172/halyk-hackathon-1](https://github.com/Eye172/halyk-hackathon-1):

```bash
python -m pip install -r requirements.txt
python tools/run_sample.py
```

The source repository and all sample inputs are publicly available.
The script reads both PDFs, checks the sample fingerprint and source references,
executes the existing Decimal calculation engine, validates the output and compares
it with this single reference answer. It makes **no API calls** and requires no key.
This is a replay of a documented plan, not an autonomous LLM extraction benchmark.

Full challenge archives, ledgers, answer packs, OCR caches and run outputs are
intentionally absent. The complete 36-cell benchmark requires separately supplied
local data and its calibration fact pack.
