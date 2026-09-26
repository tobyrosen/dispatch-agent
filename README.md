# Dispatch Agent

Dispatch one self-contained task to Codex, Cursor, Ollama, OpenRouter, or an OpenAI-compatible HTTP endpoint. `dispatch.py` prints the result; supported write lanes can also edit files. Check the files and tests afterward: a successful model response does not prove the task is complete.

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

Each backend needs its own CLI login or API key and a model available to your account. A quick preflight is `command -v codex` or `command -v agent` for CLI lanes, `ollama list` for local models, and a live request for HTTP lanes. `--dry-run` prints a plan without launching a worker or contacting a provider. It does **not** check the executable, login, key, endpoint, model availability, or whether the task will succeed. Under the planned security update, the plan shows a prompt byte count and SHA-256 hash, never the prompt text. Current plans show the resolved model and working directory; Cursor also shows its launch arguments. HTTP plans may omit the final endpoint and key-variable name, so check the flags and config you supplied before a live call.

## Write-mode examples

Run one backend example. For each, inspect the dry-run plan, then run the write command. Check the result with:

```sh
.venv/bin/python -m unittest discover -s out -p 'test_*.py' -v
```

Also read `out/slugify.py` and `out/test_slugify.py`; the test command can pass without confirming that the model followed every instruction.

### Codex CLI

