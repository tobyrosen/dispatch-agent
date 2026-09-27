"""Offline regression checks for the public security defaults."""

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch
from urllib import request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import delegate
    import delegate_web
except ModuleNotFoundError:
    import dispatch as delegate
    import dispatch_web as delegate_web


def test_shell_requires_opt_in(monkeypatch, tmp_path):
    monkeypatch.setattr(delegate_web, "ALLOW_SHELL_TOOL", False)
    assert "run_command" not in {tool["function"]["name"] for tool in delegate_web.FILE_TOOLS}
    assert "requires --unsafe-shell" in delegate_web.execute_tool(None, "run_command", {"command": "echo x"}, 1, [str(tmp_path)], False)["error"]
    monkeypatch.setattr(delegate_web, "ALLOW_SHELL_TOOL", True)
    class Proc:
        returncode = 0
        def communicate(self, timeout=None):
            return "ok", ""
    seen = {}
    def popen(*_args, **kwargs):
        seen.update(kwargs)
        return Proc()
    monkeypatch.setattr(delegate, "INHERIT_CHILD_ENV", False)
    monkeypatch.setattr(delegate_web.subprocess, "Popen", popen)
    assert delegate_web._run_command({"command": "echo x"}, [str(tmp_path)])["stdout"] == "ok"
    assert seen["env"]["HOME"] != os.environ.get("HOME")


def test_shell_flag_and_config_warn(tmp_path):
    cli = tmp_path / "dispatch.py"
    shutil.copy2(delegate.__file__, cli)
    home = tmp_path / "home"
    home.mkdir()
    task = tmp_path / "task.md"
    task.write_text("Task: inspect\n## Grants\npaths-write: ./**\nnetwork: no\ngithub-writes: no\ntools: none\n")
    command = [sys.executable, str(cli), "--model", "example", "--backend", "ollama", "--spec", str(task), "--cwd", str(tmp_path), "--write", "--dry-run", "--no-preflight"]
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "DELEGATE_NESTED_CODEX_CAPABILITY": ""}
    normal = subprocess.run(command, env=env, capture_output=True, text=True)
    assert normal.returncode == 0 and "unconfined" not in normal.stderr
    flagged = subprocess.run(command + ["--unsafe-shell"], env=env, capture_output=True, text=True)
    assert flagged.returncode == 0 and "WARNING" in flagged.stderr and "unconfined" in flagged.stderr
    config = home / ".config/dispatch-agent/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[general]\nallow_shell_tool = true\n")
    configured = subprocess.run(command, env=env, capture_output=True, text=True)
    assert configured.returncode == 0 and "WARNING" in configured.stderr


def test_child_env_allowlist_and_explicit_pass(monkeypatch):
    monkeypatch.setattr(delegate, "INHERIT_CHILD_ENV", False)
    monkeypatch.setattr(delegate, "PASS_ENV", ())
    source = {"PATH": "/bin", "HOME": "/tmp", "AWS_SECRET_ACCESS_KEY": "secret", "CURSOR_API_KEY": "cursor", "OLLAMA_API_KEY": "ollama", "CUSTOM": "chosen", "GH_TOKEN": "opted"}
    env = delegate._scrubbed_env(source, backend="cursor")
    assert env == {"PATH": "/bin", "HOME": "/tmp", "CURSOR_API_KEY": "cursor"}
    monkeypatch.setattr(delegate, "PASS_ENV", ("CUSTOM",))
    assert delegate._scrubbed_env(source, backend="cursor")["CUSTOM"] == "chosen"
    assert "CURSOR_API_KEY" not in delegate._scrubbed_env(source, backend="codex")
    assert delegate._scrubbed_env(source, backend="ollama")["OLLAMA_API_KEY"] == "ollama"
    for backend in ("ollama", "shell", "custom"):
        child_home = Path(delegate._scrubbed_env(source, backend=backend)["HOME"])
        assert child_home.is_dir() and child_home != Path(source["HOME"])
    assert delegate._scrubbed_env(source, backend="codex")["HOME"] == source["HOME"]
    monkeypatch.setattr(delegate, "PASS_ENV", ("GH_TOKEN",))
    assert delegate._scrubbed_env(source, backend="cursor")["GH_TOKEN"] == "opted"
    with pytest.raises(delegate.DelegateError, match="--pass-env"):
        delegate.main(["--model", "x", "--spec", "-", "--pass-env", "BAD=VALUE"])


def test_private_child_env_policy_keeps_home(monkeypatch):
    monkeypatch.setattr(delegate, "INHERIT_CHILD_ENV", True)
    source = {"HOME": "/tmp/real-home", "PATH": "/bin"}
    assert delegate._scrubbed_env(source, backend="ollama")["HOME"] == source["HOME"]


