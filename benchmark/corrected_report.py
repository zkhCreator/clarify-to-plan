"""Publish and independently reconstruct the 36 second-turn replacements.

Keep the immutable 216-subject baseline in place. Join its 180 untouched
transcripts and readings to new Codex second turns and two blind judging
families. Rebuild both blind schedules, the disclosure inputs and the
per-question results from the public records without a model or credentials.
"""

import argparse
import csv
import io
import shutil
from pathlib import Path

from corrected_user import DISCLOSURES, SUBJECTS, frozen_materials
from cross_family_judge import CONFIG, POLICY, check_files, compact, messages
from cross_family_report import (
    ARM_LABELS,
    ARMS,
    case_views,
    load_history,
    normalize_votes,
    old_views,
    read_json,
    read_jsonl,
    validate_new_rows,
    version_pairs,
)
from cross_family_report import (
    verify as verify_baseline_pair,
)
from grade import packets, parse_grades, success
from report import frac, majority, reliability
from run import HERE, digest, write_json
from second_turn_qwen import material

BASE = SUBJECTS.parent
CROSS = HERE / "results/cross-family-2026-11-22"
QWEN = ("run.json", "votes.jsonl", "attempts.jsonl")
FILES = (
    "second-turns.jsonl",
    "astra-packets.jsonl",
    "generation.json",
    *("qwen-" + name for name in QWEN),
    "summary.json",
    "flips.csv",
    "report.md",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def file_hash(path):
    return digest(path.read_bytes())


def write_lines(path, rows):
    path.write_text("".join(compact(r) + "\n" for r in rows))


def inputs():
    paths = [
        SUBJECTS,
        BASE / "ratings.jsonl",
        BASE / "manifest.json",
        CROSS / "votes.jsonl",
        CROSS / "run.json",
        CROSS / "attempts.jsonl",
        CROSS / "manifest.json",
        DISCLOSURES,
        CONFIG,
    ]
    return {str(p.relative_to(HERE)): file_hash(p) for p in paths}


def reconstructed(folder, source, replies):
    identity = read_json(folder / "generation.json")
    old = read_json(BASE / "manifest.json")
    for key in ("model", "arms", "effort", "codex", "source_sha256"):
        require(identity[key] == old[key], "Changed original generation configuration")
    check_files(HERE, identity["source_sha256"])
    require(
        identity["scheduled"] == len(replies)
        and identity["subjects_sha256"] == file_hash(SUBJECTS)
        and identity["disclosures_sha256"] == file_hash(DISCLOSURES)
        and identity["replay_sha256"] == file_hash(HERE / "second_turn.py")
        and identity["oracle_sha256"] == file_hash(HERE / "corrected_user.py"),
        "Changed replay inputs or runner",
    )
    rows = read_jsonl(folder / "second-turns.jsonl")
    require(
        len(rows) == len(replies) and {r["id"] for r in rows} == set(replies),
        "Expected one replacement for every frozen second turn",
    )
    cards, changed = {}, []
    for row in rows:
        key = row["id"]
        original = source["subject_index"][key]
        first, second = success(original)["turns"]
        require(
            all(row[k] == original[k] for k in ("case", "rep", "arm", "family")),
            "Unexpected replacement identity",
        )
        require(
            len(row["attempts"]) == 1
            and success(row)
            and len(success(row)["turns"]) == 1,
            "Expected one successful generation of the second turn",
        )
        replay = success(row)
        answer = replay["turns"][0]
        require(
            replay["first_answer_sha256"] == digest(first["answer"].encode())
            and answer["user"] == replies[key]
            and answer["answer"].strip(),
            "Replayed input differs",
        )
        cards[key] = {
            "case": row["case"],
            "criteria": source["case_index"][row["case"]]["criteria"],
            "dialogue": [
                {"user": t["user"], "answer": t["answer"]} for t in (first, answer)
            ],
        }
        if second["user"] != answer["user"]:
            changed.append(key)
    return cards, sorted(changed)


def astra_views(folder, cards):
    raw = read_jsonl(folder / "astra-packets.jsonl")
    pending = {r["id"]: r for r in raw}
    require(len(pending) == len(raw), "Duplicate Astra judge packet")
    votes = {r: {} for r in ("A", "B", "C")}
    for label, decisions in votes.items():
        keys = (
            sorted(cards)
            if label != "C"
            else sorted(k for k in cards if votes["A"][k] != votes["B"][k])
        )
        for job in packets(label, cards, keys):
            record = pending.pop(job["id"], {})
            require(
                all(record.get(k) == v for k, v in job.items()),
                "Changed Astra blind assignment",
            )
            attempt = success(record)
            require(
                attempt and len(attempt["turns"]) == 1,
                "Missing successful Astra judge packet",
            )
            turn = attempt["turns"][0]
            parsed = parse_grades(
                turn["answer"], [e["blind_id"] for e in job["readings"]]
            )
            require(
                parsed == attempt["votes"], "Astra vote disagrees with judge output"
            )
            for entry in job["readings"]:
                decisions[entry["key"]] = parsed[entry["blind_id"]]["pass"]
    require(not pending, "Unexpected Astra judge packet")
    return votes


def qwen_rows(folder, cards):
    items, schedule = material(cards)
    meta = read_json(folder / "qwen-run.json")
    check_files(HERE, POLICY["source_sha256"])
    expected = {
        "model": POLICY["model"],
        "revision": POLICY["revision"],
        "policy_sha256": file_hash(CONFIG),
        "schedule_sha256": digest(compact(schedule).encode()),
        "cards_sha256": digest(compact(items).encode()),
        "runner_sha256": file_hash(HERE / "second_turn_qwen.py"),
    }
    require(
        all(meta.get(k) == v for k, v in expected.items()),
        "Changed Qwen schedule, rubric, inputs or judge",
    )
    raw = normalize_votes(read_jsonl(folder / "qwen-votes.jsonl"))
    votes = {r["blind_id"]: r for r in raw}
    attempts = {}
    for row in read_jsonl(folder / "qwen-attempts.jsonl"):
        attempts.setdefault(row["blind_id"], []).append(row)
    for ix, job in enumerate(schedule, 1):
        for label in job["readings"]:
            key = label["blind_id"]
            grade = votes.pop(key, {})
            history = attempts.pop(key, [])
            require(
                all(grade.get(k) == v for k, v in label.items())
                and 1 <= len(history) <= POLICY["max_attempts"],
                "Missing or substituted Qwen vote",
            )
            for n, attempt in enumerate(history, 1):
                status = "valid" if n == len(history) else "invalid_json_or_schema"
                require(
                    all(attempt.get(k) == v for k, v in label.items())
                    and attempt["attempt"] == n
                    and attempt["seed"] == POLICY["seed"] + 100 * ix + n
                    and attempt["prompt_sha256"]
                    == digest(compact(messages(label, items)).encode())
                    and attempt["status"] == status,
                    "Qwen attempt has a different input, seed or outcome",
                )
            require(
                history[-1]["vote"]
                == {k: grade[k] for k in ("pass", "checks", "evidence")},
                "Qwen reading differs from the valid attempt",
            )
    require(not votes and not attempts, "Unassigned Qwen reading")
    return raw


def text_bits(values):
    return "".join("P" if x else "F" for x in values)


def summary_and_matrix(folder):
    verify_baseline_pair(CROSS)
    _, replies = frozen_materials()
    src = load_history()
    cards, changed = reconstructed(folder, src, replies)
    fresh = astra_views(folder, cards)
    old = old_views(src)
    base_rows = normalize_votes(CROSS / "votes.jsonl")
    validate_new_rows(base_rows, src)
    corrected_q = qwen_rows(folder, cards)
    new_slots = {(r["kind"], r["key"]) for r in corrected_q}
    combined_q = [
        r for r in base_rows if (r["kind"], r["key"]) not in new_slots
    ] + corrected_q
    validate_new_rows(combined_q, src)
    before_q = {r["key"]: r["pass"] for r in base_rows if r["kind"] == "primary"}
    after_q = {r["key"]: r["pass"] for r in combined_q if r["kind"] == "primary"}
    before = old["final_pass"]
    after = before.copy()
    for key in cards:
        after[key] = fresh["C"].get(key, fresh["A"][key])
    va, vb = (
        {**{k: x["pass"] for k, x in old[j].items()}, **fresh[j]} for j in ("A", "B")
    )
    keys = sorted(src["primary_ids"])
    revised = sorted(cards)
    all_maps = (before, after, before_q, after_q)

    def compare(a, b, chosen):
        return reliability([a[k] for k in chosen], [b[k] for k in chosen])

    stats = {}
    for arm in ARMS:
        group = [k for k in keys if src["subject_index"][k]["arm"] == arm]
        active = [k for k in group if k in cards]
        stats[arm] = {"n": len(group), "revised_n": len(active)}
        for name, mapping in zip(
            ("astra_before", "astra_after", "qwen_before", "qwen_after"), all_maps
        ):
            stats[arm][name] = {
                "passes": sum(mapping[k] for k in group),
                "revised": sum(mapping[k] for k in active),
                "case_majorities": sum(
                    majority([mapping[f"{c['id']}-{r}-{arm}"] for r in (1, 2, 3)])
                    for c in src["cases"]
                ),
            }
    per_case = {}
    for judge, past, new in (("astra", before, after), ("qwen", before_q, after_q)):
        per_case[judge] = case_views(src, past, new)
    flips = {
        judge: [
            {"id": k, "old": past[k], "new": new[k], "user_changed": k in changed}
            for k in keys
            if past[k] != new[k]
        ]
        for judge, past, new in (("astra", before, after), ("qwen", before_q, after_q))
    }
    agreement = {}
    for group, chosen in (
        ("all", keys),
        ("revised", revised),
        ("unchanged_answers", [k for k in keys if k not in cards]),
    ):
        agreement[group] = {
            "astra_AB": compare(va, vb, chosen),
            "astra_qwen": compare(after, after_q, chosen),
        }
    repeats = sorted(src["repeat_keys"])
    q_repeats = {r["key"]: r["pass"] for r in combined_q if r["kind"] == "repeat"}
    anchors = {r["key"]: r["pass"] for r in corrected_q if r["kind"] == "anchor"}
    result = {
        "subjects": len(keys),
        "revised": len(revised),
        "reused": len(keys) - len(revised),
        "changed_followups": changed,
        "arms": stats,
        "flips": flips,
        "agreement": agreement,
        "revised_astra_adjudicated": sorted(fresh["C"]),
        "qwen_reread_keys": repeats,
        "qwen_revised_reread_keys": sorted(set(repeats) & set(revised)),
        "qwen_reread": compare(after_q, q_repeats, repeats),
        "qwen_anchors": compare(src["anchor_expected"], anchors, sorted(anchors)),
        "version_pairs": {name: version_pairs(rows) for name, rows in per_case.items()},
    }
    result["cases_with_version_flips"] = {
        judge: {
            row["id"]: {arm: text_bits(row["runs"][arm]["new"]) for arm in ARMS}
            for row in cases
            if len({tuple(row["runs"][arm]["new"]) for arm in ARMS}) > 1
        }
        for judge, cases in per_case.items()
    }
    s = io.StringIO(newline="")
    writer = csv.writer(s, lineterminator="\n")
    writer.writerow(
        [
            "case",
            "family",
            "judge",
            "arm",
            "before_rep123",
            "after_rep123",
            "before_majority",
            "after_majority",
            "within_arm_changed_reps",
            "astra_qwen_disagreement_reps_after",
        ]
    )
    for name, rows in per_case.items():
        for row in rows:
            for arm in ARMS:
                p, n = row["runs"][arm]["old"], row["runs"][arm]["new"]
                qkeys = [f"{row['id']}-{r}-{arm}" for r in (1, 2, 3)]
                writer.writerow(
                    [
                        row["id"],
                        row["family"],
                        name,
                        arm,
                        text_bits(p),
                        text_bits(n),
                        text_bits([majority(p)]),
                        text_bits([majority(n)]),
                        "|".join(
                            str(i) for i, (a, b) in enumerate(zip(p, n), 1) if a != b
                        ),
                        "|".join(
                            str(i)
                            for i, k in enumerate(qkeys, 1)
                            if after[k] != after_q[k]
                        ),
                    ]
                )
    return result, s.getvalue()


def kappa(data):
    return f"{data['kappa']:.3f}" if data["kappa"] is not None else "未定义"


def report(s):
    a, q = s["agreement"]["all"]["astra_AB"], s["agreement"]["all"]["astra_qwen"]
    ar, qr = (
        s["agreement"]["revised"]["astra_AB"],
        s["agreement"]["revised"]["astra_qwen"],
    )
    lines = [
        "# 修正模拟用户后的质量复测",
        "",
        (
            "共 24 题，每题每组 3 次。沿用 180 份单轮答卷、其原始评分及 36 份首轮原文，仅各生成一次新的第二轮。"
            f"其中 {len(s['changed_followups'])} 条模拟用户回复按实际提问修正，另外 {s['revised'] - len(s['changed_followups'])} 条的输入相同，也按计划重新采样。"
            "冻结的三个条件依次为无 skill、v1.0.0 和本次基线的 main。"
        ),
        "",
        "| 条件 | Astra 原 → 修订 | 修订多轮 | Astra 题目多数 | Qwen 原 → 修订 | Qwen 修订多轮 | Qwen 题目多数 |",
        "|---|---|---|---|---|---|---|",
    ]

    def rate(data, name, metric="passes"):
        denominator = {
            "passes": data["n"],
            "revised": data["revised_n"],
            "case_majorities": 24,
        }[metric]
        return frac(data[name][metric], denominator)

    for arm, v in s["arms"].items():
        lines.append(
            f"| {ARM_LABELS[arm]} | {v['astra_before']['passes']}/72 → {rate(v, 'astra_after')} | {rate(v, 'astra_after', 'revised')} | {rate(v, 'astra_after', 'case_majorities')} | {v['qwen_before']['passes']}/72 → {rate(v, 'qwen_after')} | {rate(v, 'qwen_after', 'revised')} | {rate(v, 'qwen_after', 'case_majorities')} |"
        )
    lines.extend(
        [
            "",
            "## 逐题翻转",
            "",
            (
                "P 为通过，F 为未通过。下表列出同一份样本修订前后裁决改变的全部记录。"
                "原样本与新样本有生成和裁判采样差异，不能把每一处变化都归因于模拟用户。"
            ),
            "",
            "| 裁判 | 题目、重复与条件 | 原 → 修订 | 模拟回复变化 |",
            "|---|---|---|---|",
        ]
    )
    for judge, rows in s["flips"].items():
        if not rows:
            lines.append(f"| {judge} | 全部 216 份 | 0 处 | — |")
        for row in rows:
            lines.append(
                f"| {judge} | {row['id']} | {text_bits([row['old']])} → {text_bits([row['new']])} | {'是' if row['user_changed'] else '否'} |"
            )
    lines.extend(
        [
            "",
            "按同题同重复对齐三组，以下计数依次为 F→P 和 P→F。",
            "",
            "| 条件变化 | Astra 72 对 | Astra 24 题多数 | Qwen 72 对 | Qwen 24 题多数 |",
            "|---|---|---|---|---|",
        ]
    )

    def flips(pair, judge, metric):
        count = s["version_pairs"][judge][pair]["new"][metric]
        return f"{count['gain']} / {count['loss']}"

    for pair in s["version_pairs"]["astra"]:
        lines.append(
            f"| {pair} | {flips(pair, 'astra', 'runs')} | {flips(pair, 'astra', 'case_majorities')} | {flips(pair, 'qwen', 'runs')} | {flips(pair, 'qwen', 'case_majorities')} |"
        )
    lines.extend(
        [
            "",
            "按重复 1、2、3 排列，下表覆盖修订后所有出现版本间翻转的题目。",
            "",
            "| 裁判 | 题号 | 无 skill | v1.0.0 | main |",
            "|---|---|---|---|---|",
        ]
    )
    for judge, cases in s["cases_with_version_flips"].items():
        if not cases:
            lines.append(f"| {judge} | 无翻转 | — | — | — |")
        for case, values in cases.items():
            lines.append(
                f"| {judge} | {case} | {values['none']} | {values['release']} | {values['main']} |"
            )
    lines.extend(
        [
            "",
            "完整的 24 题、三个版本、两名模型裁判、每题三次重复及跨裁判分歧见 [flips.csv](flips.csv)。",
            "",
            "## 盲评一致性",
            "",
            f"Astra 两个独立盲读在全部答卷上一致 {frac(a['agreement'], a['n'])}，Cohen κ = {kappa(a)}。新第二轮一致 {frac(ar['agreement'], ar['n'])}，κ = {kappa(ar)}。分歧交第三次盲读裁决。",
            "",
            f"Astra 最终裁决与 Qwen3-8B 在全部答卷上一致 {frac(q['agreement'], q['n'])}，κ = {kappa(q)}。新第二轮一致 {frac(qr['agreement'], qr['n'])}，κ = {kappa(qr)}。",
            "",
            (
                f"Qwen 按原名单重读 {s['qwen_reread']['agreement']}/12，其中 {len(s['qwen_revised_reread_keys'])} 份使用新答卷，其余沿用原重读。四个锚题重新盲评，判对 {s['qwen_anchors']['agreement']}/4。"
                "同家族 Astra 同时生成和评分，跨家族结果及锚题限制了对版本优劣的推断。本报告没有纳入人工判定。"
            ),
            "",
            (
                "版本、裁判和原始材料可以用 corrected_report.py 离线核验。完整模型可见首轮作为历史消息恢复，原线程的隐含状态无法复原。"
                "单轮答卷和首轮的生成均沿用原件。受控 API 回放单独计量，Codex 质量复测的时长和 token 不参与该项计费。"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def publish(replay, qwen, out):
    out.mkdir(exist_ok=True, parents=True)
    for name, directory in (
        ("second-turns.jsonl", "subjects"),
        ("astra-packets.jsonl", "judges"),
    ):
        write_lines(
            out / name,
            [read_json(p) for p in sorted((replay / directory).glob("*.json"))],
        )
    shutil.copyfile(replay / "manifest.json", out / "generation.json")
    for name in QWEN:
        shutil.copyfile(qwen / name, out / ("qwen-" + name))
    summary, matrix = summary_and_matrix(out)
    write_json(out / "summary.json", summary)
    (out / "flips.csv").write_text(matrix)
    (out / "report.md").write_text(report(summary))
    write_json(
        out / "manifest.json",
        {
            "format": 1,
            "sources": inputs(),
            "artifacts": {n: file_hash(out / n) for n in FILES},
            "reporter_sha256": file_hash(Path(__file__)),
        },
    )
    verify(out)


def verify(folder):
    manifest = read_json(folder / "manifest.json")
    require(
        manifest["sources"] == inputs()
        and manifest["artifacts"] == {n: file_hash(folder / n) for n in FILES}
        and manifest["reporter_sha256"] == file_hash(Path(__file__)),
        "Report fingerprint differs",
    )
    summary, matrix = summary_and_matrix(folder)
    require(
        summary == read_json(folder / "summary.json")
        and matrix == (folder / "flips.csv").read_text()
        and report(summary) == (folder / "report.md").read_text(),
        "Quality report does not reproduce",
    )
    print(
        "Verified 180 retained answers, 36 replacements, both blind schedules and all reported outcomes."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="*")
    parser.add_argument("--verify", type=Path)
    options = parser.parse_args()
    if options.verify and not options.paths:
        verify(options.verify)
    elif not options.verify and len(options.paths) == 3:
        publish(*options.paths)
    else:
        parser.error("Use REPLAY QWEN OUT or --verify OUT")
