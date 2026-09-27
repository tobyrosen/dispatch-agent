#!/usr/bin/env python3
"""Bounded web and file tool loop for configured HTTP models."""

import argparse
from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request

def log_usage(*_args, **_kwargs):
    return None


try:
    import delegate as _delegate
except ModuleNotFoundError as exc:
    if exc.name != "delegate":
        raise
    import dispatch as _delegate

SCRIPT_NAME = Path(sys.argv[0]).name or Path(__file__).name
DISPATCH_NAME = "dispatch.py" if SCRIPT_NAME == "dispatch_web.py" else "delegate.py"

load_config = _delegate.load_config
DelegateError = _delegate.DelegateError
_scrubbed_env = _delegate._scrubbed_env
openrouter_post_with_backoff = _delegate.openrouter_post_with_backoff
refuse_openrouter_free_cost = _delegate.refuse_openrouter_free_cost
validate_http_url = _delegate.validate_http_url
validate_model_id = _delegate.validate_model_id
_open_url = _delegate._open_url
_read_limited = _delegate._read_limited


LOCAL_CONFIGURED = False
OLLAMA_HOST = "http://127.0.0.1:11434"
OLLAMA_TOKEN_REF = "OLLAMA_API_KEY"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_TOKEN_REF = "OPENROUTER_API_KEY"
OPENROUTER_REFERER = "https://github.com/dispatch-agent/dispatch-agent"
OPENROUTER_TITLE = "dispatch-agent"
OPENROUTER_PROVIDER_BLOCK = {"zdr": True, "data_collection": "deny", "allow_fallbacks": False}
OPENROUTER_MODELS = {}
GRANTS_LOG = os.path.expanduser("~/.local/state/dispatch-agent/delegate-grants.jsonl")
WRITE_BYTE_LIMIT = 1_000_000
READ_CHAR_LIMIT = 60_000
OLLAMA_MODELS = {}
ALLOW_SHELL_TOOL = _delegate.ALLOW_SHELL_TOOL
_ROOT_FDS = {}


def configure_public_web(args=None):
    global OLLAMA_HOST, OLLAMA_TOKEN_REF, OPENROUTER_URL, OPENROUTER_TOKEN_REF
    config = load_config()
    providers = config.get("providers", {})
    for alias, entry in config.get("models", {}).items():
        backend = entry.get("backend")
        model = entry.get("model", alias)
        validate_model_id(model)
        if backend in {"ollama", "ollama-cloud"}:
            OLLAMA_MODELS[alias] = model
            provider = providers.get(backend, {})
            OLLAMA_HOST = entry.get("base_url") or provider.get("base_url") or ("https://ollama.com" if backend == "ollama-cloud" else "http://127.0.0.1:11434")
            validate_http_url(OLLAMA_HOST)
            OLLAMA_TOKEN_REF = entry.get("api_key_env") or provider.get("api_key_env") or "OLLAMA_API_KEY"
        elif backend == "openrouter":
            OPENROUTER_MODELS[alias] = model
            provider = providers.get(backend, {})
            OPENROUTER_URL = (entry.get("base_url") or provider.get("base_url") or "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions"
            validate_http_url(OPENROUTER_URL)
            OPENROUTER_TOKEN_REF = entry.get("api_key_env") or provider.get("api_key_env") or "OPENROUTER_API_KEY"
    if args is not None and args.backend:
        model = args.model_id or args.model
        validate_model_id(model)
        host = args.base_url
        if host:
            validate_http_url(host)
        if args.backend in {"ollama", "ollama-cloud"}:
            OLLAMA_MODELS[args.model] = model
            OLLAMA_HOST = host or ("https://ollama.com" if args.backend == "ollama-cloud" else "http://127.0.0.1:11434")
            OLLAMA_TOKEN_REF = args.api_key_env or "OLLAMA_API_KEY"
        else:
            OPENROUTER_MODELS[args.model] = model
            OPENROUTER_URL = (host or "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions"
            OPENROUTER_TOKEN_REF = args.api_key_env or "OPENROUTER_API_KEY"



def op_read(ref, timeout):
    return _delegate.op_read(ref, timeout)


def add_arguments(_parser):
    return None


WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current, live information. Returns a list of results, "
            "each with a title, url, and a content snippet. Use this to find candidate "
            "pages, then call web_fetch to confirm a URL resolves and read its content."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "the search query"},
                "max_results": {
                    "type": "integer",
                    "description": "max number of results to return, <=10, default 5",
                },
            },
            "required": ["query"],
        },
    },
}

