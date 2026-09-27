---
name: dispatch
description: Use when handing a scoped task to Codex, Cursor, Ollama, OpenRouter, or an OpenAI-compatible model through Dispatch Agent. Covers model selection, self-contained specs, grants, dry-run plans, file editing, web research, and result verification.
---

# Dispatch

Hand one self-contained task to a suitable model, inspect its result, and verify the output before integration. Use the primary agent for decisions that require project context or owner judgment.

## Setup

Use Python 3.11 or newer. Follow [README.md](README.md) for installation and backend prerequisites. Keep this skill with the repository so its documentation links resolve; for a Claude skill installation, link the repository directory at `~/.claude/skills/dispatch`.

Run examples from the repository root using its `.venv/bin/python`. Copy `config.example.toml` to `~/.config/dispatch-agent/config.toml` and define aliases under `[models.<alias>]`, with provider defaults under `[providers.<backend>]`. Alternatively, supply `--backend` and a provider model ID in `--model`. Use `--model-id` to override an alias's provider model. CLI options override config; no source edits are needed.

Supported backends are `codex`, `cursor`, `ollama`, `ollama-cloud`, `openrouter`, and `openai-compatible`. Configure HTTP roots with `--base-url` and credential variable **names** with `--api-key-env`. Keep credential values in the environment, out of specs and config. Ollama Cloud defaults to `OLLAMA_API_KEY`; OpenRouter defaults to `OPENROUTER_API_KEY`. Local Ollama at `http://127.0.0.1:11434` needs no key. The generic HTTP backend needs a base URL and defaults to `OPENAI_API_KEY`.

## Model selection and spec discipline

Match task complexity, tool requirements, and output stakes to model quality. Use cost and capacity as constraints. Choose a model available to the user's account, and require reliable tool calling for HTTP file or web tasks. Do not silently switch models after a failure.

Write a spec containing the goal, non-goals, input files, constraints, exact output contract, requested verification, and decisions to escalate. Include all context the worker needs. For reviews, require findings with locations and severity, explicit checks for each requested dimension, and a verdict.

Declare permissions in a bare section in the spec:

```markdown
## Grants
paths-write: out/**
network: no
github-writes: no
tools: file editing and Python unittest
```

Resolve relative write globs from `--cwd`; separate multiple globs with commas. For text-only work, use `paths-write: none` and `tools: none`. Start fields at the beginning of their lines and use bare `yes` or `no` for network and GitHub writes.

`dispatch.py` checks grants for contradictions and logs the decision. `--write` needs a nonempty write scope overlapping `--cwd`; `network: no` rejects `--network` and `--web`. Missing grants produce a warning and the call proceeds. Treat grants as advisory validation, not filesystem or network enforcement. Direct `dispatch_web.py` calls do not run these grants checks; scope file tools with `--write-dir` and enforce other limits outside the tool.

## Plan and run

Inspect a dry-run before launching. For the README's file-editing task:

```sh
.venv/bin/python dispatch.py --backend codex --model YOUR_CODEX_MODEL --spec task.md --cwd "$PWD" --write --dry-run
```

Or use a configured alias and stdin for a text task:

```sh
.venv/bin/python dispatch.py --model compatible --spec - --dry-run < compatible-task.md
```

`--dry-run` prints JSON without launching a worker or contacting a provider. Preflight checks local executable availability and required key-variable presence. It reports only endpoint origins and prompt byte count/hash, not credential values or prompt contents. It does not verify login, model access, endpoint reachability, or task completion. Add `--no-preflight` to inspect a plan before installing a backend or setting credentials. `dispatch_web.py` has no dry-run flag.

For an authorized live run, satisfy the backend prerequisites, check the plan, then remove `--dry-run`. `dispatch.py` prints launch metadata followed by raw model output on stdout, and exits nonzero on failure. Do not treat all stdout as generated source; use the README's extraction example when saving code. Timeouts are per attempt or HTTP call; retries and tool loops can take longer in total.

## Editing and isolation

Use `--write --cwd <directory>` for Codex, Cursor, Ollama, Ollama Cloud, or OpenRouter. The generic OpenAI-compatible backend is text-only and rejects `--write`.

- Codex and Cursor use their own CLI sandbox settings and retain the user's HOME for settings and authentication. Check those CLIs' effective permissions before sensitive work.
- Ollama and OpenRouter hand editing to the `dispatch_web.py` file-tool loop, rooted at `--cwd`. This root can be broader than the spec's write globs. Narrow the working directory where practical and inspect the resulting diff.
- Install `.[web]` for the Ollama SDK loop. OpenRouter's file loop uses the standard library. Use `--max-iters` to bound either HTTP write loop.
- File tools enforce allowed directory roots and reject symlink traversal. Shell execution is off by default. `--unsafe-shell` or `[general].allow_shell_tool = true` enables an unconfined shell; a write root only sets its working directory. Use a container or VM for untrusted shell work.

Use `--pass-env NAME` only for additional variables a child needs. Public HTTP children otherwise receive an allowlist and an isolated temporary HOME. See the README for backend-specific limitations.

## Web research

For live facts, require tool evidence and fetched source URLs in the spec. Codex supports `dispatch.py --web`; `--network` separately enables Codex execution-sandbox networking and requires `--write`. Use `network: yes` for a task that authorizes those flags.

For Ollama's hosted search and fetch tools, install `.[web]`, supply a suitable API key, and use a tool-capable model:

```sh
.venv/bin/python dispatch_web.py --backend ollama-cloud --model YOUR_CLOUD_MODEL --spec research-task.md --max-iters 15 --timeout 300
```

This is a live command. Review the task and prerequisites first. The loop prints its final answer to stdout and tool traces to stderr; keep traces enabled to verify searches and fetches. `--no-trace` suppresses them. Reaching the iteration cap without a final answer fails. A successful response does not guarantee that the model searched or verified every citation; inspect the trace and returned evidence.

OpenRouter's public tool loop provides file tools with `--write-dir`; it does not provide hosted web search/fetch. Plain HTTP completions through `dispatch.py` have no browsing tools.

## Concurrency, retries, and verification

Run independent tasks in separate working directories. There is no shared queue or daemon; optional cooldown markers and audit logs are local shared state. A transport retry stays on the selected model. Inspect files after a failed or partial run before relaunching, and keep retries within the original authorization.

Before reporting completion:

- Point to the self-contained spec and selected backend/model.
- Review the actual files or text against the output contract, including required counts and sections.
- Run the requested tests and name their outcomes. Do not treat exit zero or a plausible answer as proof of completion.
- Escalate scope conflicts, unverifiable claims, and decisions requiring owner judgment before publishing or taking irreversible action.
