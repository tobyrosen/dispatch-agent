---
name: dispatch
description: Use when a Claude team leader (or a teammate) decides a scoped task should run on a non-Claude model. Covers model-matching by task type, spec discipline, a stateless dispatch tool, a web-browsing dispatch mode (agentic web research — search + fetch + verify live links), concurrency, and escalation.
---

# Dispatch

The craft layer for handing a scoped task to a non-Claude model and getting a result back. Pick the right model tier for the task type, write a tight spec, run the dispatch tool, and escalate taste/client-facing/irreversible calls to the project owner before anything ships.

## Lanes

- **Lane 1 — Claude work:** spawn Agent Teams teammates (Claude Sonnet / Opus / Haiku). Do NOT use this skill for Claude-to-Claude dispatch.
- **Lane 2 — non-Claude work:** your dispatch script (see Setup below).

## Setup

1. Copy `SKILL.md` to `~/.claude/skills/dispatch/SKILL.md`.
2. Place `dispatch.py` (and optionally `dispatch_web.py`) in a stable directory on your PATH or reference by full path.
3. Open `dispatch.py` and register your models in `HTTP_MODELS` (or `CLI_MODELS` for a CLI-based coding tool). The keys are what you pass to `--model`; the values are the provider's model IDs.
4. Set two environment variables in your shell profile or agent config:
   - `DISPATCH_BASE_URL` — your provider's OpenAI-compatible base URL (e.g. `https://api.openai.com/v1`, a local Ollama URL, or a LiteLLM proxy).
   - `DISPATCH_API_KEY` — API key for that endpoint.
5. Verify: `echo "Write hello world in Python." | path/to/dispatch.py --model <your-model> --spec -`
6. For web-browsing mode (`dispatch_web.py`), register a tool-call-capable model in `CHAT_MODELS` and wire up your search provider in `_web_search()` — see the TODO comment there for Serper, Tavily, and SerpAPI options.

## Model Matching (quality first, cost second)

Match the task to the model — the goal is great output, never the cheapest-by-default. Fill in your own model IDs using this framework as a guide.

| Task type | Tier | Note |
|---|---|---|
| Agentic / terminal coding, multi-step CLI/tool work | Capable agentic coding model | If included in a flat-rate subscription you already pay, prefer it here to conserve your metered premium budget |
| Hardest frontier reasoning, judgment, taste | Primary agent — Lane 1 | Never offload |
| Bulk / high-volume coding | Cheap coding-specialized model | When quality variance is acceptable and volume is high |
| Non-code bulk: drafts, classification, pre-screening | Cheap general-purpose model | Lowest tier; highest quality variance |
| Deliverable prose that ships to a reader | Primary agent — never offloaded | If a human sees the text, the primary agent authors it, regardless of cost |
| Heavy OSS reasoning model | Only when benchmarks show a genuine win | Not the default — benchmarks change fast, verify regularly |

**Core principle:** match task complexity and output stakes to model quality; reserve the cheap pool for tasks where quality variation is tolerable.

## Spec Discipline

A dispatch is only as good as its spec. Before firing, write a self-contained spec:

- Task goal + non-goals
- Relevant file paths / inputs / constraints
- The exact output contract (format, length, what constitutes success)
- Whether code output is expected
- Any verification requested
- Escalation notes for taste / policy / client-facing / irreversible decisions

Do not dispatch vague intent — if the model would need hidden context, put it in the spec.

## Running the Tool

One invocation = one dispatch. A minimal dispatch script interface:

```bash
path/to/dispatch.py --model <model-id> --spec <path-or-"-"> [--cwd <dir>] [--timeout <s>]
```

```bash
# spec from a file, run in a repo
path/to/dispatch.py --model <coding-model> --spec task.md --cwd /path/to/repo

# spec from stdin
printf '%s\n' "$SPEC" | path/to/dispatch.py --model <bulk-model> --spec -
```

The tool should print the model's result to stdout and exit non-zero on failure (no silent fallback to another model).

## Web-Browsing Mode (Agentic Web Research)

A stateless `dispatch.py` has NO web access — it sends one chat-completion with no tools, so the model answers from training memory. It cannot look anything up, confirm a link resolves, or return current facts.

For research that needs current, verified web facts (live product/store URLs, prices, "what exists today," anything you must not hallucinate), use a web-enabled dispatch mode instead. This runs a bounded agent loop that gives the model hosted `web_search` / `web_fetch` tools — it actually browses: searches, fetches candidate pages to confirm they resolve, and only cites verified URLs.

When to use web mode:
- The answer depends on the live web (current listings, real URLs, prices, recent events).
- You need link verification — the model fetches each URL and drops the 404s.
- Stateless mode would force you to write "verify the links yourself" in the spec — that's the tell you want web mode.

The web-browsing dispatch requires a model that emits tool calls (function-calling). Verify per-model before relying on it — not all OSS models emit tool calls reliably.

The model's final answer should print to stdout; the tool-call trace to stderr (one line per search/fetch with a result summary) so you can verify it actually browsed.

## Concurrency

Fire as many dispatches as the work genuinely needs. Each call should be independent — one subprocess or one HTTP request per dispatch. There is no shared queue, daemon, lock, or batch coordinator, so many callers can dispatch at once without clogging each other. Launch many independent commands from the caller; do not introduce a shared service.

## Gating and Escalation

- The dispatch tool returns **raw** model output — it does not gate. **Code is gated where it lands:** when a teammate integrates a dispatched code result, your CI/code-review gate runs. Do not trust un-landed dispatched code blindly — review or test before integrating.
- Do not silently accept a result that fails downstream tests; surface it.
- Surface taste, brand, client-facing, policy, or irreversible decisions to the project owner before they ship.

## Common Rationalizations

| Excuse | Rebuttal |
|---|---|
| "The model will figure out the missing context" | Dispatch is stateless — hidden context produces wrong output. If the model would need it, it goes in the spec. |
| "The result looks fine, integrate it" | The tool returns raw, ungated output. Review or test before it lands — gating happens where code lands, not in the tool. |
| "It's basically a draft — send the client-facing copy to the cheap pool" | Deliverable prose that ships to a reader is the primary agent's own work, never offloaded to a bulk model. |
| "Cheapest model first, to save credits" | Quality first, cost second — match the task to the right model tier. |
| "It failed a downstream test but it's mostly right" | Never silently accept a failing result. Surface it. |

## Verification

Before reporting a dispatch complete:
- [ ] Spec was self-contained (goal, constraints, output contract) — point to the spec file or paste it
- [ ] Result reviewed or tested before integrating — name the check that was run
- [ ] Taste / brand / client-facing / irreversible calls escalated to the project owner before anything shipped
