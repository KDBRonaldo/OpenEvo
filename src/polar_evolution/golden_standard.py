from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load_golden_standard_records(path: Path) -> list[dict[str, Any]]:
    """Load reference records from JSON object/list or JSONL."""

    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if not stripped:
        return []
    if stripped.startswith("[") or stripped.startswith("{"):
        payload = json.loads(text)
        if isinstance(payload, dict):
            records = payload.get("ground_truth_records", [])
        else:
            records = payload
    else:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [dict(record) for record in records if isinstance(record, dict)]


def evaluate_records_against_golden(
    predicted_records: list[dict[str, Any]],
    golden_records: list[dict[str, Any]],
) -> dict[str, Any]:
    aliases, canonical_article_ids = _golden_article_aliases(golden_records)
    sequence_article_lookup = _sequence_article_lookup(golden_records)
    golden_pairs = {
        (_golden_record_article_key(record), _normalize_sequence(record.get("sequence")))
        for record in golden_records
        if _normalize_sequence(record.get("sequence"))
    }
    predicted_pairs_list = [
        (
            _candidate_article_key(
                record,
                aliases,
                canonical_article_ids,
                sequence_article_lookup,
            ),
            _normalize_sequence(record.get("sequence")),
        )
        for record in predicted_records
        if _normalize_sequence(record.get("sequence"))
    ]
    predicted_pairs = set(predicted_pairs_list)

    true_positive_pairs = predicted_pairs & golden_pairs
    false_positive_pairs = predicted_pairs - golden_pairs
    false_negative_pairs = golden_pairs - predicted_pairs
    duplicate_predictions = max(0, len(predicted_pairs_list) - len(predicted_pairs))

    articles = _article_metrics(
        golden_pairs=golden_pairs,
        predicted_pairs=predicted_pairs,
        canonical_article_ids=canonical_article_ids,
    )
    return {
        "summary": {
            "gold_unique": len(golden_pairs),
            "predicted_total": len(predicted_pairs_list),
            "predicted_unique": len(predicted_pairs),
            "true_positive": len(true_positive_pairs),
            "false_positive": len(false_positive_pairs),
            "false_negative": len(false_negative_pairs),
            "duplicate_predictions": duplicate_predictions,
            **_precision_recall_f1(
                true_positive=len(true_positive_pairs),
                false_positive=len(false_positive_pairs),
                false_negative=len(false_negative_pairs),
            ),
        },
        "articles": articles,
        "leakage_basis": _leakage_basis(golden_records),
    }


def render_sanitized_golden_feedback(
    evaluation: dict[str, Any],
    *,
    max_article_buckets: int = 5,
) -> str:
    summary = _dict(evaluation.get("summary"))
    articles = _dict(evaluation.get("articles"))
    gold_unique = _number(summary.get("gold_unique"))
    predicted_unique = _number(summary.get("predicted_unique"))
    duplicate_predictions = _number(summary.get("duplicate_predictions"))
    predicted_ratio = predicted_unique / gold_unique if gold_unique else 0.0
    duplicate_rate = duplicate_predictions / max(_number(summary.get("predicted_total")), 1.0)
    precision = _number(summary.get("precision"))
    recall = _number(summary.get("recall"))
    f1 = _number(summary.get("f1"))
    fp = _number(summary.get("false_positive"))
    fn = _number(summary.get("false_negative"))

    lines = [
        "## Shared Golden Standard Evaluation (Sanitized)",
        "",
        "- A private evaluator compared candidate records to a held-out reference; keep the reference out of future task workspaces and instructions.",
        (
            "- Aggregate fit: "
            f"precision={precision:.3f}, recall={recall:.3f}, f1={f1:.3f}, "
            f"prediction/reference ratio={predicted_ratio:.2f}x, "
            f"duplicate rate={duplicate_rate:.3f}."
        ),
        "",
        "### Methodology Feedback",
        "",
    ]
    if fp > fn * 1.5:
        lines.append(
            "- Primary gap: over-inclusion. Do not treat every sequence-like column as an accepted component; first infer the paper's assayed component boundary and exclude flanks, constructs, context, motifs, and auxiliary sequences."
        )
    elif fn > fp * 1.5:
        lines.append(
            "- Primary gap: under-extraction. After identifying an eligible component class, enumerate the full eligible table or supplement instead of sampling examples."
        )
    else:
        lines.append(
            "- Primary gap: mixed boundary errors. Tighten inclusion rules before bulk extraction, then verify coverage after extraction."
        )
    if recall < 0.8:
        lines.append(
            "- Coverage check: review every allowed source bundle and confirm that eligible component classes were not skipped."
        )
    if precision < 0.8:
        lines.append(
            "- Precision check: reject rows whose sequence represents a construct, scaffold, barcode, primer, motif, short context, or measurement-only feature rather than the requested component."
        )

    bucket_rows = sorted(
        articles.values(),
        key=lambda item: (
            _number(_dict(item).get("f1")),
            -abs(_number(_dict(item).get("predicted_to_gold_ratio")) - 1.0),
        ),
    )[:max_article_buckets]
    if bucket_rows:
        lines.extend(["", "### Source Buckets", ""])
    for index, row_value in enumerate(bucket_rows, 1):
        row = _dict(row_value)
        lines.append(
            "- "
            f"source_bucket_{index}: "
            f"precision={_number(row.get('precision')):.3f}, "
            f"recall={_number(row.get('recall')):.3f}, "
            f"prediction/reference ratio={_number(row.get('predicted_to_gold_ratio')):.2f}x, "
            f"issue={_bucket_issue(row)}."
        )
    lines.extend(
        [
            "",
            "### Leakage Guard",
            "",
            "- Convert the feedback into general workflow rules only. Do not copy held-out literals, source filenames, source sheet names, row numbers, article titles, or reference records into the agent system.",
        ]
    )
    return "\n".join(lines)


