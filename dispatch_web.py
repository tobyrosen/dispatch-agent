#!/usr/bin/env python3
"""Agentic web-research dispatch.

Sibling to dispatch.py. Where dispatch.py runs one stateless chat-completion,
this runs a bounded agent loop: the model gets web_search / web_fetch tools,
browses for verified current results, and returns a final answer.

CLI interface:
  --model <id>     model ID registered in CHAT_MODELS (must support tool calls)
  --spec <path|->  spec file path, or "-" for stdin
  --timeout <s>    per-HTTP-call timeout (default 300)
  --max-iters <n>  agent-loop iteration cap (default 15)
  --no-trace       suppress the tool-call trace on stderr

Final answer → stdout. Tool-call trace → stderr. Non-zero exit on failure.

See SKILL.md → Setup and the TODO in _web_search() for wiring your search provider.
No third-party dependencies — stdlib only.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# ============================================================================
# CONFIGURATION — same env vars as dispatch.py
# ============================================================================
# DISPATCH_BASE_URL  — OpenAI-compatible base URL
# DISPATCH_API_KEY   — API key
#
# Only register models that support function/tool calling — verify with your
# provider before relying on web-browsing mode.
CHAT_MODELS = {
    # "web-researcher": "provider-model-id",
}
_COMPLETIONS_PATH = "/chat/completions"
# ============================================================================


class DispatchError(Exception):
    """Expected failure; message goes to stderr and the script exits 1."""


def read_spec(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── Tool schemas ──────────────────────────────────────────────────────────────
# These describe the interface advertised to the model.
# Actual execution is in execute_tool() below.

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current information. Returns results with title, URL, "
            "and a snippet. Call web_fetch on promising URLs to confirm they resolve."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query":       {"type": "string",  "description": "the search query"},
                "max_results": {"type": "integer", "description": "max results, <=10, default 5"},
            },
            "required": ["query"],
        },
    },
}

WEB_FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "Fetch the content of a URL. Returns the page content. "
            "Use this to CONFIRM a URL resolves before citing it, and to read "
            "real content rather than a search snippet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "absolute URL to fetch"},
            },
            "required": ["url"],
        },
    },
}

TOOLS = [WEB_SEARCH_TOOL, WEB_FETCH_TOOL]


# ── Tool execution ─────────────────────────────────────────────────────────────

def execute_tool(name, args, timeout):
    """Execute a web tool call and return a JSON-serializable dict.

    Failures return {"error": ...} rather than raising — a dead URL or empty
    search result is information the model should see, not a crash.
    """
    try:
        if name == "web_search":
            max_r = max(1, min(int(args.get("max_results", 5) or 5), 10))
            return _web_search(args.get("query", ""), max_r, timeout)
        if name == "web_fetch":
            return _web_fetch(args.get("url", ""), timeout)
        return {"error": f"unknown tool: {name}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:400]}"}


def _web_search(query, max_results, timeout):
    """Search the web. Returns {"results": [{"title", "url", "content"}, ...]}.

    TODO: replace this stub with a real search API. Common options:

      Serper (https://serper.dev) — fast Google results:
        POST https://google.serper.dev/search
        Header:  X-API-KEY: os.environ["SERPER_API_KEY"]
        Body:    {"q": query, "num": max_results}
        Extract: response["organic"] → title/link/snippet

      Tavily (https://tavily.com) — built for AI agents, returns clean content:
        POST https://api.tavily.com/search
        Body:    {"api_key": os.environ["TAVILY_API_KEY"],
                  "query": query, "max_results": max_results}
        Extract: response["results"] → title/url/content

      SerpAPI (https://serpapi.com) — broad engine support:
        GET https://serpapi.com/search.json
        Params:  q=query, num=max_results, api_key=os.environ["SERPAPI_KEY"]
        Extract: response["organic_results"] → title/link/snippet

    Normalize each result to {"title": ..., "url": ..., "content": ...}.
    """
    raise DispatchError(
        "web_search is not wired up — see the TODO comment in _web_search()"
    )


def _web_fetch(url, timeout):
    """Fetch a URL and return its content.

    Returns the raw HTTP response body truncated to 6000 chars. For cleaner
    Markdown output, prepend https://r.jina.ai/ to the URL — Jina Reader
    converts pages to clean Markdown without needing an HTML parser.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "dispatch-web/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        return {"url": url, "content": content[:6000]}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {url}"}
    except urllib.error.URLError as e:
        return {"error": f"fetch failed: {e}"}


# ── Chat and agent loop ────────────────────────────────────────────────────────

def _chat(messages, model_id, timeout):
    base_url = os.environ.get("DISPATCH_BASE_URL", "").rstrip("/")
    api_key  = os.environ.get("DISPATCH_API_KEY", "")
    if not base_url:
        raise DispatchError("DISPATCH_BASE_URL is not set")
    if not api_key:
        raise DispatchError("DISPATCH_API_KEY is not set")

    body = json.dumps({
        "model": model_id,
        "messages": messages,
        "tools": TOOLS,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url + _COMPLETIONS_PATH,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise DispatchError(
            f"HTTP {e.code}\n{e.read().decode('utf-8', errors='replace')}"
        ) from e
    except urllib.error.URLError as e:
        raise DispatchError(f"request failed: {e}") from e


def run_agent(model, spec, timeout, max_iters, trace):
    model_id = CHAT_MODELS[model]
    messages = [
        {"role": "system", "content": (
            "You are an agentic web researcher. Use the tools provided to gather current, "
            "live information — do not answer from memory alone. Workflow: web_search to find "
            "candidate pages, then web_fetch to CONFIRM each URL resolves before citing it. "
            "Only cite URLs you have actually fetched. When you have enough verified information, "
            "stop calling tools and write your final answer."
        )},
        {"role": "user", "content": spec},
    ]

    tool_call_count = 0
    for i in range(max_iters):
        try:
            resp = _chat(messages, model_id, timeout)
        except DispatchError:
            raise
        except Exception as e:
            raise DispatchError(f"chat call failed on iter {i + 1}: {e}") from e

        try:
            msg = resp["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise DispatchError(f"unexpected response shape on iter {i + 1}: {e}") from e

        content    = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []

        messages.append(msg)   # preserve the assistant turn for the next call

        if not tool_calls:
            if trace:
                sys.stderr.write(f"[iter {i + 1}] final answer ({tool_call_count} tool calls)\n")
            return content, tool_call_count

        for tc in tool_calls:
            fn      = tc.get("function", {})
            name    = fn.get("name", "")
            raw     = fn.get("arguments", "{}")
            args    = raw if isinstance(raw, dict) else json.loads(raw)
            call_id = tc.get("id", "")
            tool_call_count += 1

            if trace:
                sys.stderr.write(f"[iter {i + 1}] TOOL_CALL {name} {json.dumps(args)[:200]}\n")
            result = execute_tool(name, args, timeout)
            if trace:
                if "results" in result:
                    summary = f"{len(result['results'])} results"
                elif "content" in result:
                    summary = f"fetched ({len(result.get('content', ''))} chars)"
                elif "error" in result:
                    summary = f"ERROR: {result['error'][:120]}"
                else:
                    summary = str(result)[:80]
                sys.stderr.write(f"            -> {summary}\n")

            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": json.dumps(result),
            })

    raise DispatchError(
        f"agent loop hit the {max_iters}-iteration cap without a final answer "
        f"({tool_call_count} tool calls). Raise --max-iters or tighten the spec."
    )


# ── Entry point ────────────────────────────────────────────────────────────────

def parse_args(argv):
    models = sorted(CHAT_MODELS)
    ap = argparse.ArgumentParser(
        description="Run one agentic web-research dispatch.",
        epilog="Add model IDs to CHAT_MODELS at the top of this file.",
    )
    ap.add_argument("--model", required=True,
                    choices=models if models else None)
    ap.add_argument("--spec", required=True,
                    help='path to spec file, or "-" for stdin')
    ap.add_argument("--timeout", type=int, default=300,
                    help="per-HTTP-call timeout in seconds (default: 300)")
    ap.add_argument("--max-iters", type=int, default=15,
                    help="agent-loop iteration cap (default: 15)")
    ap.add_argument("--no-trace", action="store_true",
                    help="suppress the tool-call trace on stderr")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    if args.timeout <= 0:
        raise DispatchError("--timeout must be positive")
    if args.max_iters <= 0:
        raise DispatchError("--max-iters must be positive")

    spec = read_spec(args.spec)
    if not spec.strip():
        raise DispatchError("empty spec")

    output, n_calls = run_agent(
        args.model, spec, args.timeout, args.max_iters, trace=not args.no_trace
    )
    if not args.no_trace:
        sys.stderr.write(f"dispatch_web.py: done — {n_calls} tool calls executed\n")

    sys.stdout.write(output)
    if output and not output.endswith("\n"):
        sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except DispatchError as e:
        print(f"dispatch_web.py: {e}", file=sys.stderr)
        sys.exit(1)