def test_ollama_self_reexec_keeps_alias_and_real_home_state(tmp_path, monkeypatch):
    public_cli = tmp_path / "dispatch.py"
    shutil.copy2(delegate.__file__, public_cli)
    home = tmp_path / "home"
    config = home / ".config" / "dispatch-agent" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[models.local_alias]\nbackend = "ollama"\nmodel = "example:latest"\n')
    task = tmp_path / "task.md"
    task.write_text(
        f"Inspect this task.\n## Grants\npaths-write: {tmp_path}/**\n"
        "network: no\ngithub-writes: no\ntools: file editing\n"
    )
    venv_python = tmp_path / ".venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()
    scratch = tmp_path / "temporary"
    scratch.mkdir()
    argv = ["--model", "local_alias", "--spec", str(task), "--cwd", str(tmp_path), "--write"]
    source_env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""),
                  "TMPDIR": str(scratch), "DELEGATE_LOG_DIR": ""}

    def load_public(name):
        spec = importlib.util.spec_from_file_location(name, public_cli)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.LOCAL_CONFIGURED is False
        return module

    class ReexecRequested(Exception):
        pass

    captured = {}
    with patch.dict(os.environ, source_env, clear=True):
        first = load_public("dispatch_reexec_first")
        monkeypatch.setattr(first.tempfile, "tempdir", str(scratch))

        def capture_exec(path, args, env):
            captured.update(path=path, args=args, env=env)
            raise ReexecRequested

        monkeypatch.setattr(first.os, "execve", capture_exec)
        with pytest.raises(ReexecRequested):
            first.main(argv)
        assert first._TEMP_CHILD_HOME is None

    assert captured["path"] == str(venv_python)
    assert captured["env"]["HOME"] == str(home)
    assert captured["env"]["TMPDIR"] == str(scratch)
    with patch.dict(os.environ, captured["env"], clear=True):
        second = load_public("dispatch_reexec_second")
        monkeypatch.setattr(second.sys, "executable", str(venv_python))
        seen = {}

        def handoff(model, _spec, _timeout, _max_iters, _cwd):
            seen["model"] = model
            return "completed\n"

        monkeypatch.setattr(second, "run_web_write_handoff", handoff)
        second.main(argv)
        assert second._TEMP_CHILD_HOME is None

    assert seen["model"] == "local_alias"
    expected_log = home / ".local" / "state" / "dispatch-agent" / "delegate-grants.jsonl"
    assert second.GRANTS_LOG == str(expected_log)
    assert any(json.loads(line)["verdict"] == "allow" for line in expected_log.read_text().splitlines())
    assert not list(tmp_path.rglob("dispatch-home-*"))


def test_endpoint_origin_strips_userinfo_path_and_query():
    assert delegate.endpoint_origin("https://user:password@example.org:8443/hidden?token=secret") == "https://example.org:8443"
    assert delegate.endpoint_origin("http://[::1]:11434/api/chat") == "http://[::1]:11434"


@pytest.mark.parametrize("url", ["http://example.org/x", "http://localhost/x", "file:///etc/passwd", "https://u:p@example.org/x", "https://example.org/x#frag", "https://example.org/#", "https://example.org/" + "x" * 2050])
def test_provider_url_rejections(url):
    with pytest.raises(delegate.DelegateError):
        delegate.validate_http_url(url)


def test_provider_url_redirect_and_body_caps():
    assert delegate.validate_http_url("http://127.0.0.1:11434/api")
    assert delegate.validate_http_url("https://api.example.org/v1")
    with pytest.raises(delegate.DelegateError, match="redirects are disabled"):
        delegate._NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example/")
    with pytest.raises(delegate.DelegateError, match="size limit"):
        delegate._read_limited(io.BytesIO(b"x" * 11), 10)


@pytest.mark.parametrize("host", ["http://example.org", "https://user:password@example.org", "file:///tmp/socket"])
def test_ollama_sdk_rejects_unsafe_host_before_client(monkeypatch, host):
    calls = []
    monkeypatch.setitem(sys.modules, "ollama", SimpleNamespace(Client=lambda **kwargs: calls.append(kwargs)))
    monkeypatch.setattr(delegate_web, "OLLAMA_HOST", host)
    with pytest.raises(delegate.DelegateError):
        delegate_web.run_agent("example", "task", 1, 1, False, [], False)
    assert calls == []


