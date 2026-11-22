"""Offline regression tests for fact disclosure, blind assignment and paired verdicts."""

import json
import unittest

from codex_client import RunError
from grade import packets, parse_grades
from report import majority, paired, reliability
from run import CASES, PROTOCOL, load_packages, oracle, schedule


class BenchmarkTest(unittest.TestCase):
    def test_git_revisions_and_balanced_assignment(self):
        packages, hashes = load_packages()
        self.assertEqual(hashes["none"]["files"], {})
        self.assertNotIn("<document", packages["none"])
        self.assertIn("references/check-in-example.md", hashes["main"]["files"])
        self.assertNotEqual(
            hashes["main"]["instruction_sha256"],
            hashes["release"]["instruction_sha256"],
        )
        jobs = schedule()
        self.assertEqual(len({j["id"] for j in jobs}), 216)
        for i in range(0, len(jobs), 3):
            block = jobs[i : i + 3]
            self.assertEqual(len({(j["case"], j["rep"]) for j in block}), 1)
            self.assertEqual({j["arm"] for j in block}, set(PROTOCOL["arms"]))

    def test_only_asked_slot_is_revealed(self):
        case = CASES[1]
        reply, slots = oracle(case, "谁决定哪些段落能公开？")
        self.assertEqual(set(slots), {"owner"})
        self.assertNotIn(case["slots"][0]["value"], reply)
        self.assertEqual(
            oracle(case, "需要设置预算吗？")[0], PROTOCOL["fallback_reply"]
        )
        self.assertEqual(oracle(case, "请用纸质记录。")[1], {})
        reply, slots = oracle(case, "你最想解决哪一类问题？谁能做这个决定？")
        self.assertEqual(set(slots), {"owner", "goal"})

    def test_blind_packets_separate_cases_and_repeats(self):
        items = {f"{n}-{r}": {"case": n} for n in range(24) for r in range(3)}
        keys = sorted(items)
        repeats = keys[:12]
        a = packets("A", items, keys, repeats)
        b = packets("B", items, keys, repeats)
        readings = [x for pack in a for x in pack["readings"]]
        self.assertEqual(len(readings), len(items) + len(repeats))
        self.assertEqual(len({x["blind_id"] for x in readings}), len(readings))
        self.assertFalse(
            {x["blind_id"] for x in readings}
            & {x["blind_id"] for p in b for x in p["readings"]}
        )
        for pack in a + b:
            self.assertEqual(
                len(pack["readings"]),
                len({items[x["key"]]["case"] for x in pack["readings"]}),
            )

    def test_pairing_preserves_flips_and_missing_decisions(self):
        a = [i not in (0, 1) for i in range(24)]
        b = [i not in (2, 3) for i in range(24)]
        self.assertEqual(sum(a), sum(b))
        p = paired(a + [None], b + [True])
        self.assertEqual((p["n"], p["gain"], p["loss"]), (24, 2, 2))
        self.assertIsNone(majority([True, False, None]))
        self.assertTrue(majority([True, False, True]))
        self.assertIsNone(reliability([True], [True])["kappa"])
        self.assertEqual(reliability([False, True], [False, True])["kappa"], 1)

    def test_grader_rejects_missing_or_repeated_ids(self):
        msg = {"id": "a", "checks": [True, False, True], "evidence": "没有实际试播。"}
        self.assertFalse(
            parse_grades(json.dumps({"judgments": [msg]}), ["a"])["a"]["pass"]
        )
        with self.assertRaises(RunError):
            parse_grades(json.dumps({"judgments": [msg, msg]}), ["a"])
        with self.assertRaises(RunError):
            parse_grades(json.dumps({"judgments": [msg]}), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
