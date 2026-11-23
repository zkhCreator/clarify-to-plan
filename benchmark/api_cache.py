"""Replay frozen public conversations against the official Responses API.

Run in a user-authenticated local process. The key is accepted through a pipe
or environment, used in memory by cache_transport, and never included in the
ledger. Each arm keeps its natural prefix. A separate shared-context case makes
all three arms eligible for a controlled long-prefix comparison.
"""

import argparse
import concurrent.futures
import json
import os
import random
import sys
import threading
import time
import uuid
from pathlib import Path

from cache_transport import request, utc
from corrected_user import DISCLOSURES, SUBJECTS, frozen_materials
from grade import persist_plan
from run import ARM_NAMES, HERE, PROTOCOL, digest, load_packages, lock, write_json

MODEL = PROTOCOL["model"]
WORKERS = 4
BASE = "你是一名中文助手。请按本次指令作答。"
PRICE_SNAPSHOT = json.loads((HERE / "api_cache_rates.json").read_text())
RATES = PRICE_SNAPSHOT["rates"]
PRICE_URL = PRICE_SNAPSHOT["source"]
SEED = 20261122
RULES = {
    "model": MODEL,
    "reasoning": {"effort": "medium"},
    "service_tier": "default",
    "store": False,
    "max_output_tokens": 1024,
}


def sha(value):
    return digest(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )


def prefixes():
    packages, _ = load_packages()
    catalog = "\n".join(
        f"资料卡 {i:03}，归档柜 {(i - 1) // 8 + 1:02}，层号 {(i - 1) % 8 + 1}。"
        for i in range(1, 129)
    )
    return {
        "natural": {a: BASE + "\n\n" + packages[a] for a in ARM_NAMES},
        "shared_catalog": {
            a: BASE + "\n\n" + packages[a] + "\n\n展馆现行纸本目录\n" + catalog
            for a in ARM_NAMES
        },
    }


def units():
    result = []
    subjects, replies = frozen_materials()
    for r in subjects:
        attempt = next(a for a in r["attempts"] if a["ok"])
        history = []
        for index, turn in enumerate(attempt["turns"], 1):
            user = replies[r["id"]] if index == 2 else turn["user"]
            history.append({"role": "user", "content": user})
            result.append(
                {
                    "id": f"{r['id']}-t{index}",
                    "sample": r["id"],
                    "arm": r["arm"],
                    "scope": "natural",
                    "turn": index,
                    "expected_turns": len(attempt["turns"]),
                    "history": history.copy(),
                }
            )
            history.append({"role": "assistant", "content": turn["answer"]})
    for arm in ARM_NAMES:
        for rep in range(1, 4):
            sample = f"catalog-{rep}-{arm}"
            result.append(
                {
                    "id": sample + "-t1",
                    "sample": sample,
                    "arm": arm,
                    "scope": "shared_catalog",
                    "turn": 1,
                    "expected_turns": 1,
                    "history": [
                        {
                            "role": "user",
                            "content": "只给交接文字，写清资料卡 009 和 113 各在哪一柜、哪一层。",
                        }
                    ],
                }
            )
    random.Random(SEED + 51).shuffle(result)
    return result


def assignment(unit, prefix, calibration):
    eligible = calibration.get(unit["scope"], {}).get(unit["arm"], {}).get("eligible")
    policies = ["uncached", "cold", "warm"] if eligible is True else ["uncached"]
    random.Random(f"{SEED}:{unit['id']}").shuffle(policies)
    return {
        **{k: v for k, v in unit.items() if k != "history"},
        "policies": policies,
        "prefix_sha256": sha(prefix),
        "visible_sha256": sha(input_blocks(prefix, unit["history"], False)),
    }


def input_blocks(prefix, history, with_breakpoint):
    text = {"type": "input_text", "text": prefix}
    if with_breakpoint:
        text["prompt_cache_breakpoint"] = {"mode": "explicit"}
    return [{"role": "developer", "content": [text]}, *history]


def payload(prefix, history, phase, key):
    prewarm = phase == "prewarm"
    opts = {"mode": "explicit", "ttl": "30m"}
    if prewarm:
        opts["prewarm"] = True
    return dict(
        RULES,
        input=input_blocks(prefix, [] if prewarm else history, phase != "uncached"),
        stream=not prewarm,
        prompt_cache_options=opts,
        prompt_cache_key=key,
    )


