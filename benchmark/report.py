"""Publish auditable, offline summaries from preserved candidate transcripts and blind ratings.

Computes paired flips, repeat variability and inter-rater agreement without model
calls. Cache-policy effects and monetary charges stay null in a Codex-only run.
"""

import argparse
import csv
import io
import itertools
import json
import math
import statistics
from collections import Counter
from pathlib import Path

from grade import ANCHORS, inventory, packets, readings, repeat_sample, success
from run import (
    ARM_NAMES,
    CASES,
    HERE,
    PROTOCOL,
    digest,
    load_packages,
    schedule,
    write_json,
)

PAIRS = list(itertools.combinations(ARM_NAMES, 2))
LABELS = {"none": "无 skill", "release": "v1.0.0", "main": "main"}
FAMILIES = {
    "clarify": "主动澄清（两轮）",
    "handoff": "执行交接",
    "equivalence": "等价做法",
    "evidence": "证据范围",
    "trial": "试行门槛",
    "continuity": "承接与实际交付",
}


def rate(count, n):
    return count / n if n else None


def frac(count, n):
    return f"{count}/{n}（{100 * count / n:.1f}%）" if n else "待核实"


def paired(left, right):
    if len(left) != len(right):
        raise ValueError("Unaligned samples")
    counts = Counter(
        (a, b) for a, b in zip(left, right) if a is not None and b is not None
    )
    return {
        "n": sum(counts.values()),
        "gain": counts[False, True],
        "loss": counts[True, False],
        "both_pass": counts[True, True],
        "both_fail": counts[False, False],
    }


def reliability(a, b):
    p = paired(a, b)
    n = p["n"]
    agreement = p["both_pass"] + p["both_fail"]
    if not n:
        return dict(p, agreement=0, kappa=None)
    left = (p["both_pass"] + p["loss"]) / n
    right = (p["both_pass"] + p["gain"]) / n
    expected = left * right + (1 - left) * (1 - right)
    return dict(
        p,
        agreement=agreement,
        kappa=(agreement / n - expected) / (1 - expected) if expected < 1 else None,
    )


def majority(votes):
    if votes.count(True) >= 2:
        return True
    if votes.count(False) >= 2:
        return False
    return None


def quantiles(values):
    data = sorted(values)
    if not data:
        return {"n": 0, "median_ms": None, "p95_ms": None}
    return {
        "n": len(data),
        "median_ms": statistics.median(data),
        "p95_ms": data[math.ceil(0.95 * len(data)) - 1],
    }


def jsonl(path, rows):
    path.write_text(
        "".join(
            json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "\n"
            for x in rows
        )
    )