WEB_FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "Fetch the full content of a single web page by URL. Returns the page title, "
            "its text content, and a list of links found on the page. Use this to CONFIRM "
            "a URL actually resolves before citing it, and to read details (prices, preview "
            "images, product specifics)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "the absolute URL to fetch"},
            },
            "required": ["url"],
        },
    },
}

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read text from a file inside one of the explicit --write-dir paths. "
            "Returns content and path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "file path"},
            },
            "required": ["path"],
        },
    },
}

WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Write text to a file inside one of the explicit --write-dir paths. "
            "Max payload is 1 MB."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "file path"},
                "content": {"type": "string", "description": "text to write"},
            },
            "required": ["path", "content"],
        },
    },
}

LIST_DIR_TOOL = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "List entries in a directory inside one of the explicit --write-dir paths.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "directory path"},
            },
            "required": ["path"],
        },
    },
}

RUN_COMMAND_TOOL = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "Run a shell command via bash -lc with the working directory set to the first "
            "--write-dir path. Returns rc, stdout, stderr (stdout and stderr each capped "
            "at 20000 chars), and cwd."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "shell command to run via bash -lc",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "timeout in seconds (default 300, max 1800)",
                },
            },
            "required": ["command"],
        },
    },
}

EXTENSION_TOOLS = {}
TOOL_SET_REQUIREMENT = "OpenRouter tool loop requires --write-dir"


TOOLS = [WEB_SEARCH_TOOL, WEB_FETCH_TOOL]
FILE_TOOLS = [READ_FILE_TOOL, WRITE_FILE_TOOL, LIST_DIR_TOOL]
FILE_TOOL_NAMES = {tool["function"]["name"] for tool in FILE_TOOLS}


def log_launch(model_requested, model_resolved, cwd, lane="ollama"):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record = {
        "timestamp": ts,  # retained for consistency with delegate.py grants records
        "ts": ts,
        "cwd": os.path.abspath(os.path.expanduser(cwd)),
        "model_requested": model_requested,
        "model_resolved": model_resolved,
        "lane": lane,
        "event": "launch",
        "source": "delegate_web.py",
        "verdict": "allow",
    }
    from delegate import grants_log_paths

    errors = []
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    for log_path in grants_log_paths(cwd, default_log=GRANTS_LOG):
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
    raise DelegateError("launch log required but unavailable: " + "; ".join(errors))


