"""Build and reconcile a local, blind human-review packet.

This module never sends network requests and never puts the source key, arm,
case id, family, historical vote, selection reason, or private seed in the
public packet.  The public HTML contains only escaped prompt/dialogue/criteria
and a random blind id.  The private plan is the only place that maps a blind
id back to a frozen answer.

The default command builds a packet from the frozen subjects, ratings, cases,
and a JSONL file of any new votes.  The ``reconcile`` subcommand consumes a
public export and writes mapped results inside PRIVATE_DIR; an incomplete
three-way answer is never counted as a pass.
"""

# ruff: noqa: TRY004 -- malformed fields are reported as invalid packet data.
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import random
import secrets
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from cross_family_report import validate_historical_ratings
from grade import repeat_sample, success

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SUBJECTS_PATH = HERE / "results/codex-2026-11-22/subjects.jsonl"
RATINGS_PATH = HERE / "results/codex-2026-11-22/ratings.jsonl"
CASES_PATH = HERE / "cases.json"
ANCHORS_PATH = HERE / "anchors.json"
PACK_FORMAT = 1
PRIVATE_PLAN = "plan.json"
PRIVATE_RECONCILED = "reconciled.json"
ARM_ORDER = ("none", "release", "main")
EXPECTED_PRIMARY_COUNT = 216


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"JSONL row is not an object at {path}:{number}")
        rows.append(value)
    return rows


def validate_checks(value: Any, *, allow_missing: bool = False) -> list[bool | None]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("checks must contain exactly three values")
    result: list[bool | None] = []
    for item in value:
        if item is None and allow_missing:
            result.append(None)
        elif type(item) is bool:
            result.append(item)
        else:
            raise ValueError("checks must contain booleans or allowed nulls")
    return result


