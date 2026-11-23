"""Offline audit of a second, family-blind rating of the frozen 216 answers.

The historical subjects, cases, anchors and Astra ratings are read locally.
The new JSONL contains one new primary vote for each subject, twelve repeat
reads and four anchor reads.  This module does not call a model and does not
trust a caller-supplied family label: a subject's family comes from
``cases.json``.  It publishes only vote decisions, checks, hashes and
aggregate statistics; the evidence text is retained only in the local vote
copy so the report can be reproduced.

Usage::

    python3 benchmark/cross_family_report.py VOTES_JSONL OUT_DIR
    python3 benchmark/cross_family_report.py --verify OUT_DIR

The public helper :func:`audit_rows` is intentionally independent of the
existing ``report.py`` implementation so a human-review package can reuse the
same validation and comparison definitions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from cross_family_judge import verify as verify_blind_readings
from grade import repeat_sample
from report import verify as verify_baseline

HERE = Path(__file__).resolve().parent
HISTORY = HERE / "results/codex-2026-11-22"
ARMS = ("none", "release", "main")
PAIRS = tuple(itertools.combinations(ARMS, 2))
ARM_LABELS = {"none": "无 skill", "release": "v1.0.0", "main": "main"}
READER_LABELS = {
    "old_final": "Astra 裁决",
    "old_A": "Astra 裁判 A",
    "old_B": "Astra 裁判 B",
    "new": "Qwen 新判",
}
FAMILY_LABELS = {
    "clarify": "主动澄清（两轮）",
    "handoff": "执行交接",
    "equivalence": "等价做法",
    "evidence": "证据范围",
    "trial": "试行门槛",
    "continuity": "承接与实际交付",
}
KINDS = {"primary", "repeat", "anchor"}
COMPARISON_NAMES = ("old_final", "old_A", "old_B")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_digest(value) -> str:
    return digest(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )


def read_json(path: Path):
    return json.loads(path.read_text())


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_jsonl(path: Path, rows):
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        )
    )


def validate_historical_ratings(ratings, primary_ids, repeat_keys, anchor_keys):
    assignments = set()
    primary = {"A": {}, "B": {}, "C": {}}
    for row in ratings:
        key, rater, kind, vote = row["key"], row["rater"], row["kind"], row["vote"]
        identity = (rater, kind, key)
        if identity in assignments or rater not in primary or kind not in KINDS:
            raise ValueError("Unexpected or duplicate historical vote")
        assignments.add(identity)
        if (not isinstance(vote, dict) or not isinstance(vote.get("checks"), list)
                or len(vote["checks"]) != 3 or any(type(v) is not bool for v in vote["checks"])
                or vote.get("pass") is not all(vote["checks"])):
            raise ValueError("Invalid historical checks")
        if kind == "primary":
            primary[rater][key] = vote["pass"]
    expected = {(r, "primary", k) for r in ("A", "B") for k in primary_ids}
    expected.update((r, "repeat", k) for r in ("A", "B") for k in repeat_keys)
    expected.update((r, "anchor", k) for r in ("A", "B") for k in anchor_keys)
    expected.update(("C", "primary", k) for k in primary_ids if primary["A"].get(k) != primary["B"].get(k))
    if assignments != expected:
        raise ValueError("Historical ratings do not cover the frozen primary, reread, anchor and adjudication plans")


def load_history(base: Path = HERE, history: Optional[Path] = None):  # noqa: UP045
    """Load and validate the frozen source material used by the audit."""

    history = history or (base / "results/codex-2026-11-22")
    cases = read_json(base / "cases.json")
    anchors = read_json(base / "anchors.json")
    subjects = read_jsonl(history / "subjects.jsonl")
    ratings = read_jsonl(history / "ratings.jsonl")
    case_index = {row["id"]: row for row in cases}
    subject_index = {row["id"]: row for row in subjects}
    if len(subject_index) != len(subjects):
        raise ValueError("Historical subjects contain duplicate ids")
    expected_families = {case["id"]: case["family"] for case in cases}
    for row in subjects:
        if row.get("case") not in expected_families:
            raise ValueError("Historical subject has an unknown case")
        if row.get("family") != expected_families[row["case"]]:
            raise ValueError("Historical subject family differs from cases.json")
    primary_ids = set(subject_index)
    anchor_expected = {
        "anchor-" + row["key"]: row["pass"] for row in anchors["answers"]
    }
    repeat_keys = set(repeat_sample(subject_index))
    validate_historical_ratings(ratings, primary_ids, repeat_keys, anchor_expected)
    return {
        "base": base,
        "history": history,
        "cases": cases,
        "case_index": case_index,
        "anchors": anchors,
        "subjects": subjects,
        "subject_index": subject_index,
        "ratings": ratings,
        "primary_ids": primary_ids,
        "anchor_expected": anchor_expected,
        "repeat_keys": repeat_keys,
    }


def load_run_info(votes_path: Path):
    path = votes_path.parent / "run.json"
    if not path.is_file():
        return {"model": None, "path": None, "sha256": None}
    value = read_json(path)
    model = (
        value.get("model")
        if isinstance(value, dict) and isinstance(value.get("model"), str)
        else None
    )
    return {"model": model, "path": str(path), "sha256": digest(path.read_bytes())}


def normalize_vote(row, index):
    if not isinstance(row, dict):
        raise TypeError(f"Vote line {index} is not an object")
    kind = row.get("kind")
    key = row.get("key")
    blind_id = row.get("blind_id")
    checks = row.get("checks")
    evidence = row.get("evidence")
    if kind not in KINDS or not isinstance(key, str) or not key:
        raise ValueError(f"Vote line {index} has invalid kind/key")
    if not isinstance(blind_id, str) or not blind_id:
        raise ValueError(f"Vote line {index} has no blind_id")
    if (
        not isinstance(checks, list)
        or len(checks) != 3
        or not all(type(value) is bool for value in checks)
    ):
        raise ValueError(f"Vote line {index} must contain exactly three boolean checks")
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError(f"Vote line {index} has no evidence")
    result = {
        "kind": kind,
        "key": key,
        "checks": list(checks),
        "pass": all(checks),
        "evidence": evidence,
        "blind_id": blind_id,
    }
    for optional in ("group", "family", "rater", "judge", "packet"):
        if isinstance(row.get(optional), str):
            result[optional] = row[optional]
    return result


def normalize_votes(votes):
    if isinstance(votes, (str, Path)):
        votes = read_jsonl(Path(votes))
    rows = [normalize_vote(row, i + 1) for i, row in enumerate(votes)]
    if len({row["blind_id"] for row in rows}) != len(rows):
        raise ValueError("New votes contain duplicate blind_id values")
    return rows


def _old_vote_maps(source):
    maps = {name: {} for name in ("A", "B", "C")}
    repeats = {name: {} for name in ("A", "B", "C")}
    anchors = {name: {} for name in ("A", "B", "C")}
    for row in source["ratings"]:
        rater = row.get("rater")
        kind = row.get("kind")
        vote = row.get("vote")
        if (
            rater not in maps
            or kind not in ("primary", "repeat", "anchor")
            or not isinstance(vote, dict)
        ):
            continue
        checks = vote.get("checks")
        passed = vote.get("pass")
        if (
            not isinstance(checks, list)
            or len(checks) != 3
            or not all(type(v) is bool for v in checks)
        ):
            continue
        if type(passed) is not bool:
            passed = all(checks)
        value = {"checks": list(checks), "pass": passed}
        if kind == "primary":
            maps[rater][row.get("key")] = value
        elif kind == "repeat":
            repeats[rater][row.get("key")] = value
        else:
            anchors[rater][row.get("key")] = value
    return maps, repeats, anchors


def majority(values):
    values = [value for value in values if value is not None]
    if values.count(True) >= 2:
        return True
    if values.count(False) >= 2:
        return False
    return None


def final_vote(a, b, c):
    """Reconstruct old Astra final: C when present, otherwise A/B majority."""

    if c is not None:
        return c["pass"], c["checks"]
    passed = majority([a["pass"] if a else None, b["pass"] if b else None])
    checks = []
    for index in range(3):
        values = [
            a["checks"][index] if a else None,
            b["checks"][index] if b else None,
        ]
        checks.append(
            values[0] if values[0] is not None and values[0] == values[1] else None
        )
    return passed, checks


def old_views(source):
    maps, repeats, anchors = _old_vote_maps(source)
    final_pass, final_checks = {}, {}
    for key in source["primary_ids"]:
        passed, checks = final_vote(
            maps["A"].get(key), maps["B"].get(key), maps["C"].get(key)
        )
        final_pass[key] = passed
        final_checks[key] = checks
    return {
        "A": maps["A"],
        "B": maps["B"],
        "C": maps["C"],
        "final_pass": final_pass,
        "final_checks": final_checks,
        "repeats": repeats,
        "anchors": anchors,
    }


def validate_new_rows(rows, source):
    primary = [row for row in rows if row["kind"] == "primary"]
    repeats = [row for row in rows if row["kind"] == "repeat"]
    anchors = [row for row in rows if row["kind"] == "anchor"]
    expected = source["primary_ids"]
    primary_keys = [row["key"] for row in primary]
    if (
        len(primary) != len(expected)
        or set(primary_keys) != expected
        or len(set(primary_keys)) != len(primary)
    ):
        raise ValueError(
            "New votes must contain exactly one primary vote per historical subject"
        )
    if len(repeats) != 12 or len({row["key"] for row in repeats}) != 12:
        raise ValueError("New votes must contain exactly twelve distinct repeat reads")
    if {row["key"] for row in repeats} != source["repeat_keys"]:
        raise ValueError("Repeat subjects differ from the preregistered roster")
    expected_anchors = set(source["anchor_expected"])
    anchor_keys = [row["key"] for row in anchors]
    if (
        len(anchors) != 4
        or set(anchor_keys) != expected_anchors
        or len(set(anchor_keys)) != len(anchor_keys)
    ):
        raise ValueError("New votes must contain exactly the four frozen anchors")


def binary_stats(old_values, new_values):
    counts = Counter()
    for old, new in zip(old_values, new_values):
        if old is None or new is None:
            continue
        counts[(old, new)] += 1
    n = sum(counts.values())
    agree = counts[(False, False)] + counts[(True, True)]
    old_rate = (counts[(True, False)] + counts[(True, True)]) / n if n else None
    new_rate = (counts[(False, True)] + counts[(True, True)]) / n if n else None
    expected = old_rate * new_rate + (1 - old_rate) * (1 - new_rate) if n else None
    kappa = None if not n or expected == 1 else (agree / n - expected) / (1 - expected)
    return {
        "n": n,
        "agreement": agree,
        "agreement_rate": agree / n if n else None,
        "kappa": kappa,
        "confusion": {
            "old_false_new_false": counts[(False, False)],
            "old_false_new_true": counts[(False, True)],
            "old_true_new_false": counts[(True, False)],
            "old_true_new_true": counts[(True, True)],
        },
    }


def flip_counts(left, right):
    counts = Counter(
        (a, b) for a, b in zip(left, right) if a is not None and b is not None
    )
    return {
        "n": sum(counts.values()),
        "gain": counts[(False, True)],
        "loss": counts[(True, False)],
        "both_pass": counts[(True, True)],
        "both_fail": counts[(False, False)],
    }


def pass_fraction(values):
    values = [value for value in values if value is not None]
    return {
        "passed": sum(values),
        "n": len(values),
        "rate": sum(values) / len(values) if values else None,
    }


def family_keys(source):
    result = defaultdict(list)
    for key, row in source["subject_index"].items():
        result[source["case_index"][row["case"]]["family"]].append(key)
    return {family: sorted(keys) for family, keys in sorted(result.items())}


def _dialogue_signature(subject):
    attempts = subject.get("attempts") if isinstance(subject, dict) else None
    if not isinstance(attempts, list):
        return None
    attempt = next(
        (row for row in attempts if isinstance(row, dict) and row.get("ok")), None
    )
    if not attempt or not isinstance(attempt.get("turns"), list):
        return None
    dialogue = []
    for turn in attempt["turns"]:
        if (
            not isinstance(turn, dict)
            or not isinstance(turn.get("user"), str)
            or not isinstance(turn.get("answer"), str)
        ):
            return None
        dialogue.append({"user": turn["user"], "answer": turn["answer"]})
    return json_digest(dialogue)


def overlap_sensitivity(source, old_values, new_values):
    """Describe exact-text overlap after the main unit-level scoring.

    This is explicitly post hoc: duplicate text is not collapsed, reweighted or
    removed from any primary statistic.  Only the number of duplicate groups
    whose conclusions differ and their within-group discordant pairs is shown.
    """

    groups = defaultdict(list)
    for subject in source["subjects"]:
        signature = _dialogue_signature(subject)
        if signature:
            groups[signature].append(subject["id"])
    duplicate = []
    for signature, keys in sorted(groups.items()):
        if len(keys) < 2:
            continue
        old = [old_values.get(key) for key in sorted(keys)]
        new = [new_values.get(key) for key in sorted(keys)]
        old_pairs = sum(
            a != b
            for a, b in itertools.combinations(old, 2)
            if a is not None and b is not None
        )
        new_pairs = sum(
            a != b
            for a, b in itertools.combinations(new, 2)
            if a is not None and b is not None
        )
        duplicate.append(
            {
                "signature_sha256": signature,
                "ids": sorted(keys),
                "id_count": len(keys),
                "old_final_distinct": sorted(
                    {value for value in old if value is not None}
                ),
                "new_distinct": sorted({value for value in new if value is not None}),
                "old_final_discordant_pairs": old_pairs,
                "new_discordant_pairs": new_pairs,
            }
        )
    return {
        "duplicate_groups": len(duplicate),
        "covered_ids": sum(row["id_count"] for row in duplicate),
        "old_final_groups_with_disagreement": sum(
            len(row["old_final_distinct"]) > 1 for row in duplicate
        ),
        "new_groups_with_disagreement": sum(
            len(row["new_distinct"]) > 1 for row in duplicate
        ),
        "old_final_discordant_pairs": sum(
            row["old_final_discordant_pairs"] for row in duplicate
        ),
        "new_discordant_pairs": sum(row["new_discordant_pairs"] for row in duplicate),
        "groups": duplicate,
    }


def compare_by_family(source, old_values, new_values):
    result = {}
    values_by_family = family_keys(source)
    for family, keys in values_by_family.items():
        result[family] = binary_stats(
            [old_values.get(key) for key in keys], [new_values.get(key) for key in keys]
        )
    return result


def compare_by_arm(source, old_values, new_values):
    result = {}
    for arm in ARMS:
        keys = sorted(
            key for key, row in source["subject_index"].items() if row.get("arm") == arm
        )
        result[arm] = binary_stats(
            [old_values.get(key) for key in keys], [new_values.get(key) for key in keys]
        )
    return result


def compare_by_family_arm(source, old_values, new_values):
    result = {}
    for family, keys in family_keys(source).items():
        result[family] = {}
        for arm in ARMS:
            selected = [
                key for key in keys if source["subject_index"][key].get("arm") == arm
            ]
            result[family][arm] = binary_stats(
                [old_values.get(key) for key in selected],
                [new_values.get(key) for key in selected],
            )
    return result


def criterion_comparison(source, old_checks, new_rows_by_key):
    result = {}
    for index in range(3):
        old_values = {}
        new_values = {}
        for key in source["primary_ids"]:
            checks = old_checks.get(key)
            old_values[key] = (
                checks[index] if checks and checks[index] is not None else None
            )
            new = new_rows_by_key.get(key)
            new_values[key] = new["checks"][index] if new else None
        result[str(index + 1)] = {
            "overall": binary_stats(
                [old_values.get(key) for key in source["primary_ids"]],
                [new_values.get(key) for key in source["primary_ids"]],
            ),
            "by_family": compare_by_family(source, old_values, new_values),
            "by_arm": compare_by_arm(source, old_values, new_values),
            "by_family_arm": compare_by_family_arm(source, old_values, new_values),
        }
    return result


def case_views(source, old_values, new_values):
    rows = []
    for case in source["cases"]:
        runs = {}
        majorities = {"old": {}, "new": {}}
        for arm in ARMS:
            old = [old_values.get(f"{case['id']}-{rep}-{arm}") for rep in range(1, 4)]
            new = [new_values.get(f"{case['id']}-{rep}-{arm}") for rep in range(1, 4)]
            runs[arm] = {"old": old, "new": new}
            majorities["old"][arm] = majority(old)
            majorities["new"][arm] = majority(new)
        rows.append(
            {
                "id": case["id"],
                "family": case["family"],
                "runs": runs,
                "majority": majorities,
            }
        )
    return rows


def version_pairs(cases):
    result = {}
    for left, right in PAIRS:
        data = {}
        for source_name in ("old", "new"):
            runs_left = [
                case["runs"][left][source_name][rep - 1]
                for case in cases
                for rep in range(1, 4)
            ]
            runs_right = [
                case["runs"][right][source_name][rep - 1]
                for case in cases
                for rep in range(1, 4)
            ]
            maj_left = [case["majority"][source_name][left] for case in cases]
            maj_right = [case["majority"][source_name][right] for case in cases]
            data[source_name] = {
                "runs": flip_counts(runs_left, runs_right),
                "case_majorities": flip_counts(maj_left, maj_right),
            }
        result[left + "→" + right] = data
    return result


def repeat_consistency(source, old, new_rows):
    new_primary = {
        row["key"]: row["pass"] for row in new_rows if row["kind"] == "primary"
    }
    new_repeats = {
        row["key"]: row["pass"] for row in new_rows if row["kind"] == "repeat"
    }
    result = {}
    for name, mapping in (
        ("new", new_repeats),
        ("old_A", old["repeats"]["A"]),
        ("old_B", old["repeats"]["B"]),
        ("old_final", None),
    ):
        if name == "old_final":
            keys = set(old["repeats"]["A"]) | set(old["repeats"]["B"])
            mapping = {}
            for key in keys:
                a = old["repeats"]["A"].get(key)
                b = old["repeats"]["B"].get(key)
                c = old["repeats"]["C"].get(key)
                mapping[key] = final_vote(a, b, c)[0]
        pairs = []
        for key, value in mapping.items():
            primary = (
                new_primary.get(key)
                if name == "new"
                else old["final_pass"].get(key)
                if name == "old_final"
                else old[name[-1]].get(key, {}).get("pass")
            )
            if primary is not None:
                pairs.append(
                    (primary, value if type(value) is bool else value.get("pass"))
                )
        by_family = {}
        for family, keys in family_keys(source).items():
            selected = (
                [
                    (new_primary.get(key), new_repeats[key])
                    for key in new_repeats
                    if key in keys
                ]
                if name == "new"
                else [
                    (
                        old["final_pass"].get(key)
                        if name == "old_final"
                        else old[name[-1]].get(key, {}).get("pass"),
                        mapping[key]
                        if type(mapping[key]) is bool
                        else mapping[key].get("pass"),
                    )
                    for key in mapping
                    if key in keys
                ]
            )
            by_family[family] = binary_stats(
                [a for a, _ in selected], [b for _, b in selected]
            )
        result[name] = {
            "overall": binary_stats([a for a, _ in pairs], [b for _, b in pairs]),
            "by_family": by_family,
        }
    return result


def anchor_accuracy(source, old, new_rows):
    new = {row["key"]: row["pass"] for row in new_rows if row["kind"] == "anchor"}
    methods = {
        "new": new,
        "old_A": {key: value["pass"] for key, value in old["anchors"]["A"].items()},
        "old_B": {key: value["pass"] for key, value in old["anchors"]["B"].items()},
        "old_final": {},
    }
    for key in source["anchor_expected"]:
        methods["old_final"][key] = final_vote(
            old["anchors"]["A"].get(key),
            old["anchors"]["B"].get(key),
            old["anchors"]["C"].get(key),
        )[0]
    result = {}
    for name, values in methods.items():
        by_key = {}
        correct = 0
        total = 0
        for key, expected in source["anchor_expected"].items():
            observed = values.get(key)
            ok = observed == expected if observed is not None else None
            by_key[key] = {"expected": expected, "observed": observed, "correct": ok}
            if ok is not None:
                correct += ok
                total += 1
        result[name] = {
            "correct": correct,
            "n": total,
            "accuracy": correct / total if total else None,
            "by_key": by_key,
        }
    return result


def arm_summary(source, old, new_pass, cases):
    """Summarize primary and per-case majority results for each skill arm."""

    result = {}
    for arm in ARMS:
        keys = sorted(
            key for key, row in source["subject_index"].items() if row.get("arm") == arm
        )
        astra = [old["final_pass"].get(key) for key in keys]
        qwen = [new_pass.get(key) for key in keys]
        final_comparison = binary_stats(astra, qwen)
        astra_majority = [case["majority"]["old"][arm] for case in cases]
        qwen_majority = [case["majority"]["new"][arm] for case in cases]
        majority_comparison = binary_stats(astra_majority, qwen_majority)
        confusion = final_comparison["confusion"]
        result[arm] = {
            "label": ARM_LABELS[arm],
            "scheduled": len(keys),
            "pass": {
                "astra_final": pass_fraction(astra),
                "qwen": pass_fraction(qwen),
            },
            "same_answer": {
                "old_false_new_true": confusion["old_false_new_true"],
                "old_true_new_false": confusion["old_true_new_false"],
                "n": final_comparison["n"],
            },
            "comparison": final_comparison,
            "majority": {
                "astra": pass_fraction(astra_majority),
                "qwen": pass_fraction(qwen_majority),
                "comparison": majority_comparison,
            },
        }
    return result


def build_summary(source, rows, run_info=None):
    run_info = run_info or {"model": None, "path": None, "sha256": None}
    old = old_views(source)
    new_primary = {row["key"]: row for row in rows if row["kind"] == "primary"}
    new_pass = {key: row["pass"] for key, row in new_primary.items()}
    old_sources = {
        "old_final": old["final_pass"],
        "old_A": {key: value["pass"] for key, value in old["A"].items()},
        "old_B": {key: value["pass"] for key, value in old["B"].items()},
    }
    comparisons = {}
    for name, values in old_sources.items():
        comparisons[name] = {
            "overall": binary_stats(
                [values.get(key) for key in source["primary_ids"]],
                [new_pass.get(key) for key in source["primary_ids"]],
            ),
            "by_family": compare_by_family(source, values, new_pass),
            "by_arm": compare_by_arm(source, values, new_pass),
            "by_family_arm": compare_by_family_arm(source, values, new_pass),
            "by_criterion": criterion_comparison(
                source,
                old["final_checks"]
                if name == "old_final"
                else {key: value["checks"] for key, value in old[name[-1]].items()},
                new_primary,
            ),
        }
    families = {}
    for family, keys in family_keys(source).items():
        families[family] = {
            "scheduled": len(keys),
            "pass": {
                "new": pass_fraction([new_pass.get(key) for key in keys]),
                "old_final": pass_fraction(
                    [old["final_pass"].get(key) for key in keys]
                ),
                "old_A": pass_fraction(
                    [old["A"].get(key, {}).get("pass") for key in keys]
                ),
                "old_B": pass_fraction(
                    [old["B"].get(key, {}).get("pass") for key in keys]
                ),
            },
            "new_minus_old_final": comparisons["old_final"]["by_family"][family],
            "criteria": {
                criterion: {
                    name: comparisons[name]["by_criterion"][criterion]["by_family"][
                        family
                    ]
                    for name in COMPARISON_NAMES
                }
                for criterion in ("1", "2", "3")
            },
        }
    cases = case_views(source, old["final_pass"], new_pass)
    return {
        "format": 1,
        "run": {
            "model": run_info.get("model"),
            "run_json_sha256": run_info.get("sha256"),
        },
        "source_counts": {
            "subjects": len(source["subjects"]),
            "primary": sum(row["kind"] == "primary" for row in rows),
            "repeat": sum(row["kind"] == "repeat" for row in rows),
            "anchor": sum(row["kind"] == "anchor" for row in rows),
            "old_ratings": len(source["ratings"]),
        },
        "reader_labels": dict(READER_LABELS),
        "comparisons": comparisons,
        "arms": arm_summary(source, old, new_pass, cases),
        "families": families,
        "repeats": repeat_consistency(source, old, rows),
        "anchors": anchor_accuracy(source, old, rows),
        "version_pairs": version_pairs(cases),
        "cases": cases,
        "overlap_sensitivity": overlap_sensitivity(source, old["final_pass"], new_pass),
        "definitions": {
            "old_final": "C primary vote when present; otherwise the A/B primary majority. Criterion-level old_final uses C checks or equal A/B checks, leaving disagreements null.",
            "new": "One new primary vote per historical subject; pass is all three supplied checks.",
            "family": "Derived from each subject's case in cases.json; caller-supplied group/family labels do not alter grouping.",
            "agreement": "Raw exact agreement among non-null paired binary decisions; kappa uses the same denominator.",
            "version_pair": "Same case and repetition across arms; case_majorities are 2-of-3 repetition majorities, with missing values excluded.",
        },
    }


def audit_rows(votes, source=None):
    """Validate new votes and return reusable normalized audit material.

    ``source`` may be the dictionary returned by :func:`load_history`; when
    omitted, the frozen repository source is loaded.  This function is safe
    for a human-review pack: it performs no model calls and does not import
    the existing reporter.
    """

    source = source or load_history()
    rows = normalize_votes(votes)
    validate_new_rows(rows, source)
    return {
        "rows": rows,
        "primary": {row["key"]: row for row in rows if row["kind"] == "primary"},
        "repeat": {row["key"]: row for row in rows if row["kind"] == "repeat"},
        "anchor": {row["key"]: row for row in rows if row["kind"] == "anchor"},
        "source": source,
    }


def mask(values):
    return "".join(
        "1" if value is True else "0" if value is False else "?" for value in values
    )


def csv_text(summary):
    columns = ["case", "family"]
    for arm in ARMS:
        columns.extend(
            [
                arm + "_old",
                arm + "_new",
                arm + "_old_majority",
                arm + "_new_majority",
                arm + "_old_to_new_gain",
                arm + "_old_to_new_loss",
                arm + "_old_to_new_n",
                arm + "_old_majority_to_new_gain",
                arm + "_old_majority_to_new_loss",
                arm + "_old_majority_to_new_n",
            ]
        )
    for left, right in PAIRS:
        for source_name in ("old", "new"):
            prefix = f"{left}_to_{right}_{source_name}"
            columns.extend(
                [
                    prefix + "_gain",
                    prefix + "_loss",
                    prefix + "_n",
                    prefix + "_majority_gain",
                    prefix + "_majority_loss",
                    prefix + "_majority_n",
                ]
            )
    output = []
    output.append(columns)
    for case in summary["cases"]:
        row = [case["id"], case["family"]]
        for arm in ARMS:
            run_flip = flip_counts(case["runs"][arm]["old"], case["runs"][arm]["new"])
            majority_flip = flip_counts(
                [case["majority"]["old"][arm]],
                [case["majority"]["new"][arm]],
            )
            row.extend(
                [
                    mask(case["runs"][arm]["old"]),
                    mask(case["runs"][arm]["new"]),
                    "?"
                    if case["majority"]["old"][arm] is None
                    else "1"
                    if case["majority"]["old"][arm]
                    else "0",
                    "?"
                    if case["majority"]["new"][arm] is None
                    else "1"
                    if case["majority"]["new"][arm]
                    else "0",
                    run_flip["gain"],
                    run_flip["loss"],
                    run_flip["n"],
                    majority_flip["gain"],
                    majority_flip["loss"],
                    majority_flip["n"],
                ]
            )
        for left, right in PAIRS:
            for source_name in ("old", "new"):
                pair_runs = flip_counts(
                    case["runs"][left][source_name], case["runs"][right][source_name]
                )
                pair_majorities = flip_counts(
                    [case["majority"][source_name][left]],
                    [case["majority"][source_name][right]],
                )
                row.extend(
                    [
                        pair_runs["gain"],
                        pair_runs["loss"],
                        pair_runs["n"],
                        pair_majorities["gain"],
                        pair_majorities["loss"],
                        pair_majorities["n"],
                    ]
                )
        output.append(row)
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerows(output)
    return stream.getvalue()


def pct(value):
    return "待核实" if value is None else f"{100 * value:.1f}%"


def pair_text(value):
    if not value or value.get("n") is None:
        return "待核实"
    return f"{value.get('gain', 0)}/{value.get('loss', 0)}/{value.get('n', 0)}"


def comp_text(value):
    if not value:
        return "待核实"
    kappa = "不可估计" if value.get("kappa") is None else f"{value['kappa']:.3f}"
    return f"{value.get('agreement', 0)}/{value.get('n', 0)}（{pct(value.get('agreement_rate'))}），κ={kappa}"


def pass_text(value):
    if not value:
        return "待核实"
    return f"{value['passed']}/{value['n']}（{pct(value['rate'])}）"


def render(summary):
    run = summary["run"]
    s = [
        "# 同一批答卷的跨模型家族盲评",
        "",
        f"新判运行模型为 `{run.get('model') or '待核实'}`。历史 216 份答卷和 Astra 评分来自仓库冻结材料。",
        "通过率按 216 份主判计算。另用 12 份重读答卷检验稳定性，用 4 份有预设结论的校准样例核对判据。",
        "",
        "## 按技能版本的通过率",
        "",
        f"同一份答卷的两家判定逐一配对。最后两列按题计数，每题三次重复至少两次通过即算通过，分母为 {len(summary['cases'])}。",
        "",
        "| 技能版本 | Astra 裁决通过 | Qwen 新判通过 | 旧否→新是 | 旧是→新否 | Astra 多数通过 | Qwen 多数通过 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in ARMS:
        value = summary["arms"][arm]
        final = value["pass"]
        flip = value["same_answer"]
        majority_value = value["majority"]
        s.append(
            f"| {value['label']} | {pass_text(final['astra_final'])} | "
            f"{pass_text(final['qwen'])} | {flip['old_false_new_true']} | "
            f"{flip['old_true_new_false']} | {pass_text(majority_value['astra'])} | "
            f"{pass_text(majority_value['qwen'])} |"
        )
    s.extend(
        [
            "",
            "## 总体新旧一致性",
            "",
            "矩阵方向为历史判定到新判定。分子和分母只取非缺失配对，κ 与原始一致率使用同一分母。",
            "",
            "| 历史参照 | 原始一致率 | 混淆矩阵（否否 / 否是 / 是否 / 是是） | κ |",
            "|---|---:|---:|---:|",
        ]
    )
    for name in ("old_final", "old_A", "old_B"):
        value = summary["comparisons"][name]["overall"]
        confusion = value["confusion"]
        matrix = " / ".join(
            str(confusion[key])
            for key in (
                "old_false_new_false",
                "old_false_new_true",
                "old_true_new_false",
                "old_true_new_true",
            )
        )
        kappa = "不可估计" if value["kappa"] is None else f"{value['kappa']:.3f}"
        s.append(
            f"| {READER_LABELS[name]} | {value['agreement']}/{value['n']}（{pct(value['agreement_rate'])}） | {matrix} | {kappa} |"
        )
    s.extend(
        [
            "",
            "## 按技能版本的新旧一致性",
            "",
            "| 技能版本 | Qwen 新判与 Astra 裁决 | Qwen 新判与 Astra 裁判 A | Qwen 新判与 Astra 裁判 B |",
            "|---|---:|---:|---:|",
        ]
    )
    for arm in ARMS:
        s.append(
            f"| {ARM_LABELS[arm]} | {comp_text(summary['comparisons']['old_final']['by_arm'][arm])} | "
            f"{comp_text(summary['comparisons']['old_A']['by_arm'][arm])} | "
            f"{comp_text(summary['comparisons']['old_B']['by_arm'][arm])} |"
        )
    s.extend(
        [
            "",
            "## 题族分组",
            "",
            "| 题族 | 主判数 | Qwen 新判通过 | Astra 裁决通过 | Qwen 与 Astra 裁决一致率和 κ | 旧否→新是/旧是→新否/配对数 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for family, value in summary["families"].items():
        final = value["new_minus_old_final"]
        s.append(
            f"| {FAMILY_LABELS.get(family, family)} | {value['scheduled']} | "
            f"{pass_text(value['pass']['new'])} | "
            f"{pass_text(value['pass']['old_final'])} | "
            f"{comp_text(final)} | {final['confusion']['old_false_new_true']}/{final['confusion']['old_true_new_false']}/{final['n']} |"
        )
    s.extend(
        [
            "",
            "## 题族与判据一致性",
            "",
            "以下分别比较三项判据。历史裁决遇到无法确定的判据时，该项不计入配对。",
            "",
            "| 题族 | 判据 | Qwen 与 Astra 裁决 | Qwen 与 Astra 裁判 A | Qwen 与 Astra 裁判 B |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for family, value in summary["families"].items():
        for criterion in ("1", "2", "3"):
            s.append(
                f"| {FAMILY_LABELS.get(family, family)} | {criterion} | "
                f"{comp_text(value['criteria'][criterion]['old_final'])} | "
                f"{comp_text(value['criteria'][criterion]['old_A'])} | "
                f"{comp_text(value['criteria'][criterion]['old_B'])} |"
            )
    s.extend(
        [
            "",
            "## 事后文本重合敏感性（不改变主计分）",
            "",
            "按完整用户输入与模型答复逐字哈希分组。原答卷单元仍各自计分，不合并也不重加权。这里只报告重合组中结论不同的组数和配对数。",
            "",
            "| 相同完整文字组 | 覆盖标识数 | Astra 裁决有分歧组 | Qwen 新判有分歧组 | Astra 裁决分歧配对 | Qwen 新判分歧配对 |",
            "|---:|---:|---:|---:|---:|---:|",
            (
                f"| {summary['overlap_sensitivity']['duplicate_groups']} | {summary['overlap_sensitivity']['covered_ids']} | "
                f"{summary['overlap_sensitivity']['old_final_groups_with_disagreement']} | "
                f"{summary['overlap_sensitivity']['new_groups_with_disagreement']} | "
                f"{summary['overlap_sensitivity']['old_final_discordant_pairs']} | "
                f"{summary['overlap_sensitivity']['new_discordant_pairs']} |"
            ),
            "",
            "## 版本配对的逐次结果与多数结果",
            "",
            "每项依次为增加数、减少数和配对数。多数结果取同题三次重复中的二比一。",
            "",
            "| 对照 | Astra 逐次 | Qwen 逐次 | Astra 多数 | Qwen 多数 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for left, right in PAIRS:
        value = summary["version_pairs"][left + "→" + right]
        s.append(
            f"| {ARM_LABELS[left]}→{ARM_LABELS[right]} | {pair_text(value['old']['runs'])} | {pair_text(value['new']['runs'])} | "
            f"{pair_text(value['old']['case_majorities'])} | {pair_text(value['new']['case_majorities'])} |"
        )
    s.extend(
        [
            "",
            "## 重复与锚点",
            "",
            "| 参照 | 重读一致率 | 锚点准确率 |",
            "|---|---:|---:|",
        ]
    )
    for name in ("new", "old_final", "old_A", "old_B"):
        repeat = summary["repeats"][name]["overall"]
        anchor = summary["anchors"][name]
        s.append(
            f"| {READER_LABELS[name]} | {repeat['agreement']}/{repeat['n']}（{pct(repeat['agreement_rate'])}） | {anchor['correct']}/{anchor['n']}（{pct(anchor['accuracy'])}） |"
        )
    s.extend(
        [
            "",
            "## 逐题三次重复",
            "",
            "`1/0/?` 分别表示通过、未通过、缺失。完整矩阵见 [flips.csv](flips.csv)。",
            "",
            "| 题目 | 题族 | 无技能 Astra/Qwen | v1.0.0 Astra/Qwen | 主分支 Astra/Qwen |",
            "|---|---|---|---|---|",
        ]
    )
    for case in summary["cases"]:
        values = [
            f"{mask(case['runs'][arm]['old'])}/{mask(case['runs'][arm]['new'])}"
            for arm in ARMS
        ]
        s.append(
            f"| {case['id']} | {FAMILY_LABELS.get(case['family'], case['family'])} | "
            + " | ".join(values)
            + " |"
        )
    s.extend(
        [
            "",
            "## 复算边界",
            "",
            "Astra 的 A、B 同判时取其共同结论，有分歧时由 C 给出决定性一票。Qwen 对每份答卷提供一次主判。三组通过率描述这 24 道公开题在两套判卷流程下的表现。版本发布决策还可结合独立人工复核。",
            "",
            "[机读汇总](summary.json) · [逐题矩阵](flips.csv) · [新票副本](votes.jsonl)",
            "",
        ]
    )
    return "\n".join(s)


def source_hashes(source):
    result = {}
    for label, path in (
        ("cases.json", source["base"] / "cases.json"),
        ("anchors.json", source["base"] / "anchors.json"),
        ("subjects.jsonl", source["history"] / "subjects.jsonl"),
        ("ratings.jsonl", source["history"] / "ratings.jsonl"),
    ):
        result[label] = digest(path.read_bytes())
    return result


def verify_full_run(votes_path, source):
    if len(source["primary_ids"]) != 216:
        return
    folder = votes_path.parent
    if not all((folder / n).is_file() for n in ("run.json", "attempts.jsonl", "votes.jsonl")):
        raise ValueError("The full publication requires run.json, attempts.jsonl and votes.jsonl")
    if votes_path.read_bytes() != (folder / "votes.jsonl").read_bytes():
        raise ValueError("Published votes must match the verified run's votes.jsonl")
    verify_baseline(source["history"])
    verify_blind_readings(folder)


def publish(votes_path: Path, out: Path):
    source = load_history()
    run_info = load_run_info(votes_path)
    if len(source["primary_ids"]) == 216 and not run_info["sha256"]:
        raise ValueError("The frozen 216-answer publication requires run.json")
    verify_full_run(votes_path, source)
    raw_rows = read_jsonl(votes_path)
    audit = audit_rows(raw_rows, source)
    rows = audit["rows"]
    summary = build_summary(source, rows, run_info)
    out.mkdir(parents=True, exist_ok=True)
    if run_info["path"]:
        run_bytes = Path(run_info["path"]).read_bytes()
        if votes_path.parent.resolve() != out.resolve():
            (out / "run.json").write_bytes(run_bytes)
    full = len(source["primary_ids"]) == 216
    if full and votes_path.parent.resolve() != out.resolve():
        (out / "attempts.jsonl").write_bytes((votes_path.parent / "attempts.jsonl").read_bytes())
    # Keep the input line order and any caller-visible semantic fields for a
    # downstream cross_family_judge verifier.  Statistics use the validated
    # normalized rows above, not an implicit reordering.
    write_jsonl(out / "votes.jsonl", raw_rows)
    write_json(out / "summary.json", summary)
    (out / "flips.csv").write_text(csv_text(summary))
    (out / "report.md").write_text(render(summary))
    manifest = {
        "format": 1,
        "reporter_sha256": digest(Path(__file__).read_bytes()),
        "source_sha256": source_hashes(source),
        "run_info": {"model": run_info.get("model"), "sha256": run_info.get("sha256")},
        "artifacts_sha256": {
            name: digest((out / name).read_bytes())
            for name in (("votes.jsonl", "summary.json", "flips.csv", "report.md")
                         + (("run.json", "attempts.jsonl") if full else ()))
        },
    }
    write_json(out / "manifest.json", manifest)
    return summary


def verify(out: Path):
    manifest = read_json(out / "manifest.json")
    # The original immutable release is also checked under the stronger rules.
    legacy_reporter = "3998b5961ff90e545e0d6b7fe13e9f74e64c475d0ac6489b879b5a96209725ff"
    if manifest.get("reporter_sha256") not in (digest(Path(__file__).read_bytes()), legacy_reporter):
        raise ValueError("Reporter source differs")
    for name, expected in manifest.get("artifacts_sha256", {}).items():
        path = out / name
        if not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError("Published artifact checksum differs: " + name)
    source = load_history()
    if source_hashes(source) != manifest.get("source_sha256"):
        raise ValueError("Historical source differs")
    adjacent_run = out / "run.json"
    if (manifest.get("run_info", {}).get("sha256") or len(source["primary_ids"]) == 216) and not adjacent_run.is_file():
        raise ValueError("Published run metadata is missing")
    if adjacent_run.is_file():
        adjacent_info = load_run_info(out / "votes.jsonl")
        manifest_info = manifest.get("run_info", {})
        if adjacent_info.get("sha256") != manifest_info.get(
            "sha256"
        ) or adjacent_info.get("model") != manifest_info.get("model"):
            raise ValueError("Adjacent run.json differs from manifest run_info")
    verify_full_run(out / "votes.jsonl", source)
    rows = normalize_votes(out / "votes.jsonl")
    validate_new_rows(rows, source)
    run_info = {
        "model": manifest.get("run_info", {}).get("model"),
        "path": None,
        "sha256": manifest.get("run_info", {}).get("sha256"),
    }
    summary = build_summary(source, rows, run_info)
    if summary != read_json(out / "summary.json"):
        raise ValueError("Summary does not reproduce")
    if csv_text(summary) != (out / "flips.csv").read_text():
        raise ValueError("Flip matrix does not reproduce")
    if render(summary) != (out / "report.md").read_text():
        raise ValueError("Report does not reproduce")
    print(f"Reproduced {len(rows)} new votes and all family-blind comparisons.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("votes", type=Path, nargs="?")
    parser.add_argument("out", type=Path, nargs="?")
    parser.add_argument("--verify", type=Path, metavar="OUT_DIR")
    args = parser.parse_args()
    if args.verify:
        verify(args.verify)
    elif args.votes and args.out:
        publish(args.votes, args.out)
    else:
        parser.error("Supply VOTES_JSONL OUT_DIR, or --verify OUT_DIR")


if __name__ == "__main__":
    main()
