"""Resolve the simulated user's next message from a reviewed first answer.

The case supplies the possible facts. A frozen decision names the facts that
answer a question directed to the user and quotes the first answer as evidence.
An unseen answer must be reviewed before it can be replayed.
"""

import json

from grade import success
from run import CASES, HERE, PROTOCOL, digest, schedule

SUBJECTS = HERE / "results/codex-2026-11-22/subjects.jsonl"
DISCLOSURES = HERE / "second_turn_disclosures.json"


def reviewed_reply(case, first_answer, entries):
    key = digest(first_answer.encode())
    decision = entries.get(key)
    if not decision or decision["case"] != case["id"]:
        raise ValueError("First answer requires a reviewed disclosure decision")
    if not decision["evidence"] or decision["evidence"] not in first_answer:
        raise ValueError("Reviewed excerpt does not match the frozen first answer")
    slots = decision["slots"]
    selected = [s for s in case["slots"] if s["name"] in slots]
    if [s["name"] for s in selected] != slots:
        raise ValueError("Unknown, duplicate or unordered disclosure slot")
    if not selected:
        return PROTOCOL["fallback_reply"]
    return "".join(s["value"] for s in selected) + PROTOCOL["after_reply"]


def frozen_materials():
    raw = SUBJECTS.read_bytes()
    plan = json.loads(DISCLOSURES.read_text())
    if plan["format"] != 1 or plan["subjects_sha256"] != digest(raw):
        raise ValueError("Frozen subject ledger differs from the disclosure plan")
    cases = {c["id"]: c for c in CASES}
    subjects = [json.loads(line) for line in raw.splitlines()]
    expected = {job["id"] for job in schedule()}
    if len(subjects) != len(expected) or {s["id"] for s in subjects} != expected:
        raise ValueError("Unexpected original subject population")
    replies = {}
    hashes = set()
    for row in subjects:
        case = cases[row["case"]]
        turns = success(row)["turns"]
        if len(turns) != (2 if case.get("slots") else 1) or turns[0]["user"] != case["prompt"]:
            raise ValueError("Historical conversation differs from the frozen case")
        if len(turns) == 2:
            first = turns[0]["answer"]
            replies[row["id"]] = reviewed_reply(case, first, plan["entries"])
            hashes.add(digest(first.encode()))
    if hashes != plan["entries"].keys() or len(replies) != 36:
        raise ValueError("Disclosure plan does not cover the 36 original first turns")
    return subjects, replies
