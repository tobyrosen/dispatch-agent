import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dispatch = load_module("dispatch_under_test", ROOT / "dispatch.py")
dispatch_web = load_module("dispatch_web_under_test", ROOT / "dispatch_web.py")


class DispatchAgentSmokeTests(unittest.TestCase):
    def test_versions_match(self):
        version = (ROOT / "version.txt").read_text(encoding="utf-8").strip()
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertEqual(version, "0.1.0")
        self.assertIn(f'version = "{version}"', pyproject)

    def test_dispatch_parse_args_accepts_template_model(self):
        args = dispatch.parse_args(["--model", "example", "--spec", "-", "--timeout", "10"])

        self.assertEqual(args.model, "example")
        self.assertEqual(args.spec, "-")
        self.assertEqual(args.timeout, 10)

    def test_dispatch_rejects_empty_spec_before_backend_call(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as spec_file:
            with self.assertRaisesRegex(dispatch.DispatchError, "empty spec"):
                dispatch.main(["--model", "example", "--spec", spec_file.name])

    def test_dispatch_http_requires_base_url(self):
        with mock.patch.dict(dispatch.HTTP_MODELS, {"example": "provider-model"}, clear=True):
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(dispatch.DispatchError, "DISPATCH_BASE_URL"):
                    dispatch.run_http("example", "Spec", timeout=1)

    def test_dispatch_web_search_stub_is_explicit(self):
        with self.assertRaisesRegex(dispatch_web.DispatchError, "web_search is not wired up"):
            dispatch_web._web_search("current docs", 3, timeout=1)

    def test_dispatch_web_unknown_tool_returns_error_payload(self):
        result = dispatch_web.execute_tool("unknown", {}, timeout=1)

        self.assertIn("error", result)
        self.assertIn("unknown tool", result["error"])


if __name__ == "__main__":
    unittest.main()
