"""Run text-only, ephemeral Codex conversations through the local app-server.

The caller supplies model-visible instructions and user messages. Only public
answer text, timing and token counters leave this module. No account records,
session IDs, credentials or reasoning text are returned.
"""

import json
import queue
import subprocess
import tempfile
import threading
import time
from collections import deque


class RunError(RuntimeError):
    """A failed transport, isolation check or model turn."""


def category(value):
    text = str(value).lower()
    for match, label in (("limit", "rate_or_usage_limit"), ("quota", "quota"),
                         ("model_not_found", "model_unavailable"), ("not supported", "unsupported"),
                         ("unauthorized", "unauthenticated"), ("timeout", "timeout")):
        if match in text:
            return label
    return "codex_error"


class Client:
    def __init__(self, binary, blocked_paths=(), timeout=150):
        self.tmp = tempfile.TemporaryDirectory(prefix="clarify-benchmark-")
        self.timeout = timeout
        args = [binary, "app-server", "--listen", "stdio://"]
        for flag in ("plugins", "apps", "memories", "shell_tool", "multi_agent"):
            args += ["--disable", flag]
        args += ["-c", 'web_search="disabled"']
        if blocked_paths:
            entries = ["{path=" + json.dumps(p, ensure_ascii=False) + ",enabled=false}" for p in blocked_paths]
            args += ["-c", "skills.config=[" + ",".join(entries) + "]"]
        self.proc = subprocess.Popen(args, cwd=self.tmp.name, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.queue = queue.Queue()
        self.pending = deque()
        self.serial = 0
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.rpc("initialize", {"clientInfo": {"name": "clarify-benchmark", "version": "1"},
                                    "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.proc.stdout:
                self.queue.put(json.loads(line))
        finally:
            self.queue.put(None)

    def send(self, value):
        self.proc.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def next(self, deadline):
        try:
            event = self.queue.get(timeout=max(.01, deadline - time.monotonic()))
        except queue.Empty:
            raise RunError("timeout") from None
        if event is None:
            raise RunError("codex_exited")
        if "method" in event and "id" in event:
            self.send({"id": event["id"], "error": {"code": -32601, "message": "Text-only benchmark"}})
            raise RunError("unexpected_server_request")
        return event

    def rpc(self, method, params):
        self.serial += 1
        self.send({"id": self.serial, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            event = self.next(deadline)
            if event.get("id") == self.serial:
                if "error" in event:
                    raise RunError(method + ":" + category(event["error"]))
                return event["result"]
            if event.get("params", {}).get("threadId"):
                self.pending.append(event)
        raise RunError("timeout")

    def catalog(self):
        result = self.rpc("skills/list", {"cwds": [self.tmp.name], "forceReload": True})
        return [s for group in result["data"] for s in group["skills"]]

    def start(self, model, instructions):
        self.pending.clear()
        result = self.rpc("thread/start", {
            "cwd": self.tmp.name, "model": model, "allowProviderModelFallback": False,
            "baseInstructions": "你是一名中文助手。请按本次指令作答。",
            "developerInstructions": instructions, "ephemeral": True,
            "environments": [], "selectedCapabilityRoots": [], "dynamicTools": [],
            "approvalPolicy": "never", "sandbox": "read-only",
        })
        if result["model"] != model:
            raise RunError("model_mismatch")
        return result["thread"]["id"]

    def turn(self, thread, prompt, effort, schema=None):
        params = {"threadId": thread, "input": [{"type": "text", "text": prompt}],
                  "effort": effort, "environments": []}
        if schema:
            params["outputSchema"] = schema
        started = time.monotonic()
        deadline = started + self.timeout
        turn_id = self.rpc("turn/start", params)["turn"]["id"]
        answer = ""
        first_visible = None
        usage = None
        while time.monotonic() < deadline:
            event = self.pending.popleft() if self.pending else self.next(deadline)
            method, data = event.get("method"), event.get("params", {})
            if data.get("threadId") != thread or data.get("turnId", turn_id) != turn_id:
                continue
            if method == "model/rerouted":
                raise RunError("model_rerouted")
            if method == "item/agentMessage/delta" and data.get("delta") and first_visible is None:
                first_visible = round((time.monotonic() - started) * 1000)
            if method == "thread/tokenUsage/updated":
                last = data["tokenUsage"]["last"]
                usage = {k: last[k] for k in ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens",
                                               "outputTokens", "reasoningOutputTokens") if k in last}
            if method == "item/completed":
                item = data["item"]
                if item["type"] == "agentMessage" and item.get("phase") != "commentary":
                    answer = item.get("text", "")
                elif item["type"] not in ("agentMessage", "reasoning", "userMessage"):
                    raise RunError("unexpected_item:" + item["type"])
            if method == "turn/completed" and data["turn"]["id"] == turn_id:
                t = data["turn"]
                if t["status"] != "completed" or not answer.strip():
                    raise RunError(category(t.get("error") or t["status"]))
                return {"answer": answer.strip(), "elapsed_ms": round((time.monotonic() - started) * 1000),
                        "first_visible_ms": first_visible, "usage": usage}
        raise RunError("timeout")

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.tmp.cleanup()
