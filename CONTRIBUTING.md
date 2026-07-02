# Contributing

## Commits

Use Conventional Commits:

- `feat:` for backward-compatible user-facing features.
- `fix:` for backward-compatible bug fixes.
- `perf:` for backward-compatible performance improvements.
- `docs:`, `chore:`, `ci:`, `build:`, `test:`, `refactor:`, and `style:` for non-release changes.
- Use `type!:` or a `BREAKING CHANGE:` footer for incompatible changes.

## Pull Requests

Before requesting review:

- Confirm CI is passing.
- Keep changes scoped.
- Update README usage examples when behavior changes.
- Add or update tests when behavior changes.
- Update `AGENTS.md` or `docs/decisions/` when architecture, release shape, public behavior, or gotchas change.
- Do not edit `CHANGELOG.md` manually for normal releases.

## Releases

This repo uses release-please Release PRs.

Do not create release tags manually. Do not publish release artifacts manually unless Toby explicitly approves an exception.

Merging a Release PR is the owner approval to create the GitHub Release. Release PRs must not be auto-merged.
