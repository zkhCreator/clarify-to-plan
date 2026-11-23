"""Blind-rate the revised transcripts with the same pinned local Qwen judge.

Use the original twelve-subject reread roster restricted to the revised
transcripts, plus four calibration anchors. Sampling, parsing and retry rules
come from the original judge. The other 180 subjects retain their original votes.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from cross_family_judge import POLICY, assemble, check_files, compact, grade_batch
from grade import ANCHORS, packets, persist_plan, repeat_sample
from corrected_user import frozen_materials
from run import HERE, digest, lock, write_json
from second_turn import transcripts


def material(items):
    all_subjects, _ = frozen_materials()
    repeats = [k for k in repeat_sample({r["id"]: r for r in all_subjects}) if k in items]
    cards = dict(items)
    for anchor in ANCHORS["answers"]:
        cards["anchor-" + anchor["key"]] = {
            "case": "anchor", "criteria": ANCHORS["criteria"],
            "dialogue": [{"user": ANCHORS["prompt"], "answer": anchor["text"]}],
        }
    jobs = packets("Qwen3-corrected", cards, sorted(cards), repeats)
    ids = []
    for job in jobs:
        for reading in job["readings"]:
            reading["blind_id"] = reading["blind_id"][:8]
            ids.append(reading["blind_id"])
    if len(set(ids)) != len(ids):
        raise ValueError("Corrected Qwen blind ID collision")
    return cards, jobs


def run(replay_folder, weights, folder):
    import importlib.metadata
    import mlx.core as mx
    from mlx_lm import load
    from mlx_lm.sample_utils import make_sampler

    check_files(HERE, POLICY["source_sha256"])
    check_files(weights, POLICY["assets_sha256"])
    items, _ = transcripts(replay_folder)
    cards, jobs = material(items)
    identity = {"model": POLICY["model"], "revision": POLICY["revision"],
                "policy_sha256": digest((HERE / "cross_family_protocol.json").read_bytes()),
                "schedule_sha256": digest(compact(jobs).encode()), "cards_sha256": digest(compact(cards).encode()),
                "runner_sha256": digest(Path(__file__).read_bytes()),
                "runtime": {k: importlib.metadata.version(k) for k in ("mlx", "mlx-lm", "mlx-metal", "transformers", "tokenizers")}}
    with lock(folder):
        persist_plan(folder, "run.json", identity)
        persist_plan(folder, "schedule.json", jobs)
        model, tokenizer = load(str(weights.resolve()), trust_remote_code=False)
        sampler = make_sampler(temp=POLICY["temperature"], top_p=POLICY["top_p"], top_k=POLICY["top_k"])
        for i, job in enumerate(jobs, 1):
            path = folder / "batches" / (job["id"] + ".json")
            rows = json.loads(path.read_text()) if path.exists() else []
            for attempt in range(1, POLICY["max_attempts"] + 1):
                done = {r["blind_id"] for r in rows if r["status"] == "valid"}
                counts = Counter(r["blind_id"] for r in rows)
                pending = [r for r in job["readings"] if r["blind_id"] not in done and counts[r["blind_id"]] == attempt-1]
                if pending:
                    rows.extend(grade_batch(model, tokenizer, pending, cards, POLICY["seed"] + 100*i + attempt, sampler))
                    write_json(path, rows)
            mx.clear_cache()
            print(f"{i}/{len(jobs)} blind packets", flush=True)
        assemble(folder, jobs)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("replay", type=Path)
    cli.add_argument("weights", type=Path)
    cli.add_argument("output", type=Path)
    args = cli.parse_args()
    run(args.replay, args.weights, args.output)