def find_golden_leaks(
    text: str,
    golden_records: list[dict[str, Any]],
    *,
    min_sequence_length: int = 20,
) -> list[dict[str, str]]:
    leaks: list[dict[str, str]] = []
    haystack = text.lower()
    sequence_haystack = _normalize_sequence(text)
    for kind, literal in _forbidden_literals(
        golden_records,
        min_sequence_length=min_sequence_length,
    ):
        if not literal:
            continue
        if kind == "sequence":
            if _normalize_sequence(literal) in sequence_haystack:
                leaks.append({"kind": kind, "value": literal})
        elif kind == "source_row":
            if _source_row_reference_found(text, literal):
                leaks.append({"kind": kind, "value": literal})
        elif literal.lower() in haystack:
            leaks.append({"kind": kind, "value": literal})
    return _unique_leaks(leaks)


def assert_no_golden_leakage(text: str, golden_records: list[dict[str, Any]]) -> None:
    leaks = find_golden_leaks(text, golden_records)
    if leaks:
        kinds = sorted({leak["kind"] for leak in leaks})
        raise ValueError(f"golden standard leakage detected: {', '.join(kinds)}")


def _article_metrics(
    *,
    golden_pairs: set[tuple[str, str]],
    predicted_pairs: set[tuple[str, str]],
    canonical_article_ids: dict[str, str],
) -> dict[str, dict[str, Any]]:
    article_keys = {article_key for article_key, _ in golden_pairs | predicted_pairs}
    articles: dict[str, dict[str, Any]] = {}
    for article_key in sorted(article_keys):
        article_golden = {pair for pair in golden_pairs if pair[0] == article_key}
        article_predicted = {pair for pair in predicted_pairs if pair[0] == article_key}
        tp = len(article_golden & article_predicted)
        fp = len(article_predicted - article_golden)
        fn = len(article_golden - article_predicted)
        metrics = _precision_recall_f1(true_positive=tp, false_positive=fp, false_negative=fn)
        gold_count = len(article_golden)
        predicted_count = len(article_predicted)
        articles[article_key] = {
            "article_id": canonical_article_ids.get(article_key, article_key),
            "gold_unique": gold_count,
            "predicted_unique": predicted_count,
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "predicted_to_gold_ratio": predicted_count / gold_count if gold_count else 0.0,
            **metrics,
        }
    return articles


