# Dispatch Agent

Dispatch one self-contained task to Codex, Cursor, Ollama, OpenRouter, or an OpenAI-compatible HTTP endpoint. `dispatch.py` prints launch metadata followed by the result; supported write lanes can also edit files. Check the files and tests afterward: a successful model response does not prove the task is complete.

## Step 0: install and prepare a task

Use Python **3.11 or newer**. Check the exact interpreter you will use:

```sh
python3.11 --version
```

If `python3` reports 3.9 (common on older macOS installations), install Python 3.11 or newer from [python.org](https://www.python.org/downloads/) and use `python3.11` explicitly. Python 3.9 cannot import the required standard-library `tomllib` module.

Clone the repo, then install the package and optional web dependency in its local virtual environment.

```sh
git clone https://github.com/tobyrosen/dispatch-agent.git
cd dispatch-agent
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[web]'
mkdir -p ~/.config/dispatch-agent
cp config.example.toml ~/.config/dispatch-agent/config.toml
.venv/bin/python dispatch.py --help
```

The config file contains example aliases and environment-variable **names**, not credentials. The commands below select a backend and model explicitly, so you can try them before editing the aliases. If you prefer an existing Python 3.11 environment, `python3.11 -m pip install '.[web]'` works from the clone; keep the repo-local `.venv` for the Ollama write handoff.

Create a small task in the repo root:

```sh
cat > task.md <<'EOF'
Create out/slugify.py with a slugify(title) function that lowercases ASCII words,
replaces runs of spaces or punctuation with one hyphen, and strips edge hyphens.
Create out/test_slugify.py with three unittest cases for normal words, repeated
punctuation, and empty input. Report the files you wrote. If command execution
is available, run the tests and report the result.

## Grants
paths-write: out/**
network: no
github-writes: no
tools: file editing and Python unittest
EOF
mkdir -p out
```

Each backend needs its own CLI login or API key and a model available to your account. `--dry-run` prints a plan without launching a worker or contacting a provider. Its preflight checks whether a required executable is on `PATH` and whether a required key environment variable is set; it does not test login, endpoint reachability, model access, or task success. Use `--no-preflight` with `--dry-run` to skip those prerequisite checks and still print the plan. The plan shows only the prompt byte count and SHA-256 hash. For HTTP backends, preflight shows only the endpoint origin (`scheme://host[:port]`), never its path, query, or userinfo.

## Write-mode examples

Run one backend example. Inspect its dry-run plan, then run its write command. Create this checker once in the repo root; every write example below calls it. It exits with a failure if either expected file is missing, fewer than three tests are discovered, or a test fails.

```sh
cat > check_out.py <<'PY'
from pathlib import Path
import sys
import unittest

root = Path("out")
if not (root / "slugify.py").is_file() or not (root / "test_slugify.py").is_file():
    print("Missing out/slugify.py or out/test_slugify.py", file=sys.stderr)
    raise SystemExit(1)
suite = unittest.defaultTestLoader.discover("out", pattern="test_*.py")
if suite.countTestCases() < 3:
    print("Expected at least three discovered tests", file=sys.stderr)
    raise SystemExit(1)
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
PY
```

Also read `out/slugify.py` and `out/test_slugify.py`; passing tests do not prove the model followed every instruction.

### Codex CLI

Install the [Codex CLI](https://learn.chatgpt.com/docs/developer-commands?surface=cli) if needed. With Node.js/npm installed, run `npm install -g @openai/codex`. The public ID `gpt-6-sol` is an example; use `codex`, then `/model`, to choose a model available to your account before the live run. The dry-run works before login. Sign in before the live command. The first line below signs in only when you aren't already signed in, because `codex login` replaces an existing login; on a headless machine, use `codex login --device-auth` instead of `codex login`.

```sh
.venv/bin/python dispatch.py --backend codex --model gpt-6-sol --spec task.md --cwd "$PWD" --write --dry-run
codex login status || codex login
.venv/bin/python dispatch.py --backend codex --model gpt-6-sol --spec task.md --cwd "$PWD" --write
.venv/bin/python check_out.py
```

`--web` enables Codex search. `--network` requires `--write` and requests network access in Codex's execution sandbox. This task declares `network: no`, so leave both off.

### Cursor CLI

Install the [Cursor CLI](https://docs.cursor.com/en/cli/installation) with `curl https://cursor.com/install -fsS | bash` and make sure `agent` is on `PATH`. `YOUR_CURSOR_BASE` can be a placeholder for the offline dry-run; after login, use `agent models` to choose an available model base that supports an effort suffix. Dispatch appends `-high` by default. Replace the placeholder before the live run.

```sh
.venv/bin/python dispatch.py --backend cursor --model YOUR_CURSOR_BASE --spec task.md --cwd "$PWD" --write --dry-run
agent status || agent login
agent models
.venv/bin/python dispatch.py --backend cursor --model YOUR_CURSOR_BASE --spec task.md --cwd "$PWD" --write
.venv/bin/python check_out.py
```

If the listed model does not have a compatible effort variant, choose one that does or configure its supported efforts in `config.toml`. `--effort low|medium|high|xhigh` overrides the default where supported.

### Ollama local

Install [Ollama](https://ollama.com/download). In terminal 1, run `ollama serve` and leave it running. If the desktop app already started the service, use that instead. In **terminal 2**, from this repo:

```sh
ollama pull qwen3:4b
ollama list
.venv/bin/python dispatch.py --backend ollama --model qwen3:4b --spec task.md --cwd "$PWD" --write --dry-run
.venv/bin/python dispatch.py --backend ollama --model qwen3:4b --spec task.md --cwd "$PWD" --write
.venv/bin/python check_out.py
```

The default endpoint is `http://127.0.0.1:11434`; no API key is needed there. For a server deliberately listening on port 11435, pass `--base-url http://127.0.0.1:11435` to both Dispatch commands. The port must match the server. Ollama and OpenRouter write mode use the `dispatch_web.py` tool loop and need a model that reliably calls tools.

### Ollama Cloud

Create an Ollama API key, set the default `OLLAMA_API_KEY` environment variable, and choose a cloud model your account can use. Treat the model ID below as an example and confirm access before a live run.

```sh
export OLLAMA_API_KEY='paste-your-key-here'
.venv/bin/python dispatch.py --backend ollama-cloud --model qwen3-coder:480b-cloud --spec task.md --cwd "$PWD" --write --dry-run
.venv/bin/python dispatch.py --backend ollama-cloud --model qwen3-coder:480b-cloud --spec task.md --cwd "$PWD" --write
.venv/bin/python check_out.py
```

If your key is in another variable, add `--api-key-env YOUR_VARIABLE` to both commands. `dispatch_web.py` can also run Ollama's hosted search and fetch tools with a tool-capable model.

### OpenRouter

Create an OpenRouter API key. Treat the model below as an example; select a tool-capable slug available to your account before a live run. Dispatch requests zero data retention, denies provider data collection, and disables fallback routing.

```sh
export OPENROUTER_API_KEY='paste-your-key-here'
.venv/bin/python dispatch.py --backend openrouter --model openai/gpt-5.6-luna --spec task.md --cwd "$PWD" --write --dry-run
.venv/bin/python dispatch.py --backend openrouter --model openai/gpt-5.6-luna --spec task.md --cwd "$PWD" --write
.venv/bin/python check_out.py
```

For a `:free` slug, Dispatch also refuses a response that reports nonzero cost. Check current slugs in OpenRouter's model catalog.

### OpenAI-compatible HTTP

This backend is **text-only** and rejects `--write`. It can generate a file for you to save under your own control. Set a real HTTPS `/v1` root, a model ID from that provider, and the environment variable holding its API key. This example asks for one Python file, saves stdout, and removes the launch-metadata line; review the extracted source before running it. If your server is local, a loopback HTTP URL is also allowed.

```sh
export MY_API_KEY='paste-your-key-here'
cat > compatible-task.md <<'EOF'
Return only Python source code for a slugify(title) function that lowercases
ASCII words, replaces runs of spaces or punctuation with one hyphen, and
strips edge hyphens. No Markdown fences or explanation.

## Grants
paths-write: none
network: no
github-writes: no
tools: none
EOF
.venv/bin/python dispatch.py --backend openai-compatible --model YOUR_MODEL --base-url https://YOUR_HOST/v1 --api-key-env MY_API_KEY --spec compatible-task.md --dry-run
.venv/bin/python dispatch.py --backend openai-compatible --model YOUR_MODEL --base-url https://YOUR_HOST/v1 --api-key-env MY_API_KEY --spec compatible-task.md > out/response.txt
.venv/bin/python - <<'PYCODE'
from pathlib import Path

header, separator, source = Path("out/response.txt").read_text().partition("\n")
if not header.startswith("dispatch.py launch: ") or not separator or not source.strip():
    raise SystemExit("Unexpected dispatch output; inspect out/response.txt")
Path("out/slugify.py").write_text(source)
PYCODE
.venv/bin/python -m py_compile out/slugify.py
```

The client appends `/chat/completions`. Confirm the output is only valid source before importing or executing it. This example does not produce the two-file task above because this backend has no file tools.

## Config and flag reference

Copy `config.example.toml` as shown in Step 0. Define aliases under `[models.<alias>]` and provider defaults under `[providers.<backend>]`. CLI values override config. `[general].cooldown_seconds` optionally records a local pause after rate-limit failures; `--ignore-cooldown` bypasses an active marker.

| Flag | Meaning |
|---|---|
| `--model`, `--spec` | Required alias or model ID and task file; `--spec -` reads stdin. |
| `--backend`, `--model-id` | Backend and provider model ID when an alias does not supply them. |
| `--base-url`, `--api-key-env` | HTTP API root and **name** of the key environment variable. |
| `--cwd` | Working directory for CLI workers and the Ollama/OpenRouter write handoff. |
| `--dry-run` | Show the selected model, working directory, prompt byte count and hash, and local preflight checks without launching. HTTP endpoints appear as origins only. |
| `--no-preflight` | With `--dry-run`, skip local executable and key-variable checks while still printing the plan. |
| `--write` | Enable editing on supported backends; rejected for generic OpenAI-compatible HTTP. |
| `--unsafe-shell` | Enable the unconfined `run_command` shell in an HTTP write loop. It prints a warning. For untrusted tasks, run Dispatch inside a container or VM. |
| `--pass-env NAME` | Pass an additional named environment variable to a CLI child; repeat as needed. |
| `--timeout`, `--idle-timeout` | Per-call timeout and Cursor idle timeout, in seconds. |
| `--max-iters` | Ollama/OpenRouter write-loop iteration limit. |
| `--effort` | Backend reasoning-effort override where supported. |
| `--fast`, `--directive` | Fast mode for Codex/Cursor. `--directive` records optional audit text; the public configuration does not require it. |
| `--web`, `--network` | Codex search and Codex execution-sandbox network access; `--network` requires `--write`. |
| `--images` | JPEG, PNG, or WebP input for a configured vision-capable Ollama model; install Pillow first with `.venv/bin/python -m pip install Pillow`. |
| `--ignore-cooldown` | Bypass an active local cooldown marker. |

`--no-fast` is a compatibility no-op. `--reasoning`, `--task-class`, and `--ignore-lane-state` are compatibility options for local extensions; the standalone backends ignore them. The public cooldown setting uses `--ignore-cooldown`. `dispatch_web.py` also accepts `--write-dir` (repeatable), `--no-trace`, and the model, backend, endpoint, key-variable, timeout, and iteration options; the shell flag applies to its file-tool loop.

A `## Grants` block declares `paths-write`, `network`, `github-writes`, and `tools`. `dispatch.py` checks contradictions at launch and logs the decision. Grants are advisory, not filesystem or network enforcement. Direct `dispatch_web.py` calls do not perform these checks and have no `--dry-run` option. Its OpenRouter loop requires a file-tool root and has no hosted web search/fetch; the Ollama loop provides those hosted tools, which need API authentication even when the model runs locally.

## What is and isn't sandboxed

Codex and Cursor use their own sandbox options. Their actual isolation depends on those CLIs and the permissions you grant. They keep your real `HOME` because their login and settings live there. `--web` enables Codex search; `--network` requests network access inside Codex's execution sandbox.

If Codex reports `sandbox_apply: Operation not permitted`, rerun it from a writable environment without a nested sandbox.

Public HTTP child processes receive a small environment allowlist and an isolated, temporary `HOME` by default. Use `--pass-env NAME` only for variables the child needs. Ollama and OpenRouter file tools restrict their file operations to declared `--write-dir` roots and reject symlink traversal. The `dispatch.py --write` handoff uses all of `--cwd` as that root; narrower spec globs remain advisory. The `## Grants` block is advisory validation, not an operating-system sandbox.

`run_command` is off by default. If you enable it with `--unsafe-shell` or `[general].allow_shell_tool = true`, it runs an **unconfined shell**. `--write-dir` only sets its working directory; the shell can access other files and networks allowed to your account. Run untrusted tasks inside a container or VM.

Built-in HTTP requests allow HTTPS or numeric loopback HTTP, reject URL userinfo, disable redirects, and cap response bodies. The Ollama SDK host receives the same URL validation before client creation, but the SDK handles its own redirects and response reads. The hosted Ollama `web_fetch` tool validates the initial URL and DNS answers before passing the URL to the service. That service resolves DNS and follows redirects on its side, outside this tool's control.

`--dry-run` makes no backend request. Preflight shows only an endpoint's origin and whether a required key variable is set, never a key value. A dry-run cannot prove credentials, model access, or task completion.
