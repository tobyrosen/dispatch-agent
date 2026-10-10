#!/usr/bin/env python3
"""Run one delegation through a configured CLI or HTTP backend."""

import argparse
import atexit
import base64
import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import random
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import ipaddress

def load_local_extensions(namespace, entrypoint):
    """Load trusted *_private.py modules beside this script, never from cwd/PATH.

    Each module may export install_dispatch(namespace) and/or install_web(namespace).
    These local installation modules are executable code and must not be shipped
    with a public distribution. Missing modules leave the standalone defaults.
    """
    for path in sorted(Path(namespace["__file__"]).resolve().parent.glob("*_private.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load local extension {path.name}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        install = getattr(module, entrypoint, None)
        if install is not None:
            install(namespace)


def log_usage(*_args, **_kwargs):
    """Optional usage observer; the standalone client does not persist billing data."""
    return None


import tomllib


LOCAL_CONFIGURED = False
WORKER_ENV_REMOVE = ()
LEAF_WORKER_ENV = "DISPATCH_CURSOR_LEAF"
CONFIG_PATH = Path.home() / ".config" / "dispatch-agent" / "config.toml"
OLLAMA_URL = "http://127.0.0.1:11434/v1/chat/completions"
OLLAMA_CHAT_URL = "http://127.0.0.1:11434/api/chat"
OLLAMA_TOKEN_REF = "OLLAMA_API_KEY"
GRANTS_LOG = os.path.expanduser("~/.local/state/dispatch-agent/delegate-grants.jsonl")
DELEGATE_LOG_DIR_ENV = "DELEGATE_LOG_DIR"
NESTED_CODEX_CAPABILITY_ENV = "DELEGATE_NESTED_CODEX_CAPABILITY"
NESTED_CODEX_CAPABILITY = "write-network-v1"
GRANTS_FORMAT = (
    "## Grants\npaths-write: <glob list or none>\nnetwork: <yes|no>\n"
    "github-writes: <yes|no>\ntools: <free-text list>"
)
# Worker skills (VP orchestrator spec, Change 1, 2026-09-30). A spec may carry a
# `## Skills` section beside `## Grants`; each named ~/.claude/skills/<name>/SKILL.md
# body is appended to the prompt under `## Loaded skills`. Printed beside
# GRANTS_FORMAT because an installation module may replace that constant.
SKILLS_FORMAT = "## Skills\nload: <comma-separated skill names or none>"
SKILLS_DIR_ENV = "DELEGATE_SKILLS_DIR"
SKILLS_DIR_DEFAULT = Path.home() / ".claude" / "skills"
# Byte cap on the appended `## Loaded skills` bundle, per lane. Over cap refuses
# the launch with each skill's size; nothing is truncated. A lane not listed
# here gets SKILLS_DEFAULT_BYTE_CAP; an installation module adds caps for the
# lanes it defines.
SKILLS_BYTE_CAPS = {
    "codex": 120_000,
    "cursor": 120_000,
    "ollama": 60_000,
    "openrouter": 60_000,
}
SKILLS_DEFAULT_BYTE_CAP = 60_000
SKILLS_HEADING_RE = re.compile(r"^##[ \t]+Skills[ \t]*$", re.IGNORECASE)
SKILLS_FIELDS = {"load"}
SKILL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
CHANNEL_DOWN_EXIT = 3
CHANNEL_DOWN_MESSAGE = "delegate.py: channel down, work halted"
LANE_REFUSED_EXIT = 3
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
GRANTS_FIELDS = {"paths-write", "network", "github-writes", "tools"}
GRANTS_HEADING_RE = re.compile(r"^##[ \t]+Grants[ \t]*$", re.IGNORECASE)
MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}[ \t]+")
MARKDOWN_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
GRANT_FIELD_RE = re.compile(r"^[ \t]*([a-z][a-z-]*):[ \t]*(.*?)[ \t]*$", re.IGNORECASE)
GLOB_MAGIC_RE = re.compile(r"[*?\[]")
DIRECTIVE_RE = re.compile(r"^\S.+$")
CURSOR_DIRECTED_RULE = "This Cursor model requires --directive"
CODEX_MODELS = {}
OLLAMA_MODELS = {}
OLLAMA_MODEL_FLAGS = {}
OLLAMA_IMAGE_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}
OLLAMA_IMAGE_MAX_WIDTH = 1024
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_TOKEN_REF = "OPENROUTER_API_KEY"
OPENROUTER_REFERER = "https://github.com/dispatch-agent/dispatch-agent"
OPENROUTER_TITLE = "dispatch-agent"
OPENROUTER_PROVIDER_BLOCK = {"zdr": True, "data_collection": "deny", "allow_fallbacks": False}
OPENROUTER_MODELS = {}
OPENROUTER_NO_TOOLS = frozenset()
OPENROUTER_RETRY_ATTEMPTS = 6
OPENROUTER_RETRY_BASE_SECONDS = 5.0
OPENROUTER_RETRY_MAX_SECONDS = 120.0
OPENROUTER_RETRY_JITTER = 0.25
OPENROUTER_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
CURSOR_MODELS = {}
CURSOR_DIRECTED_MODELS = frozenset()
CURSOR_INCLUDED_MODEL = ""
CURSOR_BIN = "agent"
CURSOR_ATTEMPTS = 1
CURSOR_DEFAULT_IDLE_TIMEOUT = 600
CURSOR_RESULT_EXIT_GRACE = 3
CODEX_FAST_CONFIG = ('service_tier="fast"', "features.fast_mode=true")
FAST_DIRECTIVE_RULE = "--fast requires --directive"
GENERIC_MODELS = {}
GENERIC_URL = ""
GENERIC_TOKEN_REF = "OPENAI_API_KEY"
ALLOW_SHELL_TOOL = False
INHERIT_CHILD_ENV = False
HTTP_BODY_LIMIT = 4_000_000
HTTP_ERROR_LIMIT = 4_096
HTTP_URL_LIMIT = 2_048
PASS_ENV = ()
_TEMP_CHILD_HOME = None
MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}\Z")


def validate_model_id(value):
    if not isinstance(value, str) or not MODEL_ID_RE.fullmatch(value):
        raise DelegateError("model id must be 1-128 safe ASCII characters")
    return value


