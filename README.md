# Dispatch

[![Release](https://img.shields.io/github/v/release/tobyrosen/dispatch-agent?sort=semver&display_name=tag&label=release&style=flat-square)](https://github.com/tobyrosen/dispatch-agent/releases/latest) [![CI](https://img.shields.io/github/actions/workflow/status/tobyrosen/dispatch-agent/ci.yml?branch=main&label=CI&style=flat-square)](https://github.com/tobyrosen/dispatch-agent/actions/workflows/ci.yml) [![License](https://img.shields.io/github/license/tobyrosen/dispatch-agent?label=license&style=flat-square)](LICENSE) [![Type](https://img.shields.io/badge/type-standalone--software-3776ab?style=flat-square)](#) [![Language](https://img.shields.io/github/languages/top/tobyrosen/dispatch-agent?style=flat-square)](https://github.com/tobyrosen/dispatch-agent)

A Claude Code skill for handing scoped tasks to non-Claude models and getting usable results back. Covers model selection by task type, spec discipline, stateless dispatch, agentic web-research dispatch, concurrency, and escalation.

## Identity

| Field | Value |
|---|---|
| What | Two Python CLIs, `dispatch.py` and `dispatch_web.py`, plus `SKILL.md` guidance for Claude teams that dispatch bounded tasks to non-Claude models. |
| For | Claude Code team leaders and maintainers who need self-contained delegation specs, model-tier matching, raw-output review, and optional live web research. |
| Type | standalone-software |
| Status | experimental |

## The Problem It Solves

Claude agent teams (Lane 1) are powerful but expensive at volume. Many subtasks — bulk coding, drafts, high-volume classification, web research — can run on cheaper or specialized models without sacrificing quality. The `dispatch` skill provides the craft layer for doing this correctly: picking the right model tier, writing a tight self-contained spec, running the tool, and knowing when to escalate instead of accepting raw output.

## Two Dispatch Modes

**Stateless mode** (`dispatch.py`) — sends one chat-completion with no tools. The model answers from training. Use for coding, classification, drafts, analysis of content you provide. Fast and simple.

**Web-browsing mode** (`dispatch_web.py`) — runs a bounded agent loop with hosted `web_search` / `web_fetch` tools. The model actually browses: searches, fetches pages to confirm they resolve, drops 404s, and only cites verified URLs. Use when the answer depends on the live web or you need link verification.

## Model Matching

| Task type | Model tier |
|---|---|
| Agentic/terminal coding, multi-step CLI work | Capable agentic coding model |
| Hardest frontier reasoning | Keep on Claude (Lane 1) |
| Bulk / high-volume coding | Coding-specialized OSS model |
| Non-code bulk, drafts, pre-screening | General-purpose OSS model |
| Deliverable prose that ships | Primary Claude agent — never offloaded |

Quality first, cost second. The cheap pool is for tasks where quality variation is tolerable.

## Spec Discipline

Every dispatch requires a self-contained spec:
- Task goal + non-goals
- Relevant file paths / inputs / constraints
- Exact output contract (format, length, success criteria)
- Escalation notes for taste / policy / client-facing / irreversible decisions

Vague dispatch → wrong output. If the model would need hidden context, put it in the spec.

## Install

1. Copy `SKILL.md` to `~/.claude/skills/dispatch/SKILL.md`.
2. Copy `dispatch.py` (and optionally `dispatch_web.py`) to a stable directory. Both are reference templates — adapt the CONFIGURATION section before use.
3. Open `dispatch.py` and register your model IDs in `HTTP_MODELS` (keys = names you pass to `--model`; values = provider model IDs). Set `DISPATCH_BASE_URL` and `DISPATCH_API_KEY` in your environment to point at any OpenAI-compatible endpoint.
4. For a CLI-based coding tool, add entries to `CLI_MODELS` and fill in the `run_cli()` stub (see the comment block in that function).
5. For web-browsing mode, register a tool-call-capable model in `dispatch_web.py`'s `CHAT_MODELS` and wire up your search provider in `_web_search()` — the TODO comment shows Serper, Tavily, and SerpAPI options.

## Usage Example

```bash
# Write a self-contained spec
cat > task.md <<'EOF'
Goal: Refactor the parse_invoice() function in src/invoices.py to handle multi-page PDFs.
Non-goals: Do not change the function signature or the caller in main.py.
Output: The updated parse_invoice() function body only, as a Python code block.
Verification: Must pass the existing tests in tests/test_invoices.py.
EOF

# Dispatch to a coding model
path/to/dispatch.py --model <your-coding-model> --spec task.md --cwd /path/to/repo
```

```bash
# Web-research mode: find current verified URLs
echo "Find the 5 most active open-source Claude Code skill repositories. web_search then web_fetch each to confirm it resolves before listing." \
  | path/to/dispatch_web.py --model <your-web-model> --spec -
```

## Gating

The dispatch tool returns raw model output. Code is gated where it **lands** — in your CI, code review, or integration step. Do not trust un-landed dispatched code blindly. Review or test before integrating.

## When to Escalate (Not Dispatch)

- Taste, brand, or client-facing decisions
- Irreversible operations
- Results that fail downstream tests — surface, do not quietly resubmit

## Concurrency

Each invocation is independent. Fire as many dispatches as the work genuinely needs — there is no shared queue or lock.

## Project Notes

Maintainer and agent context lives in [AGENTS.md](AGENTS.md).

Design decisions are recorded in [docs/decisions](docs/decisions).

The Claude skill behavior contract lives in [SKILL.md](SKILL.md).

## Versioning and Releases

This repo uses Semantic Versioning, Conventional Commits, and release-please Release PRs.

The latest released version is shown in the release badge at the top of this README. The badge updates automatically from GitHub Releases after a Release PR is approved and merged.

Release tags use the format `vX.Y.Z`.

## Contributing

Use Conventional Commits for commit messages and PR titles.

Do not create release tags manually. Do not edit `CHANGELOG.md` manually for ordinary releases.

## License

This project is licensed under the terms in [LICENSE](LICENSE).
