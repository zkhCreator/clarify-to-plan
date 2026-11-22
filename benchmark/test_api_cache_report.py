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
