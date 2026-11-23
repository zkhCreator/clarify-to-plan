"""Replay only the second turns of the 36 frozen interactive conversations.

Restore the two public role-tagged first-turn messages without generating a
first answer. Start a fresh isolated Codex thread with the original arm's
instructions. The working directory contains resumable generation attempts and
blind A/B readings, with C adjudicating only binary disagreement.
"""

import argparse
import json
from pathlib import Path

from corrected_user import DISCLOSURES, SUBJECTS, frozen_materials
from grade import packets, persist_plan, readings, run_packets, success
from run import CASES, HERE, PROTOCOL, Workers, attempts, digest, load_packages, lock, manifest, schedule


def first_items(turn):
    return [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": turn["user"]}]},
        {"type": "message", "role": "assistant", "phase": "final_answer",
         "content": [{"type": "output_text", "text": turn["answer"]}]},
    ]


def generate(folder, binary):
    subjects, replies = frozen_materials()
    original = {s["id"]: s for s in subjects}
    packages, hashes = load_packages()
    workers = PROTOCOL["workers"]
    identity = manifest(binary, workers, hashes)
    frozen = json.loads(SUBJECTS.with_name("manifest.json").read_text())
    if any(identity[k] != frozen[k] for k in ("model", "arms", "effort", "codex", "source_sha256")):
        raise ValueError("Replay needs the original runner, binary, model and skill packages")
    identity.update(scheduled=len(replies), subjects_sha256=digest(SUBJECTS.read_bytes()),
                    disclosures_sha256=digest(DISCLOSURES.read_bytes()),
                    replay_sha256=digest(Path(__file__).read_bytes()),
                    oracle_sha256=digest((HERE / "corrected_user.py").read_bytes()))
    persist_plan(folder, "manifest.json", identity)
    pending = []
    for job in schedule():
        if job["id"] in replies:
            dest = folder / "subjects" / (job["id"] + ".json")
            record = json.loads(dest.read_text()) if dest.exists() else None
            if not record or not success(record) and len(record["attempts"]) < 2:
                pending.append(job)
    if not pending:
        return
    pool = Workers(binary, workers)

    def execute(job):
        def replay(client, attempt):
            thread = client.start(PROTOCOL["model"], packages[job["arm"]])
            first = success(original[job["id"]])["turns"][0]
            client.rpc("thread/inject_items", {"threadId": thread, "items": first_items(first)})
            followup = replies[job["id"]]
            observed = client.turn(thread, followup, PROTOCOL["effort"])
            attempt["first_answer_sha256"] = digest(first["answer"].encode())
            attempt["turns"].append(dict(user=followup, **observed))

        attempts(folder / "subjects" / (job["id"] + ".json"), job, pool, replay)

    pool.run(pending, execute)


def transcripts(folder):
    subjects, replies = frozen_materials()
    cases = {c["id"]: c for c in CASES}
    items, records = {}, {}
    for row in subjects:
        key = row["id"]
        if key not in replies:
            continue
        record = json.loads((folder / "subjects" / (key + ".json")).read_text())
        attempt = success(record)
        if not attempt or len(attempt["turns"]) != 1:
            raise ValueError("No single successful second turn for " + key)
        first = success(row)["turns"][0]
        turn = attempt["turns"][0]
        if (turn["user"] != replies[key] or attempt["first_answer_sha256"] != digest(first["answer"].encode())
                or any(record[k] != row[k] for k in ("id", "case", "rep", "arm", "family"))):
            raise ValueError("Replay record does not match frozen input for " + key)
        items[key] = {"case": row["case"], "criteria": cases[row["case"]]["criteria"],
                      "dialogue": [{"user": t["user"], "answer": t["answer"]} for t in (first, turn)]}
        records[key] = record
    return items, records


def grade(folder, binary):
    items, _ = transcripts(folder)
    jobs = sum((packets(label, items, sorted(items)) for label in ("A", "B")), [])
    persist_plan(folder, "blind-plan.json", jobs)
    run_packets(folder, jobs, items, binary, PROTOCOL["workers"])
    primary = {k: {} for k in items}
    for row in readings(folder):
        if row["rater"] in ("A", "B") and row["vote"] is not None:
            primary[row["key"]][row["rater"]] = row["vote"]["pass"]
    if any(set(v) != {"A", "B"} for v in primary.values()):
        raise ValueError("Missing initial blind vote")
    disputed = sorted(k for k, v in primary.items() if v["A"] != v["B"])
    finals = packets("C", items, disputed)
    persist_plan(folder, "adjudication-plan.json", finals)
    run_packets(folder, finals, items, binary, PROTOCOL["workers"])


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("folder", type=Path)
    cli.add_argument("--codex", required=True)
    args = cli.parse_args()
    with lock(args.folder):
        generate(args.folder, args.codex)
        grade(args.folder, args.codex)