def fatal(r):
    return r.get("http") in (400, 401, 402, 403, 404) or r.get("error") in (
        "insufficient_quota",
        "billing_not_active",
        "invalid_api_key",
        "model_not_found",
        "account_deactivated",
        "organization_deactivated",
        "permission_denied",
        "access_denied",
        "unsupported_parameter",
        "invalid_value",
        "invalid_request_error",
        "unsupported_country_region_territory",
    )


def transient(r):
    return not fatal(r) and (
        r.get("http") in (408, 409, 429)
        or (r.get("http") or 0) >= 500
        or r.get("error")
        in ("transport", "stream_ended", "server_error", "rate_limit_exceeded")
    )


class Collector:
    def __init__(self, folder, token, nonce):
        self.folder, self.token, self.nonce = folder, token, nonce
        self.stop = threading.Event()
        self.reason = None
        self.prefix = prefixes()

    def key(self, unit, policy):
        return "ctp-" + sha([self.nonce, unit, policy])[:40]

    def attempts(self, name, body, counting=False, stop_on_fatal=True):
        path = self.folder / "attempts" / (name + ".json")
        rows = json.loads(path.read_text()) if path.exists() else []
        if rows and rows[-1]["status"] == "in_flight":
            rows[-1].update(status="interrupted", error="unknown_remote_outcome")
            write_json(path, rows)
        while (
            len(rows) < 2
            and (not rows or transient(rows[-1]))
            and not self.stop.is_set()
        ):
            delay_start = time.monotonic()
            if rows:
                time.sleep(1)
            delay = round(1000 * (time.monotonic() - delay_start))
            rows.append(
                {
                    "status": "in_flight",
                    "request_sha256": sha(body),
                    "utc": utc(),
                    "http": None,
                    "usage": {},
                    "elapsed_ms": None,
                    "ttft_ms": None,
                    "retry_delay_ms": delay,
                }
            )
            write_json(path, rows)
            r = request(self.token, body, counting)
            rows[-1] = dict(r, retry_delay_ms=delay, request_sha256=sha(body))
            write_json(path, rows)
        if rows and stop_on_fatal and fatal(rows[-1]):
            self.reason = {
                "operation": name,
                "http": rows[-1].get("http"),
                "code": rows[-1].get("error"),
            }
            self.stop.set()
        return rows

    def calibrate(self):
        status = {}
        for scope, arms in self.prefix.items():
            status[scope] = {}
            for arm, prefix in arms.items():
                tag = "cal-" + scope + "-" + arm
                count = self.attempts(
                    tag + "-count",
                    {"model": MODEL, "input": input_blocks(prefix, [], False)},
                    counting=True,
                    stop_on_fatal=False,
                )
                tokens = count[-1].get("input_tokens") if count else None
                observed = None
                if tokens is not None and tokens >= 1024:
                    rows = self.attempts(
                        tag + "-prewarm",
                        payload(prefix, [], "prewarm", self.key(tag, "probe")),
                    )
                    if len(rows) == 1 and rows[0].get("status") == "completed":
                        u = rows[-1]["usage"]
                        if (
                            u.get("read") == 0
                            and u.get("write", 0) >= 1024
                            and u.get("output") == 0
                        ):
                            observed = u["write"]
                eligible = (
                    True
                    if observed and observed >= 1024
                    else False
                    if tokens is not None and tokens < 1024
                    else None
                )
                status[scope][arm] = {
                    "prefix_sha256": sha(prefix),
                    "count": tokens,
                    "boundary_tokens": observed,
                    "eligible": eligible,
                }
                if self.stop.is_set():
                    return status
        return status

    def collect(self, unit, calibration):
        if self.stop.is_set():
            return
        prefix = self.prefix[unit["scope"]][unit["arm"]]
        planned = assignment(unit, prefix, calibration)
        path = self.folder / "units" / (unit["id"] + ".json")
        record = (
            json.loads(path.read_text())
            if path.exists()
            else {**planned, "stages": {}}
        )
        if any(record.get(k) != v for k, v in planned.items()):
            raise ValueError("Stored unit no longer matches its allocation")
        for policy in planned["policies"]:
            if self.stop.is_set():
                break
            key = self.key(unit["id"], policy)
            stages = ("prewarm", "warm") if policy == "warm" else (policy,)
            for stage in stages:
                if self.stop.is_set():
                    break
                if stage in record["stages"]:
                    continue
                body = payload(prefix, unit["history"], stage, key)
                rows = self.attempts(unit["id"] + "-" + stage, body)
                if rows:
                    record["stages"][stage] = rows
                    write_json(path, record)


