"""Replay one published covenant using the production calculation engine; no API calls."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pypdf import PdfReader
from halyk_agent.engine import execute_plan
from halyk_agent.io import Ledger, read_json
from halyk_agent.plans import load_fact_pack, validate_plan
from halyk_agent.validation import validate_submission


def run_sample() -> dict:
    sample = ROOT / "examples" / "covenant-sample"
    ledger = Ledger.from_csv(sample / "master_ledger_2025.csv")
    plans = load_fact_pack(sample / "fact_pack.json", sample)
    document_names = {path.name for path in (sample / "documents").glob("*.pdf")}
    for name in document_names:
        reader = PdfReader(sample / "documents" / name)
        if not reader.pages or not any(page.extract_text() for page in reader.pages):
            raise ValueError(f"Sample PDF has no readable text: {name}")
    template = read_json(sample / "submission_template.json")
    result = read_json(sample / "submission_template.json")
    result.update(team="QwertyS demo", contact_email="demo@example.com", model="documented-plan-replay")
    for scenario_id, clauses in plans.items():
        for plan in clauses:
            validate_plan(plan, set(ledger.by_id), document_names)
            result["answers"][scenario_id][plan.clause] = execute_plan(scenario_id, plan, ledger).submission_value()
    validate_submission(result, template, set(ledger.by_id))
    expected = read_json(sample / "expected_answer.json")
    if result["answers"] != expected:
        raise ValueError("Sample no longer matches its recorded public-dataset answer")
    return result


if __name__ == "__main__":
    print(json.dumps(run_sample(), ensure_ascii=False, indent=2))