def test_ollama_sdk_accepts_loopback_host(monkeypatch):
    calls = []
    def client(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("client created")
    monkeypatch.setitem(sys.modules, "ollama", SimpleNamespace(Client=client))
    monkeypatch.setattr(delegate_web, "OLLAMA_HOST", "http://127.0.0.1:11434")
    monkeypatch.setattr(delegate_web, "op_read", lambda *_args: "test-token")
    with pytest.raises(RuntimeError, match="client created"):
        delegate_web.run_agent("example", "task", 1, 1, False, [], False)
    assert calls[0]["host"] == "http://127.0.0.1:11434"


def test_fetch_blocks_private_dns_before_hosted_call(monkeypatch):
    called = []
    class Client:
        def web_fetch(self, **kwargs):
            called.append(kwargs)
    monkeypatch.setattr(delegate_web.socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))])
    out = delegate_web.execute_tool(Client(), "web_fetch", {"url": "https://metadata.example/"}, 1, [], False)
    assert "not public" in out["error"] and not called
    monkeypatch.setattr(delegate_web.socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))])
    assert delegate_web._validate_public_fetch_url("https://example.org/") == "https://example.org/"
    with pytest.raises(delegate.DelegateError):
        delegate_web._validate_public_fetch_url("http://127.0.0.1/")


def test_prompt_uses_end_of_options_and_model_id_is_checked(monkeypatch, tmp_path):
    monkeypatch.setattr(delegate, "INHERIT_CHILD_ENV", False)
    monkeypatch.setattr(delegate, "CURSOR_MODELS", {"example": ("example", ("high",), "high", False)})
    with delegate.cursor_launch("example", "--dangerous", str(tmp_path)) as launch:
        assert launch["argv"][-2] == "--" and launch["argv"][-1].endswith("--dangerous")
    for value in ("-option", "model name", "x" * 129, "x\n--force"):
        with pytest.raises(delegate.DelegateError, match="model id"):
            delegate.validate_model_id(value)


def test_file_tools_reject_symlinks_and_replace_atomically(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("safe")
    (root / "link").symlink_to(outside, target_is_directory=True)
    assert "error" in delegate_web._read_file({"path": "link/secret"}, [str(root)])
    assert "error" in delegate_web._write_file({"path": "link/secret", "content": "bad"}, [str(root)])
    assert (outside / "secret").read_text() == "safe"
    target = root / "target"
    target.symlink_to(outside / "secret")
    assert "error" in delegate_web._write_file({"path": "target", "content": "bad"}, [str(root)])
    target.unlink()
    out = delegate_web._write_file({"path": "target", "content": "good"}, [str(root)])
    assert out["bytes"] == 4 and target.read_text() == "good"


def test_dry_run_redacts_prompt_and_directive(tmp_path):
    task = tmp_path / "task.md"
    task.write_text("SECRET-PROMPT\n")
    public_cli = tmp_path / "dispatch.py"
    shutil.copy2(delegate.__file__, public_cli)
    result = subprocess.run([sys.executable, str(public_cli), "--model", "example", "--backend", "codex", "--spec", str(task), "--directive", "SECRET-DIRECTIVE", "--dry-run"], cwd=tmp_path, env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", ""), "DELEGATE_NESTED_CODEX_CAPABILITY": ""}, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "SECRET-PROMPT" not in result.stdout and "SECRET-DIRECTIVE" not in result.stdout
    plan = json.loads(result.stdout)
    assert plan["prompt"] == {"bytes": len(task.read_bytes()), "sha256": hashlib.sha256(task.read_bytes()).hexdigest()}


def test_core_has_no_operational_identifiers():
    names = ("delegate.py", "delegate_web.py") if (Path(__file__).parents[1] / "delegate.py").exists() else ("dispatch.py", "dispatch_web.py")
    for filename in names:
        text = (Path(__file__).parents[1] / filename).read_text()
        for word in ("het" + "zner" + "-paper" + "clip", "Verified live", "tg" + "-", "Cow" + "ork"):
            assert word not in text


def test_local_extensions_are_loaded_only_beside_the_resolved_script(tmp_path, monkeypatch):
    import_dir = tmp_path / "trusted"
    import_dir.mkdir()
    cli = import_dir / "dispatch.py"
    shutil.copy2(delegate.__file__, cli)
    extension = import_dir / "example_private.py"
    extension.write_text('def install_dispatch(namespace):\n    namespace["EXTENSION_FIXTURE"] = "loaded"\n')
    other = tmp_path / "cwd"
    other.mkdir()
    (other / "ignored_private.py").write_text('raise RuntimeError("cwd extension must not load")\n')
    link = other / "entry.py"
    link.symlink_to(cli)
    monkeypatch.chdir(other)
    spec = importlib.util.spec_from_file_location("dispatch_extension_fixture", link)
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    assert core.EXTENSION_FIXTURE == "loaded"


def test_broken_local_extension_is_not_silently_ignored(tmp_path):
    cli = tmp_path / "dispatch.py"
    shutil.copy2(delegate.__file__, cli)
    (tmp_path / "broken_private.py").write_text('import missing_extension_fixture_dependency\n')
    spec = importlib.util.spec_from_file_location("dispatch_broken_extension", cli)
    core = importlib.util.module_from_spec(spec)
    with pytest.raises(ModuleNotFoundError, match="missing_extension_fixture_dependency"):
        spec.loader.exec_module(core)
