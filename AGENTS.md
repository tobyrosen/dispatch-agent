# Agent Context

## What This Is

`dispatch-agent` is a small Python CLI toolkit plus a Claude skill contract for sending scoped tasks to non-Claude models and getting raw results back for review.

## Why It Exists

Claude agent teams can keep high-judgment work on the primary Claude lane while sending bounded, lower-stakes, or high-volume subtasks to cheaper or specialized models. This repo provides the operating discipline and reference scripts for doing that with explicit context, local audit logs, and no silent model fallback.

## Architecture

- Main entry points: `dispatch.py`, `dispatch_web.py`
- Core files/directories:
  - `dispatch.py`: single-task dispatch client. It reads a spec, resolves a TOML alias or explicit backend/model, checks grants, retries supported transient failures, and prints launch metadata followed by model output. Supported backends can also edit files.
  - `dispatch_web.py`: bounded HTTP tool loop. Ollama models can use hosted `web_search` and `web_fetch`; Ollama and OpenRouter models can use file tools under allowed directory roots. It prints the final answer to stdout with tool traces on stderr.
  - `SKILL.md`: Claude-facing skill instructions covering model matching, spec discipline, concurrency, gating, and escalation.
  - `pyproject.toml`: local package metadata, console script entry points, and release-please version target.
  - `tests/`: pytest regressions for public configuration, grants, dry-run preflight, HTTP and CLI failures, tool behavior, and security defaults.
- Optional local extension entry points are documented in `CONTRIBUTING.md`; no extension module ships in this repository.
- Runtime/deployment shape: local Python 3.11+ command-line scripts. The core uses the standard library; the optional web/tool loop installs the `ollama` package. No daemon, queue, shared lock, package registry publish, or hosted service is part of v0.1.0.

## Develop, Run, Test

```sh
python3.11 -m pip install -e ".[dev,web]"
python3.11 dispatch.py --help
python3.11 dispatch_web.py --help
python3.11 -m pytest -q -p no:cacheprovider tests/
```

The dry-run redaction test expects `codex` on `PATH`; no login or provider credentials are needed. Tests use mocks and local loopback servers.

## Release Process

This repo follows the public software release standard:

- SemVer.
- Conventional Commits.
- release-please Release PRs.
- Release PR merge approved by the maintainer before publishing.
- GitHub Releases first; external registries only by explicit approval.

Release state is tracked in `version.txt` and `pyproject.toml`. `release-please-config.json` updates both.

## Current State

Current version: `0.1.0`. The repo is GitHub-only: release-please may create GitHub Releases after a Release PR is approved and merged, but PyPI and other registries are intentionally disabled.

## Gotchas

- Public backends are selected through `~/.config/dispatch-agent/config.toml` or explicit `--backend` and model options; no source edits or model registries are required.
- `dispatch_web.py` provides Ollama hosted search/fetch and Ollama/OpenRouter file tools. Both HTTP write loops need a tool-capable model; the Ollama loop also needs the optional `ollama` dependency.
- `dispatch.py --dry-run` checks local prerequisites without backend requests; `--no-preflight` skips those checks. `dispatch_web.py` has no dry-run flag.
- Grants are advisory. File tools enforce directory roots, but opting into `--unsafe-shell` enables an unconfined shell.
- The tools return raw model output. Review, tests, and integration gates happen where the output lands, not inside these scripts.
- The generic OpenAI-compatible HTTP backend is text-only; it rejects `--write`.

## Decision Notes

Design decisions live in `docs/decisions/`.

Add a decision note when a choice affects public behavior, release shape, architecture, compatibility, security, publishing, or future maintenance.
