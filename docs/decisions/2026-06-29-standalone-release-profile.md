# Use Standalone Software Release Profile

Date: 2026-06-29
Status: accepted

## Context

`dispatch-agent` contains a Claude skill contract in `SKILL.md`, but the shipped product also includes executable Python CLIs in `dispatch.py` and `dispatch_web.py`.

## Decision

Use the `standalone-software` release profile for the public repo. Keep `SKILL.md` as a documented hybrid surface, but release and maintain the repo as Python standalone software.

## Consequences

The repo carries package metadata, tests, contribution/security docs, issue templates, PR template, CODEOWNERS, and release-please version updates for both `version.txt` and `pyproject.toml`.

## Follow-up

Revisit only if the executable tooling is split from the Claude skill into separate repos.
