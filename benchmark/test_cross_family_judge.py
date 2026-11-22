"""Test the cross-family judge's model-visible boundary and technical grading rules."""

import json
import unittest
from collections import Counter

from codex_client import RunError
from cross_family_judge import grade_text, materials, messages


class JudgeBoundaryTest(unittest.TestCase):
    def test_all_contexts_are_single_transcript_and_hide_the_original_verdict(self):
        items, plan = materials()
        entries = [label for packet in plan for label in packet["readings"]]
        self.assertEqual(
            Counter(e["kind"] for e in entries),
            {"primary": 216, "anchor": 4, "repeat": 12},
        )
        for entry in entries:
            contexts = messages(entry, items)
            self.assertEqual([m["role"] for m in contexts], ["system", "user"])
            visible = json.loads(contexts[1]["content"])
            self.assertEqual(len(visible), 1)
            self.assertEqual(set(visible[0]), {"id", "criteria", "dialogue"})
            self.assertEqual(visible[0]["id"], entry["blind_id"])
            self.assertNotEqual(visible[0]["id"], entry["key"])
            self.assertEqual(visible[0]["criteria"], items[entry["key"]]["criteria"])
            self.assertEqual(visible[0]["dialogue"], items[entry["key"]]["dialogue"])
            for turn in visible[0]["dialogue"]:
                self.assertEqual(set(turn), {"user", "answer"})
        primaries = {e["key"]: e["blind_id"] for e in entries if e["kind"] == "primary"}
        for repeated in (e for e in entries if e["kind"] == "repeat"):
            self.assertNotEqual(repeated["blind_id"], primaries[repeated["key"]])

    def test_wrong_id_or_string_check_cannot_become_a_vote(self):
        v = {"id": "x", "checks": [True, False, True], "evidence": "逐项依据"}
        text = json.dumps({"judgments": [v]})
        self.assertFalse(grade_text("```json\n" + text + "\n```", "x")["pass"])
        with self.assertRaises(RunError):
            grade_text(text, "y")
        v["checks"][1] = "false"
        with self.assertRaises(RunError):
            grade_text(json.dumps({"judgments": [v]}), "x")