def run(folder, key):
    stamp = folder / "manifest.json"
    replay = units()
    prefix = prefixes()

    def plan(calibration):
        return [assignment(unit, prefix[unit["scope"]][unit["arm"]], calibration) for unit in replay]

    population = plan({})
    fixed = {
        "format": 2,
        "model": MODEL,
        "from": "benchmark-2026-11-22 / corrected second-turn disclosures",
        "population_sha256": sha(population),
        "disclosures_sha256": digest(DISCLOSURES.read_bytes()),
        "subjects_sha256": digest(SUBJECTS.read_bytes()),
        "source": {
            f: digest((HERE / f).read_bytes())
            for f in ("api_cache.py", "cache_transport.py", "corrected_user.py", "second_turn_disclosures.json",
                      "cases.json", "protocol.json", "run.py")
        },
        "rules": RULES,
        "workers": WORKERS,
        "seed": SEED,
        "ttl": "30m",
        "rates_usd_per_million": RATES,
        "rate_snapshot_sha256": digest((HERE / "api_cache_rates.json").read_bytes()),
        "price_source": PRICE_URL,
        "tariff_date": PRICE_SNAPSHOT["observed_on"],
        "credential_origin": "user-started local process",
        "endpoint": "https://api.openai.com/v1/responses",
        "region": "default API origin; physical processing region not exposed",
        "warm_reuses_per_prewarm": 1,
        "snapshot": "provider-reported model per response",
    }
    if stamp.exists():
        manifest = json.loads(stamp.read_text())
        if any(manifest.get(k) != v for k, v in fixed.items()):
            raise SystemExit("Frozen inputs changed. Use a new run directory.")
    else:
        manifest = dict(fixed, nonce=uuid.uuid4().hex, start_utc=utc())
        write_json(stamp, manifest)
    persist_plan(folder, "population.json", population)
    collector = Collector(folder, key, manifest["nonce"])
    body = payload(
        BASE,
        [{"role": "user", "content": "请只回复好。"}],
        "uncached",
        collector.key("access", "uncached"),
    )
    body["max_output_tokens"] = 64
    probe = collector.attempts("access", body)
    if probe and probe[-1]["status"] in ("completed", "incomplete"):
        calibration = collector.calibrate()
        write_json(folder / "calibration.json", calibration)
        if not collector.stop.is_set():
            persist_plan(folder, "allocation.json", plan(calibration))
            with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
                for n, _ in enumerate(
                    pool.map(lambda u: collector.collect(u, calibration), replay), 1
                ):
                    if n % 24 == 0 and not collector.stop.is_set():
                        print(f"{n} replay units processed", flush=True)
    else:
        last = probe[-1] if probe else {}
        collector.reason = {
            "operation": "access",
            "http": last.get("http"),
            "code": last.get("error"),
        }
    state = {
        "state": "blocked" if collector.reason else "finished",
        "stop_reason": collector.reason,
        "end_utc": utc(),
    }
    write_json(folder / "status.json", state)
    print(json.dumps(state, ensure_ascii=False), flush=True)
    return 2 if collector.reason else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--key-stdin",
        action="store_true",
        help="Accept the credential only on a private pipe",
    )
    args = parser.parse_args()
    if args.key_stdin and sys.stdin.isatty():
        parser.error("--key-stdin needs a pipe from your local credential provider")
    token = (
        sys.stdin.readline().strip()
        if args.key_stdin
        else os.environ.get("OPENAI_API_KEY", "").strip()
    )
    if not token or not token.isascii() or any(c.isspace() for c in token):
        parser.error("A local API credential is required")
    with lock(args.output):
        sys.exit(run(args.output, token))