def load_cases(path: Path = CASES_PATH) -> dict[str, dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("cases.json must be an array")
    result: dict[str, dict[str, Any]] = {}
    for case in raw:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError("case lacks an id")
        if case["id"] in result:
            raise ValueError(f"duplicate case id: {case['id']}")
        if (
            not isinstance(case.get("prompt"), str)
            or not isinstance(case.get("criteria"), list)
            or len(case["criteria"]) != 3
        ):
            raise ValueError(f"case {case['id']} lacks three criteria")
        result[case["id"]] = case
    return result


def load_subjects(
    subjects_path: Path = SUBJECTS_PATH,
    cases_path: Path = CASES_PATH,
) -> dict[str, dict[str, Any]]:
    cases = load_cases(cases_path)
    result: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(subjects_path):
        key = row.get("id")
        if not isinstance(key, str) or key in result:
            raise ValueError(f"invalid or duplicate subject id: {key!r}")
        case_id = row.get("case")
        if case_id not in cases:
            raise ValueError(f"subject {key} refers to unknown case")
        attempts = row.get("attempts")
        if not isinstance(attempts, list):
            raise ValueError(f"subject {key} has no attempts")
        successful = next((a for a in attempts if a.get("ok") is True), None)
        if successful is None:
            raise ValueError(f"subject {key} has no successful attempt")
        turns = successful.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ValueError(f"subject {key} has no turns")
        dialogue = []
        for turn in turns:
            if not isinstance(turn, dict) or not isinstance(turn.get("user"), str):
                raise ValueError(f"subject {key} has an invalid user turn")
            if not isinstance(turn.get("answer"), str):
                raise ValueError(f"subject {key} has an invalid answer turn")
            dialogue.append({"user": turn["user"], "answer": turn["answer"]})
        result[key] = {
            "key": key,
            "case": case_id,
            "family": row.get("family"),
            "arm": row.get("arm"),
            "rep": row.get("rep"),
            "prompt": cases[case_id]["prompt"],
            "criteria": [str(x) for x in cases[case_id]["criteria"]],
            "dialogue": dialogue,
        }
    return result


def load_anchor_keys(path: Path = ANCHORS_PATH) -> set[str]:
    """Return the frozen anchor ids used by the new family run."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    answers = raw.get("answers") if isinstance(raw, dict) else None
    if not isinstance(answers, list):
        raise ValueError("anchors.json lacks an answers array")
    keys = set()
    for answer in answers:
        if not isinstance(answer, dict) or not isinstance(answer.get("key"), str):
            raise ValueError("anchors.json contains an invalid answer")
        key = "anchor-" + answer["key"]
        if key in keys:
            raise ValueError("anchors.json contains duplicate anchor keys")
        keys.add(key)
    return keys


def _strict_checks(value: Any, where: str) -> list[bool]:
    if (
        not isinstance(value, list)
        or len(value) != 3
        or not all(type(item) is bool for item in value)
    ):
        raise ValueError(f"{where} must contain exactly three boolean checks")
    return list(value)


def load_historical_votes(
    path: Path,
    subject_keys: set[str],
) -> list[dict[str, Any]]:
    """Load the frozen Astra ratings without silently dropping malformed rows.

    A/B are the two complete historical primary readings.  C is sparse and is
    retained when present.  Any malformed A/B/C row or unknown primary key is
    an input error rather than an invitation to build a partial packet.
    """

    result: list[dict[str, Any]] = []
    seen_primary: dict[str, set[str]] = {rater: set() for rater in ("A", "B", "C")}
    for number, row in enumerate(read_jsonl(path), 1):
        kind = row.get("kind")
        rater = row.get("rater")
        if kind not in ("primary", "repeat", "anchor"):
            raise ValueError(f"historical row {number} has an invalid kind")
        if not isinstance(rater, str) or rater not in ("A", "B", "C"):
            raise ValueError(f"historical row {number} has an invalid rater")
        key = row.get("key")
        if not isinstance(key, str):
            raise ValueError(f"historical row {number} has no key")
        vote = row.get("vote")
        if not isinstance(vote, dict):
            raise ValueError(f"historical row {number} has no vote object")
        checks = _strict_checks(vote.get("checks"), f"historical row {number}")
        if kind == "primary":
            if key not in subject_keys:
                raise ValueError(
                    f"historical primary row {number} refers to an unknown subject"
                )
            if key in seen_primary[rater]:
                raise ValueError(
                    f"historical {rater} primary has a duplicate for {key}"
                )
            seen_primary[rater].add(key)
        elif kind == "repeat" and key not in subject_keys:
            raise ValueError(
                f"historical repeat row {number} refers to an unknown subject"
            )
        result.append(
            {
                "key": key,
                "kind": kind,
                "checks": checks,
                "evidence": str(vote.get("evidence", "")),
                "rater": rater,
                "source": "historical",
                "blind_id": row.get("blind_id"),
            }
        )
    for rater in ("A", "B"):
        if seen_primary[rater] != subject_keys:
            missing = sorted(subject_keys - seen_primary[rater])
            extra = sorted(seen_primary[rater] - subject_keys)
            raise ValueError(
                f"historical {rater} primary must cover every subject exactly once "
                f"(missing={missing[:3]}, extra={extra[:3]})"
            )
    if len(subject_keys) != EXPECTED_PRIMARY_COUNT:
        raise ValueError(
            f"frozen subjects must contain exactly {EXPECTED_PRIMARY_COUNT} primary keys"
        )
    return result


def load_new_votes_strict(
    path: Path,
    subject_keys: set[str],
    anchor_keys: set[str],
    expected_repeats: set[str],
) -> list[dict[str, Any]]:
    """Validate the complete new-family vote file before any selection occurs."""

    result: list[dict[str, Any]] = []
    seen_blind: set[str] = set()
    primary: dict[str, int] = {}
    repeats: set[str] = set()
    anchors: set[str] = set()
    for number, row in enumerate(read_jsonl(path), 1):
        if not isinstance(row, dict):
            raise ValueError(f"new vote row {number} is not an object")
        kind = row.get("kind")
        key = row.get("key")
        if (
            kind not in ("primary", "repeat", "anchor")
            or not isinstance(key, str)
            or not key
        ):
            raise ValueError(f"new vote row {number} has an invalid kind/key")
        checks = _strict_checks(row.get("checks"), f"new vote row {number}")
        evidence = row.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError(f"new vote row {number} has no evidence text")
        blind = row.get("blind_id")
        if not isinstance(blind, str) or not blind:
            raise ValueError(f"new vote row {number} has no blind_id")
        if blind in seen_blind:
            raise ValueError(f"new votes contain duplicate blind_id {blind}")
        seen_blind.add(blind)
        if kind == "primary":
            if key not in subject_keys:
                raise ValueError(
                    f"new primary row {number} refers to an unknown subject"
                )
            primary[key] = primary.get(key, 0) + 1
        elif kind == "repeat":
            if key not in subject_keys:
                raise ValueError(
                    f"new repeat row {number} refers to an unknown subject"
                )
            if key in repeats:
                raise ValueError(f"new repeat has a duplicate subject {key}")
            repeats.add(key)
        else:
            if key not in anchor_keys:
                raise ValueError(f"new anchor row {number} refers to an unknown anchor")
            if key in anchors:
                raise ValueError(f"new anchor has a duplicate key {key}")
            anchors.add(key)
        supplied_rater = row.get("rater")
        if supplied_rater is not None and (
            not isinstance(supplied_rater, str) or not supplied_rater
        ):
            raise ValueError(f"new vote row {number} has an invalid rater")
        result.append(
            {
                "key": key,
                "kind": kind,
                "checks": checks,
                "evidence": evidence,
                # The new file is one main judge plus its rereads.  Keep it
                # in its own namespace even if an exporter includes a label
                # such as "A"; otherwise a bad label can collide with old
                # Astra readings and change the disagreement set.
                "rater": "new_family",
                "source": "new_family",
                "blind_id": blind,
            }
        )
    expected = subject_keys
    if set(primary) != expected or len(primary) != EXPECTED_PRIMARY_COUNT:
        missing = sorted(expected - set(primary))
        extra = sorted(set(primary) - expected)
        raise ValueError(
            f"new votes must contain exactly one primary per {EXPECTED_PRIMARY_COUNT} "
            f"subject (missing={missing[:3]}, extra={extra[:3]})"
        )
    duplicates = sorted(key for key, count in primary.items() if count != 1)
    if duplicates:
        raise ValueError(f"new primary has duplicate subjects: {duplicates[:3]}")
    if len(repeats) != 12 or repeats != expected_repeats:
        raise ValueError("new votes must match the twelve preregistered repeat subjects")
    if len(anchors) != len(anchor_keys) or anchors != anchor_keys:
        raise ValueError("new votes must contain exactly the frozen anchor reads")
    return result


def vote_indexes(
    historical: Iterable[dict[str, Any]],
    new_votes: Iterable[dict[str, Any]],
    subjects: Mapping[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    primary: dict[str, list[dict[str, Any]]] = {}
    repeats: dict[str, list[dict[str, Any]]] = {}
    for vote in list(historical) + list(new_votes):
        key = vote["key"]
        if key not in subjects:
            continue  # anchors and unrelated future keys are not answer cards
        target = primary if vote["kind"] == "primary" else repeats
        if vote["kind"] in ("primary", "repeat"):
            target.setdefault(key, []).append(vote)
    return primary, repeats


def _check_tuple(vote: Mapping[str, Any]) -> tuple[bool, bool, bool]:
    checks = vote["checks"]
    return (bool(checks[0]), bool(checks[1]), bool(checks[2]))


def selection_reasons(
    key: str,
    primary: Mapping[str, list[dict[str, Any]]],
    repeats: Mapping[str, list[dict[str, Any]]],
) -> list[str]:
    reasons: list[str] = []
    main_votes = primary.get(key, [])
    if len({_check_tuple(v) for v in main_votes}) > 1:
        reasons.append("primary_check_disagreement")

    repeat_votes = repeats.get(key, [])
    for repeat in repeat_votes:
        matching = [v for v in main_votes if v["rater"] == repeat["rater"]]
        if matching and all(_check_tuple(v) != _check_tuple(repeat) for v in matching):
            reasons.append("repeat_vs_primary_disagreement")
            break
    if len({_check_tuple(v) for v in repeat_votes}) > 1:
        reasons.append("repeat_check_disagreement")
    return reasons


def same_judgment(
    key: str, primary: Mapping[str, list[dict[str, Any]]], selected: Sequence[str]
) -> bool:
    if key in selected:
        return False
    votes = primary.get(key, [])
    return len(votes) >= 2 and len({_check_tuple(v) for v in votes}) == 1


def pass_from_votes(votes: Sequence[Mapping[str, Any]]) -> bool | None:
    if not votes:
        return None
    checks = {_check_tuple(v) for v in votes}
    if len(checks) != 1:
        return None
    return all(next(iter(checks)))


def sample_same_judged(
    candidates: Sequence[dict[str, Any]], seed_hex: str, target_per_arm: int = 8
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rng = random.Random(int(seed_hex, 16))
    by_arm: dict[str, list[dict[str, Any]]] = {arm: [] for arm in ARM_ORDER}
    for candidate in candidates:
        by_arm.setdefault(candidate["arm"], []).append(candidate)
    for pool in by_arm.values():
        rng.shuffle(pool)

    chosen: list[dict[str, Any]] = []
    chosen_keys = set()
    details: dict[str, Any] = {
        "targets": {arm: target_per_arm for arm in ARM_ORDER},
        "arms": {},
    }

    def take(pool: list[dict[str, Any]], count: int, reason: str) -> None:
        for candidate in pool:
            if len(chosen) >= target_per_arm * len(ARM_ORDER) or count <= 0:
                break
            if candidate["key"] in chosen_keys:
                continue
            chosen.append({**candidate, "sample_reason": reason})
            chosen_keys.add(candidate["key"])
            count -= 1

    for arm in ARM_ORDER:
        pool = by_arm.get(arm, [])
        failures = [c for c in pool if not c["same_pass"]]
        passes = [c for c in pool if c["same_pass"]]
        before = len(chosen)
        take(failures, min(2, target_per_arm), "same_judged_failure_priority")
        take(
            passes,
            target_per_arm - (len(chosen) - before),
            "same_judged_pass_fill",
        )
        take(
            [c for c in failures if c["key"] not in chosen_keys],
            target_per_arm - (len(chosen) - before),
            "same_arm_stratum_backfill",
        )
        details["arms"][arm] = {
            "target": target_per_arm,
            "selected_initial": len(chosen) - before,
        }

    remaining = [c for c in candidates if c["key"] not in chosen_keys]
    rng.shuffle(remaining)
    while len(chosen) < target_per_arm * len(ARM_ORDER) and remaining:
        counts = {arm: sum(x["arm"] == arm for x in chosen) for arm in ARM_ORDER}
        min_count = min(counts.values())
        eligible_arms = [arm for arm in ARM_ORDER if counts[arm] == min_count]
        selected = next(
            (c for c in remaining if c["arm"] in eligible_arms), remaining[0]
        )
        remaining.remove(selected)
        chosen.append({**selected, "sample_reason": "cross_group_backfill"})
        chosen_keys.add(selected["key"])
    return chosen, details


def private_source_digests(votes_path: Path) -> dict[str, str]:
    return {
        "subjects": sha256_file(SUBJECTS_PATH),
        "ratings": sha256_file(RATINGS_PATH),
        "cases": sha256_file(CASES_PATH),
        "anchors": sha256_file(ANCHORS_PATH),
        "new_votes": sha256_file(votes_path),
    }


def assert_disjoint(pack_dir: Path, private_dir: Path) -> tuple[Path, Path]:
    pack = pack_dir.expanduser().resolve()
    private = private_dir.expanduser().resolve()
    try:
        common = Path(os.path.commonpath([str(pack), str(private)]))
    except ValueError as exc:
        raise ValueError("PACK_DIR and PRIVATE_DIR are on incompatible paths") from exc
    if common == pack or common == private:
        raise ValueError("PACK_DIR and PRIVATE_DIR must not overlap")
    if str(private).startswith(str(REPO_ROOT) + os.sep) or private == REPO_ROOT:
        raise ValueError("PRIVATE_DIR must be outside the repository")
    return pack, private


def ensure_empty_pack(pack: Path) -> None:
    if pack.exists() and any(pack.iterdir()):
        raise ValueError(
            "PACK_DIR must be empty to avoid leaving private or stale files"
        )
    pack.mkdir(parents=True, exist_ok=True)


def private_plan_for_new(
    seed_hex: str,
    selected: Sequence[dict[str, Any]],
    source: Mapping[str, str],
    primary: Mapping[str, list[dict[str, Any]]],
    repeats: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    entries = []
    for item in selected:
        entries.append(
            {
                "blind_id": item["blind_id"],
                "key": item["key"],
                "arm": item["arm"],
                "case": item["case"],
                "family": item.get("family"),
                "rep": item.get("rep"),
                "selection_reason": item["selection_reason"],
                "same_pass": item.get("same_pass"),
                "primary_checks": [
                    list(_check_tuple(v)) for v in primary.get(item["key"], [])
                ],
                "repeat_checks": [
                    list(_check_tuple(v)) for v in repeats.get(item["key"], [])
                ],
            }
        )
    return {
        "format": PACK_FORMAT,
        "seed_hex": seed_hex,
        "source_sha256": dict(source),
        "selected": entries,
    }


def blind_id(seed_hex: str, key: str, ordinal: int) -> str:
    secret = bytes.fromhex(seed_hex)
    message = f"human-review-v1\0{ordinal}\0{key}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()[:24]


def _public_card(subject: Mapping[str, Any], blind: str) -> dict[str, Any]:
    return {
        "blind_id": blind,
        "prompt": subject["prompt"],
        "dialogue": subject["dialogue"],
        "criteria": subject["criteria"],
    }


def _json_for_script(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>离线盲评</title>
<style>
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.55;
  margin: 0;
  background: #f5f6f8;
  color: #202124;
}
main {
  max-width: 980px;
  margin: 0 auto;
  padding: 24px;
}
header,
.card,
.toolbar {
  background: #fff;
  border: 1px solid #d9dce1;
  border-radius: 10px;
  padding: 16px;
  margin-bottom: 14px;
  box-shadow: 0 1px 2px #0000000d;
}
h1 {
  font-size: 22px;
  margin: 0 0 6px;
}
.muted {
  color: #5f6368;
  font-size: 13px;
}
button {
  border: 1px solid #9aa0a6;
  border-radius: 6px;
  background: #fff;
  padding: 8px 12px;
  cursor: pointer;
  margin-right: 6px;
}
button.primary {
  background: #1a73e8;
  color: #fff;
  border-color: #1a73e8;
}
button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.toolbar .spacer {
  flex: 1;
}
.label {
  font-weight: 600;
  margin-top: 14px;
}
.prompt {
  white-space: pre-wrap;
  background: #f8f9fa;
  border-radius: 6px;
  padding: 10px;
}
.turn {
  border-left: 3px solid #dadce0;
  padding: 8px 12px;
  margin: 10px 0;
}
.turn.user {
  border-color: #1a73e8;
}
.turn.assistant {
  border-color: #34a853;
}
.turn-title {
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 4px;
}
.criterion {
  border-top: 1px solid #e5e7eb;
  padding: 12px 0;
}
.criterion-text {
  margin-bottom: 8px;
}
.choice {
  margin-right: 18px;
}
.choice input {
  margin-right: 5px;
}
.note {
  width: 100%;
  min-height: 90px;
  box-sizing: border-box;
  padding: 8px;
  border: 1px solid #c7cbd1;
  border-radius: 6px;
  font: inherit;
}
#status {
  font-size: 13px;
  color: #5f6368;
}
.error {
  color: #b3261e;
}
.ok {
  color: #137333;
}
@media (max-width: 600px) {
  main {
    padding: 10px;
  }
  .toolbar {
    display: block;
  }
  .toolbar button {
    margin: 4px 4px 4px 0;
  }
  .toolbar .spacer {
    display: none;
  }
}
</style>
</head>
<body>
<main>
<header><h1>离线盲评</h1><div id="progress" class="muted"></div><div id="status" aria-live="polite"></div></header>
<div class="toolbar">
<button id="prev">上一张</button><button id="next" class="primary">下一张</button>
<span class="spacer"></span><button id="save">保存进度</button><button id="export">导出 JSON</button><button id="import">导入 JSON</button><input id="import-file" type="file" accept="application/json" hidden>
</div>
<section id="card" class="card"></section>
<p class="muted">每项请选择“是”或“否”，并可留下备注。三项填齐后该份答卷计为已完成。</p>
</main>
<script>
const PACK_ID = __PACK_ID__;
const CARDS = __CARDS__;
const STORAGE_KEY = "human-review-progress-v1-" + PACK_ID;
let state = { index: 0, answers: {} };

function emptyAnswer() {
  return { checks: [null, null, null], evidence: "" };
}
function answerFor(id) {
  if (!state.answers[id]) state.answers[id] = emptyAnswer();
  return state.answers[id];
}
function setStatus(text, good) {
  const node = document.getElementById("status");
  node.textContent = text;
  node.className = good ? "ok" : "";
}
function safeSave() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    setStatus("进度已保存在本浏览器。", true);
  } catch (e) {
    setStatus("浏览器存储不可用，请用导出 JSON 保存进度。", false);
  }
}
function isAnswer(value) {
  return (
    value &&
    typeof value === "object" &&
    Array.isArray(value.checks) &&
    value.checks.length === 3 &&
    value.checks.every((x) => x === true || x === false || x === null) &&
    typeof value.evidence === "string"
  );
}
function loadSaved() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (
        parsed &&
        typeof parsed === "object" &&
        parsed.answers &&
        typeof parsed.answers === "object"
      ) {
        const answers = {};
        for (const c of CARDS) {
          if (isAnswer(parsed.answers[c.blind_id]))
            answers[c.blind_id] = {
              checks: parsed.answers[c.blind_id].checks.slice(),
              evidence: parsed.answers[c.blind_id].evidence,
            };
        }
        const index =
          Number.isInteger(parsed.index) &&
          parsed.index >= 0 &&
          parsed.index < CARDS.length
            ? parsed.index
            : 0;
        state = { index, answers };
      }
    }
  } catch (e) {
    setStatus("浏览器存储不可用，请用导入/导出 JSON。", false);
  }
}
function node(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}
function completedCount() {
  return CARDS.reduce((n, c) => {
    const a = answerFor(c.blind_id);
    return n + (a.checks.every((x) => x === true || x === false) ? 1 : 0);
  }, 0);
}
function updateProgress() {
  const c = CARDS[state.index];
  document.getElementById("progress").textContent =
    "第 " +
    (state.index + 1) +
    " / " +
    CARDS.length +
    " 张　已完成 " +
    completedCount() +
    " / " +
    CARDS.length +
    "　盲号：" +
    c.blind_id;
}
function render() {
  const c = CARDS[state.index];
  const a = answerFor(c.blind_id);
  const root = document.getElementById("card");
  root.replaceChildren();
  updateProgress();
  const heading = node("h2", "题面");
  root.appendChild(heading);
  root.appendChild(node("div", c.prompt, "prompt"));
  root.appendChild(node("h2", "完整对话"));
  c.dialogue.forEach((turn, i) => {
    const box = node("div", undefined, "turn");
    box.appendChild(node("div", "第 " + (i + 1) + " 轮 · 用户", "turn-title"));
    box.appendChild(node("div", turn.user, "prompt"));
    box.appendChild(node("div", "第 " + (i + 1) + " 轮 · 答复", "turn-title"));
    box.appendChild(node("div", turn.answer, "prompt"));
    root.appendChild(box);
  });
  root.appendChild(node("h2", "三项判据"));
  c.criteria.forEach((criterion, i) => {
    const section = node("div", undefined, "criterion");
    section.appendChild(node("div", "判据 " + (i + 1), "label"));
    section.appendChild(node("div", criterion, "criterion-text"));
    [true, false].forEach((value) => {
      const label = node("label", undefined, "choice");
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "check-" + c.blind_id + "-" + i;
      input.checked = a.checks[i] === value;
      input.addEventListener("change", () => {
        a.checks[i] = value;
        safeSave();
        render();
      });
      label.appendChild(input);
      label.appendChild(document.createTextNode(value ? "是" : "否"));
      section.appendChild(label);
    });
    root.appendChild(section);
  });
  root.appendChild(node("div", "备注", "label"));
  const note = document.createElement("textarea");
  note.className = "note";
  note.placeholder = "可选";
  note.value = a.evidence;
  note.addEventListener("input", () => {
    a.evidence = note.value;
    safeSave();
  });
  root.appendChild(note);
  document.getElementById("prev").disabled = state.index === 0;
  document.getElementById("next").disabled = state.index === CARDS.length - 1;
}
function exportData() {
  return {
    format: 1,
    pack_id: PACK_ID,
    answers: CARDS.map((c) => {
      const a = answerFor(c.blind_id);
      return { blind_id: c.blind_id, checks: a.checks, evidence: a.evidence };
    }),
  };
}
function download() {
  const blob = new Blob([JSON.stringify(exportData(), null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "human-review-answers.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  setStatus("已导出 JSON。", true);
}
function importFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const data = JSON.parse(reader.result);
      if (
        !data ||
        data.format !== 1 ||
        data.pack_id !== PACK_ID ||
        !Array.isArray(data.answers)
      )
        throw new Error("格式或盲包不匹配");
      if (data.answers.length !== CARDS.length)
        throw new Error("必须包含全部盲号");
      const known = new Set(CARDS.map((c) => c.blind_id));
      const seen = new Set();
      const next = {};
      for (const row of data.answers) {
        if (
          !row ||
          typeof row !== "object" ||
          typeof row.blind_id !== "string" ||
          !known.has(row.blind_id)
        )
          throw new Error("包含未知盲号");
        if (seen.has(row.blind_id)) throw new Error("包含重复盲号");
        seen.add(row.blind_id);
        if (
          !Array.isArray(row.checks) ||
          row.checks.length !== 3 ||
          !row.checks.every((x) => x === true || x === false || x === null)
        )
          throw new Error("判据字段格式错误");
        if (typeof row.evidence !== "string")
          throw new Error("备注字段格式错误");
        next[row.blind_id] = {
          checks: row.checks.slice(),
          evidence: row.evidence,
        };
      }
      if (seen.size !== known.size) throw new Error("缺少盲号");
      state = { index: 0, answers: next };
      safeSave();
      render();
      setStatus("已导入 JSON。", true);
    } catch (e) {
      setStatus("导入失败：" + e.message, false);
    }
  };
  reader.readAsText(file);
}
document.getElementById("prev").addEventListener("click", () => {
  if (state.index > 0) {
    state.index--;
    safeSave();
    render();
  }
});
document.getElementById("next").addEventListener("click", () => {
  if (state.index < CARDS.length - 1) {
    state.index++;
    safeSave();
    render();
  }
});
document.getElementById("save").addEventListener("click", safeSave);
document.getElementById("export").addEventListener("click", download);
document
  .getElementById("import")
  .addEventListener("click", () =>
    document.getElementById("import-file").click(),
  );
document.getElementById("import-file").addEventListener("change", (e) => {
  if (e.target.files && e.target.files[0]) importFile(e.target.files[0]);
});
loadSaved();
render();
</script>
</body>
</html>
"""


def render_html(cards: Sequence[Mapping[str, Any]], pack_id: str) -> str:
    data = _json_for_script(list(cards))
    return HTML_TEMPLATE.replace("__PACK_ID__", _json_for_script(pack_id)).replace(
        "__CARDS__", data
    )


def pack_id_for_cards(cards: Sequence[Mapping[str, Any]]) -> str:
    return sha256_bytes(canonical_json(list(cards)))[:16]


def build_public_files(
    pack: Path, cards: Sequence[Mapping[str, Any]], pack_id: str | None = None
) -> None:
    pack.mkdir(parents=True, exist_ok=True)
    pack_id = pack_id or pack_id_for_cards(cards)
    pack.joinpath("index.html").write_text(
        render_html(cards, pack_id), encoding="utf-8"
    )
    pack.joinpath("README.txt").write_text(
        "用浏览器打开 index.html。每张填写三项是或否，备注可选。可随时导出 JSON 保存进度，下次导入接着评。评完后将导出的 JSON 交回。\n",
        encoding="utf-8",
    )


def review_inputs(votes_path, quality_folder=None):
    subjects = load_subjects(SUBJECTS_PATH, CASES_PATH)
    subject_keys = set(subjects)
    original = {r["id"]: r for r in read_jsonl(SUBJECTS_PATH)}
    roster = set(repeat_sample(original))
    anchors = load_anchor_keys(ANCHORS_PATH)
    validate_historical_ratings(read_jsonl(RATINGS_PATH), subject_keys, roster, anchors)
    historical = load_historical_votes(RATINGS_PATH, subject_keys)
    votes = load_new_votes_strict(votes_path, subject_keys, anchors, roster)
    source = private_source_digests(votes_path)
    if quality_folder is None:
        return subjects, historical, votes, source

    from corrected_report import CROSS, verify

    verify(quality_folder)
    if sha256_file(votes_path) != sha256_file(CROSS / "votes.jsonl"):
        raise ValueError("Correction needs the original Qwen ledger used by the verified report")
    revised = read_jsonl(quality_folder / "second-turns.jsonl")
    revised_keys = {row["id"] for row in revised}
    for row in revised:
        turn = success(row)["turns"][0]
        subjects[row["id"]]["dialogue"][1] = {k: turn[k] for k in ("user", "answer")}
    # Primary and repeat readings from an older transcript cannot score its replacement.
    historical = [r for r in historical if r["key"] not in revised_keys]
    for packet in read_jsonl(quality_folder / "astra-packets.jsonl"):
        readings = success(packet)["votes"]
        for entry in packet["readings"]:
            historical.append({**entry, **readings[entry["blind_id"]], "rater":packet["rater"]})
    new = read_jsonl(quality_folder / "qwen-votes.jsonl")
    replacements = {(r["key"],r["kind"]) for r in new}
    votes = [r for r in votes if (r["key"], r["kind"]) not in replacements]
    votes.extend(dict(r, rater="new_family") for r in new)
    source["quality_revision"] = sha256_bytes(canonical_json({
        name: sha256_file(quality_folder / name)
        for name in ("second-turns.jsonl", "astra-packets.jsonl", "qwen-votes.jsonl")
    }))
    return subjects, historical, votes, source


def _load_or_create_plan(
    private: Path,
    subjects: Mapping[str, dict[str, Any]],
    historical, new_votes, source,
    *,
    rebuild: bool,
) -> tuple[dict[str, Any], bool]:
    plan_path = private / PRIVATE_PLAN
    if plan_path.exists():
        if not rebuild:
            raise ValueError(
                "PRIVATE_DIR already has a plan; use --rebuild or choose a new directory"
            )
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if plan.get("source_sha256") != source:
            raise ValueError("private plan source digests do not match frozen inputs")
        return plan, False
    if rebuild:
        raise ValueError("There is no saved random assignment to rebuild")
    if any(private.iterdir()):
        raise ValueError("PRIVATE_DIR must be empty for a new packet")
    seed_hex = secrets.token_hex(32)
    primary, repeats = vote_indexes(historical, new_votes, subjects)
    reasons_by_key = {key: selection_reasons(key, primary, repeats) for key in subjects}
    selected_keys = {key for key, reasons in reasons_by_key.items() if reasons}
    selected: list[dict[str, Any]] = []
    for ordinal, key in enumerate(sorted(selected_keys)):
        subject = subjects[key]
        selected.append(
            {
                **subject,
                "selection_reason": reasons_by_key[key],
                "blind_id": blind_id(seed_hex, key, ordinal),
            }
        )
    same_candidates = []
    for key, subject in subjects.items():
        if not same_judgment(key, primary, selected_keys):
            continue
        same_candidates.append(
            {
                **subject,
                "same_pass": pass_from_votes(primary.get(key, [])),
                "selection_reason": ["same_judged_control_sample"],
            }
        )
    controls, _details = sample_same_judged(same_candidates, seed_hex)
    for item in controls:
        item["blind_id"] = blind_id(seed_hex, item["key"], len(selected))
        selected.append(item)
    if len({item["key"] for item in selected}) != len(selected):
        raise ValueError("selection contains duplicate answer keys")
    plan = private_plan_for_new(seed_hex, selected, source, primary, repeats)
    plan["sample_details"] = _details
    return plan, True


def build(
    votes_path: Path, pack_dir: Path, private_dir: Path, *, rebuild: bool = False,
    quality_folder: Path | None = None
) -> dict[str, Any]:
    pack, private = assert_disjoint(pack_dir, private_dir)
    ensure_empty_pack(pack)
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    subjects, historical, new_votes, source = review_inputs(votes_path, quality_folder)
    plan, is_new = _load_or_create_plan(private, subjects, historical, new_votes, source, rebuild=rebuild)
    cards = []
    for entry in plan.get("selected", []):
        key = entry.get("key")
        if key not in subjects:
            raise ValueError("private plan refers to an unknown frozen answer")
        cards.append(_public_card(subjects[key], entry["blind_id"]))
    if not cards:
        raise ValueError("private plan has no selected answers")
    pack_id = pack_id_for_cards(cards)
    plan_path = private / PRIVATE_PLAN
    if is_new:
        plan["pack_id"] = pack_id
        # Link a complete, owner-readable plan into place without replacing a
        # concurrently created assignment or leaving a half-written mapping.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=private
        ) as tmp:
            json.dump(plan, tmp, ensure_ascii=False, indent=2)
            tmp.write("\n")
            tmp.flush()
            os.link(tmp.name, plan_path)
    elif plan.get("pack_id") != pack_id:
        raise ValueError("public pack does not match the private plan")
    build_public_files(pack, cards, pack_id)
    return {"cards": len(cards), "public_files": ["index.html", "README.txt"]}


