"""
Evaluation script for Phase 6 Evidence Validation in Self Search.

Evaluates EvidenceValidator against the curated benchmark dataset
in `tests/fixtures/evidence_validation_cases.json`.

Computes:
- Precision, Recall, False-Positive Rate, Accuracy
- Error breakdown by failure mode (fabricated citations, cross-product citations, numeric mismatches, ungrounded claims)
- Detailed limitations disclaimer per Section 0.2 and Phase 6 requirements.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.evidence.evidence_pack import EvidenceItem, EvidencePack
from src.evidence.report import ValidationStatus
from src.evidence.validator import EvidenceValidator
from src.llm.schemas import EvidenceClaim, ProductRecommendation, StructuredAnswer


def run_evaluation() -> dict:
    cases_path = PROJECT_ROOT / "tests" / "fixtures" / "evidence_validation_cases.json"
    if not cases_path.exists():
        print(f"Error: Fixture {cases_path} not found.")
        sys.exit(1)

    with open(cases_path, "r", encoding="utf-8") as f:
        cases = json.load(f)

    db_path = PROJECT_ROOT / "data" / "processed" / "shelfsense.db"
    conn = sqlite3.connect(str(db_path))

    validator = EvidenceValidator(conn=conn, mode="flag")

    tp = 0  # Unsupported claim correctly flagged
    fp = 0  # Supported claim incorrectly flagged as unsupported
    fn = 0  # Unsupported claim missed (treated as supported)
    tn = 0  # Supported claim correctly verified as supported

    category_results: dict[str, dict[str, int]] = {}

    print("=" * 80)
    print(" SHELFSENSE PHASE 6: EVIDENCE VALIDATION BENCHMARK EVALUATION")
    print("=" * 80)
    print(f"Loaded {len(cases)} test cases from {cases_path.name}\n")

    for idx, case in enumerate(cases, start=1):
        case_id = case["id"]
        category = case["category"]
        product_id = case["product_id"]
        claim_text = case["claim"]
        evidence_ids = case["evidence_ids"]
        expected_supported = case["expected_supported"]
        expected_status = case["expected_status"]

        # Build evidence pack items
        items = []
        for ep_data in case.get("evidence_pack", []):
            items.append(
                EvidenceItem(
                    evidence_id=ep_data["evidence_id"],
                    product_id=ep_data["product_id"],
                    product_external_id=f"P{ep_data['product_id']:03d}",
                    review_id=ep_data.get("review_id", 1),
                    title="Review",
                    text=ep_data["text"],
                    rating=ep_data.get("rating", 5.0),
                    estimated_tokens=len(ep_data["text"]) // 4,
                )
            )

        pack = EvidencePack(query="benchmark evaluation query", items=items)

        # Build answer
        answer = StructuredAnswer(
            summary="Benchmark evaluation answer",
            recommendations=[
                ProductRecommendation(
                    product_id=product_id,
                    external_id=f"P{product_id:03d}",
                    title="Evaluation Product",
                    why_it_matches=[
                        EvidenceClaim(claim=claim_text, evidence_ids=evidence_ids)
                    ],
                )
            ],
        )

        sanitized, report = validator.validate_answer(answer, pack)
        actual_result = report.claim_results[0]
        actual_supported = actual_result.is_supported
        actual_status = actual_result.status.value

        # A "positive" in fraud/hallucination detection is an unsupported/invalid claim
        # A "negative" is a supported/valid claim
        is_ground_truth_unsupported = not expected_supported
        is_detected_unsupported = not actual_supported

        if is_ground_truth_unsupported and is_detected_unsupported:
            tp += 1
            verdict = "TP (Detected Unsupported)"
        elif not is_ground_truth_unsupported and not is_detected_unsupported:
            tn += 1
            verdict = "TN (Verified Supported)"
        elif not is_ground_truth_unsupported and is_detected_unsupported:
            fp += 1
            verdict = "FP (False Alarm)"
        else:
            fn += 1
            verdict = "FN (Missed Unsupported)"

        if category not in category_results:
            category_results[category] = {"total": 0, "correct": 0}
        category_results[category]["total"] += 1
        if actual_supported == expected_supported and (
            actual_status == expected_status or actual_supported is True
        ):
            category_results[category]["correct"] += 1

        status_flag = "[PASS]" if actual_supported == expected_supported else "[FAIL]"
        print(f"[{idx:02d}] {case_id:<38} | {verdict:<25} | {status_flag}")
        print(f"     Claim: '{claim_text}'")
        print(f"     Expected: status={expected_status}, supported={expected_supported}")
        print(f"     Actual:   status={actual_status}, supported={actual_supported}")
        if actual_result.reasons:
            print(f"     Reasons:  {actual_result.reasons}")
        print("-" * 80)

    total_samples = len(cases)
    precision = (tp / (tp + fp)) if (tp + fp) > 0 else 1.0
    recall = (tp / (tp + fn)) if (tp + fn) > 0 else 1.0
    fpr = (fp / (fp + tn)) if (fp + tn) > 0 else 0.0
    accuracy = ((tp + tn) / total_samples) if total_samples > 0 else 1.0

    print("\n" + "=" * 80)
    print(" EVALUATION METRICS SUMMARY")
    print("=" * 80)
    print(f"Total Test Cases (Sample Size N): {total_samples}")
    print(f"True Positives (TP - Hallucinations Caught):   {tp}")
    print(f"True Negatives (TN - Valid Claims Supported):   {tn}")
    print(f"False Positives (FP - Valid Claims Rejected):   {fp}")
    print(f"False Negatives (FN - Hallucinations Missed):   {fn}")
    print(f"Precision:            {precision:.4f} ({precision * 100:.1f}%)")
    print(f"Recall:               {recall:.4f} ({recall * 100:.1f}%)")
    print(f"False-Positive Rate:  {fpr:.4f} ({fpr * 100:.1f}%)")
    print(f"Overall Accuracy:     {accuracy:.4f} ({accuracy * 100:.1f}%)")

    print("\nBreakdown by Category:")
    for cat, data in category_results.items():
        cat_acc = data["correct"] / data["total"]
        print(f"  - {cat:<30}: {data['correct']}/{data['total']} correct ({cat_acc*100:.1f}%)")

    print("\n" + "=" * 80)
    print(" LIMITATIONS & METHODOLOGY DISCLAIMER")
    print("=" * 80)
    print("1. Curated Benchmark Scope:")
    print("   These results reflect evaluation on a targeted, curated offline fixture dataset.")
    print("   100% precision/recall on this suite does NOT imply zero hallucination or universal")
    print("   factual correctness across arbitrary real-world shopping queries.")
    print("2. Deterministic vs Heuristic Validation:")
    print("   - Deterministic checks (exact ID matching, product-review alignment, SQLite price/rating")
    print("     recomputation) have exact reliability with 0 false positives under specified tolerances.")
    print("   - Lexical overlap (Jaccard) and embedding cosine similarity are heuristic supporting signals.")
    print("     They detect obvious semantic drift or ungrounded assertions but cannot verify subtle")
    print("     claims (e.g., negations, paraphrased technical claims, or subtle aspect qualifiers).")
    print("3. Missing Database Data:")
    print("   When database attributes are absent (NULL), claims are assigned UNVERIFIABLE to avoid")
    print("   false negative penalization. However, unverifiable claims remain unproven.")
    print("=" * 80)

    conn.close()

    return {
        "sample_size": total_samples,
        "true_positives": tp,
        "true_negatives": tn,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": precision,
        "recall": recall,
        "false_positive_rate": fpr,
        "accuracy": accuracy,
        "category_results": category_results,
    }


if __name__ == "__main__":
    run_evaluation()