def read_spec(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _as_dict(obj):
    """Normalize an ollama response object (pydantic-ish) or a plain dict to a dict."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    return obj


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _normalize_allowed_dirs(write_dirs):
    out = []
    for raw in write_dirs:
        canonical = os.path.realpath(raw)
        if not os.path.isdir(canonical):
            raise DelegateError(f"--write-dir is not a directory: {raw}")
        if canonical not in _ROOT_FDS:
            _ROOT_FDS[canonical] = os.open(canonical, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        out.append(canonical)
    return out


def _resolve_allowed_path(raw_path, allowed_dirs):
    if not allowed_dirs:
        raise DelegateError("file tools require --write-dir")
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise DelegateError("invalid file path")
    if ".." in Path(raw_path).parts:
        raise DelegateError("parent traversal is not allowed")
    candidate = raw_path if os.path.isabs(raw_path) else os.path.join(allowed_dirs[0], raw_path)
    candidate = os.path.abspath(candidate)
    for base in allowed_dirs:
        if os.path.commonpath([candidate, base]) == base:
            return candidate
    raise DelegateError(f"path '{raw_path}' is outside allowed directories")


@contextmanager
def _secure_parent(raw_path, allowed_dirs):
    """Walk every directory from an already opened root without following links."""
    path = _resolve_allowed_path(raw_path, allowed_dirs)
    base = next(base for base in allowed_dirs if os.path.commonpath([path, base]) == base)
    parts = os.path.relpath(path, base).split(os.sep)
    root_fd = _ROOT_FDS.get(base)
    owned_root = root_fd is None
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW) if owned_root else os.dup(root_fd)
    try:
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1], path
    finally:
        os.close(fd)


def _read_file(args, allowed_dirs):
    path = args.get("path", "")
    if not isinstance(path, str):
        return {"error": "path must be a string"}
    try:
        with _secure_parent(path, allowed_dirs) as (parent_fd, name, resolved):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    return {"error": f"not a file: {path}"}
                content = f.read(READ_CHAR_LIMIT + 1)
                total_chars = len(content)
                if total_chars > READ_CHAR_LIMIT:
                    while chunk := f.read(64_000):
                        total_chars += len(chunk)
    except Exception as err:
        return {"error": f"{type(err).__name__}: {str(err)[:400]}"}

    # Signal truncation so the caller knows the whole file was not read.
    if len(content) > READ_CHAR_LIMIT:
        return {
            "path": resolved,
            "content": content[:READ_CHAR_LIMIT],
            "truncated": True,
            "total_chars": total_chars,
            "note": f"file exceeds {READ_CHAR_LIMIT} characters",
        }
    return {"path": resolved, "content": content}


def _write_file(args, allowed_dirs):
    path = args.get("path", "")
    content = args.get("content", "")
    if not isinstance(path, str):
        return {"error": "path must be a string"}
    if not isinstance(content, str):
        return {"error": "content must be a string"}
    if len(content.encode("utf-8")) > WRITE_BYTE_LIMIT:
        return {"error": f"content exceeds 1 MB limit ({WRITE_BYTE_LIMIT} bytes)"}
    try:
        with _secure_parent(path, allowed_dirs) as (parent_fd, name, resolved):
            if name == ".":
                return {"error": "path is a directory"}
            try:
                existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if not stat.S_ISREG(existing.st_mode):
                    return {"error": "target is not a regular file"}
            except FileNotFoundError:
                pass
            temporary = f".dispatch-{secrets.token_hex(16)}"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
    except Exception as err:
        return {"error": f"{type(err).__name__}: {str(err)[:400]}"}
    return {"path": resolved, "bytes": len(content.encode("utf-8"))}


def _list_dir(args, allowed_dirs):
    path = args.get("path", "")
    if not isinstance(path, str):
        return {"error": "path must be a string"}
    try:
        with _secure_parent(path, allowed_dirs) as (parent_fd, name, resolved):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                entries = sorted(os.listdir(fd))
            finally:
                os.close(fd)
    except Exception as err:
        return {"error": f"{type(err).__name__}: {str(err)[:400]}"}
    return {"path": resolved, "entries": entries}


def _run_command(args, allowed_dirs):
    if not ALLOW_SHELL_TOOL:
        return {"error": "run_command requires --unsafe-shell"}
    command = args.get("command", "")
    if not isinstance(command, str):
        return {"error": "command must be a string"}
    if not command.strip():
        return {"error": "command is empty"}
    if not allowed_dirs:
        return {"error": "run_command requires at least one --write-dir"}
    try:
        timeout = int(args.get("timeout_seconds", 300) or 300)
    except (TypeError, ValueError):
        return {"error": "timeout_seconds must be an integer"}
    timeout = max(1, min(timeout, 1800))
    cwd = allowed_dirs[0]
    try:
        proc = subprocess.Popen(
            ["bash", "-lc", command],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_scrubbed_env(backend="shell"),
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            return {
                "error": f"command timed out after {timeout}s",
                "rc": None,
                "stdout": stdout[:20000],
                "stderr": stderr[:20000],
                "cwd": cwd,
            }
    except subprocess.TimeoutExpired:
        return {
            "error": f"command timed out after {timeout}s",
            "rc": None,
            "stdout": "",
            "stderr": "",
            "cwd": cwd,
        }
    except Exception as err:
        return {"error": f"{type(err).__name__}: {str(err)[:400]}"}
    return {
        "rc": proc.returncode,
        "stdout": stdout[:20000],
        "stderr": stderr[:20000],
        "cwd": cwd,
    }


def _validate_public_fetch_url(url):
    if not isinstance(url, str) or len(url) > _delegate.HTTP_URL_LIMIT or any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise DelegateError("invalid web_fetch URL")
    try:
        parsed = urllib_parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise DelegateError("invalid web_fetch URL") from exc
    if (parsed.scheme not in {"http", "https"} or not host or
            parsed.username is not None or parsed.password is not None or "#" in url):
        raise DelegateError("web_fetch requires a public HTTP(S) URL without userinfo or fragment")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise DelegateError("web_fetch destination is not public")
    except ValueError:
        pass
    try:
        addresses = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DelegateError("web_fetch host cannot be resolved") from exc
    if not addresses:
        raise DelegateError("web_fetch host has no addresses")
    for entry in addresses:
        address = ipaddress.ip_address(entry[4][0])
        if not address.is_global:
            raise DelegateError("web_fetch destination is not public")
    return url



def _tool_call_preview(name, args):
    if name == "run_command":
        return "run_command command=<redacted>"
    if name in FILE_TOOL_NAMES:
        path = args.get("path", "")
        return f"{name} path={path}"
    if name in EXTENSION_TOOLS:
        return EXTENSION_TOOLS[name]["preview"](args)
    return json.dumps(args)[:200]


def _tool_result_summary(name, args, result):
    if name == "run_command":
        if "error" in result:
            return f"ERROR: {result['error'][:120]}"
        return f"run_command rc={result.get('rc')}"
    if name in FILE_TOOL_NAMES:
        if "error" in result:
            return f"ERROR: {result['error'][:120]}"
        return f"{name} path={args.get('path', '')}"
    if name in EXTENSION_TOOLS:
        return EXTENSION_TOOLS[name]["summary"](args, result)
    if "results" in result:
        return f"{len(result['results'])} results"
    if "title" in result:
        return f"fetched: {(result.get('title') or '')[:60]}"
    return "result"


def execute_tool(client, name, args, timeout, allowed_write_dirs, extra_enabled):
    """Execute a hosted Ollama web tool and return a JSON-serializable result dict.

    Failures are returned (not raised) as {"error": ...} so the model can recover —
    a dead URL or empty search is information the agent should see, not a crash.
    """
    if name in {"web_search", "web_fetch"} and client is None:
        raise DelegateError(
            f"{name} is unavailable on this backend (no hosted web tools)"
        )
    try:
        if name == "web_search":
            query = args.get("query", "")
            max_results = int(args.get("max_results", 5) or 5)
            max_results = max(1, min(max_results, 10))
            resp = client.web_search(query=query, max_results=max_results)
            results = _get(resp, "results", []) or []
            out = []
            for r in results:
                out.append({
                    "title": _get(r, "title", ""),
                    "url": _get(r, "url", ""),
                    "content": (_get(r, "content", "") or "")[:2000],
                })
            return {"results": out}
        if name == "web_fetch":
            url = _validate_public_fetch_url(args.get("url", ""))
            resp = client.web_fetch(url=url)
            return {
                "title": _get(resp, "title", ""),
                "content": (_get(resp, "content", "") or "")[:6000],
                "links": (_get(resp, "links", []) or [])[:50],
            }
        if name == "read_file":
            return _read_file(args, allowed_write_dirs)
        if name == "write_file":
            return _write_file(args, allowed_write_dirs)
        if name == "list_dir":
            return _list_dir(args, allowed_write_dirs)
        if name == "run_command":
            return _run_command(args, allowed_write_dirs)
        if name in EXTENSION_TOOLS:
            tool = EXTENSION_TOOLS[name]
            if not extra_enabled:
                return {"error": tool["disabled_error"]}
            return tool["execute"](args, timeout)
        return {"error": f"unknown tool: {name}"}
    except Exception as e:  # noqa: BLE001 - surface any backend error back to the model
        return {"error": f"{type(e).__name__}: {str(e)[:400]}"}


def _openrouter_chat(token, slug, messages, tools, timeout):
    """POST one OpenRouter chat completion; returns the parsed JSON body."""
    body = {
        "model": slug,
        "messages": messages,
        "provider": OPENROUTER_PROVIDER_BLOCK,
    }
    if tools:
        body["tools"] = tools
    data = json.dumps(body).encode("utf-8")

    def _request():
        return request.Request(
            OPENROUTER_URL,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "HTTP-Referer": OPENROUTER_REFERER,
                "X-Title": OPENROUTER_TITLE,
            },
        )

    raw = openrouter_post_with_backoff(_request, timeout)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise DelegateError(f"unexpected OpenRouter response shape\n{raw}") from e
    return payload


def _run_openrouter_agent(model, spec, timeout, max_iters, trace, write_dirs, extra_enabled):
    if not write_dirs and not extra_enabled:
        raise DelegateError(
            "OpenRouter lane in delegate_web.py needs at least one tool set "
            + TOOL_SET_REQUIREMENT + f"; plain research goes through {DISPATCH_NAME} without --write"
        )

    token = op_read(OPENROUTER_TOKEN_REF, min(timeout, 30))
    slug = OPENROUTER_MODELS[model]

    tools = []
    if write_dirs:
        tools.extend(FILE_TOOLS)
        if ALLOW_SHELL_TOOL:
            tools.append(RUN_COMMAND_TOOL)
    if extra_enabled:
        tools.extend(tool["definition"] for tool in EXTENSION_TOOLS.values())

    system = "You are an agentic worker."
    if write_dirs:
        system += " You have read_file, write_file, and list_dir within the --write-dir roots."
        if ALLOW_SHELL_TOOL:
            system += " You also have run_command. It is an unconfined shell; --write-dir only sets its working directory."
    if extra_enabled:
        system += "".join(tool["prompt"] for tool in EXTENSION_TOOLS.values())

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": spec},
    ]

    tool_call_count = 0
    n_prompt = 0
    n_completion = 0
    for i in range(max_iters):
        try:
            payload = _openrouter_chat(token, slug, messages, tools, timeout)
        except DelegateError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DelegateError(
                f"chat call failed on iter {i + 1}: {type(e).__name__}: {str(e)[:500]}"
            ) from e

        usage = payload.get("usage") or {}
        refuse_openrouter_free_cost(slug, usage)
        n_prompt += usage.get("prompt_tokens") or 0
        n_completion += usage.get("completion_tokens") or 0

        try:
            msg = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise DelegateError(
                f"unexpected OpenRouter response shape\n{json.dumps(payload)[:2000]}"
            ) from e
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls")

        # Keep tool_calls on the assistant turn so later results attach correctly.
        messages.append(msg)

        if not tool_calls:
            if trace:
                sys.stderr.write(f"[iter {i + 1}] final answer ({tool_call_count} tool calls total)\n")
            log_usage(
                "openrouter",
                slug,
                {
                    "prompt_tokens": n_prompt,
                    "completion_tokens": n_completion,
                    "total_tokens": n_prompt + n_completion,
                },
            )
            return content, tool_call_count

        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments", {}) or {}
            args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
            tool_call_count += 1
            if trace:
                sys.stderr.write(f"[iter {i + 1}] TOOL_CALL {_tool_call_preview(name, args)}\n")
            result = execute_tool(None, name, args, timeout, write_dirs, extra_enabled)
            if trace:
                summary = _tool_result_summary(name, args, result)
                sys.stderr.write(f"            -> {summary}\n")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id"),
                "content": json.dumps(result),
            })

    log_usage(
        "openrouter",
        slug,
        {
            "prompt_tokens": n_prompt,
            "completion_tokens": n_completion,
            "total_tokens": n_prompt + n_completion,
        },
    )
    raise DelegateError(
        f"agent loop hit the {max_iters}-iteration cap without a final answer "
        f"({tool_call_count} tool calls made). Raise --max-iters or tighten the spec."
    )


def run_agent(model, spec, timeout, max_iters, trace, write_dirs, extra_enabled):
    if model in OPENROUTER_MODELS:
        return _run_openrouter_agent(
            model, spec, timeout, max_iters, trace, write_dirs, extra_enabled
        )

    from ollama import Client  # imported lazily so --help works without the lib

    validate_http_url(OLLAMA_HOST)
    token = op_read(OLLAMA_TOKEN_REF, min(timeout, 30))
    client = Client(host=OLLAMA_HOST, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)

    tools = list(TOOLS)
    if write_dirs:
        tools.extend(FILE_TOOLS)
        if ALLOW_SHELL_TOOL:
            tools.append(RUN_COMMAND_TOOL)
    if extra_enabled:
        tools.extend(tool["definition"] for tool in EXTENSION_TOOLS.values())

    system = (
        "You are an agentic web researcher. You have two web tools: web_search and web_fetch. "
        "You MUST use them to gather current, live information — do NOT answer from memory. "
        "Workflow: web_search to find candidate pages, then web_fetch to CONFIRM each URL "
        "resolves and to read its real content before you cite it. Only cite URLs you have "
        "actually fetched and confirmed resolve."
    )
    if write_dirs:
        system += " You also have read_file, write_file, and list_dir within the --write-dir roots."
        if ALLOW_SHELL_TOOL:
            system += " You also have run_command. It is an unconfined shell; --write-dir only sets its working directory."
    if extra_enabled:
        system += "".join(tool["prompt"] for tool in EXTENSION_TOOLS.values())

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": spec},
    ]

    tool_call_count = 0
    n_prompt = 0
    n_completion = 0
    for i in range(max_iters):
        try:
            resp = client.chat(
                model=OLLAMA_MODELS[model],
                messages=messages,
                tools=tools,
            )
        except Exception as e:  # noqa: BLE001
            raise DelegateError(f"chat call failed on iter {i + 1}: {type(e).__name__}: {str(e)[:500]}") from e

        _usage = _as_dict(_get(resp, "usage", None)) or {}
        n_prompt += _usage.get("prompt_tokens") or _get(resp, "prompt_eval_count", 0) or 0
        n_completion += _usage.get("completion_tokens") or _get(resp, "eval_count", 0) or 0

        msg = _get(resp, "message")
        content = _get(msg, "content", "") or ""
        tool_calls = _get(msg, "tool_calls", None)

        # Append the assistant turn verbatim so tool results attach to the right call.
        messages.append(_as_dict(msg))

        if not tool_calls:
            if trace:
                sys.stderr.write(f"[iter {i + 1}] final answer ({tool_call_count} tool calls total)\n")
            log_usage(
                "ollama",
                OLLAMA_MODELS[model],
                {
                    "prompt_tokens": n_prompt,
                    "completion_tokens": n_completion,
                    "total_tokens": n_prompt + n_completion,
                },
            )
            return content, tool_call_count

        for tc in tool_calls:
            fn = _get(tc, "function")
            name = _get(fn, "name", "")
            raw_args = _get(fn, "arguments", {}) or {}
            args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
            tool_call_count += 1
            if trace:
                sys.stderr.write(f"[iter {i + 1}] TOOL_CALL {_tool_call_preview(name, args)}\n")
            result = execute_tool(client, name, args, timeout, write_dirs, extra_enabled)
            if trace:
                summary = _tool_result_summary(name, args, result)
                sys.stderr.write(f"            -> {summary}\n")
            messages.append({
                "role": "tool",
                "tool_name": name,
                "content": json.dumps(result),
            })

    log_usage(
        "ollama",
        OLLAMA_MODELS[model],
        {
            "prompt_tokens": n_prompt,
            "completion_tokens": n_completion,
            "total_tokens": n_prompt + n_completion,
        },
    )
    raise DelegateError(
        f"agent loop hit the {max_iters}-iteration cap without a final answer "
        f"({tool_call_count} tool calls made). Raise --max-iters or tighten the spec."
    )


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="Run a bounded web and file tool loop on a configured HTTP model."
    )
    ap.add_argument(
        "--model",
        required=True,
        choices=sorted(OLLAMA_MODELS) + sorted(OPENROUTER_MODELS) if LOCAL_CONFIGURED else None,
    )
    ap.add_argument("--spec", required=True, help='spec file path, or "-" for stdin')
    ap.add_argument("--timeout", type=int, default=300, help="per-HTTP-call timeout in seconds")
    ap.add_argument("--max-iters", type=int, default=15, help="agent-loop iteration cap")
    ap.add_argument("--no-trace", action="store_true", help="suppress the tool-call trace on stderr")
    ap.add_argument(
        "--write-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="enable file tools for this allowed directory (repeatable)",
    )
    ap.add_argument("--backend", choices=["ollama", "ollama-cloud", "openrouter"])
    ap.add_argument("--model-id")
    ap.add_argument("--base-url")
    ap.add_argument("--api-key-env")
    ap.add_argument("--unsafe-shell", action="store_true", help="enable an unconfined run_command shell")
    ap.add_argument("--pass-env", action="append", default=[], metavar="NAME", help="pass this named environment variable to child processes")
    add_arguments(ap)
    return ap.parse_args(argv)


def main(argv=None):
    global ALLOW_SHELL_TOOL
    args = parse_args(argv or sys.argv[1:])
    for name in args.pass_env:
        if not _delegate.re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise DelegateError("--pass-env requires an environment variable name")
    _delegate.PASS_ENV = tuple(args.pass_env)
    ALLOW_SHELL_TOOL = bool(ALLOW_SHELL_TOOL or args.unsafe_shell or load_config().get("general", {}).get("allow_shell_tool") is True)
    if ALLOW_SHELL_TOOL:
        print(f"{SCRIPT_NAME}: WARNING: run_command is an unconfined shell. --write-dir does not restrict access. Run untrusted tasks inside a container or VM.", file=sys.stderr)
    if not LOCAL_CONFIGURED:
        configure_public_web(args)
    if args.timeout <= 0:
        raise DelegateError("--timeout must be positive")
    if args.max_iters <= 0:
        raise DelegateError("--max-iters must be positive")

    spec = read_spec(args.spec)
    if not spec.strip():
        raise DelegateError("empty spec")

    allowed_dirs = _normalize_allowed_dirs(args.write_dir)

    if args.model in OPENROUTER_MODELS:
        resolved = OPENROUTER_MODELS[args.model]
        lane = "openrouter"
    else:
        resolved = OLLAMA_MODELS[args.model]
        lane = "ollama"
    log_launch(args.model, resolved, os.getcwd(), lane=lane)
    output, n_calls = run_agent(
        args.model,
        spec,
        args.timeout,
        args.max_iters,
        trace=not args.no_trace,
        write_dirs=allowed_dirs,
        extra_enabled=getattr(args, "extra_enabled", False),
    )
    if not args.no_trace:
        sys.stderr.write(f"{SCRIPT_NAME}: done — {n_calls} tool calls executed\n")

    sys.stdout.write(output)
    if output and not output.endswith("\n"):
        sys.stdout.write("\n")


_delegate.load_local_extensions(globals(), "install_web")


if __name__ == "__main__":
    try:
        main()
    except DelegateError as e:
        print(f"{SCRIPT_NAME}: {e}", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired as e:
        print(f"{SCRIPT_NAME}: timed out after {e.timeout}s", file=sys.stderr)
        sys.exit(1)