def _precision_recall_f1(
    *,
    true_positive: int,
    false_positive: int,
    false_negative: int,
) -> dict[str, float]:
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def _golden_article_aliases(
    records: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    aliases: dict[str, str] = {}
    canonical_article_ids: dict[str, str] = {}
    for record in records:
        canonical_key = _golden_record_article_key(record)
        if not canonical_key:
            continue
        article_id = str(record.get("article_id") or canonical_key)
        canonical_article_ids.setdefault(canonical_key, article_id)
        for alias in _record_article_alias_values(record):
            alias_key = _normalize_article(alias)
            if alias_key:
                aliases[alias_key] = canonical_key
    return aliases, canonical_article_ids


def _golden_record_article_key(record: dict[str, Any]) -> str:
    for key in ("article_key", "article_keys", "article_id"):
        value = record.get(key)
        if isinstance(value, list):
            for item in value:
                canonical = _canonical_article_key(item)
                if canonical:
                    return canonical
        else:
            canonical = _canonical_article_key(value)
            if canonical:
                return canonical
    return "unknown"


def _candidate_article_key(
    record: dict[str, Any],
    aliases: dict[str, str],
    canonical_article_ids: dict[str, str],
    sequence_article_lookup: dict[str, set[str]],
) -> str:
    del canonical_article_ids
    for key in ("article_id", "article_key", "source_article", "article_title"):
        value = record.get(key)
        normalized = _normalize_article(value)
        if normalized:
            if normalized in aliases:
                return aliases[normalized]
            return normalized
    sequence_match = _unique_sequence_article(record, sequence_article_lookup)
    if sequence_match:
        return sequence_match
    return "unknown"


def _record_article_alias_values(record: dict[str, Any]) -> list[Any]:
    values: list[Any] = [
        record.get("article_id"),
        record.get("article_key"),
        record.get("article_title"),
    ]
    article_keys = record.get("article_keys")
    if isinstance(article_keys, list):
        values.extend(article_keys)
    return values


def _normalize_article(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _canonical_article_key(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def _normalize_sequence(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value)).upper()


def _sequence_article_lookup(records: list[dict[str, Any]]) -> dict[str, set[str]]:
    lookup: dict[str, set[str]] = {}
    for record in records:
        sequence = _normalize_sequence(record.get("sequence"))
        if sequence:
            lookup.setdefault(sequence, set()).add(_golden_record_article_key(record))
    return lookup


def _unique_sequence_article(
    record: dict[str, Any],
    sequence_article_lookup: dict[str, set[str]],
) -> str:
    sequence = _normalize_sequence(record.get("sequence"))
    article_keys = sequence_article_lookup.get(sequence, set())
    if len(article_keys) == 1:
        return next(iter(article_keys))
    return ""


def _leakage_basis(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        "article_ids": sorted({str(record.get("article_id")) for record in records if record.get("article_id")}),
        "article_titles": sorted({str(record.get("article_title")) for record in records if record.get("article_title")}),
        "source_sheets": sorted({str(record.get("source_sheet")) for record in records if record.get("source_sheet")}),
        "source_files": sorted({str(record.get("source_file")) for record in records if record.get("source_file")}),
        "source_rows": sorted({row for record in records if (row := _source_row_literal(record.get("source_row")))}),
        "sequences": sorted({_normalize_sequence(record.get("sequence")) for record in records if _normalize_sequence(record.get("sequence"))}),
    }


def _forbidden_literals(
    records: list[dict[str, Any]],
    *,
    min_sequence_length: int,
) -> list[tuple[str, str]]:
    literals: list[tuple[str, str]] = []
    for record in records:
        article_id = str(record.get("article_id") or "").strip()
        if len(article_id) >= 6:
            literals.append(("article_id", article_id))
        article_title = str(record.get("article_title") or "").strip()
        if len(article_title) >= 6:
            literals.append(("article_title", article_title))
        sequence = _normalize_sequence(record.get("sequence"))
        if len(sequence) >= min_sequence_length:
            literals.append(("sequence", sequence))
        source_sheet = str(record.get("source_sheet") or "").strip()
        if len(source_sheet) >= 3:
            literals.append(("source_sheet", source_sheet))
        source_row = _source_row_literal(record.get("source_row"))
        if source_row:
            literals.append(("source_row", source_row))
        source_file = str(record.get("source_file") or "").strip()
        if source_file:
            path = Path(source_file)
            for candidate in {source_file, path.name, path.stem}:
                if len(candidate) >= 6:
                    literals.append(("source_file", candidate))
    return literals


def _source_row_literal(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text if re.fullmatch(r"\d+", text) else ""


def _source_row_reference_found(text: str, row: str) -> bool:
    return (
        re.search(
            rf"\b(?:source[_\s-]*row|row)\s*(?:[:=#]\s*)?{re.escape(row)}\b",
            text,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _unique_leaks(leaks: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, str]] = []
    for leak in leaks:
        key = (leak["kind"], leak["value"].lower())
        if key not in seen:
            seen.add(key)
            unique.append(leak)
    return unique


def _bucket_issue(row: dict[str, Any]) -> str:
    fp = _number(row.get("false_positive"))
    fn = _number(row.get("false_negative"))
    if fp > fn * 1.5:
        return "over-inclusion"
    if fn > fp * 1.5:
        return "under-extraction"
    return "mixed-boundary-errors"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float:
    if isinstance(value, int | float):
        return float(value)
    return 0.0
