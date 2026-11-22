"""Measure a single Responses API attempt in a user-started local process.

The only remote endpoint is the official Responses endpoint over HTTPS. Records
contain allowlisted error codes, model usage and monotonic timings. Headers,
provider error messages, account IDs, and generated or reasoning text stay out
of the public ledger. Redirects are never followed with authentication.
"""

import datetime
import hashlib
import http.client
import json
import time
import urllib.error
import urllib.request

ENDPOINT = "https://api.openai.com/v1/responses"
ERRORS = {
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
}


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def mark_error(record, detail):
    value = detail if isinstance(detail, dict) else {}
    code = value.get("code")
    record["error"] = code if code in ERRORS else "provider_error"
    param = value.get("param")
    if param in (
        "prompt_cache_options",
        "prompt_cache_options.prewarm",
        "prompt_cache_key",
        "model",
        "service_tier",
        "max_output_tokens",
        "reasoning.effort",
    ):
        record["parameter"] = param


def extract(record, response):
    record["model"] = response.get("model")
    record["service_tier"] = response.get("service_tier")
    record["status"] = response.get("status")
    if record["status"] == "failed":
        mark_error(record, response.get("error"))
    limits = response.get("incomplete_details") or {}
    if limits.get("reason") in ("max_output_tokens", "content_filter"):
        record["incomplete_reason"] = limits["reason"]
    usage = response.get("usage") or {}
    inputs = usage.get("input_tokens_details") or {}
    ttl = inputs.get("cache_write_tokens_details") or {}
    outputs = usage.get("output_tokens_details") or {}
    values = {
        "input": usage.get("input_tokens"),
        "output": usage.get("output_tokens"),
        "read": inputs.get("cached_tokens"),
        "write": inputs.get("cache_write_tokens"),
        "write_30m": ttl.get("ttl_30m_tokens"),
        "write_12h": ttl.get("ttl_12h_tokens"),
        "reasoning": outputs.get("reasoning_tokens"),
    }
    record["usage"] = {k: v for k, v in values.items() if type(v) is int and v >= 0}


def sse_events(stream):
    data = []
    for raw in stream:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        if not line and data:
            payload = "\n".join(data)
            data = []
            if payload != "[DONE]":
                yield json.loads(payload)
    if data and "\n".join(data) != "[DONE]":
        yield json.loads("\n".join(data))


def request(key, body, counting=False):
    url = ENDPOINT + ("/input_tokens" if counting else "")
    data = json.dumps(body, ensure_ascii=False).encode()
    message = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "clarify-to-plan-cache-benchmark/1",
        },
    )
    opener = urllib.request.build_opener(NoRedirect())
    record = {
        "utc": utc(),
        "http": None,
        "status": "failed",
        "ttft_ms": None,
        "usage": {},
    }
    start = time.monotonic()
    try:
        with opener.open(message, timeout=150) as resp:
            record["http"] = resp.status
            if counting:
                value = json.load(resp).get("input_tokens")
                if type(value) is int and value >= 0:
                    record.update(status="completed", input_tokens=value)
                else:
                    record["error"] = "missing_count"
            elif body.get("stream"):
                text_hash = hashlib.sha256()
                chars = 0
                for event in sse_events(resp):
                    name = event.get("type")
                    if name == "response.created":
                        record["created_ms"] = round((time.monotonic() - start) * 1000)
                    elif name == "response.output_text.delta" and event.get("delta"):
                        if record["ttft_ms"] is None:
                            record["ttft_ms"] = round((time.monotonic() - start) * 1000)
                        chars += len(event["delta"])
                        text_hash.update(event["delta"].encode())
                    elif name in (
                        "response.completed",
                        "response.incomplete",
                        "response.failed",
                    ):
                        extract(record, event["response"])
                        break
                    elif name == "error":
                        mark_error(record, event)
                        break
                if chars:
                    record.update(
                        answer_chars=chars, answer_sha256=text_hash.hexdigest()
                    )
                if record["status"] == "failed" and "error" not in record:
                    record["error"] = "stream_ended"
            else:
                extract(record, json.load(resp))
    except urllib.error.HTTPError as e:
        record["http"] = e.code
        try:
            mark_error(record, json.load(e).get("error"))
        except (ValueError, AttributeError, OSError):
            record["error"] = "provider_error"
    except (OSError, http.client.HTTPException, ValueError, KeyError) as e:
        record["error"] = (
            "transport"
            if isinstance(e, (OSError, http.client.HTTPException))
            else "unparseable_response"
        )
    record["elapsed_ms"] = round((time.monotonic() - start) * 1000)
    return record
