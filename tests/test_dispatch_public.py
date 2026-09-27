"""Public behavior checks using a copy without the optional private module."""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest


@pytest.fixture
def public_copy(tmp_path):
    source = Path(__file__).resolve().parent.parent
    for name in ("delegate.py", "delegate_web.py", "dispatch.py", "dispatch_web.py"):
        if (source / name).exists():
            shutil.copy2(source / name, tmp_path / name)
    home = tmp_path / "home"
    home.mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text("Describe this task.\n")
    cli = tmp_path / ("dispatch.py" if (tmp_path / "dispatch.py").exists() else "delegate.py")
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    return cli, spec, home, env


@pytest.mark.parametrize("backend", ["codex", "cursor", "ollama", "ollama-cloud", "openrouter", "openai-compatible"])
def test_backend_dry_run(public_copy, backend):
    cli, spec, _home, env = public_copy
    command = [sys.executable, str(cli), "--model", "example", "--backend", backend,
               "--model-id", "vendor/example", "--spec", str(spec), "--dry-run", "--no-preflight"]
    if backend == "openai-compatible":
        command.extend(["--base-url", "https://api.example.invalid/v1"])
    result = subprocess.run(command, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["model_requested"] == "example"
    assert plan["spawn"] is False
    assert plan["lane"] == ("ollama" if backend == "ollama-cloud" else backend)


def test_configured_model_dry_run(public_copy):
    cli, spec, home, env = public_copy
    config_dir = home / ".config" / "dispatch-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '[models.small]\nbackend = "codex"\nmodel = "vendor/small"\n'
    )
    result = subprocess.run([sys.executable, str(cli), "--model", "small", "--spec", str(spec), "--dry-run", "--no-preflight"],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["model_resolved"] == "vendor/small"


def test_configured_cooldown_refuses_before_launch(public_copy):
    cli, spec, home, env = public_copy
    config_dir = home / ".config" / "dispatch-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '[general]\ncooldown_seconds = 300\n'
        '[models.small]\nbackend = "codex"\nmodel = "vendor/small"\n'
    )
    marker = home / ".local" / "state" / "dispatch-agent" / "cooldowns.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"codex": {"until": time.time() + 300}}))
    result = subprocess.run([sys.executable, str(cli), "--model", "small", "--spec", str(spec)],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 3
    assert "cooldown_until" in result.stderr


def test_missing_key_names_variable(public_copy):
    cli, _spec, _home, env = public_copy
    result = subprocess.run([sys.executable, "-c", "import dispatch as d; d.op_read('EXAMPLE_API_KEY', 1)"],
                            cwd=cli.parent, env=env, text=True, capture_output=True)
    if cli.name == "delegate.py":
        result = subprocess.run([sys.executable, "-c", "import delegate as d; d.op_read('EXAMPLE_API_KEY', 1)"],
                                cwd=cli.parent, env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert "EXAMPLE_API_KEY" in result.stderr


def test_web_help_without_optional_library(public_copy):
    cli, _spec, _home, env = public_copy
    web = cli.parent / ("dispatch_web.py" if cli.name == "dispatch.py" else "delegate_web.py")
    result = subprocess.run([sys.executable, str(web), "--help"], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "--write-dir" in result.stdout


@pytest.mark.parametrize("backend", ["ollama", "ollama-cloud", "openrouter", "openai-compatible"])
def test_http_backend_uses_configured_endpoint_and_model(public_copy, backend):
    cli, spec, _home, env = public_copy
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, json.loads(body), self.headers.get("Authorization")))
            data = json.dumps({"choices": [{"message": {"content": "response ok"}}], "usage": {}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env["TEST_API_KEY"] = "placeholder"
        cmd = [sys.executable, str(cli), "--model", "sample", "--backend", backend,
               "--model-id", "vendor/sample", "--base-url", f"http://127.0.0.1:{server.server_port}",
               "--api-key-env", "TEST_API_KEY", "--spec", str(spec)]
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout.rstrip().endswith("response ok")
        assert "headroom:" not in result.stdout + result.stderr
        if cli.name == "dispatch.py":
            assert "delegate.py launch:" not in result.stdout + result.stderr
        assert f"{cli.name} launch:" in result.stdout
        path, body, auth = received[0]
        assert path == "/v1/chat/completions" if backend.startswith("ollama") else path == "/chat/completions"
        assert body["model"] == "vendor/sample"
        assert auth == "Bearer placeholder"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_web_file_read_reports_truncation(public_copy):
    cli, _spec, _home, env = public_copy
    allowed = cli.parent / "allowed"
    allowed.mkdir()
    (allowed / "large.txt").write_text("x" * 61_000)
    module = "dispatch_web" if cli.name == "dispatch.py" else "delegate_web"
    code = """import importlib, json, sys
w = importlib.import_module(sys.argv[1])
out = w._read_file({'path': sys.argv[2]}, [sys.argv[3]])
print(json.dumps({'truncated': out['truncated'], 'total_chars': out['total_chars']}))
"""
    result = subprocess.run([sys.executable, "-c", code, module, str(allowed / "large.txt"), str(allowed)],
                            cwd=cli.parent, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"truncated": True, "total_chars": 61_000}


def test_openrouter_web_tool_loop_writes_allowed_file(public_copy):
    cli, _spec, _home, env = public_copy
    allowed = cli.parent / "allowed"
    allowed.mkdir()
    target = allowed / "result.txt"
    module = "dispatch_web" if cli.name == "dispatch.py" else "delegate_web"
    code = """import importlib, json, sys
w = importlib.import_module(sys.argv[1])
w.OPENROUTER_MODELS['example'] = 'provider/example'
w.op_read = lambda *_args: 'placeholder'
w.log_usage = lambda *_args: None
answers = [
    {'choices': [{'message': {'content': '', 'tool_calls': [{'id': 'one', 'function': {
        'name': 'write_file', 'arguments': json.dumps({'path': sys.argv[2], 'content': 'written'})}}]}}]},
    {'choices': [{'message': {'content': 'done'}}]},
]
w._openrouter_chat = lambda *_args: answers.pop(0)
content, calls = w.run_agent('example', 'write file', 3, 3, False, [sys.argv[3]], False)
print(json.dumps({'content': content, 'calls': calls}))
"""
    result = subprocess.run([sys.executable, "-c", code, module, str(target), str(allowed)],
                            cwd=cli.parent, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"content": "done", "calls": 1}
    assert target.read_text() == "written"
