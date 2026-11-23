"""Offline tests for the family-blind audit definitions and output matrix."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cross_family_report
from cross_family_report import (
    ARMS,
    audit_rows,
    build_summary,
    csv_text,
    load_history,
    old_views,
    overlap_sensitivity,
)


def synthetic_source():
    cases = [{"id": "Q01", "family": "clarify"}, {"id": "Q02", "family": "handoff"}]
    subjects = []
    for case in cases:
        for rep in range(1, 4):
            for arm in ARMS:
                key = f"{case['id']}-{rep}-{arm}"
                subjects.append(
                    {
                        "id": key,
                        "case": case["id"],
                        "family": case["family"],
                        "rep": rep,
                        "arm": arm,
                    }
                )
    subject_index = {row["id"]: row for row in subjects}
    anchors = {"anchor-a": True, "anchor-b": True, "anchor-c": False, "anchor-d": False}
    ratings = []
    for row in subjects:
        checks_a = [True, True, True]
        checks_b = [True, True, True]
        if row["id"] == "Q01-1-none":
            checks_a = [True, True, False]
        for rater, checks in (("A", checks_a), ("B", checks_b)):
            ratings.append(
                {
                    "kind": "primary",
                    "key": row["id"],
                    "rater": rater,
                    "vote": {"checks": checks, "pass": all(checks)},
                }
            )
        if row["id"] == "Q01-1-none":
            ratings.append(
                {
                    "kind": "primary",
                    "key": row["id"],
                    "rater": "C",
                    "vote": {"checks": [True, True, True], "pass": True},
                }
            )
    for key in list(subject_index)[:12]:
        for rater in ("A", "B"):
            ratings.append(
                {
                    "kind": "repeat",
                    "key": key,
                    "rater": rater,
                    "vote": {"checks": [True, True, True], "pass": True},
                }
            )
    for key, expected in anchors.items():
        for rater in ("A", "B"):
            ratings.append(
                {
                    "kind": "anchor",
                    "key": key,
                    "rater": rater,
                    "vote": {"checks": [expected] * 3, "pass": expected},
                }
            )
    return {
        "base": Path("."),
        "history": Path("."),
        "cases": cases,
        "case_index": {row["id"]: row for row in cases},
        "anchors": {
            "answers": [
                {"key": key[7:], "pass": value} for key, value in anchors.items()
            ]
        },
        "subjects": subjects,
        "subject_index": subject_index,
        "ratings": ratings,
        "primary_ids": set(subject_index),
        "repeat_keys": set(sorted(subject_index)[:12]),
        "anchor_expected": anchors,
    }


def new_votes(source, flip_repeat=False, bad_anchor=False):
    rows = []
    for index, key in enumerate(sorted(source["primary_ids"])):
        rows.append(
            {
                "kind": "primary",
                "key": key,
                "checks": [True, True, True],
                "evidence": "synthetic primary evidence",
                "blind_id": f"p-{index}",
            }
        )
    for index, key in enumerate(sorted(source["primary_ids"])[:12]):
        checks = (
            [False, False, False] if flip_repeat and index == 0 else [True, True, True]
        )
        rows.append(
            {
                "kind": "repeat",
                "key": key,
                "checks": checks,
                "evidence": "synthetic repeat evidence",
                "blind_id": f"r-{index}",
            }
        )
    for index, (key, expected) in enumerate(sorted(source["anchor_expected"].items())):
        value = not expected if bad_anchor and index == 0 else expected
        rows.append(
            {
                "kind": "anchor",
                "key": key,
                "checks": [value] * 3,
                "evidence": "synthetic anchor evidence",
                "blind_id": f"a-{index}",
            }
        )
    return rows


class CrossFamilyReportTests(unittest.TestCase):
    def test_default_history_is_the_frozen_216_sample(self):
        source = load_history()
        self.assertEqual(len(source["subjects"]), 216)
        self.assertEqual(len(source["primary_ids"]), 216)
        self.assertEqual(len(source["ratings"]), 468)
        self.assertEqual(
            set(source["anchor_expected"]),
            {
                "anchor-equiv1",
                "anchor-equiv2",
                "anchor-false_pass",
                "anchor-false_reject",
            },
        )

    def test_overlap_is_post_hoc_and_keeps_frozen_unit_count(self):
        source = load_history()
        old = old_views(source)
        overlap = overlap_sensitivity(source, old["final_pass"], {})
        self.assertEqual(overlap["duplicate_groups"], 5)
        self.assertEqual(overlap["covered_ids"], 19)

    def test_final_prefers_c_and_reports_family_metrics(self):
        source = synthetic_source()
        audit = audit_rows(new_votes(source), source)
        summary = build_summary(
            source, audit["rows"], {"model": "test-model", "sha256": "run-hash"}
        )
        self.assertEqual(summary["source_counts"]["primary"], 18)
        self.assertEqual(summary["comparisons"]["old_final"]["overall"]["n"], 18)
        self.assertEqual(summary["families"]["clarify"]["scheduled"], 9)
        self.assertTrue(summary["cases"][0]["runs"]["none"]["old"][0])
        self.assertIn("by_criterion", summary["comparisons"]["old_final"])
        self.assertEqual(summary["run"]["model"], "test-model")
        self.assertEqual(summary["arms"]["none"]["label"], "无 skill")
        self.assertEqual(summary["arms"]["none"]["pass"]["qwen"]["n"], 6)
        self.assertEqual(summary["arms"]["none"]["majority"]["qwen"]["n"], 2)

    def test_repeat_anchor_and_csv_keep_denominators(self):
        source = synthetic_source()
        audit = audit_rows(new_votes(source, flip_repeat=True, bad_anchor=True), source)
        summary = build_summary(source, audit["rows"])
        self.assertEqual(summary["repeats"]["new"]["overall"]["n"], 12)
        self.assertEqual(summary["repeats"]["new"]["overall"]["agreement"], 11)
        self.assertEqual(summary["anchors"]["new"]["correct"], 3)
        matrix = csv_text(summary)
        self.assertIn("none_old", matrix.splitlines()[0])
        self.assertIn("none_old_to_new_gain", matrix.splitlines()[0])
        self.assertIn("none_to_release_new_gain", matrix.splitlines()[0])
        self.assertEqual(len(matrix.splitlines()), 3)

    def test_vote_shape_and_duplicate_ids_are_rejected(self):
        source = synthetic_source()
        rows = new_votes(source)
        with self.assertRaises(ValueError):
            audit_rows(rows[:-1], source)
        duplicate = list(rows)
        duplicate[-1] = dict(duplicate[-2])
        with self.assertRaises(ValueError):
            audit_rows(duplicate, source)
        malformed = list(rows)
        malformed[0] = dict(malformed[0], checks=[True, True])
        with self.assertRaises(ValueError):
            audit_rows(malformed, source)

    def test_jsonl_input_is_supported_without_model_calls(self):
        source = synthetic_source()
        rows = new_votes(source)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "votes.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            audit = audit_rows(path, source)
        self.assertEqual(len(audit["rows"]), 34)

    def test_publish_verify_preserves_raw_vote_order_and_extra_fields(self):
        source = synthetic_source()
        source["base"] = Path(__file__).resolve().parent
        source["history"] = Path(__file__).resolve().parent / "results/codex-2026-11-22"
        rows = new_votes(source)
        rows[0]["group"] = "clarify-review"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            votes_path = root / "votes.jsonl"
            out = root / "out"
            votes_path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
            )
            with patch.object(cross_family_report, "load_history", return_value=source):
                cross_family_report.publish(votes_path, out)
                cross_family_report.verify(out)
            saved = [
                json.loads(line)
                for line in (out / "votes.jsonl").read_text().splitlines()
            ]
        self.assertEqual(
            [row["blind_id"] for row in saved], [row["blind_id"] for row in rows]
        )
        self.assertEqual(saved[0]["group"], "clarify-review")

    def test_verify_checks_adjacent_run_metadata_when_present(self):
        source = synthetic_source()
        source["base"] = Path(__file__).resolve().parent
        source["history"] = Path(__file__).resolve().parent / "results/codex-2026-11-22"
        rows = new_votes(source)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            votes_path = root / "votes.jsonl"
            out = root / "out"
            run = root / "run.json"
            run.write_text(json.dumps({"model": "test-model", "seed": 7}) + "\n")
            votes_path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
            )
            with patch.object(cross_family_report, "load_history", return_value=source):
                cross_family_report.publish(votes_path, out)
                (out / "run.json").write_bytes(run.read_bytes())
                cross_family_report.verify(out)
                (out / "run.json").write_text(
                    json.dumps({"model": "other-model", "seed": 7}) + "\n"
                )
                with self.assertRaises(ValueError):
                    cross_family_report.verify(out)


if __name__ == "__main__":
    unittest.main()