def validate_http_url(url):
    if not isinstance(url, str) or len(url) > HTTP_URL_LIMIT or any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise DelegateError("invalid HTTP URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise DelegateError("invalid HTTP URL") from exc
    if not host or parsed.username is not None or parsed.password is not None or "#" in url:
        raise DelegateError("HTTP URL requires a host without userinfo or fragment")
    if parsed.scheme == "https":
        return url
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if parsed.scheme != "http" or not loopback:
        raise DelegateError("HTTP URL must use HTTPS or loopback HTTP")
    return url


def endpoint_origin(url):
    """Return only the scheme and network location safe to show in a plan."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    if not host:
        raise DelegateError("HTTP URL requires a host")
    host = f"[{host}]" if ":" in host else host
    try:
        port = parsed.port
    except ValueError as exc:
        raise DelegateError("invalid HTTP URL") from exc
    return f"{parsed.scheme}://{host}{f':{port}' if port is not None else ''}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DelegateError("HTTP redirects are disabled")


urllib.request.install_opener(urllib.request.build_opener(_NoRedirect()))


def _open_url(req, timeout):
    validate_http_url(req.full_url)
    return urllib.request.urlopen(req, timeout=timeout)


def _read_limited(stream, limit=HTTP_BODY_LIMIT):
    try:
        raw = stream.read(limit + 1)
    except TypeError:
        # Some response adapters expose a parameterless read method.
        raw = stream.read()
    if len(raw) > limit:
        raise DelegateError("HTTP response exceeds size limit")
    return raw.decode("utf-8", errors="replace")



SCRIPT_NAME = Path(sys.argv[0]).name or Path(__file__).name
CHANNEL_DOWN_MESSAGE = f"{SCRIPT_NAME}: channel down, work halted"
OPENROUTER_NO_TOOLS_MESSAGE = "that supports tool use. Use it without --write."
MODEL_ALIASES = ()
EFFORT_BACKENDS = {"codex", "cursor"}
EFFORT_BACKEND_ERROR = "--effort is codex/cursor-lane only"
BACKEND_AUTH = {
    "codex": {"CODEX_HOME"},
    "cursor": {"CURSOR_API_KEY", "CURSOR_API_ENDPOINT"},
    "ollama": {"OLLAMA_API_KEY"},
}


def before_launch(_ctx):
    return None


def after_run(_ctx, _result):
    return None


def add_arguments(_parser):
    return None


def prepare_launch(args, plan):
    return {}


def update_dry_plan(args, plan, state, dry_plan):
    dry_plan["cooldown"] = public_cooldown_marker(plan["lane"])


def check_launch_state(args, plan, state):
    return None


def print_launch_status(args, plan, state):
    return None


def update_audit_record(record, args):
    return None


def record_lane_exhaustion(plan, model, task_class, error_text, spec=None):
    return None


def run_cli_hook(argv):
    return False


def load_config():
    try:
        with CONFIG_PATH.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DelegateError(f"cannot read config {CONFIG_PATH}: {exc}") from exc
    if not isinstance(data, dict):
        raise DelegateError("config must be a table")
    return data


def public_cooldown_settings():
    general = load_config().get("general", {})
    try:
        seconds = int(general.get("cooldown_seconds", 0))
    except (TypeError, ValueError) as exc:
        raise DelegateError("general.cooldown_seconds must be an integer") from exc
    if seconds < 0:
        raise DelegateError("general.cooldown_seconds must be nonnegative")
    path = Path(os.path.expanduser(general.get(
        "cooldown_file", "~/.local/state/dispatch-agent/cooldowns.json"
    )))
    return seconds, path


def public_cooldown_marker(lane):
    seconds, path = public_cooldown_settings()
    if not seconds:
        return None
    try:
        data = json.loads(path.read_text())
        marker = data.get(lane)
        return marker if marker and marker.get("until", 0) > time.time() else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def record_public_cooldown(lane, error):
    seconds, path = public_cooldown_settings()
    if not seconds or not re.search(r"\b429\b|usage limit|rate.?limit", error, re.IGNORECASE):
        return
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[lane] = {"until": time.time() + seconds}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
            json.dump(data, handle)
            temporary = handle.name
        os.replace(temporary, path)
    except OSError:
        pass


def configure_public(args):
    """Select one configured model; CLI options override its settings."""
    global OLLAMA_URL, OLLAMA_CHAT_URL, OLLAMA_TOKEN_REF, OPENROUTER_URL
    global OPENROUTER_TOKEN_REF, GENERIC_URL, GENERIC_TOKEN_REF
    config = load_config()
    models = config.get("models", {})
    entry = models.get(args.model, {}) if isinstance(models, dict) else {}
    backend = args.backend or entry.get("backend")
    if not backend:
        raise DelegateError(f"model {args.model!r} needs a [models.{args.model}] config entry or --backend")
    model_id = args.model_id or entry.get("model") or args.model
    validate_model_id(model_id)
    providers = config.get("providers", {})
    provider = providers.get(backend, {}) if isinstance(providers, dict) else {}
    base_url = args.base_url or entry.get("base_url") or provider.get("base_url")
    if base_url:
        validate_http_url(base_url)
    key_env = args.api_key_env or entry.get("api_key_env") or provider.get("api_key_env")
    if backend == "codex":
        CODEX_MODELS[args.model] = model_id
    elif backend == "cursor":
        efforts = entry.get("efforts", ["low", "medium", "high", "xhigh"])
        CURSOR_MODELS[args.model] = (model_id, tuple(efforts), entry.get("default_effort", "high"), bool(entry.get("fast", False)))
    elif backend in {"ollama", "ollama-cloud"}:
        OLLAMA_MODELS[args.model] = model_id
        OLLAMA_MODEL_FLAGS[args.model] = {"vision": bool(entry.get("vision", False))}
        host = (base_url or ("https://ollama.com" if backend == "ollama-cloud" else "http://127.0.0.1:11434")).rstrip("/")
        OLLAMA_URL = host + "/v1/chat/completions"
        OLLAMA_CHAT_URL = host + "/api/chat"
        OLLAMA_TOKEN_REF = key_env or "OLLAMA_API_KEY"
    elif backend == "openrouter":
        OPENROUTER_MODELS[args.model] = model_id
        OPENROUTER_URL = (base_url or "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions"
        OPENROUTER_TOKEN_REF = key_env or "OPENROUTER_API_KEY"
    elif backend == "openai-compatible":
        GENERIC_MODELS[args.model] = model_id
        GENERIC_URL = (base_url or "").rstrip("/") + "/chat/completions"
        GENERIC_TOKEN_REF = key_env or "OPENAI_API_KEY"
        if not base_url:
            raise DelegateError("openai-compatible backend requires --base-url or config base_url")
    else:
        raise DelegateError(f"unsupported backend: {backend}")


def run_compatible(model, spec, timeout):
    token = op_read(GENERIC_TOKEN_REF, min(timeout, 30), provider="OpenAI-compatible")
    data = json.dumps({"model": GENERIC_MODELS[model], "messages": [{"role": "user", "content": spec}]}).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(GENERIC_URL, data=data, method="POST", headers=headers)
    try:
        with _open_url(req, timeout) as response:
            payload = json.loads(_read_limited(response))
        content = payload["choices"][0]["message"]["content"]
        log_usage("openai-compatible", GENERIC_MODELS[model], payload.get("usage"))
        return content
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise DelegateError(f"compatible API request failed: {exc}") from exc


class DelegateError(Exception):
    """Expected user/backend failure with a clean stderr message."""


class GrantsError(DelegateError):
    """Declared grants refuse the requested launch (exit 2)."""


class ChannelDownError(DelegateError):
    """An installation-specific channel is down (exit 3)."""


class LaneRefusedError(DelegateError):
    """The target lane has a live exhaustion marker; refused before spawn (exit 3)."""


class LaneExhaustedError(DelegateError):
    """A backend failed with a vendor usage-limit message; marker written (exit 1)."""

    def __init__(self, message, fall_through_line):
        super().__init__(message)
        self.fall_through_line = fall_through_line


class CursorWorkerEnvironment(dict):
    """Process environment carrying runner-only Cursor watchdog metadata."""

    cursor_idle_timeout = None


def refuse_nested_delegate():
    if os.environ.get(LEAF_WORKER_ENV) == "1":
        raise DelegateError(
            f"nested delegation refused: {LEAF_WORKER_ENV}=1 marks a Cursor leaf worker"
        )


def nested_codex_capability():
    return os.environ.get(NESTED_CODEX_CAPABILITY_ENV) == NESTED_CODEX_CAPABILITY


def grants_log_paths(cwd, default_log=None):
    """Return the preferred audit-log path followed by a workspace fallback."""
    explicit_dir = os.environ.get(DELEGATE_LOG_DIR_ENV)
    if explicit_dir:
        explicit_dir = os.path.abspath(os.path.expanduser(explicit_dir))
        return [os.path.join(explicit_dir, "delegate-grants.jsonl")]
    workspace_dir = os.path.join(
        os.path.abspath(os.path.expanduser(cwd)), ".delegate", "logs"
    )
    return [default_log or GRANTS_LOG, os.path.join(workspace_dir, "delegate-grants.jsonl")]


def resolve_cursor_model_id(model, effort=None, fast=False):
    """Resolve only an allow-listed Cursor menu id to an explicit agent CLI id."""
    base, efforts, default_effort, has_fast = CURSOR_MODELS[model]
    level = effort or default_effort
    if level not in efforts:
        raise DelegateError(f"{model} --effort must be one of: {', '.join(efforts)}")
    return f"{base}-{level}" + ("-fast" if fast and has_fast else "")


def resolve_model(model, effort=None, fast=False):
    for lane, roster in (('codex', CODEX_MODELS), ('cursor', CURSOR_MODELS), ('ollama', OLLAMA_MODELS), ('openrouter', OPENROUTER_MODELS), ('openai-compatible', GENERIC_MODELS)):
        if model in roster:
            resolved = resolve_cursor_model_id(model, effort, fast) if lane == 'cursor' else roster[model]
            validate_model_id(resolved)
            return {'model_requested': model, 'model_resolved': resolved, 'lane': lane}
    raise DelegateError(f'model {model!r} is not configured')

def validate_cursor_directive(model, directive):
    """Fail closed before launch for every paid Cursor-directed menu id."""
    if model in CURSOR_DIRECTED_MODELS and (
        not directive or DIRECTIVE_RE.fullmatch(directive) is None
    ):
        raise DelegateError(CURSOR_DIRECTED_RULE)


def channel_down_flag_path():
    return None


def check_channel_down():
    """Keep current work running during a Telegram outage (Toby, 2026-10-01).

    The owning session alerts CoS through A2A and holds every decision.
    Retain this compatibility entry point without gating or killing workers.
    """
    return None


def terminate_worker(proc):
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


def run_worker(argv, cwd, timeout, env):
    cursor_idle_timeout = getattr(env, "cursor_idle_timeout", None)
    if cursor_idle_timeout is not None and argv and argv[0] == CURSOR_BIN:
        return run_cursor_stream_worker(
            argv,
            cwd=cwd,
            timeout=timeout,
            idle_timeout=cursor_idle_timeout,
            env=env,
        )

    check_channel_down()
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout
    while True:
        try:
            stdout, stderr = proc.communicate(
                timeout=min(1.0, max(0.0, deadline - time.monotonic()))
            )
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                proc.kill()
                proc.communicate()
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                check_channel_down()
            except ChannelDownError:
                terminate_worker(proc)
                raise
            continue

        check_channel_down()
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _stop_cursor_process_group(proc):
    """Stop the Cursor CLI and any worker/LSP children it left in its process group."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def run_cursor_stream_worker(argv, cwd, timeout, idle_timeout, env):
    """Consume Cursor stream-json live and return as soon as its terminal result arrives.

    Cursor can finish a model turn but leave its CLI process alive. Reading the event
    stream makes that terminal state observable without forwarding thinking/tool events.
    Any stdout/stderr byte resets the no-output watchdog.
    """
    check_channel_down()
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
    started = time.monotonic()
    deadline = started + timeout
    last_activity = started
    result_payload = None
    result_seen_at = None
    stdout_pending = b""
    stderr_chunks = []
    event_summaries = []

    def consume_stdout(chunk):
        nonlocal stdout_pending, result_payload, result_seen_at
        stdout_pending += chunk
        lines = stdout_pending.split(b"\n")
        stdout_pending = lines.pop()
        for raw_line in lines:
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                event_summaries.append("invalid-json")
                continue
            event_type = str(event.get("type") or "unknown")
            event_subtype = str(event.get("subtype") or "")
            event_summaries.append(
                f"{event_type}:{event_subtype}" if event_subtype else event_type
            )
            if event_type == "result":
                result_payload = event
                result_seen_at = time.monotonic()

    try:
        while True:
            now = time.monotonic()
            if result_payload is not None and proc.poll() is not None:
                break
            if (
                result_seen_at is not None
                and now - result_seen_at >= CURSOR_RESULT_EXIT_GRACE
            ):
                _stop_cursor_process_group(proc)
                break
            if now >= deadline:
                _stop_cursor_process_group(proc)
                raise subprocess.TimeoutExpired(argv, timeout)
            if now - last_activity >= idle_timeout:
                _stop_cursor_process_group(proc)
                raise DelegateError(
                    f"cursor idle-timeout after {idle_timeout}s with no output"
                )
            try:
                check_channel_down()
            except ChannelDownError:
                _stop_cursor_process_group(proc)
                raise

            waits = [1.0, deadline - now, idle_timeout - (now - last_activity)]
            if result_seen_at is not None:
                waits.append(CURSOR_RESULT_EXIT_GRACE - (now - result_seen_at))
            events = selector.select(timeout=max(0.0, min(waits)))
            for key, _mask in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(key.fileobj)
                    except (KeyError, ValueError):
                        pass
                    continue
                last_activity = time.monotonic()
                if key.data == "stdout":
                    consume_stdout(chunk)
                else:
                    stderr_chunks.append(chunk)

            if proc.poll() is not None and not selector.get_map():
                break
    finally:
        selector.close()

    if proc.poll() is None:
        _stop_cursor_process_group(proc)
    returncode = proc.returncode
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    if result_payload is not None:
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(result_payload, separators=(",", ":")),
            stderr,
        )
    summary = ", ".join(event_summaries[-12:])
    if summary:
        stderr = f"{stderr.rstrip()}\ncursor stream ended without result; events: {summary}".lstrip()
    return subprocess.CompletedProcess(argv, returncode, "", stderr)


