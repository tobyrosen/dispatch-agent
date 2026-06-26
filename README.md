# Dispatch

A Claude Code skill for handing scoped tasks to non-Claude models and getting usable results back. Covers model selection by task type, spec discipline, stateless dispatch, agentic web-research dispatch, concurrency, and escalation.

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
