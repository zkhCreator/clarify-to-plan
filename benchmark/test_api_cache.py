"""Offline checks for cache assignment, frozen histories, retry accounting and secret-free output."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api_cache import (
    Collector,
    fatal,
    input_blocks,
    payload,
    prefixes,
    transient,
    units,
)
from cache_transport import extract, mark_error, sse_events


class CacheTests(unittest.TestCase):
    def test_only_a_verified_clean_write_opens_cold_and_warm_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Collector(Path(tmp), None, "test")
            c.prefix = {"natural": {"main": "fixture"}}
            count = [{"input_tokens": 2048, "status": "completed"}]
            dirty = [
                {
                    "status": "completed",
                    "usage": {"read": 512, "write": 1536, "output": 0},
                }
            ]
            clean = [
                {
                    "status": "completed",
                    "usage": {"read": 0, "write": 2040, "output": 0},
                }
            ]
            with patch.object(c, "attempts", side_effect=[count, dirty]):
                dirty_config = c.calibrate()
            self.assertIsNone(dirty_config["natural"]["main"]["eligible"])
            with patch.object(c, "attempts", side_effect=[count, clean]):
                self.assertEqual(
                    c.calibrate()["natural"]["main"]["boundary_tokens"], 2040
                )
            unit = {"id": "sample-t1", "arm": "main", "scope": "natural", "history": []}
            with patch.object(
                c, "attempts", return_value=[{"status": "completed"}]
            ) as measure:
                c.collect(unit, dirty_config)
            self.assertEqual(measure.call_count, 1)
            self.assertEqual(measure.call_args.args[0], "sample-t1-uncached")
        self.assertTrue(fatal({"http": 200, "error": "permission_denied"}))
        self.assertTrue(transient({"http": 200, "error": "rate_limit_exceeded"}))

    def test_same_input_and_separate_assignments(self):
        sample = next(x for x in units() if x["turn"] == 2)
        self.assertEqual(
            [m["role"] for m in sample["history"]], ["user", "assistant", "user"]
        )
        self.assertEqual(len(units()), 261)
        text = prefixes()[sample["scope"]][sample["arm"]]
        cold = payload(text, sample["history"], "cold", "cold-key")
        warm = payload(text, sample["history"], "warm", "warm-key")
        prime = payload(text, sample["history"], "prewarm", "warm-key")
        uncached = payload(text, sample["history"], "uncached", "uncached-key")
        self.assertEqual(cold["input"], warm["input"])
        self.assertEqual(cold["input"][:1], prime["input"])
        self.assertIn("prewarm", prime["prompt_cache_options"])
        self.assertFalse(prime["stream"])
        del cold["input"][0]["content"][0]["prompt_cache_breakpoint"]
        self.assertEqual(cold["input"], uncached["input"])
        self.assertEqual(
            uncached["input"], input_blocks(text, sample["history"], False)
        )
        self.assertNotEqual(warm["prompt_cache_key"], cold["prompt_cache_key"])

    def test_token_counters_and_errors_allowlist(self):
        r = {}
        extract(
            r,
            {
                "model": "gpt-6.2-astra",
                "service_tier": "default",
                "status": "completed",
                "usage": {
                    "input_tokens": 2000,
                    "output_tokens": 8,
                    "input_tokens_details": {
                        "cached_tokens": 1200,
                        "cache_write_tokens": 300,
                        "cache_write_tokens_details": {
                            "ttl_30m_tokens": 300,
                            "ttl_12h_tokens": 0,
                        },
                    },
                },
            },
        )
        self.assertEqual(
            r["usage"],
            {
                "input": 2000,
                "output": 8,
                "read": 1200,
                "write": 300,
                "write_30m": 300,
                "write_12h": 0,
            },
        )
        mark_error(
            r,
            {
                "code": "insufficient_quota",
                "message": "private-provider-detail",
                "param": "user-supplied-value",
            },
        )
        self.assertNotIn("private-provider-detail", json.dumps(r))
        self.assertNotIn("user-supplied-value", json.dumps(r))
        s = io.BytesIO(
            b'event: response.completed\ndata: {"type":\n'
            b'data: "response.completed"}\n\n'
        )
        self.assertEqual(list(sse_events(s)), [{"type": "response.completed"}])

    def test_fatal_and_interrupted_attempts_are_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            collector = Collector(folder, None, "run-id")
            response = {
                "http": 429,
                "status": "failed",
                "error": "insufficient_quota",
                "usage": {},
                "elapsed_ms": 12,
            }
            with patch("api_cache.request", return_value=response) as send:
                rows = collector.attempts("access", {})
                self.assertEqual(len(rows), 1)
                self.assertTrue(collector.stop.is_set())
                self.assertEqual(send.call_count, 1)
            collector = Collector(folder, None, "run-id")
            path = folder / "attempts" / "interrupted.json"
            path.write_text(json.dumps([{"status": "in_flight", "usage": {}}]))
            with patch(
                "api_cache.request", side_effect=AssertionError("Unexpected retry")
            ):
                self.assertEqual(
                    collector.attempts("interrupted", {})[0]["error"],
                    "unknown_remote_outcome",
                )
