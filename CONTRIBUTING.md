# Contributing

## Local checks

Use Python 3.11 or newer and a virtual environment:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,web]'
.venv/bin/python -m pytest -q -p no:cacheprovider tests/
.venv/bin/python dispatch.py --help
.venv/bin/python dispatch_web.py --help
```

Install the Codex CLI and put `codex` on `PATH` for the dry-run redaction regression; it checks executable presence without launching it. The tests use mocks and local loopback servers; they do not need provider credentials or login. Use the README's `--dry-run` path to check examples without launching a worker. Add `--no-preflight` if backend executables or key variables are unavailable.

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

Do not create release tags manually. Do not publish release artifacts manually unless the maintainer explicitly approves an exception.

Merging a Release PR is the owner approval to create the GitHub Release. Release PRs must not be auto-merged.

## Local extensions

The standalone modules need no extension. A trusted Python file matching `*_private.py` beside the resolved script can define `install_dispatch(namespace)` or `install_web(namespace)`. The loader uses that directory, including when invoked through a symlink; it does not search the working directory or `PYTHONPATH` for extensions. Import errors fail visibly.

An installer can register local model resolution, validation, launch and audit callbacks, a usage observer, or web tools in `EXTENSION_TOOLS`. Tool entries provide `definition`, `execute`, `prompt`, `disabled_error`, `preview`, and `summary`. Keep installation modules out of public distributions. Extensions execute with the caller's permissions, so install only trusted source files.
