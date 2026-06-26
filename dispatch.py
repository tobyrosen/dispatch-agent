#!/usr/bin/env python3
"""Stateless per-call dispatch client.

One invocation equals one dispatch. No daemon, queue, lock, cache, or shared
state: callers get concurrency by running many independent processes at once.

See SKILL.md → Setup for first-time wiring instructions.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ============================================================================
# CONFIGURATION — edit this section before first use
# ============================================================================

# HTTP lane — OpenAI-compatible endpoint
# Set two environment variables in your shell profile or agent config:
#   DISPATCH_BASE_URL   provider base URL, e.g. "https://api.openai.com/v1"
#                       or a local Ollama / vLLM / LiteLLM proxy URL
#   DISPATCH_API_KEY    API key for that endpoint
#
# Map the names you will pass to --model → provider model IDs:
HTTP_MODELS = {
    # "bulk-coder":   "provider-model-id",
    # "bulk-general": "provider-model-id",
    # "reasoner":     "provider-model-id",
}

# CLI lane — a coding model accessed via a locally-installed CLI tool (optional)
# Map names → ordered fallback list of model IDs, then implement run_cli() below.
# Example:
#   CLI_MODELS = {
#       "agentic-coder": ["preferred-id", "fallback-id"],
#   }
CLI_MODELS = {}

# ============================================================================

_COMPLETIONS_PATH = "/chat/completions"


class DispatchError(Exception):
    """Expected failure; message goes to stderr and the script exits 1."""


def read_spec(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── HTTP lane ──────────────────────────────────────────────────────────────────

def run_http(model, spec, timeout):
    base_url = os.environ.get("DISPATCH_BASE_URL", "").rstrip("/")
    api_key  = os.environ.get("DISPATCH_API_KEY", "")
    if not base_url:
        raise DispatchError("DISPATCH_BASE_URL is not set")
    if not api_key:
        raise DispatchError("DISPATCH_API_KEY is not set")

    body = json.dumps({
        "model": HTTP_MODELS[model],
        "messages": [{"role": "user", "content": spec}],
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
            payload = json.loads(resp.read())
        return payload["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        raise DispatchError(
            f"HTTP {e.code}\n{e.read().decode('utf-8', errors='replace')}"
        ) from e
    except urllib.error.URLError as e:
        raise DispatchError(f"request failed: {e}") from e
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise DispatchError(f"unexpected response shape: {e}") from e


# ── CLI lane ───────────────────────────────────────────────────────────────────

def run_cli(model, spec, cwd, timeout):
    """Run a spec via a locally-installed CLI coding tool.

    Replace the subprocess.run() call with your tool's real invocation.
    The stub below shows the common pattern (spec as a positional argument);
    adapt if your tool reads from a file, env var, or stdin instead.
    Strip any header/footer wrapper lines the CLI adds around the model output.
    """
    model_id = CLI_MODELS[model][0]   # extend list in CLI_MODELS for fallback chains

    # TODO: replace this with your actual CLI invocation
    proc = subprocess.run(
        ["your-cli-tool", "exec", "--model", model_id, spec],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,     # prevent the tool from blocking on stdin
    )
    if proc.returncode != 0:
        raise DispatchError(
            f"CLI tool failed (exit {proc.returncode})\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


# ── Retry wrapper ──────────────────────────────────────────────────────────────

def run_with_retries(fn, attempts=3):
    """Retry fn() up to attempts times, only on transient errors."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except subprocess.TimeoutExpired as e:
            last = DispatchError(f"backend timed out after {e.timeout}s")
        except DispatchError as e:
            last = e
            if not any(x in str(e).lower() for x in ("timeout", "temporar", "rate", "429", " 5")):
                break   # non-transient — don't retry
        if i < attempts - 1:
            time.sleep(0.5 * (i + 1))
    raise last or DispatchError("dispatch failed")


# ── Entry point ────────────────────────────────────────────────────────────────

def parse_args(argv):
    all_models = sorted(HTTP_MODELS) + sorted(CLI_MODELS)
    ap = argparse.ArgumentParser(
        description="Run one stateless dispatch on a non-primary-agent model.",
        epilog="Add model IDs to HTTP_MODELS / CLI_MODELS at the top of this file.",
    )
    ap.add_argument("--model", required=True,
                    choices=all_models if all_models else None,
                    help="model name registered in HTTP_MODELS or CLI_MODELS")
    ap.add_argument("--spec", required=True,
                    help='path to spec file, or "-" to read from stdin')
    ap.add_argument("--cwd", default=os.getcwd(),
                    help="working directory for CLI-lane subprocesses")
    ap.add_argument("--timeout", type=int, default=1800,
                    help="per-call timeout in seconds (default: 1800)")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    if args.timeout <= 0:
        raise DispatchError("--timeout must be positive")

    spec = read_spec(args.spec)
    if not spec.strip():
        raise DispatchError("empty spec")

    if args.model in CLI_MODELS:
        output = run_with_retries(lambda: run_cli(args.model, spec, args.cwd, args.timeout))
    else:
        output = run_with_retries(lambda: run_http(args.model, spec, args.timeout))

    sys.stdout.write(output)
    if output and not output.endswith("\n"):
        sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except DispatchError as e:
        print(f"dispatch.py: {e}", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired as e:
        print(f"dispatch.py: timed out after {e.timeout}s", file=sys.stderr)
        sys.exit(1)
