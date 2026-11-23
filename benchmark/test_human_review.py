"""Offline tests for the private-plan and public blind-review boundary."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import human_review as hr


def _jsonl(path: Path, rows):
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def fixture(root: Path):
    cases = [
        {
            "id": "Q01",
            "family": "clarify",
            "prompt": "恶意题面 <script>alert('prompt')</script>",
            "criteria": ["第一项", "第二项", "第三项"],
        }
    ]
    subjects = []
    families = ("clarify", "continuity", "equivalence", "evidence", "handoff", "trial")
    cases = [dict(cases[0], id=f"Q{i:02}", family=family) for i, family in enumerate(families, 1)]
    for i in range(72):
        case = cases[i // 12]
        rep = i % 12 + 1
        for arm in hr.ARM_ORDER:
            key = f"{case['id']}-{rep}-{arm}"
            subjects.append(
                {
                    "id": key,
                    "case": case["id"],
                    "family": case["family"],
                    "rep": rep,
                    "arm": arm,
                    "attempts": [
                        {
                            "ok": True,
                            "turns": [
                                {
                                    "user": "用户 <img src=x onerror=alert(1)>",
                                    "answer": "答复 </script><script>alert('answer')</script>",
                                },
                                {"user": "第二轮用户", "answer": "第二轮答复"},
                            ],
                        }
                    ],
                }
            )
    history = []
    for rater in ("A", "B"):
        for row in subjects:
            checks = [True, True, True]
            if rater == "A" and row["id"] == "Q01-1-none":
                checks = [False, True, True]
            history.append(
                {
                    "blind_id": f"old-{rater}-{row['id']}",
                    "key": row["id"],
                    "kind": "primary",
                    "rater": rater,
                    "vote": {"checks": checks, "pass": all(checks), "evidence": "old"},
                }
            )
    anchors = {
        "anchor-equiv1",
        "anchor-equiv2",
        "anchor-false_pass",
        "anchor-false_reject",
    }
    roster = hr.repeat_sample({r["id"]: r for r in subjects})
    by_identity = {(r["rater"], r["key"]):r for r in history}
    for rater in ("A","B"):
        for key in roster:
            history.append(dict(by_identity[rater,key], kind="repeat", blind_id=f"repeat-{key}-{rater}"))
        for key in anchors:
            history.append({"kind":"anchor","rater":rater,"key":key,"vote":{"checks":[True]*3,"pass":True}})
    history.append(dict(by_identity["B", "Q01-1-none"], rater="C", blind_id="C-adjudication"))
    new = []
    for index, row in enumerate(subjects):
        new.append(
            {
                "kind": "primary",
                "key": row["id"],
                "checks": [True, True, True],
                "evidence": "new primary",
                "blind_id": f"new-primary-{index}",
            }
        )
    for index, key in enumerate(hr.repeat_sample({r["id"]: r for r in subjects})):
        new.append(
            {
                "kind": "repeat",
                "key": key,
                "checks": [True, True, True],
                "evidence": "new repeat",
                "blind_id": f"new-repeat-{index}",
            }
        )
    for index, key in enumerate(sorted(anchors)):
        new.append(
            {
                "kind": "anchor",
                "key": key,
                "checks": [True, True, True],
                "evidence": "new anchor",
                "blind_id": f"new-anchor-{index}",
            }
        )
    cases_path = root / "cases.json"
    subjects_path = root / "subjects.jsonl"
    ratings_path = root / "ratings.jsonl"
    anchors_path = root / "anchors.json"
    cases_path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    _jsonl(subjects_path, subjects)
    _jsonl(ratings_path, history)
    anchors_path.write_text(
        json.dumps(
            {"answers": [{"key": key[7:], "pass": True} for key in sorted(anchors)]}
        ),
        encoding="utf-8",
    )
    votes_path = root / "new.jsonl"
    _jsonl(votes_path, new)
    return {
        "cases": cases_path,
        "subjects": subjects_path,
        "ratings": ratings_path,
        "anchors": anchors_path,
        "votes": votes_path,
        "subjects_rows": subjects,
        "history_rows": history,
        "new_rows": new,
    }


class HumanReviewTests(unittest.TestCase):
    def patched_sources(self, source):
        return mock.patch.multiple(
            hr,
            CASES_PATH=source["cases"],
            SUBJECTS_PATH=source["subjects"],
            RATINGS_PATH=source["ratings"],
            ANCHORS_PATH=source["anchors"],
        )

    def test_build_is_strict_and_public_is_blind_and_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = fixture(root)
            pack, private = root / "pack", root / "private"
            with self.patched_sources(source):
                result = hr.build(source["votes"], pack, private)
            self.assertEqual(result["cards"], 25)
            self.assertEqual(
                sorted(p.name for p in pack.iterdir()), ["README.txt", "index.html"]
            )
            html = (pack / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("Q01-1-none", html)
            self.assertNotIn("selection_reason", html)
            self.assertNotIn("source_sha256", html)
            self.assertNotIn("seed_hex", html)
            self.assertNotIn("<script>alert", html)
            self.assertIn("\\u003cscript", html)
            self.assertIn("textContent", html)
            self.assertIn("包含重复盲号", html)
            self.assertNotIn("fetch(", html)
            self.assertNotIn("https://", html)
            self.assertNotIn("innerHTML", html)
            plan = json.loads((private / hr.PRIVATE_PLAN).read_text(encoding="utf-8"))
            self.assertEqual(
                plan["pack_id"],
                hr.pack_id_for_cards(
                    [
                        {
                            "blind_id": row["blind_id"],
                            "prompt": "恶意题面 <script>alert('prompt')</script>",
                            "dialogue": [
                                {
                                    "user": "用户 <img src=x onerror=alert(1)>",
                                    "answer": "答复 </script><script>alert('answer')</script>",
                                },
                                {"user": "第二轮用户", "answer": "第二轮答复"},
                            ],
                            "criteria": ["第一项", "第二项", "第三项"],
                        }
                        for row in json.loads(
                            (pack / "index.html")
                            .read_text(encoding="utf-8")
                            .split("const CARDS = ", 1)[1]
                            .split(";", 1)[0]
                        )
                    ]
                ),
            )
            self.assertEqual(len(plan["selected"]), 25)

    def test_new_primary_and_historical_coverage_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = fixture(root)
            key = source["subjects_rows"][0]["id"]
            missing = [
                row
                for row in source["new_rows"]
                if not (row["kind"] == "primary" and row["key"] == key)
            ]
            missing_path = root / "missing.jsonl"
            _jsonl(missing_path, missing)
            duplicate = list(source["new_rows"])
            duplicate_row = dict(
                next(row for row in duplicate if row["kind"] == "primary")
            )
            duplicate_row["blind_id"] = "new-primary-duplicate"
            duplicate.append(duplicate_row)
            duplicate_path = root / "duplicate.jsonl"
            _jsonl(duplicate_path, duplicate)
            malformed = list(source["new_rows"])
            malformed[0] = dict(malformed[0], checks=[True, 1, True])
            malformed_path = root / "malformed.jsonl"
            _jsonl(malformed_path, malformed)
            with self.patched_sources(source):
                for path in (missing_path, duplicate_path, malformed_path):
                    with self.assertRaises(ValueError):
                        hr.build(
                            path,
                            root / (path.stem + "-pack"),
                            root / (path.stem + "-private"),
                        )
                incomplete_history = [
                    row
                    for row in source["history_rows"]
                    if row["key"] != key or row["rater"] != "A"
                ]
                _jsonl(source["ratings"], incomplete_history)
                with self.assertRaises(ValueError):
                    hr.build(
                        source["votes"], root / "history-pack", root / "history-private"
                    )

    def test_reconcile_requires_bound_pack_and_never_counts_missing_as_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = fixture(root)
            pack, private = root / "pack", root / "private"
            with self.patched_sources(source):
                hr.build(source["votes"], pack, private)
            plan = json.loads((private / hr.PRIVATE_PLAN).read_text(encoding="utf-8"))
            first = plan["selected"][0]
            export = root / "answers.json"
            export.write_text(
                json.dumps(
                    {
                        "format": 1,
                        "pack_id": plan["pack_id"],
                        "answers": [
                            {
                                "blind_id": first["blind_id"],
                                "checks": [True, None, True],
                                "evidence": "partial",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            hr.reconcile(export, private)
            result = json.loads(
                (private / hr.PRIVATE_RECONCILED).read_text(encoding="utf-8")
            )
            row = next(
                item
                for item in result["answers"]
                if item["blind_id"] == first["blind_id"]
            )
            self.assertFalse(row["complete"])
            self.assertIsNone(row["pass"])
            wrong = root / "wrong.json"
            wrong.write_text(
                export.read_text(encoding="utf-8").replace(
                    plan["pack_id"], "wrong-pack"
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                hr.reconcile(wrong, private)
            with self.assertRaises(ValueError):
                hr.reconcile(export, private, private / hr.PRIVATE_PLAN)

    def test_rebuild_rejects_changed_votes_without_replacing_mapping(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = fixture(root)
            private = root / "private"
            with self.patched_sources(source):
                hr.build(source["votes"], root / "pack", private)
                before = (private / hr.PRIVATE_PLAN).read_text(encoding="utf-8")
                source["votes"].write_text(
                    source["votes"].read_text(encoding="utf-8") + "\n", encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    hr.build(source["votes"], root / "repack", private, rebuild=True)
            self.assertEqual(
                (private / hr.PRIVATE_PLAN).read_text(encoding="utf-8"), before
            )

    def test_directories_and_blind_ids_have_safe_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(ValueError):
                hr.assert_disjoint(root / "pack", root / "pack" / "private")
        self.assertNotEqual(
            hr.blind_id("00" * 32, "Q01-1-none", 0),
            hr.blind_id("11" * 32, "Q01-1-none", 0),
        )


if __name__ == "__main__":
    unittest.main()
