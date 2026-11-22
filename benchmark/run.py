"""Generate the three arms of the frozen Codex benchmark.

Each case/repetition gets one fresh conversation per arm, in balanced random
order. A local, predeclared user oracle reveals only facts actually asked for.
The output directory contains resumable attempts and a content-addressed manifest.
"""

import argparse
import concurrent.futures
import contextlib
import datetime
import fcntl
import hashlib
import json
import random
import re
import subprocess
import threading
import time
from pathlib import Path

from codex_client import Client, RunError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PROTOCOL = json.loads((HERE / "protocol.json").read_text())
CASES = json.loads((HERE / "cases.json").read_text())
ARM_NAMES = tuple(PROTOCOL["arms"])


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


@contextlib.contextmanager
def lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def load_packages():
    packages, hashes = {}, {}
    for arm, sha in PROTOCOL["arms"].items():
        parts, source = [], {}
        if sha:
            def blob(name):
                return subprocess.check_output(["git", "show", sha + ":" + name], cwd=ROOT)
            entry = blob("SKILL.md")
            references = sorted(set(re.findall(r"\]\((references/[\w./-]+\.md)\)", entry.decode())))
            for name in ["SKILL.md", *references]:
                data = entry if name == "SKILL.md" else blob(name)
                source[name] = digest(data)
                parts.append("<document name=\"" + name + "\">\n" + data.decode() + "\n</document>")
        packages[arm] = "\n\n".join([PROTOCOL["subject_instructions"], *parts])
        hashes[arm] = {"git_sha": sha, "files": source, "instruction_sha256": digest(packages[arm].encode())}
    return packages, hashes


QUESTION = re.compile(r"请(?:问|选|告诉|说说|说明|补充|描述)|想(?:了解|确认|问)|你(?:更|最|希望|想)|是否|能否|可否|哪[一类个些种里位]|怎么|什么|有没有|[？?]")
GOAL = re.compile(r"最(?:想|希望|先|常|需要|困扰|严重|影响|容易|麻烦)|更(?:希望|想|在意|常|关心|急|麻烦)|优先|痛点|主要.{0,12}(?:问题|目标|麻烦|困扰|卡|想|希望|乱|确认|找)|哪[一类个些种].{0,8}(?:目标|环节|场景|问题|情况|事情|困难|变化|一步|一项)|卡在|什么(?:事|问题)|最近.{0,8}(?:例子|一次)")


def oracle(case, answer):
    questions = [m.group().strip() for m in re.finditer(r"[^。！!；;\n？?]+[？?]?", answer)
                 if QUESTION.search(m.group())]
    matched = {}
    facts = []
    for slot in case["slots"]:
        for q in questions:
            if re.search(slot["pattern"], q) and (slot["name"] != "goal" or GOAL.search(q)):
                matched[slot["name"]] = q
                facts.append(slot["value"])
                break
    message = "".join(facts) + PROTOCOL["after_reply"] if facts else PROTOCOL["fallback_reply"]
    return message, matched


def schedule():
    rng = random.Random(PROTOCOL["seed"])
    blocks = [(i, c) for i in range(1, PROTOCOL["repetitions"] + 1) for c in CASES]
    rng.shuffle(blocks)
    jobs = []
    for rep, case in blocks:
        arms = list(ARM_NAMES)
        rng.shuffle(arms)
        for arm in arms:
            jobs.append({"id": f"{case['id']}-{rep}-{arm}", "case": case["id"], "family": case["family"],
                         "rep": rep, "arm": arm})
    return jobs


def manifest(binary, workers, hashes):
    paths = ["cases.json", "anchors.json", "protocol.json", "run.py", "codex_client.py"]
    return {"format": 1, "model": PROTOCOL["model"], "effort": PROTOCOL["effort"],
            "codex": subprocess.check_output([binary, "--version"], text=True).strip(), "workers": workers,
            "arms": hashes, "source_sha256": {p: digest((HERE / p).read_bytes()) for p in paths},
            "scheduled": len(schedule()), "cache_mode": "uncontrolled_codex", "api_cost_usd": None,
            "isolation": "ephemeral thread, no environments, no tools used; all discovered skills disabled and checked"}


def discover(binary):
    probe = Client(binary)
    try:
        return sorted({s["path"] for s in probe.catalog() if s["enabled"]})
    finally:
        probe.close()


class Workers:
    def __init__(self, binary, count):
        self.binary = binary
        self.paths = discover(binary)
        self.count = count
        self.local = threading.local()
        self.clients = []
        self.guard = threading.Lock()

    def client(self):
        if not getattr(self.local, "client", None):
            client = Client(self.binary, self.paths)
            if any(s["enabled"] for s in client.catalog()):
                client.close()
                raise RunError("skill_catalog_not_isolated")
            self.local.client = client
            with self.guard:
                self.clients.append(client)
        return self.local.client

    def reset(self):
        client = getattr(self.local, "client", None)
        if client:
            client.close()
            with self.guard:
                self.clients.remove(client)
            self.local.client = None

    def run(self, jobs, action):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.count) as executor:
                futures = [executor.submit(action, job) for job in jobs]
                for idx, future in enumerate(concurrent.futures.as_completed(futures), 1):
                    future.result()
                    if idx % 12 == 0 or idx == len(jobs):
                        print(f"Completed {idx}/{len(jobs)} scheduled jobs", flush=True)
        finally:
            for client in self.clients:
                client.close()


def attempts(path, job, pool, callback):
    record = json.loads(path.read_text()) if path.exists() else dict(job, attempts=[])
    while len(record["attempts"]) < 2 and not any(a.get("ok") for a in record["attempts"]):
        started = time.monotonic()
        attempt = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "turns": []}
        try:
            callback(pool.client(), attempt)
            attempt["ok"] = True
        except (RunError, OSError) as err:
            attempt.update(ok=False, error=str(err) if isinstance(err, RunError) else type(err).__name__)
            pool.reset()
        attempt["attempt_wall_ms"] = round((time.monotonic() - started) * 1000)
        record["attempts"].append(attempt)
        write_json(path, record)
    return record


def generate(folder, binary, workers):
    packages, hashes = load_packages()
    meta = manifest(binary, workers, hashes)
    fp = folder / "manifest.json"
    if fp.exists() and json.loads(fp.read_text()) != meta:
        raise SystemExit("Run manifest changed. Choose a fresh results directory.")
    write_json(fp, meta)
    by_id = {c["id"]: c for c in CASES}
    pool = Workers(binary, workers)

    def execute(job):
        case = by_id[job["case"]]
        def subject(client, attempt):
            thread = client.start(PROTOCOL["model"], packages[job["arm"]])
            def turn(prompt):
                out = client.turn(thread, prompt, PROTOCOL["effort"])
                attempt["turns"].append(dict(user=prompt, **out))
            turn(case["prompt"])
            if case.get("slots"):
                reply, matches = oracle(case, attempt["turns"][0]["answer"])
                attempt["revealed_slots"] = matches
                turn(reply)
        return attempts(folder / "subjects" / (job["id"] + ".json"), job, pool, subject)
    pool.run(schedule(), execute)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--workers", type=int, default=PROTOCOL["workers"], choices=range(1, 17))
    args = parser.parse_args()
    with lock(args.out):
        generate(args.out, args.codex, args.workers)