def summarize(records, votes):
    primary = {}
    for r in votes:
        if r["kind"] == "primary" and r["vote"] is not None:
            primary.setdefault(r["key"], {})[r["rater"]] = r["vote"]

    def decision(key):
        p = primary.get(key, {})
        if not all(j in p for j in ("A", "B")):
            return None
        return majority([v["pass"] for v in p.values()])

    final = {k: decision(k) for k in records}
    shared = [p for p in primary.values() if "A" in p and "B" in p]
    quality = reliability(
        [p["A"]["pass"] for p in shared], [p["B"]["pass"] for p in shared]
    )
    quality["individual_checks_agree"] = sum(
        x == y for p in shared for x, y in zip(p["A"]["checks"], p["B"]["checks"])
    )
    quality["individual_checks_n"] = 3 * len(shared)
    quality["third_reviews_completed"] = sum("C" in p for p in shared)
    quality["anchors"] = {}
    quality["re_reads"] = {}
    gold = {"anchor-" + a["key"]: a["pass"] for a in ANCHORS["answers"]}
    for judge in ("A", "B"):
        anchors = [
            r
            for r in votes
            if r["rater"] == judge and r["kind"] == "anchor" and r["vote"] is not None
        ]
        repeats = [
            r
            for r in votes
            if r["rater"] == judge
            and r["kind"] == "repeat"
            and r["vote"] is not None
            and judge in primary.get(r["key"], {})
        ]
        quality["anchors"][judge] = [
            sum(gold[r["key"]] == r["vote"]["pass"] for r in anchors),
            len(anchors),
        ]
        quality["re_reads"][judge] = [
            sum(r["vote"]["pass"] == primary[r["key"]][judge]["pass"] for r in repeats),
            len(repeats),
        ]
    nrep = PROTOCOL["repetitions"]
    cases = []
    for c in CASES:
        runs = {
            a: [final[f"{c['id']}-{r}-{a}"] for r in range(1, nrep + 1)]
            for a in ARM_NAMES
        }
        cases.append(
            {
                "id": c["id"],
                "family": c["family"],
                "runs": runs,
                "majority": {a: majority(v) for a, v in runs.items()},
                "pairs": {a + "→" + b: paired(runs[a], runs[b]) for a, b in PAIRS},
            }
        )
    arms = {}
    for arm in ARM_NAMES:
        rows = [r for r in records.values() if r["arm"] == arm]
        completed = [success(r) for r in rows if success(r)]
        all_attempts = [a for row in rows for a in row["attempts"]]
        valid = [final[r["id"]] for r in rows if final[r["id"]] is not None]
        task_votes = [
            c["majority"][arm] for c in cases if c["majority"][arm] is not None
        ]
        within = [
            x != y
            for c in cases
            for x, y in itertools.combinations(c["runs"][arm], 2)
            if x is not None and y is not None
        ]
        counters = Counter()
        turns = [t for a in all_attempts for t in a["turns"]]
        for t in turns:
            counters.update(t["usage"] or {})
        cache_reads = [
            t["usage"]["cachedInputTokens"]
            for t in turns
            if t["usage"] and "cachedInputTokens" in t["usage"]
        ]
        arms[arm] = {
            "scheduled": len(rows),
            "generated": len(completed),
            "attempts": len(all_attempts),
            "initial_technical_failures": sum(not r["attempts"][0]["ok"] for r in rows),
            "graded": len(valid),
            "passed": sum(valid),
            "pass_rate": rate(sum(valid), len(valid)),
            "case_majorities": [sum(task_votes), len(task_votes)],
            "confirmed_fraction_scheduled": rate(sum(valid), len(rows)),
            "within_repeat_flips": [sum(within), len(within)],
            "dialogue_time": quantiles(
                [sum(t["elapsed_ms"] for t in a["turns"]) for a in completed]
            ),
            "attempt_wall_time": quantiles(
                [sum(a["attempt_wall_ms"] for a in r["attempts"]) for r in rows]
            ),
            "first_visible_time": quantiles(
                [
                    t["first_visible_ms"]
                    for t in turns
                    if t["first_visible_ms"] is not None
                ]
            ),
            "reported_usage": dict(counters),
            "turns_with_positive_cache_reads": [
                sum(x > 0 for x in cache_reads),
                len(cache_reads),
            ],
            "turns_with_usage": sum(t["usage"] is not None for t in turns),
            "observed_turns": len(turns),
            "api_cold_elapsed_ms": None,
            "api_hot_elapsed_ms": None,
            "api_cold_cost_usd": None,
            "api_hot_cost_usd": None,
            "api_cold_minus_hot_elapsed_ms": None,
            "api_cold_minus_hot_cost_usd": None,
        }
    comparisons = {}
    for a, b in PAIRS:
        lhs = [v for c in cases for v in c["runs"][a]]
        rhs = [v for c in cases for v in c["runs"][b]]
        comparisons[a + "→" + b] = {
            "runs": paired(lhs, rhs),
            "case_majorities": paired(
                [c["majority"][a] for c in cases], [c["majority"][b] for c in cases]
            ),
        }

    def group_passes(group):
        by_arm = {}
        for arm in ARM_NAMES:
            scores = [v for c in group for v in c["runs"][arm] if v is not None]
            by_arm[arm] = [sum(scores), len(scores)]
        return by_arm

    families = {
        family: group_passes([c for c in cases if c["family"] == family])
        for family in FAMILIES
    }
    interactive_ids = {c["id"] for c in CASES if c.get("slots")}
    return {
        "arms": arms,
        "rater_agreement": quality,
        "comparisons": comparisons,
        "families": families,
        "single_turn": group_passes(
            [c for c in cases if c["id"] not in interactive_ids]
        ),
        "cases": cases,
    }, final


def mask(values):
    return "".join("1" if v is True else "0" if v is False else "?" for v in values)


def seconds(ms):
    return "待核实" if ms is None else f"{ms / 1000:.2f}"