Install the [Codex CLI](https://learn.chatgpt.com/docs/developer-commands?surface=cli) if needed, then sign in with `codex login` and check with `codex login status`. With Node.js/npm installed, the install command is `npm install -g @openai/codex`. Run `codex`, type `/model`, and choose an available model; use its ID below. The public ID `gpt-6-sol` is an example, but availability depends on your account.

```sh
codex login
codex login status
.venv/bin/python dispatch.py --backend codex --model gpt-6-sol --spec task.md --cwd "$PWD" --write --dry-run
.venv/bin/python dispatch.py --backend codex --model gpt-6-sol --spec task.md --cwd "$PWD" --write
.venv/bin/python -m unittest discover -s out -p 'test_*.py' -v
```

`--web` enables Codex search. `--network` requires `--write` and requests network access in Codex's execution sandbox. This task declares `network: no`, so leave both off.

### Cursor CLI

Install the [Cursor CLI](https://docs.cursor.com/en/cli/installation) with `curl https://cursor.com/install -fsS | bash`, make sure `agent` is on `PATH`, run `agent login`, and list models with `agent models`. Choose a model base that supports an effort suffix in Dispatch; Dispatch appends `-high` by default. Replace `YOUR_CURSOR_BASE` with that base.

```sh
agent login
agent models
.venv/bin/python dispatch.py --backend cursor --model YOUR_CURSOR_BASE --spec task.md --cwd "$PWD" --write --dry-run
.venv/bin/python dispatch.py --backend cursor --model YOUR_CURSOR_BASE --spec task.md --cwd "$PWD" --write
.venv/bin/python -m unittest discover -s out -p 'test_*.py' -v
```

If the listed model does not have a compatible effort variant, choose one that does or configure its supported efforts in `config.toml`. `--effort low|medium|high|xhigh` overrides the default where supported.

### Ollama local

Install [Ollama](https://ollama.com/download). In terminal 1, run `ollama serve` and leave it running. If the desktop app already started the service, use that instead. In **terminal 2**, from this repo:

```sh
ollama pull qwen3:4b
ollama list
.venv/bin/python dispatch.py --backend ollama --model qwen3:4b --spec task.md --cwd "$PWD" --write --dry-run
.venv/bin/python dispatch.py --backend ollama --model qwen3:4b --spec task.md --cwd "$PWD" --write
.venv/bin/python -m unittest discover -s out -p 'test_*.py' -v
```

The default endpoint is `http://127.0.0.1:11434`; no API key is needed there. For a server deliberately listening on port 11435, pass `--base-url http://127.0.0.1:11435` to both Dispatch commands. The port must match the server. Ollama and OpenRouter write mode use the `dispatch_web.py` tool loop and need a model that reliably calls tools.

### Ollama Cloud

Create an Ollama API key, set the default `OLLAMA_API_KEY` environment variable, and choose a cloud model your account can use. The example ID below is published by Ollama.

```sh
export OLLAMA_API_KEY='paste-your-key-here'
.venv/bin/python dispatch.py --backend ollama-cloud --model qwen3-coder:480b-cloud --spec task.md --cwd "$PWD" --write --dry-run
.venv/bin/python dispatch.py --backend ollama-cloud --model qwen3-coder:480b-cloud --spec task.md --cwd "$PWD" --write
.venv/bin/python -m unittest discover -s out -p 'test_*.py' -v
```

If your key is in another variable, add `--api-key-env YOUR_VARIABLE` to both commands. `dispatch_web.py` can also run Ollama's hosted search and fetch tools with a tool-capable model.

### OpenRouter

Create an OpenRouter API key. The model below is listed by OpenRouter with tool calling; select another current tool-capable slug if it is unavailable to your account. Dispatch requests zero data retention, denies provider data collection, and disables fallback routing.

```sh
export OPENROUTER_API_KEY='paste-your-key-here'
.venv/bin/python dispatch.py --backend openrouter --model openai/gpt-5.6-luna --spec task.md --cwd "$PWD" --write --dry-run
.venv/bin/python dispatch.py --backend openrouter --model openai/gpt-5.6-luna --spec task.md --cwd "$PWD" --write
.venv/bin/python -m unittest discover -s out -p 'test_*.py' -v
```

For a `:free` slug, Dispatch also refuses a response that reports nonzero cost. Check current slugs in OpenRouter's model catalog.

### OpenAI-compatible HTTP

This backend is **text-only** and rejects `--write`. It can generate a file for you to save under your own control. Set a real HTTPS `/v1` root, a model ID from that provider, and the environment variable holding its API key. This example asks for one Python file and redirects the returned text; review it before running it. If your server is local, a loopback HTTP URL is also allowed.

```sh
export MY_API_KEY='paste-your-key-here'
cat > compatible-task.md <<'EOF'
Return only Python source code for a slugify(title) function that lowercases
ASCII words, replaces runs of spaces or punctuation with one hyphen, and
strips edge hyphens. No Markdown fences or explanation.
EOF
.venv/bin/python dispatch.py --backend openai-compatible --model YOUR_MODEL --base-url https://YOUR_HOST/v1 --api-key-env MY_API_KEY --spec compatible-task.md --dry-run
.venv/bin/python dispatch.py --backend openai-compatible --model YOUR_MODEL --base-url https://YOUR_HOST/v1 --api-key-env MY_API_KEY --spec compatible-task.md > out/slugify.py
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
| `--dry-run` | Show selected model, working directory, and prompt hash without launching. HTTP endpoint/key details may be omitted; it is not a connection check. |
| `--write` | Enable editing on supported backends; rejected for generic OpenAI-compatible HTTP. |
| `--unsafe-shell` | Enable the file-tool loop's `run_command` shell explicitly, with a warning. Off by default; config alternative: `[general].allow_shell_tool = true`. |
| `--pass-env NAME` | Pass an additional named environment variable to a CLI child; repeat as needed. |
| `--timeout`, `--idle-timeout` | Per-call timeout and Cursor idle timeout, in seconds. |
| `--max-iters` | Ollama/OpenRouter write-loop iteration limit. |
| `--effort` | Backend reasoning-effort override where supported. |
| `--fast`, `--directive` | Fast mode for Codex/Cursor requires `--directive` audit text. `--directive` alone is optional except for configured directed models. |
| `--web`, `--network` | Codex search and Codex execution-sandbox network access; `--network` requires `--write`. |
| `--images` | JPEG, PNG, or WebP input for a configured vision-capable Ollama model. |
| `--ignore-cooldown`, `--ignore-lane-state` | Bypass an active local cooldown or optional lane-state marker. |
| `--task-class` | Routing hint when a lane-state provider is installed. |

`--no-fast` is a compatibility no-op. `--reasoning` applies only to configured backends that use it. `dispatch_web.py` also accepts `--write-dir` (repeatable), `--no-trace`, and the model, backend, endpoint, key-variable, timeout, and iteration options; the shell flag applies to its file-tool loop.

A `## Grants` block declares `paths-write`, `network`, `github-writes`, and `tools`. Dispatch checks contradictions at launch and logs the decision. Grants are advisory, not filesystem or network enforcement.

## What is and isn't sandboxed

Codex and Cursor use their own sandbox options. Their actual isolation depends on those CLIs and the permissions you grant. `--web` enables Codex search; `--network` requests network access inside Codex's execution sandbox.

Ollama/OpenRouter file tools limit file operations to the declared `--write-dir` roots and reject symlink traversal under the planned security update. The `run_command` shell tool is off unless you explicitly pass `--unsafe-shell` or set `[general].allow_shell_tool = true`; enabling it prints a warning. A shell started there is **not** confined by `--write-dir`. Use an OS sandbox or disposable workspace for shell-capable tasks.

CLI children receive an allowlist of basic process variables and backend authentication variables. Use `--pass-env NAME` only for extra variables the child needs. HTTP endpoints must use HTTPS, except loopback HTTP for local services; redirects across origins are rejected. Hosted web tools may have their own network reach, beyond local file-tool restrictions. `--dry-run` makes no backend request and shows a prompt hash rather than task text. Never treat it as proof of credentials, model access, or task completion.
