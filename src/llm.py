"""OpenRouter chat client (stdlib only) with a generic tool loop and a real-dollar cost tally.
Key: $OPENROUTER_API_KEY or ~/.config/agent-integrity/openrouter.key"""
import json
import os
import pathlib
import threading
import time
import urllib.error
import urllib.request

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
RETRIES, TIMEOUT = 5, 180

MODELS = {
    "proposer": os.environ.get("PG_PROPOSER_MODEL", "google/gemini-2.5-flash"),
    "solver": os.environ.get("PG_SOLVER_MODEL", "openai/gpt-4.1-mini"),
    "breaker": os.environ.get("PG_BREAKER_MODEL", "deepseek/deepseek-chat-v3-0324"),
}


def _key():
    k = os.environ.get("OPENROUTER_API_KEY")
    if k:
        return k.strip()
    p = pathlib.Path.home() / ".config/agent-integrity/openrouter.key"
    if p.exists():
        return p.read_text().strip()
    raise RuntimeError("No OpenRouter key: set $OPENROUTER_API_KEY or write ~/.config/agent-integrity/openrouter.key")


class Cost:
    def __init__(self):
        self.usd, self.tokens, self.calls = 0.0, 0, 0
        self.lock = threading.Lock()

    def add(self, usage):
        with self.lock:
            self.calls += 1
            if usage:
                self.usd += usage.get("cost") or 0.0
                self.tokens += usage.get("total_tokens") or 0

    def snapshot(self):
        return {"usd": round(self.usd, 4), "tokens": self.tokens, "calls": self.calls}


COST = Cost()


def chat(model, messages, tools=None, max_tokens=1200, temperature=0.0):
    """Returns (assistant_message_dict, usage). Raises RuntimeError on terminal failure."""
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
            "usage": {"include": True}}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    data = json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json",
               "HTTP-Referer": "https://localhost/proving-ground", "X-Title": "proving-ground"}
    last = ""
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(ENDPOINT, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.load(r)
            if "error" in d:
                last = str(d["error"].get("message", d["error"]))
                if "rate" not in last.lower() and "overloaded" not in last.lower():
                    raise RuntimeError(last)
            else:
                usage = d.get("usage") or {}
                COST.add(usage)
                msg = d["choices"][0]["message"]
                if not (msg.get("content") or msg.get("tool_calls")) and d["choices"][0].get("finish_reason") == "length" and max_tokens < 8000:
                    return chat(model, messages, tools, max_tokens * 4, temperature)
                return msg, usage
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in (408, 429, 500, 502, 503, 504):
                try:
                    last = json.load(e).get("error", {}).get("message", last)
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(last)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = str(e)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"{last} after {RETRIES} tries")


def fit(result, cap=30000):
    """Serialise a tool result within cap characters without producing broken JSON."""
    text = json.dumps(result)
    if len(text) <= cap:
        return text
    if isinstance(result, list):
        keep = list(result)
        while keep and len(json.dumps(keep)) > cap - 200:
            keep.pop()
        return json.dumps({"results": keep, "truncated": True, "returned": len(keep), "note": "more results exist; narrow the search with filters"})
    return json.dumps({"truncated": True, "partial": text[:cap - 200]})


def tool_loop(model, system, user, tools, dispatch, terminal, max_steps=14, max_tokens=1200):
    """Run a tool-using agent until it calls a terminal tool, stops calling tools, or hits max_steps.
    dispatch(name, args) -> JSON-serialisable result. Returns a transcript dict."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    steps, usd, tokens, ended_by = [], 0.0, 0, None
    for _ in range(max_steps):
        msg, usage = chat(model, messages, tools, max_tokens=max_tokens)
        usd += usage.get("cost") or 0.0
        tokens += usage.get("total_tokens") or 0
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        calls = msg.get("tool_calls") or []
        if not calls:
            ended_by = "final_message"
            break
        for tc in calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = dispatch(name, args)
            except Exception as e:  # noqa: BLE001 - the agent sees API errors as tool results
                result = {"error": str(e)}
            steps.append({"tool": name, "args": args, "result": result})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": fit(result)})
            if name in terminal and not (isinstance(result, dict) and result.get("error")):
                ended_by = name
        if ended_by:
            break
    else:
        ended_by = "max_steps"
    return {"model": model, "ended_by": ended_by, "steps": steps, "usd": round(usd, 5), "tokens": tokens,
            "final": messages[-1].get("content") if messages[-1]["role"] == "assistant" else None}
