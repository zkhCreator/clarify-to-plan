"""Blind-rate the frozen public Astra transcripts once with a pinned local Qwen model.

Each independently tokenized chat contains only an opaque ID, frozen criteria and
one unaltered public dialogue. Parallel lanes have separate attention caches.
A fresh run uses the predeclared schedule, anchors, hidden rereads and sampling
settings. Valid semantic votes are never retried. A checkpoint is replaced
atomically per inference batch. The offline verifier needs only the public run
artifacts and standard library.
"""

import argparse
import hashlib
import importlib.metadata
import json
import platform
from collections import Counter
from pathlib import Path

from codex_client import RunError
from grade import (
    ANCHORS,
    GRADE_SCHEMA,
    judge_payload,
    packets,
    parse_grades,
    repeat_sample,
    success,
)
from run import CASES, HERE, PROTOCOL, digest, lock, write_json

CONFIG = HERE / "cross_family_protocol.json"
POLICY = json.loads(CONFIG.read_text())
SUBJECTS = HERE / "results/codex-2026-11-22/subjects.jsonl"


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def hash_file(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check_files(root, checksums):
    for filename, expected in checksums.items():
        if hash_file(root / filename) != expected:
            raise ValueError(f"Unexpected checksum for {filename}")


def materials():
    cases = {case["id"]: case for case in CASES}
    records = {r["id"]: r for r in map(json.loads, SUBJECTS.read_text().splitlines())}
    items = {}
    for key, r in records.items():
        turns = success(r)["turns"]
        items[key] = {
            "case": r["case"],
            "criteria": cases[r["case"]]["criteria"],
            "dialogue": [{"user": t["user"], "answer": t["answer"]} for t in turns],
        }
    for anchor in ANCHORS["answers"]:
        items["anchor-" + anchor["key"]] = {
            "case": "anchor",
            "criteria": ANCHORS["criteria"],
            "dialogue": [{"user": ANCHORS["prompt"], "answer": anchor["text"]}],
        }
    schedule = packets("Qwen3", items, sorted(items), repeat_sample(records))
    # One chat per reading, even though disjoint chats share an inference batch.
    for job in schedule:
        for entry in job["readings"]:
            entry["blind_id"] = entry["blind_id"][:8]
    ids = [r["blind_id"] for job in schedule for r in job["readings"]]
    if len(set(ids)) != 232 or len(ids) != 232 or len(records) != 216:
        raise ValueError("Frozen sample or blind identity coverage changed")
    return items, schedule


def messages(entry, items):
    instruction = (
        PROTOCOL["judge_instructions"]
        + " 直接返回符合下列 JSON Schema 的单个 JSON 对象。"
        + compact(GRADE_SCHEMA)
    )
    material = judge_payload({"readings": [entry]}, items)
    return [
        {"role": "system", "content": instruction},
        {"role": "user", "content": compact(material)},
    ]


def grade_text(text, blind_id):
    # A surrounding Markdown JSON fence has no effect on the parsed vote.
    body = text.strip()
    if body.startswith("```") and body.endswith("```"):
        lines = body.splitlines()
        if lines[0].strip().lower() in ("```json", "```"):
            body = "\n".join(lines[1:-1]).strip()
    return parse_grades(body, [blind_id])[blind_id]


def read_json(path):
    return json.loads(path.read_text())


def write_lines(path, rows):
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(compact(r) + "\n" for r in rows))
    tmp.replace(path)


def grade_batch(model, tokenizer, labels, items, seed, sampler):
    import mlx.core as mx
    from mlx_lm import batch_generate

    tokens = []
    for label in labels:
        encoded = tokenizer.apply_chat_template(
            messages(label, items),
            tokenize=True,
            add_generation_prompt=True,
            enable_thinking=POLICY["enable_thinking"],
            return_dict=False,
        )
        if not isinstance(encoded, list) or not all(type(t) is int for t in encoded):
            raise TypeError("Expected one token sequence for each blind reading")
        tokens.append(encoded)
    mx.random.seed(seed)
    answer = batch_generate(
        model,
        tokenizer,
        prompts=tokens,
        sampler=sampler,
        max_tokens=POLICY["max_new_tokens"],
        prefill_batch_size=2,
        completion_batch_size=6,
        return_token_ids=True,
    )
    observations = []
    for label, prompt, text, output in zip(
        labels, tokens, answer.texts, answer.token_ids, strict=True
    ):
        record = {
            "blind_id": label["blind_id"],
            "seed": seed,
            "prompt_sha256": digest(compact(messages(label, items)).encode()),
            "tokenized_prompt_sha256": digest(compact(prompt).encode()),
            "prompt_tokens": len(prompt),
            "output_tokens": len(output),
            "output_sha256": digest(text.encode()),
        }
        try:
            record["vote"] = grade_text(text, label["blind_id"])
            record["status"] = "valid"
        except (RunError, TypeError, KeyError, IndexError, AttributeError, ValueError):
            record["status"] = "invalid_json_or_schema"
        observations.append(record)
    return observations


