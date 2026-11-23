"""Verify cache proof, retry costs and dialogue denominators without external calls."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import api_cache
from api_cache_report import build_summary, cost_for, publish, verify


class CacheReportTest(unittest.TestCase):
    def test_verified_hit_does_not_drop_a_failed_attempts_wall_time_or_cost(self):
        manifest = {
            "model": api_cache.MODEL,
            "rates_usd_per_million": api_cache.RATES,
            "rules": {"service_tier": "default"},
        }

        def completed(read, write, output):
            return {
                "status": "completed",
                "model": api_cache.MODEL,
                "service_tier": "default",
                "elapsed_ms": 40,
                "ttft_ms": 10,
                "usage": {
                    "input": 1200,
                    "read": read,
                    "write": write,
                    "write_30m": write,
                    "write_12h": 0,
                    "output": output,
                },
            }

        prime = completed(0, 1100, 0)
        hot = completed(1100, 0, 10)
        failed = {"status": "failed", "http": 500, "elapsed_ms": 100, "usage": {}}
        unit = {
            "id": "Q01-1-main-t1",
            "sample": "Q01-1-main",
            "scope": "natural",
            "arm": "main",
            "turn": 1,
            "expected_turns": 2,
            "policies": ["warm"],
            "prefix_sha256": "a" * 64,
            "visible_sha256": "b" * 64,
        }

        def snapshot(pre, formal):
            return {
                "units": [unit],
                "attempts": {
                    "attempts/Q01-1-main-t1-prewarm.json": pre,
                    "attempts/Q01-1-main-t1-warm.json": formal,
                },
                "calibration": {
                    "natural": {"main": {"eligible": True, "boundary_tokens": 1100}}
                },
                "run_manifest": manifest,
                "run_status": {"state": "blocked"},
            }

        group = build_summary(
            snapshot([prime], [failed, dict(hot, retry_delay_ms=1000)])
        )
        actual = group["groups"]["natural"]["main"]["warm"]["allocation_itt"]
        self.assertEqual(
            actual["total_elapsed_ms_including_retries"]["median_ms"], 1140
        )
        self.assertIsNone(actual["estimated_usage_cost_usd"]["estimated_usage_usd"])
        self.assertEqual(actual["cache_classification"], {"full_hit": 1})
        self.assertEqual(group["dialogues"][0]["wait"]["n"], 0)
        miss = build_summary(snapshot([failed], [hot]))
        self.assertEqual(
            miss["groups"]["natural"]["main"]["warm"]["allocation_itt"][
                "cache_classification"
            ],
            {"full_read_without_verified_prewarm": 1},
        )
        failure = build_summary(snapshot([prime], [failed]))
        self.assertEqual(
            failure["groups"]["natural"]["main"]["warm"]["allocation_itt"][
                "total_elapsed_ms_including_retries"
            ]["median_ms"],
            100,
        )
        self.assertAlmostEqual(
            cost_for(hot, manifest)["estimated_usage_usd"],
            (100 * 8 + 1100 * 0.4 + 10 * 40) / 1_000_000,
        )
        experiment = snapshot([prime], [hot])
        unit["policies"] = ["cold", "warm"]
        cold_path = "attempts/Q01-1-main-t1-cold.json"
        experiment["attempts"][cold_path] = [hot]
        paired = build_summary(experiment)["paired_differences"]["natural"]["main"][
            "cold→warm"
        ]
        self.assertEqual(paired["verified_full_hit"]["n"], 1)
        self.assertEqual(paired["verified_pair"]["n"], 0)
        experiment["attempts"][cold_path] = [completed(0, 1100, 5)]
        self.assertEqual(
            build_summary(experiment)["paired_differences"]["natural"]["main"][
                "cold→warm"
            ]["verified_pair"]["n"],
            1,
        )

    def test_verified_readout_includes_one_prewarm_per_pair_and_excludes_wrong_tiers(self):
        from api_cache_report import verified_readout

        def stage(elapsed, cost):
            return {
                "total_elapsed_ms_including_retries": elapsed,
                "first_visible_ms_including_retries": None,
                "estimated_usage_cost_usd": {"estimated_usage_usd": cost},
            }

        tasks, pairs = [], []
        for i, (prime, hot) in enumerate(((1, 9), (9, 10), (10, 1), (100, 100))):
            tasks.append({"unit_id": i, "scope": "natural", "arm": "main",
                          "policies": {"cold": {"actual": stage(8, 0.02)},
                                       "warm": {"actual": stage(hot, 0.01), "prewarm": stage(prime, 0.03)}}})
            pairs.append({"unit_id": i, "left": "cold", "right": "warm", "both_policies_verified": i != 3})
        readout = verified_readout(tasks, pairs)["natural"]["main"]
        combined = readout["warm_including_prewarm"]
        self.assertEqual((combined["units"], combined["priced_units"]), (3, 3))
        self.assertEqual(combined["wait_ms"]["median_ms"], 11)
        self.assertEqual(combined["mean_usage_usd"], 0.04)
        self.assertEqual(readout["uncached"]["units"], 0)

    def test_first_visible_text_can_arrive_in_a_failed_stream(self):
        from api_cache_report import with_retry_first_visible

        first = {"status": "failed", "ttft_ms": 1790, "elapsed_ms": 4343}
        retried = {"status": "completed", "ttft_ms": 1850, "elapsed_ms": 3360, "retry_delay_ms": 1000}
        self.assertEqual(with_retry_first_visible([first, retried]), 1790)
        self.assertEqual(with_retry_first_visible([dict(first, ttft_ms=None), retried]), 7193)
        self.assertIsNone(with_retry_first_visible([dict(first, ttft_ms=None, elapsed_ms=None), retried]))

    def test_preflight_stop_reproduces_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            response = {
                "http": 403,
                "status": "failed",
                "error": "permission_denied",
                "elapsed_ms": 19,
                "ttft_ms": None,
                "usage": {},
            }
            out = folder / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                with patch("api_cache.request", return_value=response):
                    self.assertEqual(api_cache.run(folder / "run", None), 2)
                publish(folder / "run", out)
                verify(out)
            summary = json.loads((out / "summary.json").read_text())
            for arm in api_cache.ARM_NAMES:
                entry = summary["groups"]["natural"][arm]["cold"]["allocation_itt"]
                self.assertIsNone(entry["scheduled"])
                self.assertIsNone(
                    entry["estimated_usage_cost_usd"]["estimated_usage_usd"]
                )
