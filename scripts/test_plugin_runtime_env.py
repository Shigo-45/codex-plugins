"""Validate private transport configuration used during standalone-MCP migration."""
import json
from pathlib import Path
import runpy
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
load = runpy.run_path(str(ROOT / "packaging/gemini-baton/scripts/run.py"))["runtime_environment"]


class RuntimeEnvironmentTests(unittest.TestCase):
    def test_saved_transport_settings_survive_plugin_relocation(self):
        with tempfile.TemporaryDirectory() as temp:
            env = {"GEMINI_BATON_STATE_DIR": temp, "HTTPS_PROXY": "http://old:80", "PATH": "original"}
            self.assertEqual(load(env), env)
            saved = {"GEMINI_BATON_ENV_FILE": "/private/selected.env", "HTTPS_PROXY": "http://local:10808"}
            (Path(temp) / "runtime-env.json").write_text(json.dumps(saved))
            self.assertEqual(load(env), {**env, **saved})

    def test_invalid_values_do_not_disclose_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "runtime-env.json"
            for saved in ({"GEMINI_API_KEY": "private-value"}, {"HTTP_PROXY": 1}, {"HTTP_PROXY": "bad\x00value"}):
                path.write_text(json.dumps(saved))
                with self.assertRaisesRegex(RuntimeError, "Invalid private Gemini") as error:
                    load({"GEMINI_BATON_STATE_DIR": temp})
                self.assertNotIn("private-value", str(error.exception))


if __name__ == "__main__":
    unittest.main()