def render(summary, manifest):
    s = []
    s.extend(
        [
            "# Codex 基准实测",
            "",
            (
                f"目标模型为 {manifest['model']}，生成和盲评使用 {PROTOCOL['effort']} 推理档位。"
                f"运行端为 {manifest['codex']}，并发数为 {manifest['workers']}。24 道公开回归题，6 个题族，3 个版本条件，每题独立重复 3 次。"
            ),
            "",
            "## 任务通过率和实测等待",
            "",
            "| 版本 | 答卷通过/已判 | 三次多数通过的题目 | 已生成/计划 | 首试技术失败 | 对话等待中位数 / P95（秒） | 同版本重复翻转 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for arm, r in summary["arms"].items():
        t = r["dialogue_time"]
        s.append(
            f"| {LABELS[arm]} | {frac(r['passed'], r['graded'])} | {frac(*r['case_majorities'])} | {r['generated']}/{r['scheduled']} | "
            f"{r['initial_technical_failures']} | {seconds(t['median_ms'])} / {seconds(t['p95_ms'])} | "
            f"{frac(*r['within_repeat_flips'])} |"
        )
    s.extend(
        [
            "",
            "Codex 返回的原生用量如下。每轮按该轮最后一次用量事件计数。缓存读取列是已报告输入的一部分。",
            "",
            "| 版本 | 返回正数缓存读取的轮数/有读数的轮数 | 输入 token | 其中缓存读取 token | 输出 token |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for arm, row in summary["arms"].items():
        usage = row["reported_usage"]
        s.append(
            f"| {LABELS[arm]} | {frac(*row['turns_with_positive_cache_reads'])} | "
            f"{usage.get('inputTokens', '待测')} | {usage.get('cachedInputTokens', '待测')} | "
            f"{usage.get('outputTokens', '待测')} |"
        )
    s.extend(
        [
            "",
            (
                "对话等待是各轮从发起请求至完成的实测时间之和。P95 使用最近秩。"
                "同版本翻转比较每题三个重复之间的全部三对组合。本轮无法指定 Codex 的缓存分组或读取 API 账单。"
                "正式 API 冷写、热读耗时和两组费用均待测。"
            ),
            "",
            "## 版本间翻转",
            "",
            "比较方向为左侧到右侧。增加表示未通过变通过，减少表示通过变未通过。",
            "",
            "| 对照 | 同次配对数 | 增加 / 减少 | 翻转率 | 按每题三次多数判定的增加 / 减少 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for (a, b), v in zip(PAIRS, summary["comparisons"].values()):
        p, m = v["runs"], v["case_majorities"]
        s.append(
            f"| {LABELS[a]} → {LABELS[b]} | {p['n']} | {p['gain']} / {p['loss']} | "
            f"{frac(p['gain'] + p['loss'], p['n'])} | {m['gain']} / {m['loss']} |"
        )
    s.extend(
        [
            "",
            "## 各题族的通过数",
            "",
            "| 题族 | 无 skill | v1.0.0 | main |",
            "|---|---:|---:|---:|",
        ]
    )
    groups = [(FAMILIES[k], v) for k, v in summary["families"].items()]
    groups.append(("只看 20 道单轮题", summary["single_turn"]))
    for label, results in groups:
        s.append(
            "| " + " | ".join([label, *[frac(*results[a]) for a in ARM_NAMES]]) + " |"
        )
    k = summary["rater_agreement"]
    kval = "不可估计" if k["kappa"] is None else f"{k['kappa']:.3f}"
    s.extend(
        [
            "",
            "## 盲评一致性",
            "",
            (
                f"独立盲评 A、B 在仲裁前同判 {frac(k['agreement'], k['n'])}，Cohen’s κ 为 {kval}。"
                f"逐条判据一致 {frac(k['individual_checks_agree'], k['individual_checks_n'])}。"
                f"第三次盲评完成 {k['third_reviews_completed']} 份分歧答卷。"
            ),
            "",
            (
                f"校准样例与预设判定吻合度为 A {frac(*k['anchors']['A'])}，B {frac(*k['anchors']['B'])}。"
                f"同一答卷用新盲号重复阅读时，A 同判 {frac(*k['re_reads']['A'])}，B 同判 {frac(*k['re_reads']['B'])}。"
            ),
            "",
            "## 逐题判定",
            "",
            (
                "三个字符依次对应重复 1、2、3，1 表示通过，0 表示未通过，? 表示缺少判定。"
                "后三列是三次配对中的增加数/减少数。"
            ),
            "",
            "| 题目 | 题族 | 无 skill | v1.0.0 | main | 无→v1 | 无→main | v1→main |",
            "|---|---|---|---|---|---:|---:|---:|",
        ]
    )
    for c in summary["cases"]:
        bits = [mask(c["runs"][a]) for a in ARM_NAMES]
        flips = [f"{p['gain']}/{p['loss']}" for p in c["pairs"].values()]
        s.append("| " + " | ".join([c["id"], c["family"], *bits, *flips]) + " |")
    if "review.md" in manifest.get("artifacts_sha256", {}):
        s.extend(["", "本轮附有针对原始对话和判定差异的[补充复核](review.md)。"])
    s.extend(
        [
            "",
            "## 验证范围",
            "",
            (
                "这是一轮强制加载指定版本的 Codex 文字任务对照。评分来自同一模型的独立会话。"
                "运行端给出模型别名，后台权重快照不可见。"
                "24 题来自六个题族。本轮没有预设发布或等效门槛，结果用于定位回归和重读分歧。"
                "多轮题的用户回复来自预登记词表，原始记录保留了触发回复的问句。"
            ),
            "",
            (
                "[运行清单](manifest.json) · [机读汇总](summary.json) · [逐题矩阵](flips.csv) · "
                "[原始答卷与用量](subjects.jsonl) · [盲评分项与依据](ratings.jsonl) · [裁判运行记录](judge-attempts.jsonl)"
            ),
            "",
        ]
    )
    return "\n".join(s)


def publish(folder, out):
    records, _ = inventory(folder)
    votes = readings(folder)
    plans = [
        json.loads((folder / n).read_text())
        for n in ("blind-plan.json", "adjudication-plan.json")
    ]
    expected = [
        (job["rater"], r["blind_id"])
        for plan in plans
        for job in plan
        for r in job["readings"]
    ]
    if sorted(expected) != sorted((r["rater"], r["blind_id"]) for r in votes):
        raise ValueError("Incomplete blind evaluation")
    summary, final = summarize(records, votes)
    out.mkdir(parents=True, exist_ok=True)
    public = []
    for key, r in sorted(records.items()):
        public.append(dict(r, passed=final[key]))
    jsonl(out / "subjects.jsonl", public)
    jsonl(out / "ratings.jsonl", votes)
    audits = []
    for f in sorted((folder / "judges").glob("*.json")):
        row = json.loads(f.read_text())
        clean = [
            {
                "ok": a["ok"],
                "error": a.get("error"),
                "utc": a["utc"],
                "attempt_wall_ms": a["attempt_wall_ms"],
                "turns": [
                    {k: v for k, v in t.items() if k != "answer"} for t in a["turns"]
                ],
            }
            for a in row["attempts"]
        ]
        audits.append({"packet": row["id"], "rater": row["rater"], "attempts": clean})
    jsonl(out / "judge-attempts.jsonl", audits)
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["grading"] = json.loads((folder / "grade_manifest.json").read_text())
    manifest["protocol_sha256"] = digest((HERE / "protocol.json").read_bytes())
    manifest["reporter_sha256"] = digest(Path(__file__).read_bytes())
    manifest["first_utc"] = min(a["utc"] for r in public for a in r["attempts"])
    manifest["artifacts_sha256"] = {
        f: digest((out / f).read_bytes())
        for f in ("subjects.jsonl", "ratings.jsonl", "judge-attempts.jsonl")
    }
    for name in ("review.md", "first-turn.json"):
        if (out / name).is_file():
            manifest["artifacts_sha256"][name] = digest((out / name).read_bytes())
    write_json(out / "manifest.json", manifest)
    write_json(out / "summary.json", summary)
    (out / "report.md").write_text(render(summary, manifest))
    (out / "flips.csv").write_text(flip_matrix(summary))


def flip_matrix(summary):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        [
            "case",
            "family",
            *ARM_NAMES,
            *[f"{a}_to_{b}_{key}" for a, b in PAIRS for key in ("gain", "loss", "n")],
        ]
    )
    for c in summary["cases"]:
        writer.writerow(
            [
                c["id"],
                c["family"],
                *[mask(c["runs"][a]) for a in ARM_NAMES],
                *[
                    c["pairs"][a + "→" + b][key]
                    for a, b in PAIRS
                    for key in ("gain", "loss", "n")
                ],
            ]
        )
    return stream.getvalue()


def verify(out):
    """Recalculate final verdicts, assignment and statistics using only public artifacts."""
    manifest = json.loads((out / "manifest.json").read_text())
    for path, expected in manifest["artifacts_sha256"].items():
        if digest((out / path).read_bytes()) != expected:
            raise ValueError("Raw artifact checksum differs: " + path)
    for path, expected in manifest["source_sha256"].items():
        if digest((HERE / path).read_bytes()) != expected:
            raise ValueError("Frozen source differs: " + path)
    if digest((HERE / "grade.py").read_bytes()) != manifest["grading"]["grader_digest"]:
        raise ValueError("Grader source differs")
    if digest(Path(__file__).read_bytes()) != manifest["reporter_sha256"]:
        raise ValueError("Reporter source differs")
    if manifest["source_sha256"]["protocol.json"] != manifest["protocol_sha256"]:
        raise ValueError("Protocol checksum differs")
    if load_packages()[1] != manifest["arms"]:
        raise ValueError("Skill package differs from the recorded hashes")

    def lines(filename):
        return [json.loads(x) for x in (out / filename).read_text().splitlines()]

    all_rows = lines("subjects.jsonl")
    records = {r["id"]: r for r in all_rows}
    expected_ids = {j["id"] for j in schedule()}
    if len(records) != len(all_rows) or records.keys() != expected_ids:
        raise ValueError("Missing or duplicate scheduled sample")
    for job in schedule():
        row = records[job["id"]]
        if any(row[k] != value for k, value in job.items()):
            raise ValueError("Sample assignment differs")
    votes = lines("ratings.jsonl")
    for r in votes:
        v = r["vote"]
        if v is not None and (len(v["checks"]) != 3 or v["pass"] != all(v["checks"])):
            raise ValueError("Invalid per-criterion vote")
    lookup = {}
    case_index = {c["id"]: c for c in CASES}
    for key, row in records.items():
        attempt = success(row)
        if attempt:
            lookup[key] = {
                "case": row["case"],
                "criteria": case_index[row["case"]]["criteria"],
                "dialogue": [
                    {k: turn[k] for k in ("user", "answer")}
                    for turn in attempt["turns"]
                ],
            }
    for anchor in ANCHORS["answers"]:
        lookup["anchor-" + anchor["key"]] = {
            "case": "anchor",
            "criteria": ANCHORS["criteria"],
            "dialogue": [{"user": ANCHORS["prompt"], "answer": anchor["text"]}],
        }
    if (
        digest(json.dumps(lookup, sort_keys=True, ensure_ascii=False).encode())
        != manifest["grading"]["items_digest"]
    ):
        raise ValueError(
            "The transcripts and criteria differ from the frozen blind grading inputs"
        )
    expected_jobs = []
    for j in ("A", "B"):
        expected_jobs.extend(packets(j, lookup, sorted(lookup), repeat_sample(records)))
    binary = {}
    for r in votes:
        if (
            r["rater"] in ("A", "B")
            and r["kind"] == "primary"
            and r["vote"] is not None
        ):
            binary.setdefault(r["key"], {})[r["rater"]] = r["vote"]["pass"]
    disputed = sorted(k for k, p in binary.items() if len(p) == 2 and p["A"] != p["B"])
    expected_jobs.extend(packets("C", lookup, disputed))
    allocation = sorted(
        (j["id"], j["rater"], v["kind"], v["key"], v["blind_id"])
        for j in expected_jobs
        for v in j["readings"]
    )
    actual = sorted(
        (v["packet"], v["rater"], v["kind"], v["key"], v["blind_id"]) for v in votes
    )
    if allocation != actual:
        raise ValueError(
            "Blinded primary, repeat, anchor or adjudication assignment differs"
        )
    summary, final = summarize(records, votes)
    if summary != json.loads((out / "summary.json").read_text()):
        raise ValueError("Summary does not reproduce")
    if any(final[k] != r["passed"] for k, r in records.items()):
        raise ValueError("Published sample verdict differs")
    if render(summary, manifest) != (out / "report.md").read_text():
        raise ValueError("Published report differs")
    if flip_matrix(summary) != (out / "flips.csv").read_text():
        raise ValueError("Published per-case flip matrix differs")
    if "first-turn.json" in manifest["artifacts_sha256"]:
        from first_turn import verify as verify_first_turn

        verify_first_turn(out)
    print(
        f"Reproduced {len(records)} samples, {len(votes)} blinded readings and every reported verdict."
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work", type=Path, nargs="?")
    p.add_argument("destination", type=Path, nargs="?")
    p.add_argument("--verify", type=Path, metavar="PUBLISHED_DIRECTORY")
    args = p.parse_args()
    if args.verify:
        verify(args.verify)
    elif args.work and args.destination:
        publish(args.work, args.destination)
    else:
        p.error("Supply WORK DESTINATION, or --verify PUBLISHED_DIRECTORY")
