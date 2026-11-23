"""Blind-rate completed transcripts twice and resolve binary disagreements with a third vote.

Arm identifiers, timings, token counts and peer votes stay out of judge prompts.
Packet assignment is persisted before judging; exact duplicate reads and known
anchors are distinguishable only after the ratings have been returned.
"""

import argparse
import json
import math
import random
from pathlib import Path

from codex_client import RunError
from run import (
    CASES,
    HERE,
    PROTOCOL,
    Workers,
    attempts,
    digest,
    lock,
    schedule,
    write_json,
)

ANCHORS = json.loads((HERE / "anchors.json").read_text())
PACKET_SIZE = 6
GRADE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["judgments"],
    "properties": {
        "judgments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "checks", "evidence"],
                "properties": {
                    "id": {"type": "string"},
                    "checks": {
                        "type": "array",
                        "items": {"type": "boolean"},
                        "minItems": 3,
                        "maxItems": 3,
                    },
                    "evidence": {"type": "string"},
                },
            },
        }
    },
}


def success(record):
    return next((a for a in record["attempts"] if a["ok"]), None)


def inventory(folder):
    cases = {c["id"]: c for c in CASES}
    records, items = {}, {}
    for job in schedule():
        path = folder / "subjects" / (job["id"] + ".json")
        if not path.is_file():
            raise ValueError("Subject generation is incomplete")
        row = json.loads(path.read_text())
        attempt = success(row)
        if not attempt and len(row["attempts"]) < 2:
            raise ValueError("Subject retry is still pending")
        records[row["id"]] = row
        if attempt:
            case = cases[row["case"]]
            dialogue = [
                {"user": t["user"], "answer": t["answer"]} for t in attempt["turns"]
            ]
            items[row["id"]] = {
                "case": row["case"],
                "criteria": case["criteria"],
                "dialogue": dialogue,
            }
    for anchor in ANCHORS["answers"]:
        items["anchor-" + anchor["key"]] = {
            "case": "anchor",
            "criteria": ANCHORS["criteria"],
            "dialogue": [{"user": ANCHORS["prompt"], "answer": anchor["text"]}],
        }
    return records, items


def repeat_sample(records):
    rng = random.Random(PROTOCOL["seed"] + 91)
    repeats = []
    for family in sorted({c["family"] for c in CASES}):
        group = sorted(
            k for k, v in records.items() if v["family"] == family and success(v)
        )
        repeats.extend(rng.sample(group, min(2, len(group))))
    return repeats


def packets(rater, items, primary_ids, repeat_ids=()):
    rng = random.Random(f"{PROTOCOL['seed']}/{rater}")
    readings = [
        (k, "anchor" if k.startswith("anchor-") else "primary") for k in primary_ids
    ]
    readings += [(k, "repeat") for k in repeat_ids]
    rng.shuffle(readings)
    groups = [[] for _ in range(math.ceil(len(readings) / PACKET_SIZE))]
    for key, kind in readings:
        case = items[key]["case"]
        eligible = [
            g
            for g in groups
            if len(g) < PACKET_SIZE and all(items[r["key"]]["case"] != case for r in g)
        ]
        if not eligible:
            groups.append([])
            eligible = [groups[-1]]
        group = min(eligible, key=len)
        blind = digest(f"{PROTOCOL['seed']}|{rater}|{key}|{kind}".encode())[:14]
        group.append({"blind_id": blind, "key": key, "kind": kind})
    rng.shuffle(groups)
    result = []
    for i, group in enumerate(groups, 1):
        rng.shuffle(group)
        result.append({"id": f"{rater}-{i:03}", "rater": rater, "readings": group})
    return result


def judge_payload(job, items):
    return [
        {
            "id": r["blind_id"],
            "criteria": items[r["key"]]["criteria"],
            "dialogue": items[r["key"]]["dialogue"],
        }
        for r in job["readings"]
    ]


def parse_grades(text, expected):
    try:
        rows = json.loads(text)["judgments"]
        result = {}
        for row in rows:
            key, checks, evidence = row["id"], row["checks"], row["evidence"]
            if (
                key in result
                or len(checks) != 3
                or not all(type(v) is bool for v in checks)
            ):
                raise ValueError()
            if not isinstance(evidence, str) or not evidence.strip():
                raise ValueError()
            result[key] = {"checks": checks, "pass": all(checks), "evidence": evidence}
        if set(result) != set(expected):
            raise ValueError()
        return result
    except (KeyError, TypeError, ValueError):
        raise RunError("invalid_grade_payload") from None


def persist_plan(folder, name, data):
    target = folder / name
    if target.exists() and json.loads(target.read_text()) != data:
        raise ValueError("Existing blind plan no longer matches the frozen inputs")
    write_json(target, data)


def run_packets(folder, jobs, items, binary, workers):
    pending = []
    for job in jobs:
        f = folder / "judges" / (job["id"] + ".json")
        old = json.loads(f.read_text()) if f.exists() else None
        if not old or (not success(old) and len(old["attempts"]) < 2):
            pending.append(job)
    if not pending:
        return
    pool = Workers(binary, workers)

    def execute(job):
        def judge(client, attempt):
            th = client.start(PROTOCOL["judge_model"], PROTOCOL["judge_instructions"])
            payload = judge_payload(job, items)
            turn = client.turn(
                th,
                json.dumps(payload, ensure_ascii=False),
                PROTOCOL["judge_effort"],
                GRADE_SCHEMA,
            )
            attempt["turns"].append(turn)
            attempt["votes"] = parse_grades(
                turn["answer"], [r["blind_id"] for r in job["readings"]]
            )

        return attempts(folder / "judges" / (job["id"] + ".json"), job, pool, judge)

    pool.run(pending, execute)


def readings(folder):
    output = []
    for f in sorted((folder / "judges").glob("*.json")):
        job = json.loads(f.read_text())
        attempt = success(job)
        for entry in job["readings"]:
            vote = attempt["votes"][entry["blind_id"]] if attempt else None
            output.append(dict(entry, rater=job["rater"], packet=job["id"], vote=vote))
    return output


def grade(folder, binary, workers):
    records, items = inventory(folder)
    fixed = {
        "items_digest": digest(
            json.dumps(items, sort_keys=True, ensure_ascii=False).encode()
        ),
        "grader_digest": digest(Path(__file__).read_bytes()),
        "model": PROTOCOL["judge_model"],
        "effort": PROTOCOL["judge_effort"],
    }
    persist_plan(folder, "grade_manifest.json", fixed)
    repeat_ids = repeat_sample(records)
    jobs = packets("A", items, sorted(items), repeat_ids) + packets(
        "B", items, sorted(items), repeat_ids
    )
    random.Random(PROTOCOL["seed"] + 17).shuffle(jobs)
    persist_plan(folder, "blind-plan.json", jobs)
    run_packets(folder, jobs, items, binary, workers)
    by_key = {}
    for row in readings(folder):
        if (
            row["kind"] == "primary"
            and row["rater"] in ("A", "B")
            and row["vote"] is not None
        ):
            by_key.setdefault(row["key"], {})[row["rater"]] = row["vote"]["pass"]
    disputed = sorted(k for k, v in by_key.items() if len(v) == 2 and v["A"] != v["B"])
    finals = packets("C", items, disputed)
    persist_plan(folder, "adjudication-plan.json", finals)
    run_packets(folder, finals, items, binary, workers)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--codex", default="codex")
    parser.add_argument(
        "--workers", type=int, choices=range(1, 17), default=PROTOCOL["workers"]
    )
    args = parser.parse_args()
    with lock(args.out):
        grade(args.out, args.codex, args.workers)
