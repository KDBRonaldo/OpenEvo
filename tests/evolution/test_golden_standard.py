from __future__ import annotations

import json

import pytest

from openevo.evolution.golden_standard import (
    assert_no_golden_leakage,
    evaluate_records_against_golden,
    find_golden_leaks,
    load_golden_standard_records,
    render_sanitized_golden_feedback,
)


def test_load_golden_standard_records_from_json_object(tmp_path):
    path = tmp_path / "gold.json"
    path.write_text(
        json.dumps(
            {
                "ground_truth_records": [
                    {
                        "article_id": "Paper A",
                        "article_keys": ["paper a", "paper_a"],
                        "component_name": "Promoter A",
                        "sequence": "AAAACCCCGGGGTTTTAAAACCCC",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    records = load_golden_standard_records(path)

    assert records == [
        {
            "article_id": "Paper A",
            "article_keys": ["paper a", "paper_a"],
            "component_name": "Promoter A",
            "sequence": "AAAACCCCGGGGTTTTAAAACCCC",
        }
    ]


def test_evaluate_records_against_golden_matches_article_scoped_sequences():
    golden = [
        {
            "article_id": "Paper A",
            "article_keys": ["paper_a"],
            "component_name": "Promoter A1",
            "sequence": "AAAACCCCGGGGTTTTAAAACCCC",
        },
        {
            "article_id": "Paper A",
            "article_keys": ["paper_a"],
            "component_name": "Promoter A2",
            "sequence": "CCCCGGGGTTTTAAAACCCCGGGG",
        },
        {
            "article_id": "Paper B",
            "article_keys": ["paper_b"],
            "component_name": "Promoter B1",
            "sequence": "TTTTAAAACCCCGGGGTTTTAAAA",
        },
    ]
    predicted = [
        {
            "article_id": "Paper A",
            "component_name": "hit",
            "sequence": "aaaaccccggggttttaaaacccc",
        },
        {
            "article_id": "paper_a",
            "component_name": "duplicate hit",
            "sequence": "AAAACCCCGGGGTTTTAAAACCCC",
        },
        {
            "article_id": "Paper A",
            "component_name": "wrong extra",
            "sequence": "GGGGAAAATTTTCCCCGGGGAAAA",
        },
        {
            "article_id": "Paper B",
            "component_name": "hit",
            "sequence": "TTTTAAAACCCCGGGGTTTTAAAA",
        },
    ]

    evaluation = evaluate_records_against_golden(predicted, golden)

    assert evaluation["summary"] == {
        "gold_unique": 3,
        "predicted_total": 4,
        "predicted_unique": 3,
        "true_positive": 2,
        "false_positive": 1,
        "false_negative": 1,
        "duplicate_predictions": 1,
        "precision": pytest.approx(2 / 3),
        "recall": pytest.approx(2 / 3),
        "f1": pytest.approx(2 / 3),
    }
    assert evaluation["articles"]["paper_a"]["true_positive"] == 1
    assert evaluation["articles"]["paper_a"]["false_positive"] == 1
    assert evaluation["articles"]["paper_a"]["false_negative"] == 1
    assert evaluation["articles"]["paper_b"]["true_positive"] == 1
    assert evaluation["leakage_basis"]["article_ids"] == ["Paper A", "Paper B"]


def test_evaluate_records_against_golden_aligns_short_article_slugs():
    golden = [
        {
            "article_id": (
                "Metagenomic mining of regulatory elements enables programmable "
                "species-selective gene expression"
            ),
            "article_key": (
                "metagenomic_mining_regulatory_elements_enables_programmable_"
                "species_selective_gene_expression"
            ),
            "article_keys": [
                (
                    "metagenomic_mining_of_regulatory_elements_enables_programmable_"
                    "species_selective_gene_expression"
                ),
                "johns_2018_metagenomic_regulatory_sequences",
            ],
            "sequence": "TCCCACTATTTGTCGGCTAGCCAGATTGTT",
        }
    ]
    predicted = [
        {
            "article_id": "johns_2018_metagenomic_regulatory_sequences",
            "sequence": "TCCCACTATTTGTCGGCTAGCCAGATTGTT",
        },
        {
            "article_id": "johns_2018_metagenomic_regulatory_sequences",
            "sequence": "GGGGAAAATTTTCCCCGGGGAAAATTTTCCCC",
        },
    ]

    evaluation = evaluate_records_against_golden(predicted, golden)

    assert evaluation["summary"]["true_positive"] == 1
    assert evaluation["summary"]["false_positive"] == 1
    article = evaluation["articles"][
        "metagenomic_mining_regulatory_elements_enables_programmable_species_selective_gene_expression"
    ]
    assert article["true_positive"] == 1
    assert article["false_positive"] == 1
    assert article["false_negative"] == 0


def test_evaluate_records_against_golden_does_not_fuzzy_match_wrong_article_tokens():
    golden = [
        {
            "article_id": (
                "Metagenomic mining of regulatory elements enables programmable "
                "species-selective gene expression"
            ),
            "article_key": (
                "metagenomic_mining_regulatory_elements_enables_programmable_"
                "species_selective_gene_expression"
            ),
            "sequence": "AAAACCCCGGGGTTTTAAAACCCC",
        }
    ]
    predicted = [
        {
            "article_id": "unrelated programmable gene expression paper",
            "sequence": "AAAACCCCGGGGTTTTAAAACCCC",
        }
    ]

    evaluation = evaluate_records_against_golden(predicted, golden)

    assert evaluation["summary"]["true_positive"] == 0
    assert evaluation["summary"]["false_positive"] == 1
    assert evaluation["summary"]["false_negative"] == 1


def test_evaluate_records_against_golden_preserves_wrong_article_predictions_as_misses():
    golden = [
        {
            "article_id": "Paper A",
            "article_keys": ["paper_a"],
            "sequence": "AAAACCCCGGGGTTTTAAAACCCC",
        }
    ]
    wrong_article_prediction = [
        {
            "article_id": "Unrelated Paper",
            "sequence": "AAAACCCCGGGGTTTTAAAACCCC",
        }
    ]
    missing_article_prediction = [{"sequence": "AAAACCCCGGGGTTTTAAAACCCC"}]

    wrong_article = evaluate_records_against_golden(wrong_article_prediction, golden)
    missing_article = evaluate_records_against_golden(missing_article_prediction, golden)

    assert wrong_article["summary"]["true_positive"] == 0
    assert wrong_article["summary"]["false_positive"] == 1
    assert wrong_article["summary"]["false_negative"] == 1
    assert missing_article["summary"]["true_positive"] == 1
    assert missing_article["summary"]["false_positive"] == 0
    assert missing_article["summary"]["false_negative"] == 0


def test_render_sanitized_feedback_omits_ground_truth_literals():
    golden = [
        {
            "article_id": "Paper A With Specific Title",
            "article_keys": ["paper_a"],
            "component_name": "Specific promoter name",
            "sequence": "AAAACCCCGGGGTTTTAAAACCCC",
            "source_file": "/private/path/golden_source.xlsx",
            "source_sheet": "VerySpecificSheet",
            "source_row": 17,
        },
    ]
    predicted = [
        {
            "article_id": "Paper A With Specific Title",
            "component_name": "extra",
            "sequence": "GGGGAAAATTTTCCCCGGGGAAAA",
        }
    ]
    evaluation = evaluate_records_against_golden(predicted, golden)

    feedback = render_sanitized_golden_feedback(evaluation)

    assert "Paper A With Specific Title" not in feedback
    assert "AAAACCCCGGGGTTTTAAAACCCC" not in feedback
    assert "VerySpecificSheet" not in feedback
    assert "golden_source.xlsx" not in feedback
    assert "source_bucket_1" in feedback
    assert "methodology" in feedback.lower()
    assert "exact sequence" not in feedback.lower()


def test_leakage_guard_flags_exact_golden_literals():
    golden = [
        {
            "article_id": "paper_a",
            "article_title": "Paper A With Specific Title",
            "article_keys": ["paper_a"],
            "component_name": "Specific promoter name",
            "sequence": "AAAACCCCGGGGTTTTAAAACCCC",
            "source_file": "/private/path/golden_source.xlsx",
            "source_sheet": "VerySpecificSheet",
            "source_row": 17,
        }
    ]
    leaking_text = (
        "# Evolved Agent System\n\n"
        "For Paper A With Specific Title, use VerySpecificSheet and "
        "AAAACCCCGGGGTTTTAAAACCCC."
    )

    leaks = find_golden_leaks(leaking_text, golden)

    assert {leak["kind"] for leak in leaks} == {"article_title", "sequence", "source_sheet"}
    with pytest.raises(ValueError, match="golden standard leakage"):
        assert_no_golden_leakage(leaking_text, golden)


def test_leakage_guard_flags_whitespace_wrapped_sequences():
    golden = [{"sequence": "AAAACCCCGGGGTTTTAAAACCCC"}]
    leaking_text = "# Evolved Agent System\n\nReject AAAA CCCC GGGG TTTT AAAA CCCC."

    leaks = find_golden_leaks(leaking_text, golden)

    assert leaks == [{"kind": "sequence", "value": "AAAACCCCGGGGTTTTAAAACCCC"}]
    with pytest.raises(ValueError, match="golden standard leakage"):
        assert_no_golden_leakage(leaking_text, golden)


def test_leakage_guard_flags_source_row_references():
    golden = [{"source_row": 17}]
    leaking_text = "# Evolved Agent System\n\nUse source_row 17 as the extraction anchor."

    leaks = find_golden_leaks(leaking_text, golden)

    assert leaks == [{"kind": "source_row", "value": "17"}]
    with pytest.raises(ValueError, match="golden standard leakage"):
        assert_no_golden_leakage(leaking_text, golden)
