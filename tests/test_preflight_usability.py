"""Public dry-run and verified CLI failure regressions."""
import importlib.util
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def outsider(tmp_path):
    source = Path(__file__).resolve().parent.parent
    # The source tree names the scripts delegate*.py; the published package names them dispatch*.py.
    for old, new in (("delegate.py", "dispatch.py"), ("delegate_web.py", "dispatch_web.py")):
        shutil.copy2(source / old if (source / old).exists() else source / new, tmp_path / new)
    home = tmp_path / "home"
    home.mkdir()
    spec = tmp_path / "task.md"
    spec.write_text("Inspect this task.\n")
    return tmp_path / "dispatch.py", spec, home


def dry(outsider, backend, *flags, env_extra=None):
    cli, spec, home = outsider
    env = {"HOME": str(home), "PATH": "", "DELEGATE_NESTED_CODEX_CAPABILITY": ""}
    env.update(env_extra or {})
    command = [sys.executable, str(cli), "--model", "example", "--backend", backend,
               "--spec", str(spec), "--dry-run", *flags]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    return result, json.loads(result.stdout)


@pytest.mark.parametrize("backend,binary", [("codex", "codex"), ("cursor", "agent"), ("ollama", "ollama")])
def test_missing_binary_fails_with_fix_and_script_name(outsider, backend, binary):
    result, plan = dry(outsider, backend)
    assert result.returncode == 1
    assert f"dispatch.py: preflight: install {binary} and add it to PATH" in result.stderr
    assert plan["preflight"]["binary"] == {"name": binary, "on_path": False}
    assert "headroom" not in plan and "lane_state" not in plan
    assert "headroom:" not in result.stderr + result.stdout


@pytest.mark.parametrize("backend,name", [("ollama-cloud", "OLLAMA_API_KEY"),
                                         ("openrouter", "OPENROUTER_API_KEY"),
                                         ("openai-compatible", "MY_API_KEY")])
def test_missing_key_fails_and_reports_endpoint(outsider, backend, name):
    flags = ["--base-url", "https://api.example.test/v1"] if backend == "openai-compatible" else []
    if backend == "openai-compatible":
        flags += ["--api-key-env", name]
    result, plan = dry(outsider, backend, *flags)
    assert result.returncode == 1
    assert f"dispatch.py: preflight: set {name}" in result.stderr
    assert plan["preflight"]["key_variable"] == {"name": name, "set": False, "required": True}
    assert plan["preflight"]["endpoint"] == (
        "https://api.example.test" if backend == "openai-compatible" else
        "https://ollama.com" if backend == "ollama-cloud" else
        "https://openrouter.ai"
    )
    assert "headroom:" not in result.stderr + result.stdout


def test_endpoint_path_and_query_are_hidden_in_dry_run(outsider):
    marker = "private-endpoint-marker"
    result, plan = dry(outsider, "openai-compatible", "--base-url",
                       f"https://api.example.test:8443/{marker}?token={marker}",
                       "--api-key-env", "MY_API_KEY")
    assert plan["preflight"]["endpoint"] == "https://api.example.test:8443"
    assert marker not in result.stdout + result.stderr


def test_key_value_never_printed_and_no_preflight_escape(outsider):
    secret = "test-secret-must-stay-hidden"
    result, plan = dry(outsider, "openrouter", env_extra={"OPENROUTER_API_KEY": secret})
    assert result.returncode == 0
    assert plan["preflight"]["key_variable"]["set"] is True
    assert secret not in result.stdout + result.stderr
    skipped, plan = dry(outsider, "openrouter", "--no-preflight")
    assert skipped.returncode == 0
    assert plan["preflight"] == {"skipped": True}


def test_public_fast_needs_no_directive(outsider):
    result, plan = dry(outsider, "codex", "--fast", "--no-preflight")
    assert result.returncode == 0, result.stderr
    assert plan["fast"] is True
    assert plan["directive"] is None


def test_codex_zero_exit_with_observed_apply_failure_is_error(outsider, monkeypatch):
    cli, _spec, _home = outsider
    spec = importlib.util.spec_from_file_location("dispatch_public_test", cli)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "run_worker", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, "I couldn’t write files.", "apply_patch failed: sandbox_apply: Operation not permitted"))
    module.CODEX_MODELS["example"] = "example"
    with pytest.raises(module.DelegateError, match="sandbox_apply: Operation not permitted"):
        module.run_codex("example", "inspect", str(cli.parent), 1, write=True)


def test_codex_answer_quoting_apply_failure_is_not_error(outsider, monkeypatch):
    cli, _spec, _home = outsider
    spec = importlib.util.spec_from_file_location("dispatch_public_test", cli)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "run_worker", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, "Report: the verifier hit sandbox_apply: Operation not permitted earlier.", ""))
    module.CODEX_MODELS["example"] = "example"
    assert "Report:" in module.run_codex("example", "inspect", str(cli.parent), 1, write=True)


def test_public_live_messages_use_script_name_and_omit_private_status(outsider):
    cli, task, home = outsider

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers["Content-Length"])
            self.rfile.read(size)
            payload = json.dumps({"choices": [{"message": {"content": "response ok"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {"HOME": str(home), "PATH": "", "TEST_KEY": "placeholder", "DELEGATE_NESTED_CODEX_CAPABILITY": ""}
        result = subprocess.run([sys.executable, str(cli), "--model", "example", "--backend", "openai-compatible",
                                 "--base-url", f"http://127.0.0.1:{server.server_port}", "--api-key-env", "TEST_KEY",
                                 "--spec", str(task)], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert "dispatch.py launch:" in result.stdout
        assert "headroom:" not in result.stdout + result.stderr
        assert "delegate.py:" not in result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
