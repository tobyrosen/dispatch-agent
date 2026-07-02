# Agent Context

## What This Is

`dispatch-agent` is a small Python CLI toolkit plus a Claude skill contract for sending scoped tasks to non-Claude models and getting raw results back for review.

## Why It Exists

Claude agent teams can keep high-judgment work on the primary Claude lane while sending bounded, lower-stakes, or high-volume subtasks to cheaper or specialized models. This repo provides the operating discipline and reference scripts for doing that without hidden context, shared state, or silent fallback behavior.

## Architecture

- Main entry points: `dispatch.py`, `dispatch_web.py`
- Core files/directories:
  - `dispatch.py`: stateless one-shot dispatch client. It reads a spec, selects a registered HTTP or CLI model, retries transient failures, and prints raw model output to stdout.
  - `dispatch_web.py`: bounded agentic web-research client. It exposes `web_search` and `web_fetch` tool schemas to a tool-call-capable chat model and prints the final answer to stdout with tool traces on stderr.
  - `SKILL.md`: Claude-facing skill instructions covering model matching, spec discipline, concurrency, gating, and escalation.
  - `pyproject.toml`: local package metadata, console script entry points, and release-please version target.
  - `tests/`: smoke tests for CLI argument handling, setup failures, version alignment, and web-tool stubs.
- Runtime/deployment shape: local Python 3.9+ command-line scripts using only the standard library at runtime. No daemon, queue, shared lock, package registry publish, or hosted service is part of v0.1.0.

## Develop, Run, Test

```sh
python -m pip install -e ".[dev]"
python dispatch.py --help
python dispatch_web.py --help
python -m unittest discover -s tests
```

## Release Process

This repo follows the public software release standard:

- SemVer.
- Conventional Commits.
- release-please Release PRs.
- Toby-approved Release PR merge before publishing.
- GitHub Releases first; external registries only by explicit approval.

Release state is tracked in `version.txt` and `pyproject.toml`. `release-please-config.json` updates both.

## Current State

Seeded for public release at `0.1.0`. The repo is GitHub-only: release-please may create GitHub Releases after a Release PR is approved and merged, but PyPI and other registries are intentionally disabled.

## Gotchas

- `dispatch.py` ships with empty `HTTP_MODELS` and `CLI_MODELS`; users must register provider model IDs or a local CLI invocation before real dispatch works.
- `dispatch_web.py` ships with empty `CHAT_MODELS` and an intentionally unimplemented `_web_search()` stub. Web mode does not browse until a search provider is wired in.
- The tools return raw model output. Review, tests, and integration gates happen where the output lands, not inside these scripts.
- `run_cli()` is a template stub and must be replaced before CLI-lane dispatch can run.
- `dispatch_web.py` only works reliably with models that emit tool calls in the provider's OpenAI-compatible response shape.

## Decision Notes

Design decisions live in `docs/decisions/`.

Add a decision note when a choice affects public behavior, release shape, architecture, compatibility, security, publishing, or future maintenance.
