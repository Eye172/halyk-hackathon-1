from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

from .documents import DocumentIndex
from .engine import execute_plan
from .io import Ledger, read_json, sha256_file, write_json, write_json_atomic
from .llm import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    PLANNER_CONTRACT_VERSION,
    LLMError,
    LLMPlanner,
    OpenAIResponsesClient,
    vision_ocr_record,
)
from .models import CovenantResult
from .plans import load_fact_pack, matching_fact_pack, parse_plan, validate_plan
from .quality import build_preflight_report, build_quality_report
from .validation import validate_submission


PACKAGE_DIR = Path(__file__).resolve().parent
PUBLIC_FACT_PACK = PACKAGE_DIR / "config" / "public_fact_pack.json"


def planning_fingerprint(
    *,
    template_path: Path,
    ledger_path: Path,
    index: DocumentIndex,
    model: str,
    reasoning_effort: str,
    review: bool,
) -> str:
    """Bind resumable plans to the exact dataset and planner configuration."""
    payload = {
        "contract_version": PLANNER_CONTRACT_VERSION,
        "template_sha256": sha256_file(template_path),
        "ledger_sha256": sha256_file(ledger_path),
        "documents": sorted((record.path.name, record.sha256) for record in index.records),
        "model": model,
        "reasoning_effort": reasoning_effort,
        "review": review,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def checkpoint_filename(scenario_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", scenario_id)
    digest = hashlib.sha256(scenario_id.encode("utf-8")).hexdigest()[:10]
    return f"{safe}-{digest}.json"


def load_planning_checkpoint(
    path: Path,
    *,
    scenario_id: str,
    clauses: list[str],
    ledger_ids: set[str],
    document_names: set[str],
) -> tuple[list[Any], dict[str, Any] | None]:
    raw = read_json(path)
    if raw.get("contract_version") != PLANNER_CONTRACT_VERSION:
        raise ValueError("planner contract version changed")
    if raw.get("scenario_id") != scenario_id or set(raw.get("clauses", [])) != set(clauses):
        raise ValueError("scenario or clause set changed")
    plans = [parse_plan(item) for item in raw.get("plans", [])]
    if {plan.clause for plan in plans} != set(clauses):
        raise ValueError("checkpoint clause mismatch")
    for plan in plans:
        validate_plan(plan, ledger_ids, document_names)
    review_log = raw.get("review")
    return plans, review_log if isinstance(review_log, dict) else None


def find_required_file(data_dir: Path, filename: str) -> Path:
    direct = data_dir / filename
    if direct.exists():
        return direct
    matches = list(data_dir.rglob(filename))
    if len(matches) != 1:
        raise FileNotFoundError(f"Expected exactly one {filename} under {data_dir}, found {len(matches)}")
    return matches[0]


def preflight_agent(
    data_dir: Path,
    mode: str = "auto",
    team: str = "CHANGE_ME_TEAM",
    contact_email: str = "CHANGE_ME_EMAIL",
    artifacts_dir: Path | None = None,
) -> dict[str, Any]:
    """Inspect every hard dependency without calling an LLM or creating answers."""
    data_dir = data_dir.resolve()
    template_path = find_required_file(data_dir, "submission_template.json")
    ledger_path = find_required_file(data_dir, "master_ledger_2025.csv")
    documents_dir = template_path.parent / "documents"
    if not documents_dir.exists():
        raise FileNotFoundError(f"Documents directory not found: {documents_dir}")
    artifacts_dir = (artifacts_dir or Path("artifacts")).resolve()
    template = read_json(template_path)
    ledger = Ledger.from_csv(ledger_path)
    index = DocumentIndex.build(
        documents_dir,
        artifacts_dir / "document_text_cache",
        {txn.account_id for txn in ledger.transactions},
    )
    public_match = matching_fact_pack(PUBLIC_FACT_PACK, template_path.parent)
    report = build_preflight_report(
        data_dir=data_dir,
        template=template,
        ledger=ledger,
        index=index,
        requested_mode=mode,
        public_match=public_match,
        team=team,
        contact_email=contact_email,
    )
    write_json(artifacts_dir / "preflight_report.json", report)
    return report


def run_agent(
    data_dir: Path,
    output_path: Path,
    team: str,
    contact_email: str,
    mode: str = "auto",
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    review: bool = True,
    workers: int = 3,
    artifacts_dir: Path | None = None,
    resume: bool = True,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    def emit(message: str) -> None:
        if progress_callback is not None:
            progress_callback(message)

    data_dir = data_dir.resolve()
    if workers < 1 or workers > 6:
        raise ValueError("workers must be between 1 and 6")
    template_path = find_required_file(data_dir, "submission_template.json")
    ledger_path = find_required_file(data_dir, "master_ledger_2025.csv")
    documents_dir = template_path.parent / "documents"
    if not documents_dir.exists():
        raise FileNotFoundError(f"Documents directory not found: {documents_dir}")
    template = read_json(template_path)
    ledger = Ledger.from_csv(ledger_path)
    artifacts_dir = (artifacts_dir or output_path.parent / "artifacts").resolve()
    cache_dir = artifacts_dir / "document_text_cache"
    index = DocumentIndex.build(
        documents_dir,
        cache_dir,
        {txn.account_id for txn in ledger.transactions},
    )

    public_match = matching_fact_pack(PUBLIC_FACT_PACK, template_path.parent)
    review_log: dict[str, dict[str, Any]] = {}
    planning_checkpoint_info: dict[str, Any] = {
        "resume_enabled": resume,
        "fingerprint": None,
        "reused_scenarios": [],
    }
    client: OpenAIResponsesClient | None = None
    preflight = build_preflight_report(
        data_dir=data_dir,
        template=template,
        ledger=ledger,
        index=index,
        requested_mode=mode,
        public_match=public_match,
        team=team,
        contact_email=contact_email,
    )
    write_json(artifacts_dir / "preflight_report.json", preflight)
    if not preflight["hard_gates_passed"]:
        failed = [item["title"] for item in preflight["checks"] if item["status"] == "fail"]
        raise ValueError(f"Preflight failed: {', '.join(failed)}")
    if mode == "public" and not public_match:
        raise ValueError("--mode public is allowed only for the fingerprinted public dataset")
    emit(
        f"[preflight] route={preflight['resolved_route']} documents={len(index.records)} "
        f"transactions={len(ledger.transactions)}"
    )
    if mode in {"auto", "public"} and public_match:
        plans = load_fact_pack(PUBLIC_FACT_PACK, template_path.parent)
        model_label = "hybrid-deterministic-public-v1"
        planning_mode = "fingerprinted-public-fact-pack"
    else:
        client = OpenAIResponsesClient(model=model, reasoning_effort=reasoning_effort)
        planner = LLMPlanner(client)
        vision_cache = artifacts_dir / "vision_ocr_cache"
        vision_cache.mkdir(parents=True, exist_ok=True)
        ocr_total = sum(bool(record.low_text_pages) for record in index.records)
        ocr_completed = 0
        for record in index.records:
            if not record.low_text_pages:
                continue
            cache_file = vision_cache / f"{record.sha256}.txt"
            if cache_file.exists():
                ocr_text = cache_file.read_text(encoding="utf-8")
            else:
                try:
                    ocr_text = vision_ocr_record(client, record)
                except LLMError as exc:
                    raise LLMError(f"Vision OCR failed for {record.path.name}: {exc}") from exc
                cache_file.write_text(ocr_text, encoding="utf-8")
            index.replace_text(record, ocr_text)
            ocr_completed += 1
            emit(f"[ocr] {ocr_completed}/{ocr_total} {record.path.name}")
        ledger_ids = set(ledger.by_id)
        document_names = {item.path.name for item in index.records}
        fingerprint = planning_fingerprint(
            template_path=template_path,
            ledger_path=ledger_path,
            index=index,
            model=model,
            reasoning_effort=reasoning_effort,
            review=review,
        )
        checkpoint_dir = artifacts_dir / "planning_checkpoints" / fingerprint
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        progress_path = artifacts_dir / "planning_progress.json"
        planning_checkpoint_info["fingerprint"] = fingerprint
        planned: dict[str, list[Any]] = {}
        reused_scenarios: list[str] = []
        invalid_checkpoints: list[str] = []
        scenario_items = [(scenario_id, list(clauses)) for scenario_id, clauses in template["answers"].items()]

        if resume:
            for scenario_id, clauses in scenario_items:
                checkpoint_path = checkpoint_dir / checkpoint_filename(scenario_id)
                if not checkpoint_path.exists():
                    continue
                try:
                    cached_plans, cached_review = load_planning_checkpoint(
                        checkpoint_path,
                        scenario_id=scenario_id,
                        clauses=clauses,
                        ledger_ids=ledger_ids,
                        document_names=document_names,
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    invalid_checkpoints.append(scenario_id)
                    continue
                planned[scenario_id] = cached_plans
                reused_scenarios.append(scenario_id)
                if cached_review is not None:
                    planner.review_log[scenario_id] = cached_review

        planning_checkpoint_info["reused_scenarios"] = reused_scenarios
        planning_checkpoint_info["invalid_checkpoints"] = invalid_checkpoints
        remaining = [item for item in scenario_items if item[0] not in planned]
        emit(
            f"[planning] total={len(scenario_items)} reused={len(reused_scenarios)} "
            f"pending={len(remaining)} workers={workers}"
        )

        def write_progress(status: str, failed: dict[str, str] | None = None) -> None:
            write_json_atomic(progress_path, {
                "status": status,
                "fingerprint": fingerprint,
                "contract_version": PLANNER_CONTRACT_VERSION,
                "resume_enabled": resume,
                "scenarios_total": len(scenario_items),
                "scenarios_completed": sorted(planned),
                "scenarios_reused": sorted(reused_scenarios),
                "scenarios_pending": [item[0] for item in scenario_items if item[0] not in planned],
                "failed": failed,
                "llm_usage_current_process": client.usage_snapshot(),
            })

        def plan_and_checkpoint(scenario_id: str, clauses: list[str]) -> list[Any]:
            account_id = ledger.account_for_scenario(scenario_id)
            records = index.for_scenario(scenario_id, account_id)
            scenario_plans = planner.plan_scenario(
                scenario_id,
                account_id,
                clauses,
                records,
                ledger,
                review,
            )
            write_json_atomic(
                checkpoint_dir / checkpoint_filename(scenario_id),
                {
                    "contract_version": PLANNER_CONTRACT_VERSION,
                    "scenario_id": scenario_id,
                    "clauses": clauses,
                    "plans": [plan.trace_value() for plan in scenario_plans],
                    "review": planner.review_log.get(scenario_id),
                },
            )
            return scenario_plans

        write_progress("running")
        queue = iter(remaining)
        active: dict[Future[list[Any]], str] = {}
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="halyk-plan")

        def fill_worker_slots() -> None:
            while len(active) < workers:
                try:
                    scenario_id, clauses = next(queue)
                except StopIteration:
                    return
                active[pool.submit(plan_and_checkpoint, scenario_id, clauses)] = scenario_id

        fill_worker_slots()
        failure: tuple[str, Exception] | None = None
        try:
            while active and failure is None:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    scenario_id = active.pop(future)
                    try:
                        planned[scenario_id] = future.result()
                        emit(f"[planning] completed {scenario_id} ({len(planned)}/{len(scenario_items)})")
                    except Exception as exc:  # Preserve checkpoints, then stop scheduling paid calls.
                        failure = (scenario_id, exc)
                        emit(f"[planning] failed {scenario_id}: {str(exc)[:300]}")
                write_progress("failed" if failure else "running")
                if failure is None:
                    fill_worker_slots()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        # Harvest any in-flight scenarios that completed while shutdown waited. Their
        # checkpoint is already durable even if this run must still report the first error.
        for future, scenario_id in list(active.items()):
            if not future.done() or future.cancelled():
                continue
            try:
                planned[scenario_id] = future.result()
            except Exception:
                pass
        if failure is not None:
            failed_scenario, failed_error = failure
            write_json(artifacts_dir / "plan_review.json", {"scenarios": planner.review_log})
            write_progress("failed", {"scenario_id": failed_scenario, "error": str(failed_error)[:1500]})
            raise LLMError(
                f"{failed_scenario}: planning failed: {failed_error}. "
                f"Saved {len(planned)}/{len(scenario_items)} scenario checkpoints; rerun the same "
                "command to resume without repaying for completed scenarios."
            ) from failed_error

        write_progress("complete")
        emit(f"[planning] complete {len(planned)}/{len(scenario_items)}")
        plans = {scenario_id: planned[scenario_id] for scenario_id in template["answers"]}
        model_label = model
        planning_mode = "llm-semantic-planner"
        write_json(artifacts_dir / "plan_review.json", {"scenarios": planner.review_log})
        review_log = planner.review_log

    required_scenarios = set(template["answers"])
    if set(plans) != required_scenarios:
        raise ValueError(f"Plan scenarios differ from template: {sorted(set(plans) ^ required_scenarios)}")

    submission = copy.deepcopy(template)
    submission["team"] = team
    submission["contact_email"] = contact_email
    submission["model"] = model_label
    traces: list[dict[str, Any]] = []
    results: list[CovenantResult] = []
    document_names = {item.path.name for item in index.records}
    semantic_plans = {
        scenario_id: [plan.trace_value() for plan in scenario_plans]
        for scenario_id, scenario_plans in plans.items()
    }
    write_json(
        artifacts_dir / "semantic_plans.json",
        {"planning_mode": planning_mode, "scenarios": semantic_plans},
    )
    for scenario_id, clauses in template["answers"].items():
        scenario_plans = {plan.clause: plan for plan in plans[scenario_id]}
        if set(scenario_plans) != set(clauses):
            raise ValueError(f"{scenario_id}: plan clause mismatch")
        for clause in clauses:
            plan = scenario_plans[clause]
            validate_plan(plan, set(ledger.by_id), document_names)
            result = execute_plan(scenario_id, plan, ledger)
            submission["answers"][scenario_id][clause] = result.submission_value()
            traces.append(result.trace_value())
            results.append(result)

    validate_submission(submission, template, set(ledger.by_id))
    write_json(output_path.resolve(), submission)
    emit(f"[validation] submission valid: {output_path.resolve()}")
    write_json(artifacts_dir / "decision_trace.json", {"planning_mode": planning_mode, "results": traces})
    manifest = index.manifest()
    write_json(artifacts_dir / "document_manifest.json", manifest)
    quality = build_quality_report(
        submission=submission,
        template=template,
        ledger=ledger,
        index=index,
        plans=plans,
        results=results,
        planning_mode=planning_mode,
        output_path=output_path.resolve(),
        plan_review=review_log,
    )
    write_json(artifacts_dir / "quality_report.json", quality)
    report = {
        "status": "ok" if quality["hard_gates_passed"] else "attention_required",
        "planning_mode": planning_mode,
        "model": model_label,
        "data_dir": str(data_dir),
        "output": str(output_path.resolve()),
        "transactions": len(ledger.transactions),
        "documents": len(index.records),
        "pages": sum(len(item.pages) for item in index.records),
        "low_text_pages": sum(len(item.low_text_pages) for item in index.records),
        "scenarios": len(template["answers"]),
        "covenant_cells": sum(len(value) for value in template["answers"].values()),
        "metadata_placeholders": [
            key for key, value in {"team": team, "contact_email": contact_email}.items()
            if not value or value.startswith("CHANGE_ME")
        ],
        "openai_api_key_used": planning_mode == "llm-semantic-planner" and bool(os.environ.get("OPENAI_API_KEY")),
        "llm_second_pass_review": planning_mode == "llm-semantic-planner" and review,
        "planning_workers": workers if planning_mode == "llm-semantic-planner" else 0,
        "llm_usage": client.usage_snapshot() if client is not None else None,
        "planning_checkpoints": planning_checkpoint_info,
        "quality_status": quality["status"],
        "hard_gates_passed": quality["hard_gates_passed"],
        "readiness_score": quality["readiness_score"],
        "submission_sha256": quality["submission_sha256"],
    }
    write_json(artifacts_dir / "run_report.json", report)
    emit(f"[quality] status={quality['status']} readiness={quality['readiness_score']}")
    return report
