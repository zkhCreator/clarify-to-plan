"""Publish a credential-free, offline report for a Responses cache run.

The collector in :mod:`api_cache` writes one JSON list per scheduled request
under ``attempts/``. The frozen population and calibrated allocation include
units that were never attempted. Stage records contain observations only.
This reporter never calls the API and never publishes prompt or provider text.
It copies only an explicit allow-list of run metadata, timings, usage counters,
hashes and error codes into a public snapshot and ledger.  The published
snapshot makes ``--verify`` independent of the private run directory.

Usage::

    python3 benchmark/api_cache_report.py RUN OUT
    python3 benchmark/api_cache_report.py --verify OUT

Costs in this report are usage-based estimates from the recorded token counts
and frozen rates.  They are not invoices or billing statements.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

REPORT_FORMAT = 1
ARM_NAMES = ("none", "release", "main")
ARM_LABELS = {"none": "无 skill", "release": "v1.0.0", "main": "main"}
POLICIES = ("uncached", "cold", "warm")
EXPECTED_CACHE = {
    "uncached": "verified_uncached",
    "cold": "verified_cold_write",
    "warm": "full_hit",
}
STAGES = ("uncached", "cold", "prewarm", "warm")
TERMINAL_SUCCESS = {"completed", "incomplete"}
KNOWN_ERRORS = {
    "insufficient_quota",
    "invalid_api_key",
    "model_not_found",
    "rate_limit_exceeded",
    "permission_denied",
    "unsupported_parameter",
    "invalid_value",
    "invalid_request_error",
    "billing_not_active",
    "organization_deactivated",
    "account_deactivated",
    "unsupported_country_region_territory",
    "server_error",
    "access_denied",
    "provider_error",
    "transport",
    "stream_ended",
    "unparseable_response",
    "missing_count",
    "unknown_remote_outcome",
}
KNOWN_PARAMETERS = {
    "prompt_cache_options",
    "prompt_cache_options.prewarm",
    "prompt_cache_key",
    "model",
    "service_tier",
    "max_output_tokens",
    "reasoning.effort",
}
USAGE_FIELDS = (
    "input",
    "output",
    "read",
    "write",
    "write_30m",
    "write_12h",
    "reasoning",
)
RATE_FIELDS = ("ordinary", "read", "write_30m", "write_12h", "output")
ATTEMPT_FIELDS = (
    "utc",
    "request_sha256",
    "http",
    "status",
    "error",
    "parameter",
    "model",
    "service_tier",
    "incomplete_reason",
    "input_tokens",
    "usage",
    "created_ms",
    "ttft_ms",
    "elapsed_ms",
    "retry_delay_ms",
    "answer_chars",
    "answer_sha256",
)


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha_json(value) -> str:
    return sha_bytes(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )


def read_json(path: Path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def jsonl_text(rows) -> str:
    return "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in rows
    )


def finite_int(value):
    return value if type(value) is int and value >= 0 else None


def safe_string(value, max_length=256):
    if not isinstance(value, str) or len(value) > max_length:
        return None
    return value


def safe_hash(value):
    if isinstance(value, str) and len(value) == 64:
        try:
            int(value, 16)
        except ValueError:
            return None
        return value
    return None


def clean_usage(value):
    if not isinstance(value, dict):
        return {}
    return {
        key: finite_int(value.get(key))
        for key in USAGE_FIELDS
        if finite_int(value.get(key)) is not None
    }


def clean_attempt(value):
    """Copy only the allow-listed, non-secret attempt fields."""

    if not isinstance(value, dict):
        return {"status": "invalid_record", "error": "provider_error", "usage": {}}
    result = {}
    utc = safe_string(value.get("utc"), 80)
    if utc is not None:
        result["utc"] = utc
    http = finite_int(value.get("http"))
    if http is not None and 100 <= http <= 599:
        result["http"] = http
    status = safe_string(value.get("status"), 32)
    if status is not None:
        result["status"] = status
    error = value.get("error")
    if isinstance(error, str):
        result["error"] = error if error in KNOWN_ERRORS else "provider_error"
    parameter = value.get("parameter")
    if isinstance(parameter, str) and parameter in KNOWN_PARAMETERS:
        result["parameter"] = parameter
    for key in ("model", "service_tier", "incomplete_reason"):
        text = safe_string(value.get(key), 128)
        if text is not None:
            result[key] = text
    for key in (
        "input_tokens",
        "created_ms",
        "ttft_ms",
        "elapsed_ms",
        "retry_delay_ms",
        "answer_chars",
    ):
        number = finite_int(value.get(key))
        if number is not None:
            result[key] = number
    request_hash = safe_hash(value.get("request_sha256"))
    if request_hash is not None:
        result["request_sha256"] = request_hash
    result["usage"] = clean_usage(value.get("usage"))
    answer_hash = safe_hash(value.get("answer_sha256"))
    if answer_hash is not None:
        result["answer_sha256"] = answer_hash
    return result


def clean_manifest(value):
    """Keep run facts needed to reproduce the report, not arbitrary metadata."""

    if not isinstance(value, dict):
        return {}
    result = {}
    for key in (
        "format",
        "from",
        "population_sha256",
        "disclosures_sha256",
        "model",
        "workers",
        "seed",
        "ttl",
        "tariff_date",
        "price_source",
        "rate_snapshot_sha256",
        "credential_origin",
        "endpoint",
        "region",
        "warm_reuses_per_prewarm",
        "snapshot",
        "start_utc",
    ):
        item = value.get(key)
        if type(item) in (str, int, float, bool) or item is None:
            result[key] = item
    if isinstance(value.get("rules"), dict):
        rules = value["rules"]
        result["rules"] = {
            key: rules[key]
            for key in (
                "model",
                "reasoning",
                "service_tier",
                "store",
                "max_output_tokens",
            )
            if key in rules and isinstance(rules[key], (dict, str, int, float, bool))
        }
    if isinstance(value.get("rates_usd_per_million"), dict):
        rates = value["rates_usd_per_million"]
        result["rates_usd_per_million"] = {
            key: rates[key]
            for key in RATE_FIELDS
            if type(rates.get(key)) in (int, float) and rates[key] >= 0
        }
    if isinstance(value.get("source"), dict):
        result["source"] = {
            str(key): safe_hash(item)
            for key, item in value["source"].items()
            if safe_hash(item) is not None
        }
    rate_hash = safe_hash(value.get("rate_snapshot_sha256"))
    if rate_hash is not None:
        result["rate_snapshot_sha256"] = rate_hash
    if isinstance(value.get("subjects_sha256"), str) and safe_hash(
        value["subjects_sha256"]
    ):
        result["subjects_sha256"] = value["subjects_sha256"]
    if isinstance(value.get("nonce"), str):
        # The nonce is an assignment identifier, not a credential.  Keep only
        # its shape-free hash so it cannot be mistaken for a secret.
        result["nonce_sha256"] = sha_bytes(value["nonce"].encode())
    elif safe_hash(value.get("nonce_sha256")):
        result["nonce_sha256"] = value["nonce_sha256"]
    return result


def clean_status(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    state = safe_string(value.get("state"), 32)
    if state is not None:
        result["state"] = state
    reason = value.get("stop_reason")
    if isinstance(reason, dict):
        cleaned = {}
        for key in ("operation", "code"):
            text = safe_string(reason.get(key), 128)
            if text is not None:
                cleaned[key] = (
                    text if key != "code" or text in KNOWN_ERRORS else "provider_error"
                )
        http = finite_int(reason.get("http"))
        if http is not None and 100 <= http <= 599:
            cleaned["http"] = http
        if cleaned:
            result["stop_reason"] = cleaned
    end = safe_string(value.get("end_utc"), 80)
    if end is not None:
        result["end_utc"] = end
    return result


def clean_calibration(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for scope, arms in value.items():
        if not isinstance(arms, dict):
            continue
        scope_result = {}
        for arm, row in arms.items():
            if not isinstance(row, dict):
                continue
            item = {}
            prefix = safe_hash(row.get("prefix_sha256"))
            if prefix is not None:
                item["prefix_sha256"] = prefix
            for key in ("count", "boundary_tokens"):
                if finite_int(row.get(key)) is not None:
                    item[key] = row[key]
            if isinstance(row.get("eligible"), bool):
                item["eligible"] = row["eligible"]
            elif row.get("eligible") is None:
                item["eligible"] = None
            scope_result[str(arm)] = item
        result[str(scope)] = scope_result
    return result


def clean_unit(value):
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ("id", "sample", "arm", "scope"):
        text = safe_string(value.get(key), 256)
        if text is not None:
            result[key] = text
    turn = finite_int(value.get("turn"))
    if turn is not None:
        result["turn"] = turn
    expected_turns = finite_int(value.get("expected_turns"))
    if expected_turns:
        result["expected_turns"] = expected_turns
    policies = value.get("policies")
    if isinstance(policies, list):
        result["policies"] = [p for p in policies if p in POLICIES]
    result["prefix_sha256"] = safe_hash(value.get("prefix_sha256"))
    result["visible_sha256"] = safe_hash(value.get("visible_sha256"))
    stages = value.get("stages")
    if isinstance(stages, (dict, list)):
        result["stages"] = sorted(str(x) for x in stages if str(x) in STAGES)
    else:
        result["stages"] = []
    if not result.get("id"):
        return None
    return result


def load_snapshot_from_run(run: Path):
    manifest = read_json(run / "manifest.json")
    if not isinstance(manifest, dict):
        raise TypeError("RUN/manifest.json is missing or invalid")
    if manifest.get("format") != 2:
        raise ValueError("Cannot determine a verified allocation denominator from a legacy run without a frozen population")
    status = clean_status(read_json(run / "status.json", {}))
    calibration = clean_calibration(read_json(run / "calibration.json", {}))
    plan = read_json(run / "allocation.json")
    population = read_json(run / "population.json")
    if not isinstance(population, list) or sha_json(population) != manifest.get("population_sha256"):
        raise ValueError("Missing or altered frozen cache population")
    if plan is None and (run / "units").is_dir():
        raise ValueError("Missing allocation for a run with formal units")
    if plan is not None:
        if not isinstance(plan, list) or len(plan) != len(population):
            raise ValueError("Allocation differs from the frozen population")
        normalized = [dict(row, policies=["uncached"]) for row in plan]
        if normalized != population:
            raise ValueError("Changed unit identity or input in frozen allocation")
    planned = plan if plan is not None else population
    units_by_id = {}
    for row in planned or []:
        unit = clean_unit(row)
        if unit is None or unit["id"] in units_by_id:
            raise ValueError("Invalid or duplicate planned unit")
        eligible = calibration.get(unit["scope"], {}).get(unit["arm"], {}).get("eligible")
        expected = POLICIES if eligible is True and plan is not None else ("uncached",)
        if len(unit["policies"]) != len(expected) or set(unit["policies"]) != set(expected):
            raise ValueError("Cache policies differ from calibrated allocation")
        units_by_id[unit["id"]] = unit
    unit_context = {}
    units_dir = run / "units"
    if units_dir.is_dir():
        for path in sorted(units_dir.glob("*.json")):
            unit = clean_unit(read_json(path))
            if unit is None:
                continue
            expected = units_by_id.get(unit["id"])
            if planned is not None and (expected is None or any(
                unit.get(k) != v for k, v in expected.items() if k != "stages"
            )):
                raise ValueError("Observed unit differs from frozen allocation")
            units_by_id[unit["id"]] = unit
    units = sorted(units_by_id.values(), key=lambda x: x["id"])
    for job in unit_jobs({"units": units}):
        unit_context[job["attempt_file"]] = {k: v for k, v in job.items() if k not in ("attempt_file", "job", "attempts")}
    attempts = {}
    attempts_dir = run / "attempts"
    if attempts_dir.is_dir():
        for path in sorted(attempts_dir.glob("*.json")):
            rows = read_json(path, [])
            if not isinstance(rows, list):
                rows = []
            rel = "attempts/" + path.name
            attempts[rel] = [clean_attempt(row) for row in rows]
    snapshot = {
        "format": REPORT_FORMAT,
        "run_manifest": clean_manifest(manifest),
        "run_status": status,
        "calibration": calibration,
        "units": units,
        "allocation_finalized": plan is not None,
        "attempts": attempts,
        "unit_context": unit_context,
    }
    # A partially copied RUN can have a stage in the unit record but no file.
    # Its missing request is retained as a scheduled, unobserved job below.
    return snapshot


def validate_calibration(snapshot, prefixes):
    records = snapshot["attempts"]
    calibrated = snapshot["calibration"]
    if snapshot["allocation_finalized"] and {
        (scope, arm) for scope, arms in calibrated.items() for arm in arms
    } != {(scope, arm) for scope, arms in prefixes.items() for arm in arms}:
        raise ValueError("The formal allocation requires all prefix calibrations")
    for scope, arms in calibrated.items():
        for arm, row in arms.items():
            if scope not in prefixes or arm not in prefixes[scope]:
                raise ValueError("Unknown calibrated prefix")
            tag = f"attempts/cal-{scope}-{arm}"
            counts = records.get(tag + "-count.json", [])
            measured = counts[-1].get("input_tokens") if counts else None
            prime = records.get(tag + "-prewarm.json", [])
            boundary = None
            if measured is not None and measured >= 1024 and len(prime) == 1:
                attempt = prime[0]
                usage = attempt["usage"]
                if (attempt.get("status") == "completed" and usage.get("read") == 0
                        and usage.get("write", 0) >= 1024 and usage.get("output") == 0):
                    boundary = usage["write"]
            eligible = True if boundary else False if measured is not None and measured < 1024 else None
            if (row.get("count") != measured or row.get("boundary_tokens") != boundary
                    or row.get("eligible") is not eligible or row.get("prefix_sha256") != sha_json(prefixes[scope][arm])):
                raise ValueError("Calibrated assignment differs from the prefix token and prewarm receipts")


def validate_public_allocation(snapshot):
    """Rebuild the full schedule from the public corrected inputs, including unattempted jobs."""
    import api_cache

    manifest = snapshot["run_manifest"]
    if manifest.get("format") != 2:
        raise ValueError("A public cache report requires the frozen population format")
    source = manifest.get("source", {})
    required = ("api_cache.py", "cache_transport.py", "corrected_user.py", "second_turn_disclosures.json",
                "cases.json", "protocol.json", "run.py")
    if source != {name: sha_bytes((api_cache.HERE / name).read_bytes()) for name in required}:
        raise ValueError("Cache source fingerprints differ")
    if (manifest.get("subjects_sha256") != sha_bytes(api_cache.SUBJECTS.read_bytes())
            or manifest.get("disclosures_sha256") != sha_bytes(api_cache.DISCLOSURES.read_bytes())
            or manifest.get("rules") != api_cache.RULES or manifest.get("model") != api_cache.MODEL
            or manifest.get("rate_snapshot_sha256") != sha_bytes((api_cache.HERE / "api_cache_rates.json").read_bytes())
            or manifest.get("rates_usd_per_million") != api_cache.RATES):
        raise ValueError("Cache replay or pricing differs from the frozen protocol")
    catalog = api_cache.prefixes()
    validate_calibration(snapshot, catalog)
    replay = api_cache.units()
    def plan(calibration):
        return [api_cache.assignment(u, catalog[u["scope"]][u["arm"]], calibration) for u in replay]

    if sha_json(plan({})) != manifest.get("population_sha256"):
        raise ValueError("Cache population does not reproduce from public inputs")
    calibrated = snapshot["calibration"] if snapshot["allocation_finalized"] else {}
    expected = sorted(plan(calibrated), key=lambda u: u["id"])
    observed = [{k: v for k, v in u.items() if k != "stages"} for u in snapshot["units"]]
    if observed != expected:
        raise ValueError("Public cache allocation differs from the frozen replay")
    expected_context = {j["attempt_file"]: {k:v for k,v in j.items() if k not in ("attempts","attempt_file","job")}
                        for j in unit_jobs(snapshot)}
    expected_jobs = set(expected_context)
    if snapshot["unit_context"] != expected_context:
        raise ValueError("Public cache request population differs from the allocation")
    allowed = {"attempts/access.json"}
    for scope, arms in catalog.items():
        for arm in arms:
            for action in ("count", "prewarm"):
                allowed.add(f"attempts/cal-{scope}-{arm}-{action}.json")
    if not snapshot["allocation_finalized"] and set(snapshot["attempts"]) & expected_jobs:
        raise ValueError("Formal attempts require a frozen allocation")
    if set(snapshot["attempts"]) - expected_jobs - allowed:
        raise ValueError("Unassigned response attempt")


def clean_snapshot(value):
    if not isinstance(value, dict) or value.get("format") != REPORT_FORMAT:
        raise ValueError("Invalid published snapshot")
    result = {
        "format": REPORT_FORMAT,
        "run_manifest": clean_manifest(value.get("run_manifest", {})),
        "run_status": clean_status(value.get("run_status", {})),
        "allocation_finalized": value.get("allocation_finalized") is True,
        "calibration": clean_calibration(value.get("calibration", {})),
        "units": [],
        "attempts": {},
        "unit_context": {},
    }
    for row in value.get("units", []):
        unit = clean_unit(row)
        if unit is not None:
            result["units"].append(unit)
    result["units"].sort(key=lambda x: x["id"])
    for name, rows in (value.get("attempts", {}) or {}).items():
        if not isinstance(name, str) or not name.startswith("attempts/"):
            continue
        result["attempts"][name] = [
            clean_attempt(row) for row in rows if isinstance(rows, list)
        ]
    for name, context in (value.get("unit_context", {}) or {}).items():
        if isinstance(name, str) and isinstance(context, dict):
            result["unit_context"][name] = {
                key: context.get(key)
                for key in (
                    "kind",
                    "unit_id",
                    "sample",
                    "arm",
                    "scope",
                    "turn",
                    "policy",
                    "stage",
                    "visible_sha256",
                    "prefix_sha256",
                )
                if key in context
            }
    return result


def ledger_rows(snapshot):
    rows = []
    context = snapshot.get("unit_context", {})
    for name in sorted(snapshot.get("attempts", {})):
        ctx = context.get(name, {"kind": "operation"})
        job = name[len("attempts/") : -len(".json")] if name.endswith(".json") else name
        for index, attempt in enumerate(snapshot["attempts"][name], 1):
            row = {
                "kind": ctx.get("kind", "operation"),
                "job": job,
                "attempt_file": name,
                "attempt_index": index,
            }
            for key in (
                "unit_id",
                "sample",
                "arm",
                "scope",
                "turn",
                "policy",
                "stage",
                "visible_sha256",
                "prefix_sha256",
            ):
                if key in ctx:
                    row[key] = ctx[key]
            row.update(attempt)
            rows.append(row)
    return rows


def unit_jobs(snapshot):
    """Return scheduled stage jobs, including jobs with no attempt file."""

    attempts = snapshot.get("attempts", {})
    jobs = []
    for unit in snapshot.get("units", []):
        for policy in unit.get("policies", []):
            stage_names = ("prewarm", "warm") if policy == "warm" else (policy,)
            for stage in stage_names:
                name = "attempts/" + unit["id"] + "-" + stage + ".json"
                jobs.append(
                    {
                        "kind": "unit",
                        "job": name[len("attempts/") : -len(".json")],
                        "attempt_file": name,
                        "unit_id": unit["id"],
                        "sample": unit.get("sample"),
                        "arm": unit.get("arm"),
                        "scope": unit.get("scope"),
                        "turn": unit.get("turn"),
                        "policy": policy,
                        "stage": stage,
                        "visible_sha256": unit.get("visible_sha256"),
                        "prefix_sha256": unit.get("prefix_sha256"),
                        "attempts": attempts.get(name, []),
                    }
                )
    return jobs


def terminal(attempt):
    return isinstance(attempt, dict) and attempt.get("status") in TERMINAL_SUCCESS


def eventual_attempt(attempts):
    for index in range(len(attempts) - 1, -1, -1):
        if terminal(attempts[index]):
            return index, attempts[index]
    return None, None


def first_success_attempt(attempts):
    for index, attempt in enumerate(attempts):
        if terminal(attempt):
            return index, attempt
    return None, None


def total_elapsed(attempts):
    if not attempts:
        return None
    values = []
    for attempt in attempts:
        elapsed = finite_int(attempt.get("elapsed_ms"))
        delay = finite_int(attempt.get("retry_delay_ms"))
        if elapsed is None:
            return None
        values.append(elapsed + (delay or 0))
    return sum(values)


def with_retry_first_visible(attempts):
    """Return the first visible text across all attempts, even if its stream later fails."""
    elapsed_before = 0
    for attempt in attempts:
        elapsed_before += finite_int(attempt.get("retry_delay_ms")) or 0
        first_visible = finite_int(attempt.get("ttft_ms"))
        if first_visible is not None:
            return elapsed_before + first_visible
        elapsed = finite_int(attempt.get("elapsed_ms"))
        if elapsed is None:
            return None
        elapsed_before += elapsed
    return None


def target_tokens(snapshot, scope, arm):
    row = snapshot.get("calibration", {}).get(scope, {}).get(arm, {})
    # A count alone establishes length, not that the first prewarm really
    # wrote the unique breakpoint.  Only the collector's positive eligibility
    # decision is a calibration target for a verified cache claim.
    if row.get("eligible") is True:
        value = finite_int(row.get("boundary_tokens"))
        if value is not None and value >= 1024:
            return value
    return None


def usage_of(attempt):
    usage = attempt.get("usage") if isinstance(attempt, dict) else None
    return usage if isinstance(usage, dict) else {}


def cache_class(job, attempt, target):
    if not isinstance(attempt, dict):
        return "unknown"
    usage = usage_of(attempt)
    read = usage.get("read")
    write = usage.get("write")
    if read is None or write is None:
        return "unknown"
    if job["policy"] == "uncached":
        if read == 0 and write == 0:
            return "verified_uncached"
        return "unexpected_cache"
    if job["stage"] == "prewarm":
        if attempt.get("status") != "completed":
            return "unconfirmed_prewarm"
        output = usage.get("output")
        if output is None:
            return "unknown"
        if target is not None and write == target and read == 0 and output == 0:
            return "verified_write"
        if write > 0:
            return "partial_write"
        return "no_write"
    if job["policy"] == "cold":
        if read == 0 and write > 0:
            return (
                "verified_cold_write"
                if target is not None and write == target
                else "partial_write"
            )
        if read and write:
            return "read_and_write"
        if read:
            return "unexpected_read"
        return "no_write"
    if job["stage"] == "warm":
        if target is not None and read == target and write == 0:
            return "full_hit"
        if read > 0 and write == 0:
            return "partial_hit" if target is not None else "read_observed"
        if read == 0 and write == 0:
            return "miss"
        if write and read:
            return "read_and_write"
        if write:
            return "rewrite"
    return "unknown"


def priced_model(actual, expected):
    if actual == expected:
        return True
    if expected == "gpt-6.2-astra" and isinstance(actual, str):
        return re.fullmatch(r"gpt-6\.2-astra-\d{4}-\d{2}-\d{2}", actual) is not None
    return False


def cost_for(attempt, manifest):
    """Return component estimates, or null components when usage is incomplete."""

    null = {
        "ordinary_input_usd": None,
        "cache_read_usd": None,
        "cache_write_30m_usd": None,
        "cache_write_12h_usd": None,
        "output_usd": None,
        "estimated_usage_usd": None,
    }
    if not isinstance(attempt, dict):
        return null
    expected_model = manifest.get("model")
    expected_tier = (manifest.get("rules") or {}).get("service_tier", "default")
    if (
        not priced_model(attempt.get("model"), expected_model)
        or attempt.get("service_tier") != expected_tier
    ):
        return null
    rate = manifest.get("rates_usd_per_million")
    if expected_model != "gpt-6.2-astra" or expected_tier != "default":
        return null
    if not isinstance(rate, dict) or not all(
        type(rate.get(k)) in (int, float) and rate[k] >= 0 for k in RATE_FIELDS
    ):
        return null
    usage = usage_of(attempt)
    if not all(key in usage for key in ("input", "read", "write", "output")):
        return null
    if usage["input"] > 272_000:
        return null
    ordinary = usage["input"] - usage["read"] - usage["write"]
    if ordinary < 0:
        return null
    write30 = usage.get("write_30m")
    write12 = usage.get("write_12h")
    if write30 is None and write12 is None:
        # Zero writes carry no TTL allocation.  A positive aggregate write
        # without the provider's lifetime breakdown must remain unknown.
        if usage["write"] == 0:
            write30, write12 = 0, 0
        else:
            return null
    elif write30 is None and write12 == usage["write"]:
        write30 = 0
    elif write12 is None and write30 == usage["write"]:
        write12 = 0
    elif write30 is None or write12 is None:
        return null
    if write30 + write12 != usage["write"]:
        return null
    values = {
        "ordinary_input_usd": ordinary * rate["ordinary"] / 1_000_000,
        "cache_read_usd": usage["read"] * rate["read"] / 1_000_000,
        "cache_write_30m_usd": write30 * rate["write_30m"] / 1_000_000,
        "cache_write_12h_usd": write12 * rate["write_12h"] / 1_000_000,
        "output_usd": usage["output"] * rate["output"] / 1_000_000,
    }
    values["estimated_usage_usd"] = sum(values.values())
    return {key: round(value, 12) for key, value in values.items()}


def quantiles(values):
    values = sorted(value for value in values if type(value) in (int, float))
    if not values:
        return {"n": 0, "median_ms": None, "p95_ms": None}
    return {
        "n": len(values),
        "median_ms": statistics.median(values),
        "p95_ms": values[math.ceil(0.95 * len(values)) - 1],
    }


def aggregate_cost(costs):
    if not costs:
        return {
            key: None
            for key in (
                "ordinary_input_usd",
                "cache_read_usd",
                "cache_write_30m_usd",
                "cache_write_12h_usd",
                "output_usd",
                "estimated_usage_usd",
            )
        }
    keys = costs[0]
    result = {}
    for key in keys:
        result[key] = (
            round(sum(item[key] for item in costs), 12)
            if all(item.get(key) is not None for item in costs)
            else None
        )
    return result


def aggregate_usage(attempts):
    if not attempts:
        return {key: None for key in USAGE_FIELDS}
    result = {}
    for key in USAGE_FIELDS:
        values = [usage_of(row).get(key) for row in attempts]
        result[key] = (
            sum(values) if all(type(value) is int for value in values) else None
        )
    return result


def job_metric(job, snapshot):
    attempts = job.get("attempts", [])
    first_index, _ = first_success_attempt(attempts)
    final_index, final = eventual_attempt(attempts)
    target = target_tokens(snapshot, job.get("scope"), job.get("arm"))
    classification = cache_class(job, final, target)
    selected = final if final is not None else {}
    all_costs = [cost_for(a, snapshot.get("run_manifest", {})) for a in attempts]
    total = total_elapsed(attempts)
    return {
        "job": job.get("job"),
        "attempt_file": job.get("attempt_file"),
        "unit_id": job.get("unit_id"),
        "sample": job.get("sample"),
        "arm": job.get("arm"),
        "scope": job.get("scope"),
        "turn": job.get("turn"),
        "policy": job.get("policy"),
        "stage": job.get("stage"),
        "visible_sha256": job.get("visible_sha256"),
        "prefix_sha256": job.get("prefix_sha256"),
        "scheduled": True,
        "attempt_count": len(attempts),
        "retry_count": max(0, len(attempts) - 1),
        "first_attempt_status": attempts[0].get("status") if attempts else None,
        "eventual_status": final.get("status") if final else None,
        "first_attempt_completed": terminal(attempts[0]) if attempts else False,
        "eventual_completed": final is not None,
        "first_success_attempt_index": first_index + 1
        if first_index is not None
        else None,
        "eventual_attempt_index": final_index + 1 if final_index is not None else None,
        "total_elapsed_ms_including_retries": total,
        "eventual_attempt_elapsed_ms": finite_int(selected.get("elapsed_ms")),
        "ttft_ms": finite_int(selected.get("ttft_ms")),
        "first_visible_ms_including_retries": with_retry_first_visible(attempts),
        "cache_target_tokens": target,
        "cache_classification": classification,
        "usage": aggregate_usage(attempts),
        "estimated_usage_cost_usd": aggregate_cost(all_costs),
        "attempts_with_priced_usage": sum(
            c["estimated_usage_usd"] is not None for c in all_costs
        ),
        "model": selected.get("model"),
        "service_tier": selected.get("service_tier"),
        "attempt_identities": [{k: a.get(k) for k in ("model", "service_tier")} for a in attempts],
    }


def empty_metric(status):
    return {
        "status": status,
        "scheduled": None,
        "attempted": None,
        "first_attempt_completed": None,
        "eventual_completed": None,
        "coverage_fraction": None,
        "initial_failures": None,
        "retry_used": None,
        "attempts_total": None,
        "retries_total": None,
        "total_elapsed_ms_including_retries": quantiles([]),
        "eventual_attempt_elapsed_ms": quantiles([]),
        "ttft_ms": quantiles([]),
        "first_visible_ms_including_retries": quantiles([]),
        "cache_classification": {},
        "usage_tokens": {key: None for key in USAGE_FIELDS},
        "estimated_usage_cost_usd": {
            key: None
            for key in (
                "ordinary_input_usd",
                "cache_read_usd",
                "cache_write_30m_usd",
                "cache_write_12h_usd",
                "output_usd",
                "estimated_usage_usd",
            )
        },
        "models": [],
        "service_tiers": [],
        "unknown_model_or_service_tier": None,
    }


def zero_metric(status="measured_no_verified_hit"):
    value = empty_metric(status)
    for key in (
        "scheduled",
        "attempted",
        "first_attempt_completed",
        "eventual_completed",
        "initial_failures",
        "retry_used",
        "attempts_total",
        "retries_total",
    ):
        value[key] = 0
    value["coverage_fraction"] = None
    return value


def summary_metric(metrics, status="measured"):
    if not metrics:
        return (
            zero_metric(status)
            if status == "measured_no_verified_hit"
            else empty_metric(status)
        )
    completed = [m for m in metrics if m["eventual_completed"]]
    first_completed = sum(m["first_attempt_completed"] for m in metrics)
    retries = sum(m["retry_count"] for m in metrics)
    attempts = sum(m["attempt_count"] for m in metrics)
    classification = Counter(m["cache_classification"] for m in metrics)
    costs = [m["estimated_usage_cost_usd"] for m in metrics]
    usage_rows = [m["usage"] for m in metrics]
    return {
        "status": status,
        "scheduled": len(metrics),
        "attempted": sum(m["attempt_count"] > 0 for m in metrics),
        "first_attempt_completed": first_completed,
        "eventual_completed": len(completed),
        "coverage_fraction": len(completed) / len(metrics) if metrics else None,
        "initial_failures": sum(
            m["attempt_count"] > 0 and not m["first_attempt_completed"] for m in metrics
        ),
        "retry_used": sum(m["retry_count"] > 0 for m in metrics),
        "attempts_total": attempts,
        "retries_total": retries,
        "total_elapsed_ms_including_retries": quantiles(
            [m["total_elapsed_ms_including_retries"] for m in metrics]
        ),
        "eventual_attempt_elapsed_ms": quantiles(
            [m["eventual_attempt_elapsed_ms"] for m in completed]
        ),
        "ttft_ms": quantiles([m["ttft_ms"] for m in completed]),
        "first_visible_ms_including_retries": quantiles(
            [m["first_visible_ms_including_retries"] for m in metrics]
        ),
        "cache_classification": dict(sorted(classification.items())),
        "usage_tokens": aggregate_usage(
            [
                {
                    "usage": row,
                }
                for row in usage_rows
            ]
        ),
        "estimated_usage_cost_usd": aggregate_cost(costs),
        "models": sorted({m["model"] for m in completed if m["model"]}),
        "service_tiers": sorted(
            {m["service_tier"] for m in completed if m["service_tier"]}
        ),
        "attempts_with_priced_usage": sum(
            m["attempts_with_priced_usage"] for m in metrics
        ),
        "unknown_model_or_service_tier": sum(
            m["eventual_completed"] and (not m["model"] or not m["service_tier"])
            for m in metrics
        ),
    }


def difference(values):
    values = [value for value in values if type(value) in (int, float)]
    if not values:
        return {"n": 0, "median": None, "p95": None, "mean": None}
    return {
        "n": len(values),
        "median": statistics.median(values),
        "p95": sorted(values)[math.ceil(0.95 * len(values)) - 1],
        "mean": round(statistics.mean(values), 6),
    }


def service_match(metrics, manifest):
    requested = manifest.get("rules") or {}
    model = requested.get("model") or manifest.get("model")
    tier = requested.get("service_tier")
    identities = [a for m in metrics for a in ((m or {}).get("attempt_identities") or [m or {}])]
    if any(a.get("service_tier") and a["service_tier"] != tier for a in identities):
        return "tier_mismatch"
    if any(a.get("model") and not priced_model(a["model"], model) for a in identities):
        return "model_mismatch"
    if not model or not tier or any(not a.get("model") or not a.get("service_tier") for a in identities):
        return "identity_unavailable"
    if len({a["model"] for a in identities}) != 1:
        return "snapshot_mismatch"
    return "matched"


def pair_rows(task_metrics, manifest):
    rows = []
    for task in task_metrics:
        if not task.get("visible_sha256"):
            continue
        by_policy = task["policies"]
        for left, right in itertools.combinations(POLICIES, 2):
            l = by_policy.get(left, {}).get("actual")
            r = by_policy.get(right, {}).get("actual")
            if not l or not r:
                continue
            stages = [l, r]
            if "warm" in (left, right):
                stages.append(by_policy["warm"].get("prewarm"))
            comparison = service_match(stages, manifest)
            row = {
                "unit_id": task["unit_id"],
                "sample": task["sample"],
                "scope": task["scope"],
                "arm": task["arm"],
                "turn": task["turn"],
                "visible_sha256": task["visible_sha256"],
                "left": left,
                "right": right,
                "service_match": comparison,
                "both_policies_verified": (
                    comparison == "matched" and l["cache_classification"] == EXPECTED_CACHE[left]
                    and r["cache_classification"] == EXPECTED_CACHE[right]
                ),
                "warm_full_hit": (
                    by_policy.get("warm", {})
                    .get("actual", {})
                    .get("cache_classification")
                    == "full_hit" and comparison == "matched"
                    if "warm" in by_policy
                    else None
                ),
            }
            for metric in (
                "total_elapsed_ms_including_retries",
                "ttft_ms",
                "first_visible_ms_including_retries",
            ):
                lv = l.get(metric)
                rv = r.get(metric)
                row[metric + "_delta_right_minus_left"] = (
                    rv - lv
                    if comparison == "matched" and type(lv) in (int, float) and type(rv) in (int, float)
                    else None
                )
            lc = l.get("estimated_usage_cost_usd", {}).get("estimated_usage_usd")
            rc = r.get("estimated_usage_cost_usd", {}).get("estimated_usage_usd")
            row["estimated_usage_cost_usd_delta_right_minus_left"] = (
                rc - lc if comparison == "matched" and lc is not None and rc is not None else None
            )
            rows.append(row)
    return rows


def pair_summary(rows):
    groups = defaultdict(list)
    for row in rows:
        key = (row["scope"], row["arm"], row["left"] + "→" + row["right"])
        groups[key].append(row)
    result = {}
    for (scope, arm, pair), values in sorted(groups.items()):
        itt = values
        has_warm = "warm" in pair.split("→")
        full = (
            [row for row in values if row["warm_full_hit"] is True]
            if has_warm
            else None
        )
        data = {}
        for label, chosen in (
            ("ITT", itt),
            ("verified_full_hit", full),
            ("verified_pair", [row for row in values if row["both_policies_verified"]]),
        ):
            if chosen is None:
                data[label] = None
                continue
            data[label] = {
                "n": len(chosen),
                "service_match": dict(Counter(row["service_match"] for row in chosen)),
                "total_elapsed_ms_including_retries_delta_right_minus_left": difference(
                    [
                        x["total_elapsed_ms_including_retries_delta_right_minus_left"]
                        for x in chosen
                    ]
                ),
                "ttft_ms_delta_right_minus_left": difference(
                    [x["ttft_ms_delta_right_minus_left"] for x in chosen]
                ),
                "first_visible_ms_including_retries_delta_right_minus_left": difference(
                    [
                        x["first_visible_ms_including_retries_delta_right_minus_left"]
                        for x in chosen
                    ]
                ),
                "estimated_usage_cost_usd_delta_right_minus_left": difference(
                    [
                        x["estimated_usage_cost_usd_delta_right_minus_left"]
                        for x in chosen
                    ]
                ),
            }
        result.setdefault(scope, {}).setdefault(arm, {})[pair] = data
    return result


def dialogue_summary(snapshot, metrics):
    """Sum all required turn waits and costs before summarizing each dialogue."""
    expected = {u["sample"]: u.get("expected_turns") for u in snapshot["units"]}
    by_sample = defaultdict(dict)
    for m in metrics:
        key = (m["scope"], m["arm"], m["policy"], m["sample"])
        by_sample[key][m["stage"], m["turn"]] = m
    groups = defaultdict(list)
    for (scope, arm, policy, sample), stages in by_sample.items():
        count = expected.get(sample)
        indexes = range(1, count + 1) if count else ()
        formal = [stages.get((policy, t)) for t in indexes]
        overhead = (
            [stages.get(("prewarm", t)) for t in indexes] if policy == "warm" else []
        )
        all_stages = formal + overhead
        waits = [m["total_elapsed_ms_including_retries"] if m else None for m in formal]

        def sum_cost(rows):
            if not rows or not all(rows):
                return None
            return aggregate_cost([m["estimated_usage_cost_usd"] for m in rows])[
                "estimated_usage_usd"
            ]

        groups[scope, arm, policy].append(
            {
                "sample": sample,
                "all_responses": bool(formal)
                and all(m and m["eventual_completed"] for m in formal),
                "wait_ms": sum(waits)
                if waits and all(t is not None for t in waits)
                else None,
                "formal_usd": sum_cost(formal),
                "including_prewarm_usd": sum_cost(all_stages),
            }
        )
    result = []

    def money(samples, key):
        values = [s[key] for s in samples]
        return round(sum(values), 12) if all(v is not None for v in values) else None

    for (scope, arm, policy), samples in sorted(groups.items()):
        result.append(
            {
                "scope": scope,
                "arm": arm,
                "policy": policy,
                "recorded_samples": len(samples),
                "all_responses": sum(s["all_responses"] for s in samples),
                "wait": quantiles([s["wait_ms"] for s in samples]),
                "formal_usd": money(samples, "formal_usd"),
                "including_prewarm_usd": money(samples, "including_prewarm_usd"),
            }
        )
    return result


def recorded_costs(ledger, manifest):
    """Preserve identifiable spend when a different request has no priced receipt."""
    by_stage = {"access": [], "prefix_count": [], "calibration_prewarm": [], "formal": [], "prewarm": []}
    for row in ledger:
        if row.get("kind") == "unit":
            group = "prewarm" if row.get("stage") == "prewarm" else "formal"
        elif row["job"] == "access":
            group = "access"
        elif row["job"].endswith("-count"):
            group = "prefix_count"
        else:
            group = "calibration_prewarm"
        by_stage[group].append(cost_for(row, manifest)["estimated_usage_usd"])
    return {
        group: {
            "recorded_attempts": len(values),
            "priced_attempts": sum(v is not None for v in values),
            "observed_priced_subtotal_usd": round(sum(v for v in values if v is not None), 12)
            if any(v is not None for v in values) else None,
        } for group, values in by_stage.items()
    }


def verified_readout(tasks, pairs):
    """Compare the same cold/warm units; include one prewarm in the all-in cost and wait."""
    paired_ids = {
        r["unit_id"] for r in pairs
        if (r["left"], r["right"]) == ("cold", "warm") and r["both_policies_verified"]
    }
    data = defaultdict(lambda: defaultdict(list))
    for task in tasks:
        stages = data[task["scope"], task["arm"]]
        policies = task["policies"]
        uncached = policies.get("uncached", {}).get("actual")
        if (uncached and uncached["service_match"] == "matched"
                and uncached["cache_classification"] == EXPECTED_CACHE["uncached"]):
            stages["uncached"].append((uncached,))
        if task["unit_id"] in paired_ids:
            cold = policies["cold"]["actual"]
            hot = policies["warm"]["actual"]
            prewarm = policies["warm"]["prewarm"]
            stages["cold"].append((cold,))
            stages["warm"].append((hot,))
            stages["prewarm"].append((prewarm,))
            stages["warm_including_prewarm"].append((prewarm, hot))

    result = {}
    for (scope, arm), stages in sorted(data.items()):
        values = {}
        for stage in ("uncached", "cold", "warm", "prewarm", "warm_including_prewarm"):
            groups = stages[stage]
            waits, visible, costs = [], [], []
            for group in groups:
                timing = [m["total_elapsed_ms_including_retries"] for m in group]
                cost = [m["estimated_usage_cost_usd"]["estimated_usage_usd"] for m in group]
                if all(v is not None for v in timing):
                    waits.append(sum(timing))
                if all(v is not None for v in cost):
                    costs.append(sum(cost))
                if len(group) == 1 and group[0].get("first_visible_ms_including_retries") is not None:
                    visible.append(group[0]["first_visible_ms_including_retries"])
            values[stage] = {"units": len(groups), "wait_ms": quantiles(waits),
                             "first_visible_ms": quantiles(visible), "priced_units": len(costs),
                             "mean_usage_usd": round(statistics.mean(costs), 12) if costs else None}
        result.setdefault(scope, {})[arm] = values
    return result


def readout_table(summary):
    lines = [
        "## 已验证请求的耗时与单次费用", "",
        ("各耗时为客户端中位数，单位为秒。每格依次为首个可见文本和完整响应。无缓存列取本组已验证的对照请求。"
        "冷写和热读只取两边、预热的身份与计数均核实的同一批输入。长目录每组仅三个单元。"), "",
        "| 输入范围 | 版本 | 已验证无缓存/分配 | 有效冷热配对/分配 | 无缓存首字/完整 s | 冷写首字/完整 s | 热读首字/完整 s | 预热加热读完整 s |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    money = ["", "单次费用按有完整计费计数的样本取算术平均，单位为美元。末列为同一输入的一次预热与一次热读之和。各列的耗时与费用分母见机读字段 `verified_readout`。", "",
             "| 输入范围 | 版本 | 无缓存 USD | 冷写 USD | 热读 USD | 单次预热 USD | 含预热的热组 USD |",
             "|---|---|---:|---:|---:|---:|---:|"]
    def seconds(value):
        return f"{value / 1000:.3f}" if value is not None else "—"

    for scope, arms in summary["verified_readout"].items():
        for arm in ARM_NAMES:
            if arm not in arms:
                continue
            row = arms[arm]
            name = f"| {summary['scope_labels'][scope]} | {ARM_LABELS[arm]} |"
            assigned = summary["groups"][scope][arm]["cold"]["allocation_itt"]["scheduled"]
            proof = (f"{row['cold']['units']}/{assigned}" if assigned
                     else "不适用" if summary["calibration"].get(scope, {}).get(arm, {}).get("eligible") is False else "待定")
            cells = []
            for stage in ("uncached", "cold", "warm"):
                times = row[stage]
                cells.append(seconds(times["first_visible_ms"]["median_ms"]) + " / " + seconds(times["wait_ms"]["median_ms"])
                             if times["units"] else "—")
            all_in = seconds(row["warm_including_prewarm"]["wait_ms"]["median_ms"])
            uc_scheduled = summary["groups"][scope][arm]["uncached"]["allocation_itt"]["scheduled"]
            uc_count = f"{row['uncached']['units']}/{uc_scheduled}" if uc_scheduled is not None else "待定"
            lines.append(name + f" {uc_count} | {proof} | " + " | ".join([*cells, all_in]) + " |")
            costs = [row[k]["mean_usage_usd"] for k in ("uncached", "cold", "warm", "prewarm", "warm_including_prewarm")]
            money.append(name + " " + " | ".join(f"{v:.6f}" if v is not None else "—" for v in costs) + " |")
    return lines + money


def build_summary(snapshot):
    jobs = unit_jobs(snapshot)
    metrics = [job_metric(job, snapshot) for job in jobs]
    manifest = snapshot.get("run_manifest", {})
    for metric in metrics:
        metric["service_match"] = service_match([metric], manifest)
    by_unit = defaultdict(dict)
    for metric in metrics:
        pair = by_unit[metric["unit_id"]].setdefault(
            metric["policy"], {"actual": None, "prewarm": None}
        )
        pair["prewarm" if metric["stage"] == "prewarm" else "actual"] = metric
    for policies in by_unit.values():
        warm = policies.get("warm", {})
        actual = warm.get("actual") or {}
        prime = warm.get("prewarm") or {}
        if actual:
            actual["service_match"] = service_match([actual, prime], manifest)
        if (
            actual.get("cache_classification") == "full_hit"
            and prime.get("cache_classification") != "verified_write"
        ):
            actual["cache_classification"] = "full_read_without_verified_prewarm"
    task_metrics = []
    unit_index = {unit["id"]: unit for unit in snapshot.get("units", [])}
    for unit_id in sorted(by_unit):
        unit = unit_index.get(unit_id, {})
        task_metrics.append(
            {
                "unit_id": unit_id,
                "sample": unit.get("sample"),
                "scope": unit.get("scope"),
                "arm": unit.get("arm"),
                "turn": unit.get("turn"),
                "visible_sha256": unit.get("visible_sha256"),
                "prefix_sha256": unit.get("prefix_sha256"),
                "policies": by_unit[unit_id],
            }
        )
    grouped_jobs = defaultdict(list)
    for metric in metrics:
        if metric["stage"] != "prewarm":
            grouped_jobs[(metric["scope"], metric["arm"], metric["policy"])].append(
                metric
            )
    grouped = defaultdict(dict)
    scopes = sorted(
        {"natural", "shared_catalog"}
        | {u.get("scope") for u in snapshot.get("units", []) if u.get("scope")}
    )
    units_exist = bool(snapshot.get("units"))
    for scope in scopes:
        for arm in ARM_NAMES:
            grouped[scope][arm] = {}
            for policy in POLICIES:
                values = grouped_jobs.get((scope, arm, policy), [])
                if values:
                    allocation = summary_metric(values)
                else:
                    eligibility = snapshot.get("calibration", {}).get(scope, {}).get(arm, {}).get("eligible")
                    if snapshot.get("allocation_finalized"):
                        reason = "not_assigned"
                        if policy != "uncached" and eligibility is not True:
                            reason = "short_prefix" if eligibility is False else "eligibility_unverified"
                        allocation = zero_metric(reason)
                    else:
                        allocation = empty_metric("allocation_pending" if units_exist else "not_measured")
                verified = [
                    x
                    for x in values
                    if x["cache_classification"] == EXPECTED_CACHE[policy] and x["service_match"] == "matched"
                ]
                subgroup = (
                    summary_metric(
                        verified, "measured" if verified else "measured_no_verified_hit"
                    )
                    if values
                    else None
                )
                grouped[scope][arm][policy] = {
                    "allocation_itt": allocation,
                    "verified_full_hit_subset": subgroup if policy == "warm" else None,
                    "verified_cold_write_subset": subgroup
                    if policy == "cold"
                    else None,
                    "prewarm": None,
                }
    prewarm_grouped = defaultdict(list)
    for metric in metrics:
        if metric["stage"] == "prewarm":
            prewarm_grouped[(metric["scope"], metric["arm"], "warm")].append(metric)
    for (scope, arm, policy), values in sorted(prewarm_grouped.items()):
        group = grouped[scope][arm][policy]
        group["prewarm"] = summary_metric(values)
    # Prewarm costs are shown both as a total and as an amortized amount.  A
    # verified hit is the only request that proves the prewarm was reused.
    for scope, arms in grouped.items():
        for arm, policies in arms.items():
            warm = policies.get("warm")
            if not warm or not warm.get("prewarm"):
                continue
            pre_cost = warm["prewarm"]["estimated_usage_cost_usd"].get(
                "estimated_usage_usd"
            )
            hits = warm.get("verified_full_hit_subset", {}).get("eventual_completed", 0)
            warm["prewarm"]["amortization"] = {
                "verified_full_hit_count": hits,
                "prewarm_estimated_usage_usd_per_verified_full_hit": (
                    round(pre_cost / hits, 12)
                    if pre_cost is not None and hits
                    else None
                ),
                "eventual_warm_completed_count": warm["allocation_itt"][
                    "eventual_completed"
                ],
                "prewarm_estimated_usage_usd_per_eventual_warm_completion": (
                    round(pre_cost / warm["allocation_itt"]["eventual_completed"], 12)
                    if pre_cost is not None
                    and warm["allocation_itt"]["eventual_completed"]
                    else None
                ),
            }
    ledger = ledger_rows(snapshot)
    operations = []
    known_unit_files = {j["attempt_file"] for j in jobs}
    for row in ledger:
        if row["attempt_file"] not in known_unit_files:
            operations.append(row)
    pairs = pair_rows(task_metrics, manifest)
    return {
        "format": REPORT_FORMAT,
        "run": manifest,
        "status": snapshot.get("run_status", {}),
        "planned_units": len(snapshot["units"]),
        "allocation_finalized": snapshot.get("allocation_finalized", False),
        "calibration": snapshot.get("calibration", {}),
        "scope_labels": {
            "natural": "自然前缀",
            "shared_catalog": "独立共享长目录",
        },
        "arms": {arm: ARM_LABELS[arm] for arm in ARM_NAMES},
        "groups": grouped,
        "dialogues": dialogue_summary(snapshot, metrics),
        "paired_differences": pair_summary(pairs),
        "verified_readout": verified_readout(task_metrics, pairs),
        "requests": metrics,
        "tasks": task_metrics,
        "operations": operations,
        "ledger_rows": len(ledger),
        "recorded_costs": recorded_costs(ledger, manifest),
        "definitions": {
            "successful_response": sorted(TERMINAL_SUCCESS),
            "total_elapsed": "sum of every recorded attempt elapsed_ms plus retry_delay_ms; null if any elapsed is missing",
            "ttft": "ttft_ms is for the final successful attempt. first_visible_ms_including_retries measures first text observed across all attempts including waiting before it, even when the first stream later fails.",
            "ITT": "all assignments in the policy, including cache misses and requests completed only after retry",
            "verified_full_hit": "verified write in this unit followed by a warm read exactly equal to the calibrated breakpoint and zero new writes",
            "verified_pair": "both policies match the expected cache counters and the requested service tier on the same reported model identity, including the warm-up",
            "cost": "estimated usage cost from recorded token counters and frozen rates; null when model, service tier, rates or required counters are unknown; not a bill",
            "pair_delta": "right policy minus left policy for the same unit and visible input hash",
        },
    }


def render_report(summary, artifact_names=None):
    artifact_names = artifact_names or {}
    run = summary.get("run", {})
    status = summary.get("status", {})
    s = [
        "# Responses API 缓存对照报告",
        "",
        f"本轮请求模型为 `{run.get('model') or '待核实'}`，运行状态为 `{status.get('state') or '待核实'}`。实际服务档位取自每次响应的 `service_tier`。",
        "金额按实测用量与冻结费率离线核算，项目账单待核对。尝试明细保留预热、首试、失败和重试。",
        f"本轮登记 {summary['planned_units']} 个输入单元。正式分配表{'已冻结' if summary['allocation_finalized'] else '尚未完成校准'}。各组列出原计划、已尝试与最终完成数。",
        "",
        "## 运行与校准",
        "",
        f"运行结束时间为 {status.get('end_utc') or '待核实'}。记录的停止原因为 {json.dumps(status.get('stop_reason'), ensure_ascii=False) if status.get('stop_reason') else '无记录'}。",
        "",
        "| 前缀范围 | 版本 | 可见 token 计数 | 断点观测 token | 可缓存 |",
        "|---|---|---:|---:|---|",
    ]
    calibration = summary.get("calibration", {})
    for scope in sorted(calibration):
        for arm in sorted(calibration[scope]):
            row = calibration[scope][arm]
            s.append(
                f"| {summary.get('scope_labels', {}).get(scope, scope)} | {ARM_LABELS.get(arm, arm)} | "
                f"{row.get('count', '待核实')} | {row.get('boundary_tokens', '待核实')} | {row.get('eligible', '待核实')} |"
            )
    s.extend(
        [
            "",
            *readout_table(summary),
            "",
            "## 每请求策略覆盖、时间与缓存判读",
            "",
            "总耗时涵盖有计时的失败、全部重试及重试等待。这里的完成数包含服务端以 incomplete 结束的响应，例如达到输出上限。本表的 TTFT 来自最终有可见文本的响应。上方汇总表的首字时间计至客户端第一次收到可见文本，包括后来中断的流。冷、热组各自列出全部分配和服务端计数验证的子集。",
            "",
            "| 范围 | 版本 | 策略 | 计划 | 已尝试 | 首试完成 | 最终完成 | 重试请求/重试次数 | 总耗时 P50/P95 ms | TTFT P50/P95 ms | 缓存状态与策略相符/最终完成 | 正式请求合计 USD |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for scope in sorted(summary.get("groups", {})):
        for arm in sorted(summary["groups"][scope]):
            for policy in POLICIES:
                group = summary["groups"][scope][arm].get(policy)
                if not group:
                    continue
                m = group["allocation_itt"]
                policy_label = (
                    policy
                    if m.get("status") == "measured"
                    else policy + "（" + str(m.get("status") or "待测") + "）"
                )
                t = m["total_elapsed_ms_including_retries"]
                q = m["ttft_ms"]
                hits = m["cache_classification"].get(EXPECTED_CACHE[policy], 0)
                s.append(
                    f"| {summary['scope_labels'].get(scope, scope)} | {ARM_LABELS.get(arm, arm)} | {policy_label} ITT | "
                    f"{fmt_count(m['scheduled'])} | {fmt_count(m['attempted'])} | {fmt_count(m['first_attempt_completed'])} | {fmt_count(m['eventual_completed'])} | "
                    f"{fmt_retry(m)} | {fmt_pair(t)} | {fmt_pair(q)} | {fmt_hit(hits, m['eventual_completed'])} | {fmt_money(m['estimated_usage_cost_usd'].get('estimated_usage_usd'))} |"
                )
                h = group.get("verified_full_hit_subset") or group.get(
                    "verified_cold_write_subset"
                )
                if h:
                    ht = h["total_elapsed_ms_including_retries"]
                    hq = h["ttft_ms"]
                    s.append(
                        f"| {summary['scope_labels'].get(scope, scope)} | {ARM_LABELS.get(arm, arm)} | {policy} 已验证 | "
                        f"{fmt_count(h['scheduled'])} | {fmt_count(h['attempted'])} | {fmt_count(h['first_attempt_completed'])} | {fmt_count(h['eventual_completed'])} | "
                        f"{fmt_retry(h)} | {fmt_pair(ht)} | {fmt_pair(hq)} | "
                        f"{fmt_hit(h['scheduled'], h['eventual_completed'])} | {fmt_money(h['estimated_usage_cost_usd'].get('estimated_usage_usd'))} |"
                    )
    s.extend(
        [
            "",
            "完整命中要求同一实验单元的预热已验证写入，且正式热组的读取数恰等于已校准的唯一断点长度、写入数为零。其他读数保留在分配组中。",
            "",
            "## 预热与估算费用",
            "",
            "每次尝试按实际模型、服务档位和计费 token 分项核算。缺少必要字段时，该项及依赖它的合计显示为待核实。",
            "",
            "| 范围 | 版本 | 预热计划/最终完成 | 预热耗时 P50/P95 ms | 普通输入 | 缓存读 | 30m 写 | 12h 写 | 输出 | 合计估算 | 完整命中摊销 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for scope in sorted(summary.get("groups", {})):
        for arm in sorted(summary["groups"][scope]):
            group = summary["groups"][scope][arm].get("warm")
            if not group or not group.get("prewarm"):
                continue
            pre = group["prewarm"]
            c = pre["estimated_usage_cost_usd"]
            amort = (pre.get("amortization") or {}).get(
                "prewarm_estimated_usage_usd_per_verified_full_hit"
            )
            s.append(
                f"| {summary['scope_labels'].get(scope, scope)} | {ARM_LABELS.get(arm, arm)} | {fmt_count(pre['scheduled'])}/{fmt_count(pre['eventual_completed'])} | "
                f"{fmt_pair(pre['total_elapsed_ms_including_retries'])} | {fmt_money(c.get('ordinary_input_usd'))} | "
                f"{fmt_money(c.get('cache_read_usd'))} | {fmt_money(c.get('cache_write_30m_usd'))} | "
                f"{fmt_money(c.get('cache_write_12h_usd'))} | {fmt_money(c.get('output_usd'))} | "
                f"{fmt_money(c.get('estimated_usage_usd'))} | {fmt_money(amort)} |"
            )
    s.extend(
        [
            "",
            "## 同可见输入的配对差",
            "",
            "差值取右侧策略减左侧策略。ITT 保留全部计划配对。耗时与金额差只取返回相同模型标识及所请求档位的观测，热组也核对预热请求。逐组不匹配原因和数量见机读汇总中的 service_match。verified_pair 还要求两侧的读写计数均符合分配策略。",
            "",
            "| 范围 | 版本 | 配对 | 子集 | 分配数/可比耗时/TTFT/费用 | 总耗时差 P50/P95 ms | TTFT 差 P50/P95 ms | 费用差 P50/P95 USD |",
            "|---|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for scope in sorted(summary.get("paired_differences", {})):
        for arm in sorted(summary["paired_differences"][scope]):
            for pair, data in summary["paired_differences"][scope][arm].items():
                for subset in ("ITT", "verified_full_hit", "verified_pair"):
                    row = data.get(subset)
                    if not row or not row.get("n"):
                        continue
                    wait = row[
                        "total_elapsed_ms_including_retries_delta_right_minus_left"
                    ]
                    ttft = row["ttft_ms_delta_right_minus_left"]
                    cost = row["estimated_usage_cost_usd_delta_right_minus_left"]
                    s.append(
                        f"| {summary['scope_labels'].get(scope, scope)} | {ARM_LABELS.get(arm, arm)} | {pair} | {subset} | "
                        f"{row['n']}/{wait['n']}/{ttft['n']}/{cost['n']} | {fmt_pair(wait, 'median', 'p95')} | "
                        f"{fmt_pair(ttft, 'median', 'p95')} | {fmt_pair(cost, 'median', 'p95', places=6)} |"
                    )
    s.extend(
        [
            "",
            "## 口径与公开材料",
            "",
            "`requests` 按每个实验单元的请求阶段列出结果。`ledger.jsonl` 逐条保留首试、重试、失败、预热和中断。每个版本内，同一答卷同一轮次的各策略使用相同历史，可按可见输入的 SHA-256 配对。跨版本的多轮历史来自各自冻结的答卷，因此跨版本耗时差只作描述性比较。",
            "冻结费率适用于实际返回的 `gpt-6.2-astra` 或其日期快照、Standard 档位及至多 272K 输入 token。其他模型或档位、超长输入、正写入缺少 TTL 明细等情况保留空值。",
            "",
        ]
    )
    s.extend(["", "## 可核算回执小计", "",
              "按阶段统计有价格依据的回执。若有失败或缺少计费字段，小计只覆盖表中已计价的尝试。前缀计数单列。", "",
              "| 阶段 | 已记录尝试 | 可计价尝试 | 可核算小计 USD |", "|---|---:|---:|---:|"])
    stages = {"access": "接入预检", "prefix_count": "前缀计数", "calibration_prewarm": "资格试写",
              "formal": "正式请求", "prewarm": "正式热组预热"}
    for stage, row in summary["recorded_costs"].items():
        s.append(f"| {stages[stage]} | {row['recorded_attempts']} | {row['priced_attempts']} | {fmt_money(row['observed_priced_subtotal_usd'])} |")
    if artifact_names:
        s.extend(
            [
                "",
                "## 每份任务的耗时与费用",
                "",
                "同一答卷的各轮先加总再计算分位数。时间保留已完整计时的失败。金额合计含重试，任一必需用量缺失则相应合计待核实。热组末列按每次正式读取承担一次预热。",
                "",
                "| 范围 | 版本 | 策略 | 收到所有轮次 / 已记录任务 | 任务耗时 P50/P95 ms | 正式请求合计 USD | 含预热合计 USD |",
                "|---|---|---|---:|---:|---:|---:|",
            ]
        )
        for row in summary["dialogues"]:
            s.append(
                f"| {summary['scope_labels'][row['scope']]} | {ARM_LABELS[row['arm']]} | {row['policy']} | "
                f"{row['all_responses']}/{row['recorded_samples']} | {fmt_pair(row['wait'])} | "
                f"{fmt_money(row['formal_usd'])} | {fmt_money(row['including_prewarm_usd'])} |"
            )
        if not summary["dialogues"]:
            s.append("| 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |")
        s.append("")
        links = [f"[{name}]({path})" for name, path in artifact_names.items()]
        s.append(" · ".join(links))
        s.append("")
    return "\n".join(s)


def fmt_pair(value, left="median_ms", right="p95_ms", places=2):
    if not isinstance(value, dict):
        return "待核实"
    a, b = value.get(left), value.get(right)
    if a is None or b is None:
        return "待核实"
    return f"{a:.{places}f}/{b:.{places}f}"


def fmt_money(value):
    return "待核实" if value is None else f"${value:.6f}"


def fmt_count(value):
    return "待测" if value is None else str(value)


def fmt_retry(metric):
    if metric.get("retry_used") is None or metric.get("retries_total") is None:
        return "待测"
    return f"{metric['retry_used']}/{metric['retries_total']}"


def fmt_hit(hits, completed):
    if hits is None or completed is None:
        return "待测"
    return f"{hits}/{completed}"


def publish(run: Path, out: Path):
    snapshot = load_snapshot_from_run(run)
    snapshot = clean_snapshot(snapshot)
    validate_public_allocation(snapshot)
    summary = build_summary(snapshot)
    ledger = ledger_rows(snapshot)
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "snapshot.json", snapshot)
    (out / "ledger.jsonl").write_text(jsonl_text(ledger))
    write_json(out / "summary.json", summary)
    report = render_report(
        summary,
        {
            "snapshot": "snapshot.json",
            "ledger": "ledger.jsonl",
            "summary": "summary.json",
        },
    )
    (out / "report.md").write_text(report)
    input_hashes = {
        "run/manifest.json": sha_bytes((run / "manifest.json").read_bytes())
    }
    for name in ("status.json", "calibration.json", "population.json", "allocation.json"):
        path = run / name
        if path.is_file():
            input_hashes["run/" + name] = sha_bytes(path.read_bytes())
    for root in ("attempts", "units"):
        directory = run / root
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                input_hashes[f"run/{root}/{path.name}"] = sha_bytes(path.read_bytes())
    manifest = {
        "format": REPORT_FORMAT,
        "reporter_sha256": sha_bytes(Path(__file__).read_bytes()),
        "snapshot_sha256": sha_json(snapshot),
        "input_sha256": input_hashes,
        "artifacts_sha256": {
            "snapshot.json": sha_bytes((out / "snapshot.json").read_bytes()),
            "ledger.jsonl": sha_bytes((out / "ledger.jsonl").read_bytes()),
            "summary.json": sha_bytes((out / "summary.json").read_bytes()),
            "report.md": sha_bytes((out / "report.md").read_bytes()),
        },
        "state": snapshot.get("run_status", {}).get("state"),
        "model": snapshot.get("run_manifest", {}).get("model"),
        "service_tier_requested": (
            snapshot.get("run_manifest", {}).get("rules") or {}
        ).get("service_tier"),
    }
    write_json(out / "manifest.json", manifest)
    return summary


def verify(out: Path):
    manifest = read_json(out / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("format") != REPORT_FORMAT:
        raise ValueError("Invalid published manifest")
    if manifest.get("reporter_sha256") != sha_bytes(Path(__file__).read_bytes()):
        raise ValueError("Reporter source differs")
    artifacts = manifest.get("artifacts_sha256", {})
    if set(artifacts) != {"snapshot.json", "ledger.jsonl", "summary.json", "report.md"}:
        raise ValueError("Incomplete cache report artifact manifest")
    for name, expected in artifacts.items():
        path = out / name
        if not path.is_file() or sha_bytes(path.read_bytes()) != expected:
            raise ValueError("Published artifact checksum differs: " + name)
    snapshot = clean_snapshot(read_json(out / "snapshot.json"))
    if manifest.get("snapshot_sha256") != sha_json(snapshot):
        raise ValueError("Snapshot checksum differs")
    validate_public_allocation(snapshot)
    expected_ledger = jsonl_text(ledger_rows(snapshot))
    if expected_ledger != (out / "ledger.jsonl").read_text():
        raise ValueError("Ledger does not reproduce")
    expected_summary = build_summary(snapshot)
    if expected_summary != read_json(out / "summary.json"):
        raise ValueError("Summary does not reproduce")
    expected_report = render_report(
        expected_summary,
        {
            "snapshot": "snapshot.json",
            "ledger": "ledger.jsonl",
            "summary": "summary.json",
        },
    )
    if expected_report != (out / "report.md").read_text():
        raise ValueError("Report does not reproduce")
    print(
        f"Reproduced {len(snapshot.get('units', []))} units, {len(ledger_rows(snapshot))} ledger attempts and all offline metrics."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, nargs="?")
    parser.add_argument("destination", type=Path, nargs="?")
    parser.add_argument("--verify", type=Path, metavar="PUBLISHED_DIRECTORY")
    args = parser.parse_args()
    if args.verify:
        verify(args.verify)
    elif args.run and args.destination:
        publish(args.run, args.destination)
    else:
        parser.error("Supply RUN OUT, or --verify PUBLISHED_DIRECTORY")


if __name__ == "__main__":
    main()
