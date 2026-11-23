"""Exercise the original review counterexamples through the public offline entry points.

A mock transport supplies deterministic clock and cache fields. Full quality
verification uses the frozen public answers and never calls a provider.
"""

import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import api_cache
import api_cache_report as cache
import corrected_report as quality
import cross_family_report as cross
import human_review
from corrected_user import DISCLOSURES, frozen_materials, reviewed_reply
from grade import success
from run import CASES, HERE, write_json

CROSS = HERE / "results/cross-family-2026-11-22"
QUALITY = HERE / "results/corrected-2026-11-22"


def receipt(read=0, write=0, out=2, ms=120, **fields):
    return {
        "status": "completed",
        "http": 200,
        "model": api_cache.MODEL,
        "service_tier": "default",
        "elapsed_ms": ms,
        "ttft_ms": 10,
        "usage": {
            "input": 1300,
            "read": read,
            "write": write,
            "write_30m": write,
            "write_12h": 0,
            "output": out,
        },
        **fields,
    }


class ReviewRegressions(unittest.TestCase):
    def test_three_assignments_survive_two_attempts_without_unit_checkpoints(self):
        replay = [
            u
            for u in api_cache.units()
            if u["scope"] == "natural" and u["arm"] == "none"
        ][:3]
        response = receipt()
        rejection = {
            "status": "failed",
            "http": 403,
            "error": "permission_denied",
            "elapsed_ms": 7,
        }

        def respond(key, data, counting=False):
            if counting:
                return {"status": "completed", "input_tokens": 12, "http": 200}
            return next(responses)

        responses = iter((response, response, rejection))
        with tempfile.TemporaryDirectory() as tmp:
            folder, out = Path(tmp) / "run", Path(tmp) / "public"
            with (
                patch.object(api_cache, "units", return_value=replay),
                patch.object(api_cache, "WORKERS", 1),
                patch.object(api_cache, "request", side_effect=respond),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(api_cache.run(folder, None), 2)
                self.assertEqual(len(list((folder / "units").glob("*.json"))), 2)
                # Include receipts even if interrupted before writing a single unit checkpoint.
                shutil.rmtree(folder / "units")
                s = cache.publish(folder, out)
                cache.verify(out)
                m = s["groups"]["natural"]["none"]["uncached"]["allocation_itt"]
                self.assertEqual(
                    (m["scheduled"], m["attempted"], m["eventual_completed"]), (3, 2, 1)
                )
                self.assertEqual(m["coverage_fraction"], 1 / 3)
                cold = s["groups"]["natural"]["none"]["cold"]["allocation_itt"]
                self.assertEqual(
                    (cold["status"], cold["scheduled"]), ("short_prefix", 0)
                )
                self.assertIsNone(
                    cold["total_elapsed_ms_including_retries"]["median_ms"]
                )
                self.assertEqual(s["planned_units"], 3)
                self.assertTrue(s["allocation_finalized"])
                snap = json.loads((out / "snapshot.json").read_text())
                snap["units"].pop()
                with self.assertRaisesRegex(ValueError, "allocation"):
                    cache.validate_public_allocation(snap)
                plan = json.loads((folder / "allocation.json").read_text())
                write_json(folder / "allocation.json", plan[:-1])
                with self.assertRaisesRegex(ValueError, "population"):
                    cache.publish(folder, out)

    def test_partial_cost_subtotal_uses_only_priced_receipts_and_keeps_denominator(
        self,
    ):
        manifest = {
            "model": api_cache.MODEL,
            "rules": api_cache.RULES,
            "rates_usd_per_million": api_cache.RATES,
        }
        formal = dict(receipt(write=1100), kind="unit", stage="cold")
        del formal["usage"][
            "write_12h"
        ]  # The separately reported 30m writes exhaust total writes.
        missing = dict(receipt(service_tier="priority"), kind="unit", stage="cold")
        result = cache.recorded_costs([formal, missing], manifest)["formal"]
        self.assertEqual(
            (result["recorded_attempts"], result["priced_attempts"]), (2, 1)
        )
        self.assertAlmostEqual(
            result["observed_priced_subtotal_usd"],
            (200 * 8 + 1100 * 10 + 2 * 40) / 1_000_000,
        )
        formal["usage"]["write_30m"] = 200
        self.assertIsNone(cache.cost_for(formal, manifest)["estimated_usage_usd"])

    def test_tier_snapshot_identity_and_counters_gate_pairs_but_keep_allocation(self):
        unit = {
            "id": "unit",
            "turn": 1,
            "arm": "main",
            "scope": "natural",
            "sample": "s",
            "expected_turns": 1,
            "policies": ["cold", "warm"],
            "visible_sha256": "a" * 64,
            "prefix_sha256": "b" * 64,
        }
        records = {
            "attempts/unit-cold.json": [receipt(write=1100)],
            "attempts/unit-prewarm.json": [receipt(write=1100, out=0)],
            "attempts/unit-warm.json": [receipt(read=1100, ms=45)],
        }
        snap = {
            "units": [unit],
            "attempts": records,
            "run_manifest": {
                "model": api_cache.MODEL,
                "rules": api_cache.RULES,
                "rates_usd_per_million": api_cache.RATES,
            },
            "calibration": {
                "natural": {"main": {"eligible": True, "boundary_tokens": 1100}}
            },
        }

        def pairs(s):
            return cache.build_summary(s)["paired_differences"]["natural"]["main"][
                "cold→warm"
            ]

        time = "total_elapsed_ms_including_retries_delta_right_minus_left"
        self.assertEqual(pairs(snap)["verified_pair"][time]["median"], -75)
        for stage, field, value, reason in [
            ("warm", "service_tier", "priority", "tier_mismatch"),
            ("prewarm", "service_tier", "priority", "tier_mismatch"),
            ("cold", "model", "gpt-6.2-astra-2026-11-01", "snapshot_mismatch"),
            ("warm", "model", "other", "model_mismatch"),
            ("warm", "service_tier", None, "identity_unavailable"),
        ]:
            with self.subTest(stage=stage, reason=reason):
                altered = copy.deepcopy(snap)
                altered["attempts"][f"attempts/unit-{stage}.json"][0][field] = value
                row = pairs(altered)
                self.assertEqual(row["ITT"]["n"], 1)
                self.assertEqual(row["ITT"]["service_match"], {reason: 1})
                self.assertEqual(row["ITT"][time]["n"], 0)
                self.assertEqual(row["verified_pair"]["n"], 0)
        unknown = copy.deepcopy(snap)
        del unknown["attempts"]["attempts/unit-warm.json"][0]["usage"]["write"]
        self.assertEqual(pairs(unknown)["verified_pair"]["n"], 0)
        changed_retry = copy.deepcopy(snap)
        changed_retry["attempts"]["attempts/unit-cold.json"].insert(
            0, receipt(status="failed", service_tier="priority")
        )
        self.assertEqual(
            pairs(changed_retry)["ITT"]["service_match"], {"tier_mismatch": 1}
        )

    def test_wrong_reread_and_missing_primary_or_journals_are_rejected(self):
        source = cross.load_history()
        rows = cross.read_jsonl(CROSS / "votes.jsonl")
        first = next(r for r in rows if r["kind"] == "repeat")
        first["key"] = min(source["primary_ids"] - source["repeat_keys"])
        with self.assertRaisesRegex(ValueError, "roster"):
            cross.audit_rows(rows, source)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            quality.write_lines(folder / "votes.jsonl", rows)
            with self.assertRaisesRegex(ValueError, "preregistered"):
                human_review.load_new_votes_strict(
                    folder / "votes.jsonl",
                    source["primary_ids"],
                    set(source["anchor_expected"]),
                    source["repeat_keys"],
                )
            shutil.copyfile(quality.SUBJECTS, folder / "subjects.jsonl")
            ratings = source["ratings"].copy()
            ratings.remove(
                next(r for r in ratings if r["rater"] == "A" and r["kind"] == "primary")
            )
            quality.write_lines(folder / "ratings.jsonl", ratings)
            with self.assertRaisesRegex(ValueError, "cover"):
                cross.load_history(history=folder)
            shutil.copytree(CROSS, folder / "out")
            for name in ("run.json", "attempts.jsonl"):
                with self.subTest(name=name):
                    path = folder / "out" / name
                    path.unlink()
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        cross.verify(folder / "out")
                    shutil.copyfile(CROSS / name, path)

    def test_frozen_templates_examples_and_direct_questions_disclose_only_supported_facts(
        self,
    ):
        subjects, replies = frozen_materials()
        cases = {c["id"]: c for c in CASES}
        cases_by_key = {row["id"]: cases[row["case"]] for row in subjects}
        examples = {
            "Q01-1-none": set(),
            "Q01-1-release": {"goal"},
            "Q02-1-main": {"goal"},
            "Q02-1-release": {"goal"},
            "Q04-2-none": {"goal"},
        }
        for key, expected in examples.items():
            with self.subTest(key=key):
                actual = {
                    slot["name"]
                    for slot in cases_by_key[key]["slots"]
                    if slot["value"] in replies[key]
                }
                self.assertEqual(actual, expected)

    def test_disclosure_works_only_for_the_reviewed_case_and_verbatim_first_turn(self):
        subjects, replies = frozen_materials()
        self.assertEqual((len(subjects), len(replies)), (216, 36))
        cases = {c["id"]: c for c in CASES}
        entries = json.loads(DISCLOSURES.read_text())["entries"]
        row = next(r for r in subjects if r["id"] in replies)
        first = success(row)["turns"][0]["answer"]
        for case, answer in ((cases[row["case"]], first + " "), (cases["Q10"], first)):
            with self.assertRaisesRegex(ValueError, "reviewed disclosure"):
                reviewed_reply(case, answer, entries)

    def test_bound_quality_evidence_rejects_a_changed_first_answer_or_reread(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "report"
            shutil.copytree(QUALITY, folder)
            records = cross.read_jsonl(folder / "second-turns.jsonl")
            records[0]["attempts"][0]["first_answer_sha256"] = "0" * 64
            quality.write_lines(folder / "second-turns.jsonl", records)
            source = cross.load_history()
            _, replies = frozen_materials()
            with self.assertRaisesRegex(ValueError, "Replayed input"):
                quality.reconstructed(folder, source, replies)
            shutil.copyfile(
                QUALITY / "second-turns.jsonl", folder / "second-turns.jsonl"
            )
            items, _ = quality.reconstructed(folder, source, replies)
            votes = cross.read_jsonl(folder / "qwen-votes.jsonl")
            next(r for r in votes if r["kind"] == "repeat")["key"] = "Q01-99-none"
            quality.write_lines(folder / "qwen-votes.jsonl", votes)
            with self.assertRaisesRegex(ValueError, "substituted"):
                quality.qwen_rows(folder, items)


if __name__ == "__main__":
    unittest.main()