def assemble(folder, schedule):
    rows, journal = [], []
    for job in schedule:
        checkpoint = read_json(folder / "batches" / (job["id"] + ".json"))
        for label in job["readings"]:
            attempts = [r for r in checkpoint if r["blind_id"] == label["blind_id"]]
            for index, r in enumerate(attempts, 1):
                journal.append({**label, "attempt": index, **r})
            valid = [r for r in attempts if r["status"] == "valid"]
            if not valid:
                raise RuntimeError(
                    "Technical failure still unresolved: " + label["blind_id"]
                )
            if len(valid) != 1:
                raise ValueError("Duplicate valid vote")
            vote = valid[0]["vote"]
            rows.append(
                {**label, "checks": vote["checks"], "evidence": vote["evidence"]}
            )
    write_lines(folder / "votes.jsonl", rows)
    write_lines(folder / "attempts.jsonl", journal)
    return rows, journal


def run(weights, folder):
    import mlx.core as mx
    from mlx_lm import load
    from mlx_lm.sample_utils import make_sampler

    check_files(HERE, POLICY["source_sha256"])
    check_files(weights, POLICY["assets_sha256"])
    items, schedule = materials()
    with lock(folder):
        run_path = folder / "run.json"
        identity = {
            "model": POLICY["model"],
            "revision": POLICY["revision"],
            "protocol_sha256": hash_file(CONFIG),
            "runner_sha256": hash_file(Path(__file__)),
            "schedule_sha256": digest(compact(schedule).encode()),
            "runtime": {
                k: importlib.metadata.version(k)
                for k in ("mlx-lm", "mlx", "mlx-metal", "transformers", "tokenizers")
            },
            "python": platform.python_version(),
            "platform": platform.system() + "/" + platform.machine(),
        }
        if run_path.exists() and read_json(run_path) != identity:
            raise ValueError(
                "Existing run uses different model, protocol, source or runtime"
            )
        write_json(run_path, identity)
        model, tokenizer = load(str(weights.resolve()), trust_remote_code=False)
        sampler = make_sampler(
            temp=POLICY["temperature"], top_p=POLICY["top_p"], top_k=POLICY["top_k"]
        )
        for index, job in enumerate(schedule, 1):
            path = folder / "batches" / (job["id"] + ".json")
            records = read_json(path) if path.is_file() else []
            for attempt in range(1, POLICY["max_attempts"] + 1):
                done = {r["blind_id"] for r in records if r["status"] == "valid"}
                counts = Counter(r["blind_id"] for r in records)
                pending = [
                    r
                    for r in job["readings"]
                    if r["blind_id"] not in done
                    and counts[r["blind_id"]] == attempt - 1
                ]
                if pending:
                    seed = POLICY["seed"] + index * 100 + attempt
                    records.extend(
                        grade_batch(model, tokenizer, pending, items, seed, sampler)
                    )
                    write_json(path, records)
            print(f"{index}/{len(schedule)} blind batches recorded", flush=True)
            mx.clear_cache()
        votes, journal = assemble(folder, schedule)
        print(f"{len(votes)} blind votes, {len(journal)} inference attempts.")
    verify(folder)


def verify(folder):
    check_files(HERE, POLICY["source_sha256"])
    meta = read_json(folder / "run.json")
    items, schedule = materials()
    if (
        meta["model"],
        meta["revision"],
        meta["protocol_sha256"],
        meta["schedule_sha256"],
    ) != (
        POLICY["model"],
        POLICY["revision"],
        hash_file(CONFIG),
        digest(compact(schedule).encode()),
    ) or meta["runner_sha256"] != hash_file(Path(__file__)):
        raise ValueError("Run identity or schedule does not match the frozen assets")
    grades = [json.loads(l) for l in (folder / "votes.jsonl").read_text().splitlines()]
    log = [json.loads(l) for l in (folder / "attempts.jsonl").read_text().splitlines()]
    labels = [r for job in schedule for r in job["readings"]]
    if len(labels) != len(grades) or {r["blind_id"] for r in labels} != {
        r["blind_id"] for r in log
    }:
        raise ValueError("Incomplete readings or unassigned attempt")
    for label, grade in zip(labels, grades):
        if any(grade[k] != v for k, v in label.items()):
            raise ValueError("Vote mapped to the wrong original transcript")
        histories = [r for r in log if r["blind_id"] == label["blind_id"]]
        if (
            not 1 <= len(histories) <= POLICY["max_attempts"]
            or histories[-1]["status"] != "valid"
        ):
            raise ValueError("Invalid terminal status")
        for attempt, r in enumerate(histories, 1):
            if (
                any(r[k] != v for k, v in label.items())
                or r["attempt"] != attempt
                or (
                    attempt < len(histories) and r["status"] != "invalid_json_or_schema"
                )
                or r["prompt_sha256"]
                != digest(compact(messages(label, items)).encode())
            ):
                raise ValueError("Attempt identity, order or visible input differs")
        last = histories[-1]["vote"]
        if (
            grade["checks"] != last["checks"]
            or grade["evidence"] != last["evidence"]
            or type(grade["checks"]) is not list
            or len(grade["checks"]) != 3
            or any(type(value) is not bool for value in grade["checks"])
            or last["pass"] is not all(grade["checks"])
            or not grade["evidence"].strip()
        ):
            raise ValueError("Vote disagrees with its valid attempt")
    print(f"Verified all {len(grades)} blind readings, inputs and recorded votes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="+")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify and len(args.paths) == 1:
        verify(args.paths[0])
    elif not args.verify and len(args.paths) == 2:
        run(*args.paths)
    else:
        parser.error("Use WEIGHTS RUN or --verify RUN")