def _require_private_output(output: Path, private: Path) -> Path:
    target = output.expanduser().resolve()
    private = private.expanduser().resolve()
    if target == private or os.path.commonpath([str(target), str(private)]) != str(
        private
    ):
        raise ValueError("reconcile output must stay inside PRIVATE_DIR")
    if target == private / PRIVATE_PLAN:
        raise ValueError("reconcile output cannot overwrite the private plan")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def reconcile(
    export_path: Path, private_dir: Path, output: Path | None = None
) -> dict[str, Any]:
    private = private_dir.expanduser().resolve()
    plan_path = private / PRIVATE_PLAN
    if not plan_path.is_file():
        raise ValueError("PRIVATE_DIR has no plan")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    data = json.loads(export_path.read_text(encoding="utf-8"))
    if data.get("format") != 1 or not isinstance(data.get("answers"), list):
        raise ValueError("unsupported human export")
    if (
        not isinstance(plan.get("pack_id"), str)
        or data.get("pack_id") != plan["pack_id"]
    ):
        raise ValueError("export does not match the private pack binding")
    expected = {entry["blind_id"]: entry for entry in plan.get("selected", [])}
    received: dict[str, dict[str, Any]] = {}
    for row in data["answers"]:
        if not isinstance(row, dict) or row.get("blind_id") not in expected:
            raise ValueError("export contains an unknown blind id")
        blind = row["blind_id"]
        if blind in received:
            raise ValueError("export contains a duplicate blind id")
        checks = validate_checks(
            row.get("checks", [None, None, None]), allow_missing=True
        )
        evidence = row.get("evidence", "")
        if not isinstance(evidence, str):
            evidence = str(evidence)
        received[blind] = {"checks": checks, "evidence": evidence}
    rows = []
    for blind, entry in expected.items():
        answer = received.get(blind, {"checks": [None, None, None], "evidence": ""})
        checks = answer["checks"]
        complete = all(item is not None for item in checks)
        rows.append(
            {
                "blind_id": blind,
                "key": entry["key"],
                "arm": entry["arm"],
                "case": entry["case"],
                "family": entry.get("family"),
                "checks": checks,
                "complete": complete,
                "pass": all(checks) if complete else None,
                "evidence": answer["evidence"],
            }
        )
    result = {
        "format": 1,
        "source": "private_plan",
        "answers": rows,
        "complete": sum(row["complete"] for row in rows),
        "scheduled": len(rows),
    }
    target = _require_private_output(output or (private / PRIVATE_RECONCILED), private)
    target.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"output": str(target), "answers": len(rows), "complete": result["complete"]}


def _generate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("votes_jsonl", type=Path)
    parser.add_argument("pack_dir", type=Path)
    parser.add_argument("private_dir", type=Path)
    parser.add_argument("--quality-report", type=Path, help="use the verified corrected second turns and votes")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="rebuild public files from an existing private plan",
    )
    return parser


def _reconcile_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconcile a public human export privately"
    )
    parser.add_argument("export_json", type=Path)
    parser.add_argument("private_dir", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args and args[0] == "reconcile":
            parsed = _reconcile_parser().parse_args(args[1:])
            result = reconcile(parsed.export_json, parsed.private_dir, parsed.output)
        else:
            parsed = _generate_parser().parse_args(args)
            result = build(
                parsed.votes_jsonl,
                parsed.pack_dir,
                parsed.private_dir,
                rebuild=parsed.rebuild,
                quality_folder=parsed.quality_report,
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