def read_spec(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def parse_grants(spec):
    """Return (declared, fields, error) for the first Grants markdown section."""
    lines = spec.splitlines()
    start = None
    fence = None
    for index, line in enumerate(lines):
        fence_match = MARKDOWN_FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence is None and GRANTS_HEADING_RE.match(line):
            start = index + 1
            break
    if start is None:
        return False, {}, None

    fields = {}
    fence = None
    for line in lines[start:]:
        fence_match = MARKDOWN_FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence is None and MARKDOWN_HEADING_RE.match(line):
            break
        match = GRANT_FIELD_RE.match(line)
        if not match:
            continue
        key = match.group(1).lower()
        if key not in GRANTS_FIELDS:
            continue
        if key in fields:
            return True, fields, f"duplicate {key} entry in ## Grants"
        fields[key] = match.group(2).strip()
    return True, fields, None


class SkillsError(DelegateError):
    """A skills request cannot be satisfied; refused before any worker starts."""


def parse_skills_section(spec):
    """Return (declared, fields, error) for the first `## Skills` section.

    Same rules as parse_grants: fence-aware, first section wins, a duplicate
    field is an error, unknown fields are ignored.
    """
    lines = spec.splitlines()
    start = None
    fence = None
    for index, line in enumerate(lines):
        fence_match = MARKDOWN_FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence is None and SKILLS_HEADING_RE.match(line):
            start = index + 1
            break
    if start is None:
        return False, {}, None

    fields = {}
    fence = None
    for line in lines[start:]:
        fence_match = MARKDOWN_FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence is None and MARKDOWN_HEADING_RE.match(line):
            break
        match = GRANT_FIELD_RE.match(line)
        if not match:
            continue
        key = match.group(1).lower()
        if key not in SKILLS_FIELDS:
            continue
        if key in fields:
            return True, fields, f"duplicate {key} entry in ## Skills"
        fields[key] = match.group(2).strip()
    return True, fields, None


def parse_skill_names(raw, source):
    """Parse `a, b` or `none` into a list of names; refuse anything else."""
    if raw is None or not raw.strip():
        raise SkillsError(f"skills entry is empty ({source}); use a comma-separated list or none")
    if raw.strip().lower() == "none":
        return []
    names = [item.strip() for item in raw.split(",")]
    if any(not name for name in names):
        raise SkillsError(f"skills entry has an empty name ({source}): {raw!r}")
    if any(name.lower() == "none" for name in names):
        raise SkillsError(f'skills must be either "none" or a comma-separated list ({source})')
    bad = [name for name in names if not SKILL_NAME_RE.fullmatch(name)]
    if bad:
        raise SkillsError(f"invalid skill name ({source}): {', '.join(bad)}")
    seen = set()
    duplicates = [name for name in names if name in seen or seen.add(name)]
    if duplicates:
        raise SkillsError(f"duplicate skill name ({source}): {', '.join(duplicates)}")
    return names


def requested_skills(spec, cli_value=None):
    """Return (names, source). The --skills flag beats the spec's ## Skills section."""
    if cli_value is not None:
        return parse_skill_names(cli_value, "--skills"), "cli"
    declared, fields, error = parse_skills_section(spec)
    if error:
        raise SkillsError(error)
    if not declared:
        return [], None
    if "load" not in fields:
        raise SkillsError("## Skills section has no load: entry")
    return parse_skill_names(fields["load"], "## Skills"), "spec"


def skills_dir():
    override = os.environ.get(SKILLS_DIR_ENV)
    return Path(os.path.expanduser(override)) if override else SKILLS_DIR_DEFAULT


def strip_frontmatter(text):
    """Drop a leading YAML frontmatter block (--- ... --- or ...)."""
    text = text.lstrip("﻿")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return text
    for index in range(1, len(lines)):
        if lines[index].rstrip() in {"---", "..."}:
            return "".join(lines[index + 1:])
    raise SkillsError("unterminated YAML frontmatter")


def resolve_skills(names, base=None):
    """Return [(name, body)] in the order given; refuse every unknown name at once."""
    base = Path(base) if base is not None else skills_dir()
    missing = []
    loaded = []
    for name in names:
        path = base / name / "SKILL.md"
        if not SKILL_NAME_RE.fullmatch(name) or not path.is_file():
            missing.append(f"{name} (no {path})")
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SkillsError(f"cannot read skill {name}: {path}: {exc}") from exc
        try:
            body = strip_frontmatter(raw).strip()
        except SkillsError as exc:
            raise SkillsError(f"skill {name}: {exc} in {path}") from exc
        if not body:
            raise SkillsError(f"skill {name} has an empty body: {path}")
        loaded.append((name, body))
    if missing:
        raise SkillsError("unknown skill: " + "; ".join(missing))
    return loaded


def format_skills_bundle(loaded):
    """Return the `## Loaded skills` section text, or "" for no skills."""
    if not loaded:
        return ""
    parts = [f"### {name}\n\n{body}" for name, body in loaded]
    return "## Loaded skills\n\n" + "\n\n".join(parts) + "\n"


def skills_bundle(names, base=None):
    """Resolve and format in one step; shared with skill-bundle.py."""
    return format_skills_bundle(resolve_skills(names, base))


def check_skills_cap(lane, loaded, bundle):
    """Return the bundle's byte size, or refuse it with every skill's size."""
    cap = SKILLS_BYTE_CAPS.get(lane, SKILLS_DEFAULT_BYTE_CAP)
    size = len(bundle.encode("utf-8"))
    if size > cap:
        sizes = ", ".join(f"{name}={len(body.encode('utf-8'))}" for name, body in loaded)
        raise SkillsError(
            f"skills bundle {size} bytes exceeds the {lane} lane cap of {cap} bytes "
            f"(skill sizes in bytes: {sizes}); name fewer or smaller skills"
        )
    return size


def append_skills(spec, bundle):
    """Append the bundle after the spec text; no bundle leaves the spec unchanged."""
    if not bundle:
        return spec
    return spec.rstrip("\n") + "\n\n" + bundle


def _prepare_skills_legacy(spec, args, lane):
    """Resolve the requested skills and return (prompt, names, bytes)."""
    names, _source = requested_skills(spec, getattr(args, "skills", None))
    loaded = resolve_skills(names)
    bundle = format_skills_bundle(loaded)
    size = check_skills_cap(lane, loaded, bundle)
    return append_skills(spec, bundle), [name for name, _body in loaded], size


class SkillRouteError(SkillsError):
    """An explicitly enforced route cannot authorize a launch (exit 2)."""


def prepare_skills(spec, args, lane):
    """Observe real launches; retain the exact legacy skill-loading function."""
    if getattr(args, "dry_run", False) and not getattr(args, "dry_run_route", False):
        return _prepare_skills_legacy(spec, args, lane)
    try:
        from skillroute.shadow_hook import prepare_skills as routed_prepare
        from types import SimpleNamespace
        return routed_prepare(SimpleNamespace(**globals()), spec, args, lane)
    except SkillsError:
        raise
    except (Exception, SystemExit) as exc:
        # Import/config/observer faults never prevent a legacy shadow launch.
        print("delegate.py skill route: verdict=REVIEW note=observer failure "
              + type(exc).__name__, file=sys.stderr)
        return _prepare_skills_legacy(spec, args, lane)


def _paths_write_globs(raw):
    if raw is None:
        return None, None
    if not raw.strip():
        return None, "paths-write entry is empty"
    if raw.strip().lower() == "none":
        return [], None
    globs = [item.strip() for item in raw.split(",") if item.strip()]
    if not globs:
        return None, "paths-write entry is empty"
    if any(item.lower() == "none" for item in globs):
        return None, 'paths-write must be either "none" or a comma-separated glob list'
    return globs, None


def _glob_anchor(pattern, cwd):
    expanded = os.path.expanduser(pattern)
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    absolute = os.path.abspath(expanded)
    anchor_parts = []
    for part in absolute.split(os.sep):
        if not part:
            continue
        if GLOB_MAGIC_RE.search(part):
            break
        anchor_parts.append(part)
    anchor = os.sep + os.path.join(*anchor_parts) if anchor_parts else os.sep
    return os.path.realpath(anchor)


def _glob_can_touch_cwd(pattern, cwd):
    cwd = os.path.realpath(os.path.abspath(os.path.expanduser(cwd)))
    anchor = _glob_anchor(pattern, cwd)
    try:
        common = os.path.commonpath([cwd, anchor])
    except ValueError:
        return False
    return common == cwd or common == anchor


def _grants_summary(fields, paths_globs):
    if "paths-write" not in fields:
        paths_summary = None
    elif paths_globs == []:
        paths_summary = "none"
    else:
        paths_summary = paths_globs
    return {
        "paths-write": paths_summary,
        "network": fields.get("network"),
        "github-writes": fields.get("github-writes"),
        "tools": fields.get("tools"),
    }


def log_grants_decision(spec_path, grants, args, plan, verdict, reason=None):
    if args.dry_run:
        return
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cwd = os.path.abspath(os.path.expanduser(args.cwd))
    if args.grants_check_only:
        event = "grants-check"
    else:
        event = "launch"
    record = {
        "timestamp": ts,  # retained for existing log readers
        "ts": ts,
        "cwd": cwd,
        "model_requested": plan["model_requested"],
        "model_resolved": plan["model_resolved"],
        "lane": plan["lane"],
        "directive": args.directive,
        "cursor_pool": plan.get("cursor_pool"),
        "event": event,
        "spec_path": "-" if spec_path == "-" else os.path.abspath(os.path.expanduser(spec_path)),
        "grants": grants,
        "flags": {
            "write": args.write,
            "network": args.network,
            "web": args.web,
            "cwd": cwd,
        },
        "verdict": verdict,
        "task_class": getattr(args, "task_class", None),
    }
    if event == "launch":
        record["skills_loaded"] = list(getattr(args, "skills_loaded", None) or [])
        record["skills_bytes"] = getattr(args, "skills_bytes", None) or 0
        for key in ("route_id", "route_verdict", "skills_routed", "skills_override_reason"):
            if hasattr(args, key):
                record[key] = getattr(args, key)
    if getattr(args, "fast", False):

        record["fast"] = True
    update_audit_record(record, args)
    if getattr(args, "max_iters", None) is not None:
        # Only the Ollama/OpenRouter write hand-off consumes it; record it
        # when the caller gave it so the launch audit shows the chosen cap.
        record["max_iters"] = args.max_iters
    if reason:
        record["reason"] = reason
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    errors = []
    for log_path in grants_log_paths(cwd):
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
            if log_path != GRANTS_LOG:
                print(f"{SCRIPT_NAME}: grants log: {log_path}", file=sys.stderr)
            return
        except OSError as e:
            errors.append(f"{log_path}: {e}")
    detail = "; ".join(errors)
    if event == "launch" and verdict in {"allow", "allow-no-grants"}:
        raise DelegateError(f"launch log required but unavailable: {detail}")
    print(f"{SCRIPT_NAME}: grants log unavailable (continuing): {detail}", file=sys.stderr)


def validate_grants(spec, spec_path, args, plan):
    declared, fields, parse_error = parse_grants(spec)
    flags = f"--write={args.write}, --network={args.network}, --web={args.web}"
    if not declared:
        log_grants_decision(spec_path, None, args, plan, "allow-no-grants")
        print(
            f"{SCRIPT_NAME}: no grants declared ({spec_path}); accepted format:\n{GRANTS_FORMAT}\n"
            f"optional, beside it:\n{SKILLS_FORMAT}",
            file=sys.stderr,
        )
        return

    paths_globs, paths_error = _paths_write_globs(fields.get("paths-write"))
    summary = _grants_summary(fields, paths_globs)
    reason = parse_error or paths_error
    for key in ("network", "github-writes"):
        value = fields.get(key)
        if reason is None and value is not None and value.lower() not in {"yes", "no"}:
            reason = f'{key} must be "yes" or "no"'
    if reason is None and args.write and "paths-write" not in fields:
        reason = "--write requires a paths-write entry in ## Grants"
    if reason is None and args.write and paths_globs == []:
        reason = '--write is not allowed because paths-write is "none"'
    if (
        reason is None
        and (args.network or args.web)
        and fields.get("network", "").lower() == "no"
    ):
        reason = "--network/--web is not allowed because network is \"no\""
    if (
        reason is None
        and args.write
        and paths_globs
        and not any(_glob_can_touch_cwd(pattern, args.cwd) for pattern in paths_globs)
    ):
        reason = f"every paths-write glob falls outside --cwd ({args.cwd})"

    if reason is not None:
        log_grants_decision(spec_path, summary, args, plan, "refuse", reason)
        raise GrantsError(f"grants refused launch: {reason}; flags: {flags}")
    log_grants_decision(spec_path, summary, args, plan, "allow")


def strip_codex_wrappers(text):
    """Remove common headless CLI framing while leaving model content intact."""
    lines = text.splitlines()
    while lines and _looks_like_codex_wrapper(lines[0]):
        lines.pop(0)
    while lines and _looks_like_codex_wrapper(lines[-1]):
        lines.pop()
    return "\n".join(lines).strip() + ("\n" if lines else "")


def _looks_like_codex_wrapper(line):
    s = line.strip()
    if not s:
        return False
    lower = s.lower()
    return (
        lower.startswith("codex ")
        or lower.startswith("model:")
        or lower.startswith("provider:")
        or lower.startswith("tokens:")
        or lower.startswith("usage:")
        or lower.startswith("reasoning:")
        or lower in {"---", "```"}
    )


# Remove message-capable credentials from child processes.
SCRUBBED_ENV_VARS = (
    "OPENAI_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_PAT",
)
_GITHUB_SCRUBBED_ENV_VARS = tuple(name for name in SCRUBBED_ENV_VARS if name != "OPENAI_API_KEY")


def _scrubbed_env(base=None, extra=(), backend=None):
    """Build a minimal child environment, or use an explicit installation compatibility policy."""
    source = dict(base) if base is not None else os.environ.copy()
    if INHERIT_CHILD_ENV:
        env = source
    else:
        allowed = {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LANGUAGE", "LC_ALL",
                   "LC_CTYPE", "TZ"}
        allowed.update(BACKEND_AUTH.get(backend, ()))
        allowed.update(PASS_ENV)
        env = {name: value for name, value in source.items() if name in allowed}
        if backend not in {"codex", "cursor"}:
            global _TEMP_CHILD_HOME
            if _TEMP_CHILD_HOME is None:
                _TEMP_CHILD_HOME = tempfile.TemporaryDirectory(prefix="dispatch-home-")
                atexit.register(_TEMP_CHILD_HOME.cleanup)
            env["HOME"] = _TEMP_CHILD_HOME.name
    to_scrub = (set(_GITHUB_SCRUBBED_ENV_VARS) | set(extra)) if INHERIT_CHILD_ENV else (set(extra) - set(PASS_ENV))
    removed = [name for name in SCRUBBED_ENV_VARS if name in to_scrub and name in env]
    for name in removed:
        del env[name]
    if removed:
        print(f"{SCRIPT_NAME}: scrubbed from worker env: {', '.join(removed)}", file=sys.stderr)
    return env


def _self_reexec_env():
    """Preserve this trusted CLI's settings when changing Python interpreters."""
    return os.environ.copy()


@contextmanager
def cursor_launch(
    model,
    spec,
    cwd,
    write=False,
    effort=None,
    fast=False,
    idle_timeout=CURSOR_DEFAULT_IDLE_TIMEOUT,
):
    cursor_id = resolve_cursor_model_id(model, effort=effort, fast=fast)
    env = CursorWorkerEnvironment(_scrubbed_env(backend='cursor'))
    env.cursor_idle_timeout = idle_timeout
    argv = [CURSOR_BIN, '-p', '--trust', '--model', cursor_id, '--output-format', 'stream-json', '--sandbox', 'enabled', '--workspace', cwd]
    if write:
        argv.append('--force')
    else:
        argv.extend(['--mode', 'ask'])
    argv.extend(['--', spec])
    yield {'argv': argv, 'env': env, 'env_report': {'set': {}, 'unset': list(_GITHUB_SCRUBBED_ENV_VARS) if INHERIT_CHILD_ENV else [], 'inherit_other_environment': INHERIT_CHILD_ENV, 'pass_env': list(PASS_ENV)}, 'hook_files': {}, 'disabled_plugins': []}
    return

def run_cursor(
    model,
    spec,
    cwd,
    timeout,
    write=False,
    effort=None,
    fast=False,
    idle_timeout=CURSOR_DEFAULT_IDLE_TIMEOUT,
):
    """Run the spec headless on the Cursor CLI lane. No fallback chain: the requested
    variant runs or the call fails loud. --write maps to --force (apply edits);
    read-only runs use --mode ask. Sandbox stays enabled either way. A run-scoped
    plugin and native tool exclusions force the worker to remain a leaf."""
    cursor_id = resolve_cursor_model_id(model, effort=effort, fast=fast)
    with cursor_launch(
        model,
        spec,
        cwd,
        write=write,
        effort=effort,
        fast=fast,
        idle_timeout=idle_timeout,
    ) as launch:
        proc = run_worker(
            launch["argv"], cwd=cwd, timeout=timeout, env=launch["env"]
        )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise DelegateError(f"cursor agent ({cursor_id}) failed with exit {proc.returncode}\n{detail}")
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as e:
        raise DelegateError(f"unexpected cursor agent output\n{proc.stdout[:2000]}") from e
    if payload.get("is_error") or payload.get("subtype") != "success":
        raise DelegateError(f"cursor agent ({cursor_id}) reported failure\n{json.dumps(payload)[:2000]}")
    usage = payload.get("usage") or {}
    log_usage(
        "cursor",
        cursor_id,
        {
            "prompt_tokens": usage.get("inputTokens"),
            "completion_tokens": usage.get("outputTokens"),
            "cache_read_tokens": usage.get("cacheReadTokens"),
            "total_tokens": (usage.get("inputTokens") or 0) + (usage.get("outputTokens") or 0),
        },
    )
    result = payload.get("result")
    if not isinstance(result, str):
        raise DelegateError(f"cursor agent returned no result field\n{json.dumps(payload)[:2000]}")
    return result


def run_codex(
    model, spec, cwd, timeout, write=False, web=False, effort=None, network=False, fast=False
):
    """Run one exact allow-listed OpenAI/Codex id via ChatGPT OAuth; never fall forward."""
    codex_id = CODEX_MODELS[model]
    # force ChatGPT OAuth (never the metered API key) and strip GitHub tokens

    env = _scrubbed_env(extra=("OPENAI_API_KEY",), backend="codex")
    nested = nested_codex_capability()
    if write and network:
        # Only a launcher invocation carrying both capabilities may authorize a
        # Codex worker to launch another Codex worker.
        env[NESTED_CODEX_CAPABILITY_ENV] = NESTED_CODEX_CAPABILITY
    if nested:
        # Codex's normal home contains mutable application state. Keep
        # nested state in the parent's writable workspace. Link only the OAuth
        # credential read-only; do not copy secrets into the workspace.
        nested_home = os.path.join(
            os.path.abspath(os.path.expanduser(cwd)), ".delegate", "codex-home"
        )
        os.makedirs(nested_home, exist_ok=True)
        auth_source = os.path.join(os.path.expanduser("~/.codex"), "auth.json")
        auth_target = os.path.join(nested_home, "auth.json")
        if not os.path.lexists(auth_target):
            try:
                os.symlink(auth_source, auth_target)
            except FileExistsError:
                # Two children launched in one shell command (`a & b & wait`)
                # race here; the loser is fine when the winner's link already
                # points at the same credential (BOSS, 2026-10-06, director proof).
                if os.readlink(auth_target) != auth_source:
                    raise DelegateError(
                        f"nested Codex auth link unavailable: {auth_target}: "
                        "exists with a different target"
                    )
            except OSError as e:
                raise DelegateError(
                    f"nested Codex auth link unavailable: {auth_target}: {e}"
                ) from e
        env["CODEX_HOME"] = nested_home
    # Network access can turn dormant notification code into a real outbound
    # message. Remove configured notification credentials from this child.
    for _leak in WORKER_ENV_REMOVE:
        env.pop(_leak, None)
    # Toby 2026-10-08: ChatGPT connector grants (HubSpot, Gmail, GitHub...) are
    # mounted into every Codex launch by the "apps" feature; the fleet must never see them.
    # Verified: eval-outputs/personal-bot-2026-10-08/REPORT-PROBE.md.
    argv = ["codex", "exec", "--disable", "apps", "--model", codex_id, "--skip-git-repo-check", "--", spec]
    if fast:


        argv[2:2] = [item for value in CODEX_FAST_CONFIG for item in ("-c", value)]
    if nested:
        # A Codex worker already runs inside the parent's workspace-write
        # sandbox. Applying a second macOS sandbox from inside that sandbox
        # fails with sandbox_apply: Operation not permitted; bypass only this
        # marked child so the parent's sandbox remains the outer boundary.
        argv[2:2] = ["--dangerously-bypass-approvals-and-sandbox"]
    elif write:
        argv[2:2] = ["-s", "workspace-write"]
    if network:
        # Outbound network inside the exec sandbox, independent of --web.
        # Requires workspace-write; codex ignores it under the read-only sandbox.
        argv[2:2] = ["-c", "sandbox_workspace_write.network_access=true"]
    if web:
        # Backend web_search is independent of the local exec sandbox.
        argv[2:2] = ["-c", "tools.web_search=true"]
    if effort:
        # overrides ~/.codex/config.toml model_reasoning_effort (default xhigh);

        argv[2:2] = ["-c", f"model_reasoning_effort={effort}"]
    proc = run_worker(
        argv,
        cwd=cwd,
        timeout=timeout,
        env=env,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise DelegateError(
            f"codex exec ({codex_id}) failed with exit {proc.returncode}\n{detail}"
        )
    # Check codex's own log stream only: the final answer on stdout may quote this
    # text (e.g. a report about sandboxes) without any apply having failed.
    if write and "sandbox_apply: Operation not permitted" in proc.stderr:
        raise DelegateError(
            "codex exec could not apply changes: sandbox_apply: Operation not permitted\n"
            f"--- worker output ---\n{strip_codex_wrappers(proc.stdout)}"
        )
    return strip_codex_wrappers(proc.stdout)


def op_read(ref, timeout, provider="Ollama"):
    token = os.environ.get(ref, "")
    if not token and not (ref == "OLLAMA_API_KEY" and OLLAMA_URL.startswith("http://127.0.0.1:")):
        raise DelegateError(f"{provider} API key unavailable: set {ref}")
    return token

def prepare_ollama_images(paths):
    """Validate, resize, and base64-encode Ollama vision inputs in memory."""
    if not paths:
        return []
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as e:
        raise DelegateError(
            "--images requires Pillow (python3 -m pip install Pillow)"
        ) from e

    prepared = []
    for raw_path in paths:
        path = os.path.abspath(os.path.expanduser(raw_path))
        extension = os.path.splitext(path)[1].lower()
        expected_format = OLLAMA_IMAGE_FORMATS.get(extension)
        if expected_format is None:
            raise DelegateError(
                f"unsupported image extension for {raw_path!r}; use jpg, png, or webp"
            )
        if not os.path.isfile(path):
            raise DelegateError(f"image file not found: {raw_path}")

        try:
            with Image.open(path) as image:
                image.load()
                actual_format = (image.format or "").upper()
                if actual_format != expected_format:
                    raise DelegateError(
                        f"image format mismatch for {raw_path!r}: "
                        f"extension says {expected_format}, file is {actual_format or 'unknown'}"
                    )
                original_width, original_height = image.size
                if original_width <= 0 or original_height <= 0:
                    raise DelegateError(f"image has invalid dimensions: {raw_path}")

                if original_width > OLLAMA_IMAGE_MAX_WIDTH:
                    image.thumbnail(
                        (OLLAMA_IMAGE_MAX_WIDTH, original_height),
                        Image.Resampling.LANCZOS,
                    )
                    if expected_format == "JPEG" and image.mode not in {"RGB", "L"}:
                        image = image.convert("RGB")
                    output = io.BytesIO()
                    save_options = {}
                    if expected_format == "PNG":
                        save_options["optimize"] = True
                    elif expected_format == "JPEG":
                        save_options.update(quality=90, optimize=True)
                    elif expected_format == "WEBP":
                        save_options.update(quality=90, method=6)
                    image.save(output, format=expected_format, **save_options)
                    image_bytes = output.getvalue()
                else:
                    with open(path, "rb") as image_file:
                        image_bytes = image_file.read()
                sent_width, sent_height = image.size
        except DelegateError:
            raise
        except (UnidentifiedImageError, OSError) as e:
            raise DelegateError(f"could not read image {raw_path!r}: {e}") from e

        prepared.append(
            {
                "path": path,
                "format": expected_format.lower(),
                "original_width": original_width,
                "original_height": original_height,
                "sent_width": sent_width,
                "sent_height": sent_height,
                "bytes": len(image_bytes),
                "data": base64.b64encode(image_bytes).decode("ascii"),
            }
        )
    return prepared


def run_ollama(model, spec, timeout, images=None):
    check_channel_down()
    token = op_read(OLLAMA_TOKEN_REF, min(timeout, 30))
    check_channel_down()
    message = {"role": "user", "content": spec}
    if images:
        message["images"] = [image["data"] for image in images]
    body = {
        "model": OLLAMA_MODELS[model],
        "messages": [message],
    }
    request_url = OLLAMA_URL
    if images:
        # Native chat is Ollama's documented REST shape for base64 `images`.
        body["stream"] = False
        request_url = OLLAMA_CHAT_URL
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        request_url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with _open_url(req, timeout) as resp:
            raw = _read_limited(resp)
    except urllib.error.HTTPError as e:
        detail = _read_limited(e, HTTP_ERROR_LIMIT)
        raise DelegateError(f"Ollama HTTP {e.code}\n{detail}") from e
    except urllib.error.URLError as e:
        raise DelegateError(f"Ollama request failed: {e}") from e
    except TimeoutError as e:
        raise DelegateError(f"Ollama request timed out after {timeout}s") from e

    try:
        payload = json.loads(raw)
        if images:
            content = payload["message"]["content"]
            usage = {
                "prompt_tokens": payload.get("prompt_eval_count"),
                "completion_tokens": payload.get("eval_count"),
                "total_tokens": (payload.get("prompt_eval_count") or 0)
                + (payload.get("eval_count") or 0),
            }
        else:
            content = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise DelegateError(f"unexpected Ollama response shape\n{raw}") from e
    log_usage("ollama", OLLAMA_MODELS[model], usage)
    return content


def refuse_openrouter_free_cost(slug, usage):
    """Refuse a billed response on a :free OpenRouter slug."""
    if not str(slug).endswith(":free"):
        return
    usage = usage or {}
    cost = usage.get("cost")
    if cost is None:
        return
    try:
        cost_num = float(cost)
    except (TypeError, ValueError):
        return
    if cost_num > 0:
        raise DelegateError(
            f"OpenRouter free slug {slug} reported nonzero cost {cost}"
        )


def _openrouter_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "HTTP-Referer": OPENROUTER_REFERER,
        "X-Title": OPENROUTER_TITLE,
    }


def _openrouter_retry_after(headers):
    """Return an integer Retry-After, or None when absent or invalid."""
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def openrouter_post_with_backoff(
    req_factory,
    timeout,
    attempts=OPENROUTER_RETRY_ATTEMPTS,
    base=OPENROUTER_RETRY_BASE_SECONDS,
    sleep=time.sleep,
    log=sys.stderr,
):
    """POST via urllib, retrying transient OpenRouter failures with backoff.

    ``req_factory`` is a zero-argument callable returning a fresh
    ``urllib.request.Request`` for each attempt (the body bytes may be reused;
    the Request is rebuilt). Retries HTTP 429/500/502/503/504 and
    ``urllib.error.URLError`` (connection reset, timeout); every other 4xx
    raises at once. Returns the decoded response body. One stderr line is
    written per retry and never carries a request or response body.
    """
    last_message = None
    for attempt in range(1, attempts + 1):
        req = req_factory()
        retry_after = None
        try:
            with _open_url(req, timeout) as resp:
                return _read_limited(resp)
        except urllib.error.HTTPError as e:
            detail = _read_limited(e, HTTP_ERROR_LIMIT)
            if e.code not in OPENROUTER_RETRY_STATUSES:
                raise DelegateError(f"OpenRouter HTTP {e.code}\n{detail}") from e
            last_message = f"OpenRouter HTTP {e.code}\n{detail}"
            label = f"HTTP {e.code}"
            retry_after = _openrouter_retry_after(e.headers)
        except urllib.error.URLError as e:
            last_message = f"OpenRouter request failed: {e}"
            label = f"URLError: {e.reason}"
        except TimeoutError as e:
            # urlopen wraps connect timeouts in URLError, but a read timeout
            # can surface raw; treat it as the same retryable transport failure.
            last_message = f"OpenRouter request timed out after {timeout}s"
            label = f"URLError: {e}"
        if attempt >= attempts:
            break
        delay = min(
            base * (2 ** (attempt - 1)) * (1.0 + random.random() * OPENROUTER_RETRY_JITTER),
            OPENROUTER_RETRY_MAX_SECONDS,
        )
        if retry_after is not None:
            delay = min(max(delay, retry_after), OPENROUTER_RETRY_MAX_SECONDS)
        print(
            f"openrouter: {label} (attempt {attempt}/{attempts}), "
            f"retrying in {delay:.1f}s",
            file=log,
        )
        sleep(delay)
    raise DelegateError(f"{last_message} after {attempts} attempts")


def run_openrouter(model, spec, timeout):
    check_channel_down()
    token = op_read(OPENROUTER_TOKEN_REF, min(timeout, 30), provider="OpenRouter")
    check_channel_down()
    slug = OPENROUTER_MODELS[model]
    body = {
        "model": slug,
        "messages": [{"role": "user", "content": spec}],
        "provider": OPENROUTER_PROVIDER_BLOCK,
    }
    data = json.dumps(body).encode("utf-8")

    def _request():
        return urllib.request.Request(
            OPENROUTER_URL,
            data=data,
            method="POST",
            headers=_openrouter_headers(token),
        )

    raw = openrouter_post_with_backoff(_request, timeout)

    try:
        payload = json.loads(raw)
        content = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise DelegateError(f"unexpected OpenRouter response shape\n{raw}") from e
    refuse_openrouter_free_cost(slug, usage)
    log_usage("openrouter", slug, usage)
    return content


def require_nonempty_result(lane, model, output):
    """Refuse a transport-success that delivered no model answer.

    Some CLI permission denials and other silent ends
    exit 0 with whitespace-only stdout. That must never look like success.
    """
    if not (output or "").strip():
        raise DelegateError(f"{lane} {model} returned an empty result")
    return output


def run_with_retries(fn, attempts):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except subprocess.TimeoutExpired as e:
            last = DelegateError(f"backend timed out after {e.timeout}s")
        except DelegateError as e:
            last = e
            message = str(e).lower()
            # Retry only on transport-shaped failures. The old substrings
            # ("rate", " 5") matched ordinary words and line numbers, so most
            # Spec or tool failures should not be retried.
            transient = bool(
                re.search(
                    r"timed out|timeout|temporar|rate.?limit|\b429\b|\b5\d\d\b",
                    message,
                )
            )
            if not transient:
                break
        if i < attempts - 1:
            time.sleep(0.5 * (i + 1))
    raise last if last is not None else DelegateError("delegation failed with no captured error")

DEFAULT_WEB_WRITE_MAX_ITERS = 40
DEFAULT_OLLAMA_WRITE_MAX_ITERS = DEFAULT_WEB_WRITE_MAX_ITERS  # alias


def run_web_write_handoff(model, spec, timeout, max_iters, cwd):
    """Hand --write off to delegate_web.run_agent for Ollama and OpenRouter."""
    try:
        import delegate_web
    except ModuleNotFoundError as exc:
        if exc.name != "delegate_web":
            raise
        import dispatch_web as delegate_web
    delegate_web.ALLOW_SHELL_TOOL = ALLOW_SHELL_TOOL
    delegate_web._delegate.PASS_ENV = PASS_ENV
    if not LOCAL_CONFIGURED:
        if model in OPENROUTER_MODELS:
            delegate_web.OPENROUTER_MODELS[model] = OPENROUTER_MODELS[model]
            delegate_web.OPENROUTER_URL = OPENROUTER_URL
            delegate_web.OPENROUTER_TOKEN_REF = OPENROUTER_TOKEN_REF
        else:
            delegate_web.OLLAMA_MODELS[model] = OLLAMA_MODELS[model]
            delegate_web.OLLAMA_HOST = OLLAMA_URL.rsplit("/v1/chat/completions", 1)[0]
            delegate_web.OLLAMA_TOKEN_REF = OLLAMA_TOKEN_REF

    if model in getattr(delegate_web, "OPENROUTER_MODELS", {}):
        label = "OpenRouter"
        table = "OPENROUTER_MODELS"
        allowed = delegate_web.OPENROUTER_MODELS
    else:
        label = "Ollama"
        table = "OLLAMA_MODELS"
        allowed = delegate_web.OLLAMA_MODELS
    if model not in allowed:
        raise DelegateError(
            f"--write handoff: delegate_web.py does not allow-list {label} model "
            f"{model!r}; add it to delegate_web.{table} or choose an "
            "allow-listed id"
        )
    if model in OPENROUTER_NO_TOOLS:
        raise DelegateError(
            f"--write refused: OpenRouter free slug {allowed[model]} has no endpoint "
            + OPENROUTER_NO_TOOLS_MESSAGE
        )
    content, _tool_call_count = delegate_web.run_agent(
        model,
        spec,
        timeout,
        max_iters,
        trace=True,
        write_dirs=[os.path.abspath(os.path.expanduser(cwd))],
        extra_enabled=False,
    )
    return content


def _max_iters_arg(value):
    """argparse type for --max-iters: an integer in the inclusive range 1..500."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}")
    if not 1 <= parsed <= 500:
        raise argparse.ArgumentTypeError("must be between 1 and 500")
    return parsed


def parse_args(argv):
    models = sorted(
        list(CODEX_MODELS)
        + list(OLLAMA_MODELS)
        + list(MODEL_ALIASES)
        + list(CURSOR_MODELS)
        + list(OPENROUTER_MODELS)
    )
    ap = argparse.ArgumentParser(
        description="Run one delegation through a configured CLI or HTTP backend.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"Example: {Path(__file__).name} --model example --backend codex --spec task.md --dry-run"
        ),
    )
    ap.add_argument(
        "--model",
        required=True,
        metavar="MODEL",
        help=f"model id or configured alias ({', '.join(models)})" if models else "model id or configured alias",
    )
    ap.add_argument("--spec", required=True, help='spec file path, or "-" for stdin')
    ap.add_argument("--backend", choices=["codex", "cursor", "ollama", "ollama-cloud", "openrouter", "openai-compatible"])
    ap.add_argument("--model-id")
    ap.add_argument("--base-url")
    ap.add_argument("--api-key-env")
    ap.add_argument("--pass-env", action="append", default=[], metavar="NAME", help="pass this named environment variable to child processes")
    ap.add_argument("--unsafe-shell", action="store_true", help="enable an unconfined shell in the HTTP write loop")
    ap.add_argument("--ignore-cooldown", action="store_true")
    ap.add_argument(
        "--images",
        nargs="+",
        metavar="PATH",
        help="Ollama vision models only: jpg/png/webp paths; images wider than "
        "1024 px are downscaled in memory",
    )
    ap.add_argument(
        "--directive",
        default=None,
        help="directive audit text; required for configured directed models",
    )
    ap.add_argument("--cwd", default=os.getcwd(), help="working directory for subprocesses")
    ap.add_argument(
        "--write",
        action="store_true",
        default=False,
        help="enable backend editing; HTTP file tools use --cwd, but --unsafe-shell is unconfined",
    )
    ap.add_argument("--timeout", type=int, default=1800, help="per-call timeout in seconds")
    ap.add_argument(
        "--max-iters",
        type=_max_iters_arg,
        default=None,
        metavar="N",
        help="Ollama/OpenRouter write handoff only: agent-loop iteration cap, 1-500 (default: 40)",
    )
    ap.add_argument(
        "--idle-timeout",
        type=int,
        default=CURSOR_DEFAULT_IDLE_TIMEOUT,
        help="Cursor lane: kill the CLI after this many seconds with no stdout/stderr (default: 600)",
    )
    ap.add_argument(
        "--effort",
        choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
        default=None,
        help="reasoning effort override; supported levels depend on the backend and model",
    )
    ap.add_argument(
        "--web",
        action="store_true",
        help="codex lane only: enable the backend web_search tool (live web research)",
    )
    ap.add_argument(
        "--network",
        action="store_true",
        help="Codex only, requires --write: allow outbound network in its execution sandbox",
    )
    ap.add_argument(
        "--reasoning",
        choices=["none", "low", "high", "max"],
        default="high",
        help="reasoning effort for compatible configured backends",
    )
    ap.add_argument(
        "--no-fast",
        action="store_true",
        default=False,
        help="no-op, kept for old callers: standard speed is the default on the codex "
        "and cursor lanes",
    )
    ap.add_argument(
        "--fast",
        action="store_true",
        default=False,
        help="codex/cursor lanes: run Fast mode; configured policy may require --directive",
    )
    ap.add_argument(
        "--task-class",
        default=None,
        metavar="KEY",
        help="compatibility option for local extensions; unused by standalone backends",
    )
    ap.add_argument(
        "--ignore-lane-state",
        action="store_true",
        default=False,
        help="compatibility option for local extensions; unused by standalone backends",
    )
    ap.add_argument(
        "--skills",
        default=None,
        metavar="A,B",
        help="comma-separated skill names appended to the prompt from "
        "~/.claude/skills/<name>/SKILL.md, or none; overrides the spec's ## Skills section",
    )
    ap.add_argument("--skills-override-reason", metavar="TEXT",
                    help="audit why named skills replace routing (required in enforce)")
    ap.add_argument("--skill-route-mode", choices=["shadow", "assist", "enforce"],
                    help="tighten shadow to assist/enforce; configured binding cannot be relaxed")
    ap.add_argument("--request", type=Path, help="original request file for routing")
    ap.add_argument("--dry-run-route", action="store_true",
                    help="explicitly include a local route receipt in --dry-run")
    ap.add_argument(
        "--grants-check-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve and print the launch plan without spawning or contacting a backend",
    )
    ap.add_argument("--no-preflight", action="store_true", help="skip dry-run prerequisite checks")
    add_arguments(ap)
    return ap.parse_args(argv)


def dry_run_preflight(args, plan):
    """Report local prerequisites without reading or printing credential values."""
    if args.no_preflight:
        return {"skipped": True}, []
    lane = plan["lane"]
    backend = args.backend or lane
    binary = {"codex": "codex", "cursor": CURSOR_BIN}.get(lane)
    if backend == "ollama" and not LOCAL_CONFIGURED:
        binary = "ollama"
    report = {"binary": None, "key_variable": None, "endpoint": None}
    issues = []
    if binary:
        found = shutil.which(binary) is not None
        report["binary"] = {"name": binary, "on_path": found}
        if not found:
            issues.append(f"install {binary} and add it to PATH")
    if lane == "ollama":
        endpoint, key_name = OLLAMA_CHAT_URL if args.write else OLLAMA_URL, OLLAMA_TOKEN_REF
        required = backend == "ollama-cloud" or not endpoint.startswith("http://127.0.0.1:")
    elif lane == "openrouter":
        endpoint, key_name, required = OPENROUTER_URL, OPENROUTER_TOKEN_REF, True
    elif lane == "openai-compatible":
        endpoint, key_name, required = GENERIC_URL, GENERIC_TOKEN_REF, True
    else:
        endpoint, key_name, required = None, None, False
    if endpoint:
        report["endpoint"] = endpoint_origin(endpoint)
        is_variable = bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_name or ""))
        is_set = bool(os.environ.get(key_name)) if is_variable else False
        report["key_variable"] = {"name": key_name if is_variable else None, "set": is_set, "required": required}
        if required and not is_set and is_variable:
            issues.append(f"set {key_name}")
    return report, issues


def validate_options(args, plan, phase):
    """Optional validation at the boundary of shared capability checks."""
    return None


def validate_backend_options(args, plan):
    lane = plan['lane']
    validate_options(args, plan, "before")
    if args.effort == 'none' and lane != 'cursor':
        raise DelegateError('--effort none is directed cursor-lane only')
    if args.effort == 'minimal' and lane != 'cursor':
        raise DelegateError('--effort minimal is directed cursor-lane only')
    validate_options(args, plan, "capabilities")
    if args.web and lane != 'codex':
        raise DelegateError('--web is codex-lane only (use delegate_web.py for Ollama models)')
    if args.effort and lane not in EFFORT_BACKENDS:
        raise DelegateError(EFFORT_BACKEND_ERROR)
    if args.network and lane != "codex":
        raise DelegateError("--network is codex-lane only")
    if args.network and (not args.write):
        raise DelegateError('--network requires --write (codex ignores it under the read-only sandbox)')
    if args.images and lane != 'ollama':
        raise DelegateError('--images is Ollama-lane only')
    if args.images and (not OLLAMA_MODEL_FLAGS.get(args.model, {}).get('vision', False)):
        raise DelegateError(f'--images requires an Ollama model flagged vision-capable; refused {args.model!r}')
    if args.max_iters is not None and lane not in {'ollama', 'openrouter'}:
        raise DelegateError(f'--max-iters is Ollama/OpenRouter-lane only (the {lane} lane has no agent-loop iteration cap)')
    if lane == 'openai-compatible' and args.write:
        raise DelegateError('--write is unavailable for the generic HTTP backend')


def main(argv=None):
    global PASS_ENV, ALLOW_SHELL_TOOL
    refuse_nested_delegate()
    args = parse_args(argv or sys.argv[1:])
    for name in args.pass_env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise DelegateError("--pass-env requires an environment variable name")
    PASS_ENV = tuple(args.pass_env)
    shell_opt_in = bool(args.unsafe_shell or load_config().get("general", {}).get("allow_shell_tool") is True)
    ALLOW_SHELL_TOOL = bool(ALLOW_SHELL_TOOL or shell_opt_in)
    # Warn on an explicit opt-in; a local installation default stays quiet.
    if shell_opt_in:
        print(f"{SCRIPT_NAME}: WARNING: run_command is an unconfined shell. --cwd does not restrict access. Run untrusted tasks inside a container or VM.", file=sys.stderr)
    if not LOCAL_CONFIGURED:
        configure_public(args)
    if args.no_fast:
        print(f"{SCRIPT_NAME}: --no-fast is the default", file=sys.stderr)
    plan = resolve_model(args.model, effort=args.effort, fast=args.fast)
    hook_error = before_launch({"args": args, "plan": plan})
    if hook_error:
        raise DelegateError(hook_error)
    if args.fast:
        if plan["lane"] not in {"codex", "cursor"}:
            raise DelegateError("--fast is codex/cursor-lane only")
    if nested_codex_capability() and plan["lane"] != "codex":
        raise DelegateError(
            f"nested delegation refused: {NESTED_CODEX_CAPABILITY_ENV} permits Codex models only"
        )
    validate_cursor_directive(args.model, args.directive)
    if not args.grants_check_only and not args.dry_run:
        check_channel_down()
    if args.timeout <= 0:
        raise DelegateError("--timeout must be positive")
    if args.idle_timeout <= 0:
        raise DelegateError("--idle-timeout must be positive")
    if args.grants_check_only:
        spec = read_spec(args.spec)
        if not spec.strip():
            raise DelegateError("empty spec")
        validate_grants(spec, args.spec, args, plan)
        return
    lane = plan["lane"]
    validate_backend_options(args, plan)
    if not LOCAL_CONFIGURED:
        cooldown = public_cooldown_marker(lane)
        if cooldown and not args.ignore_cooldown and not args.dry_run:
            raise LaneRefusedError(f"{SCRIPT_NAME} refused: lane={lane} cooldown_until={cooldown['until']}")

    launch_state = prepare_launch(args, plan)

    # The Ollama write loop imports the ollama SDK. Re-enter this CLI under
    # its installed venv before reading stdin or writing a launch record.
    if lane == "ollama" and args.write and not args.dry_run:
        venv_python = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python3")
        if os.path.abspath(sys.executable) != venv_python:
            if not os.path.isfile(venv_python):
                raise DelegateError(f"Ollama write interpreter unavailable: {venv_python}")
            forwarded_args = list(argv) if argv is not None else sys.argv[1:]
            os.execve(venv_python, [venv_python, os.path.abspath(__file__), *forwarded_args], _self_reexec_env())

    spec = read_spec(args.spec)
    if not spec.strip():
        raise DelegateError("empty spec")
    # Skills resolve before the launch record and before any worker starts:
    # an unknown name or an oversize bundle refuses the launch here.
    prompt, args.skills_loaded, args.skills_bytes = prepare_skills(spec, args, lane)
    validate_grants(spec, args.spec, args, plan)
    spec = prompt
    prepared_images = prepare_ollama_images(args.images)

    if args.dry_run:
        dry_plan = dict(plan)
        preflight, preflight_issues = dry_run_preflight(args, plan)
        prompt_bytes = spec.encode("utf-8")
        dry_plan.update(
            {
                "cwd": os.path.abspath(os.path.expanduser(args.cwd)),
                "directive": "<redacted>" if args.directive else None,
                "dry_run": True,
                "prompt": {"bytes": len(prompt_bytes), "sha256": hashlib.sha256(prompt_bytes).hexdigest()},
                "spawn": False,
                "preflight": preflight,
                "task_class": args.task_class,
            }
        )
        if args.skills_loaded:
            # Only when skills load, so a skill-free plan keeps its existing shape.
            dry_plan["prompt"]["skills_loaded"] = args.skills_loaded
            dry_plan["prompt"]["skills_bytes"] = args.skills_bytes
        if args.dry_run_route:
            dry_plan["skill_route"] = getattr(args, "skill_route", None)
            for key in ("route_id", "route_verdict", "skills_routed", "skills_override_reason"):
                if hasattr(args, key):
                    dry_plan[key] = getattr(args, key)
        update_dry_plan(args, plan, launch_state, dry_plan)
        if args.fast:
            dry_plan["fast"] = True
            if lane == "codex":
                dry_plan["codex_config"] = list(CODEX_FAST_CONFIG)
        if args.write and lane in {"ollama", "openrouter"}:
            dry_plan["write_lane"] = "delegate_web"
        if prepared_images:
            dry_plan["images"] = [
                {key: value for key, value in image.items() if key != "data"}
                for image in prepared_images
            ]
        if lane == "cursor":
            with cursor_launch(
                args.model,
                spec,
                args.cwd,
                write=args.write,
                effort=args.effort,
                fast=args.fast,
                idle_timeout=args.idle_timeout,
            ) as launch:
                dry_plan.update(
                    {
                        "argv": launch["argv"][:-1] + ["<redacted prompt>"],
                        "env": launch["env_report"],
                        "hook_files": launch["hook_files"],
                        "disabled_plugins": launch["disabled_plugins"],
                        "attempts": CURSOR_ATTEMPTS,
                        "idle_timeout": args.idle_timeout,
                    }
                )
        print(json.dumps(dry_plan, sort_keys=True))
        if preflight_issues and not LOCAL_CONFIGURED:
            raise DelegateError("preflight: " + "; ".join(preflight_issues))
        check_launch_state(args, plan, launch_state)
        return

    print(
        f"{SCRIPT_NAME} launch: "
        f"lane={lane} model_requested={plan['model_requested']} "
        f"model_resolved={plan['model_resolved']} "
        f"directive={json.dumps(args.directive, ensure_ascii=False)} "
        f"cursor_pool={json.dumps(plan.get('cursor_pool'))}"
    )
    print_launch_status(args, plan, launch_state)
    print(
        f"{SCRIPT_NAME} skills: "
        f"skills_loaded={json.dumps(getattr(args, '_skills_status_loaded', args.skills_loaded))} "
        f"skills_bytes={getattr(args, '_skills_status_bytes', args.skills_bytes)}"
    )
    sys.stdout.flush()

    try:
        output = _dispatch_backend(args, plan, spec, prepared_images)
    except (ChannelDownError, GrantsError):
        raise
    except Exception as e:  # noqa: BLE001 - re-raised unchanged unless a vendor limit
        detail = str(e)
        if isinstance(e, subprocess.TimeoutExpired):
            raise
        fall_through = record_lane_exhaustion(
            plan, args.model, args.task_class, detail, spec
        )
        if fall_through is None:
            raise
        raise LaneExhaustedError(detail, fall_through) from e

    require_nonempty_result(lane, args.model, output)
    after_run({"args": args, "plan": plan}, output)
    sys.stdout.write(output)
    if output and not output.endswith("\n"):
        sys.stdout.write("\n")


def _dispatch_backend(args, plan, spec, prepared_images):
    """Run the resolved lane's backend once (with that lane's existing retries)."""
    lane = plan["lane"]
    if lane == "cursor":
        output = run_with_retries(
            lambda: run_cursor(
                args.model,
                spec,
                args.cwd,
                args.timeout,
                args.write,
                args.effort,
                args.fast,
                args.idle_timeout,
            ),
            attempts=CURSOR_ATTEMPTS,
        )
    elif lane == "codex":
        output = run_with_retries(
            lambda: run_codex(
                args.model,
                spec,
                args.cwd,
                args.timeout,
                args.write,
                args.web,
                args.effort,
                args.network,
                args.fast,
            ),
            attempts=3,
        )
    elif lane == "ollama":
        if args.write:

            # the delegate_web.py agent loop. Its file tools use --cwd.
            # The optional shell has no filesystem boundary. No
            # run_with_retries wrapper: the agent loop owns its own iteration
            # budget (--max-iters, default 40) and returns (content,
            # tool_call_count).
            max_iters = (
                args.max_iters
                if args.max_iters is not None
                else DEFAULT_WEB_WRITE_MAX_ITERS
            )
            output = run_web_write_handoff(
                args.model, spec, args.timeout, max_iters, args.cwd
            )
        else:
            output = run_with_retries(
                lambda: run_ollama(args.model, spec, args.timeout, prepared_images),
                attempts=3,
            )
    elif lane == "openrouter":
        if args.write:
            max_iters = (
                args.max_iters
                if args.max_iters is not None
                else DEFAULT_WEB_WRITE_MAX_ITERS
            )
            output = run_web_write_handoff(
                args.model, spec, args.timeout, max_iters, args.cwd
            )
        else:
            output = run_with_retries(
                lambda: run_openrouter(args.model, spec, args.timeout),
                attempts=3,
            )
    elif lane == "openai-compatible":
        output = run_with_retries(lambda: run_compatible(args.model, spec, args.timeout), attempts=3)
    else:  # pragma: no cover - resolve_model owns the exhaustive lane set
        raise DelegateError(f"internal error: unresolved lane {lane!r}")
    return output


load_local_extensions(globals(), "install_dispatch")


if __name__ == "__main__":
    try:
        if not run_cli_hook(sys.argv[1:]):
            main()
    except ChannelDownError:
        print(CHANNEL_DOWN_MESSAGE, file=sys.stderr)
        sys.exit(CHANNEL_DOWN_EXIT)
    except LaneRefusedError as e:
        # The message is the full '{SCRIPT_NAME} refused: ...' line.
        print(str(e), file=sys.stderr)
        sys.exit(LANE_REFUSED_EXIT)
    except LaneExhaustedError as e:
        print(f"{SCRIPT_NAME}: {e}", file=sys.stderr)
        print(e.fall_through_line, file=sys.stderr)
        sys.exit(1)
    except (GrantsError, SkillRouteError) as e:
        print(f"{SCRIPT_NAME}: {e}", file=sys.stderr)
        sys.exit(2)
    except DelegateError as e:
        print(f"{SCRIPT_NAME}: {e}", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired as e:
        print(f"{SCRIPT_NAME}: timed out after {e.timeout}s", file=sys.stderr)
        sys.exit(1)
