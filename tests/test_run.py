import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from runner import DEFAULT_URL, browser_command, run_account, selected_browser


class RunnerStopTests(unittest.TestCase):
    def setUp(self):
        self.settings = tempfile.TemporaryDirectory()
        self.addCleanup(self.settings.cleanup)
        self.config_path = Path(self.settings.name) / "browser.json"
        config = patch("runner.BROWSER_CONFIG", self.config_path)
        config.start()
        self.addCleanup(config.stop)

    def test_runtime_browser_setting_overrides_old_server_environment(self):
        with patch.dict(os.environ, {"TM_BROWSER":"chromium"}):
            self.config_path.write_text('{"browser":"cloakbrowser"}')
            self.assertEqual(selected_browser(), "cloakbrowser")
            self.config_path.write_text('{"browser":"firefox"}')
            self.assertEqual(selected_browser(), "firefox")
            self.config_path.write_text('{"browser":42}')
            with self.assertRaises(ValueError):
                selected_browser()

    def test_default_is_cloakbrowser_without_runtime_setting(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(selected_browser(), "cloakbrowser")

    def test_chromium_selection_reaches_child_and_saved_context(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"TM_BROWSER":"chromium"}):
            out = Path(temporary) / "attempt"
            script = "import json,sys; data=json.load(sys.stdin); print(json.dumps({'authenticated':False,'browser':data['browser']}))"
            result = run_account("sample@example.invalid", "fixture-secret", out,
                                 [sys.executable, "-c", script], False, DEFAULT_URL)
            self.assertEqual(result["browser"], "chromium")
            self.assertEqual(json.loads((out / "run-context.json").read_text())["browser"], "chromium")

    def test_invalid_engine_fails_before_starting_a_child(self):
        with patch.dict(os.environ, {"TM_BROWSER":"unknown-browser"}), patch("runner.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "TM_BROWSER"):
                browser_command("login.cjs", "auto")
            run.assert_not_called()

    def test_stop_file_terminates_child_while_it_is_waiting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out, stop = root / "attempt", root / "STOP"
            script = (
                "import json,os,pathlib,sys,time; "
                "data=json.load(sys.stdin); "
                "pathlib.Path(data['outputDirectory'],'ready').write_text(str(os.getpid())); "
                "time.sleep(60)"
            )

            def stop_when_ready():
                deadline = time.monotonic() + 5
                while not (out / "ready").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                stop.touch()

            thread = threading.Thread(target=stop_when_ready)
            thread.start()
            try:
                started = time.monotonic()
                result = run_account("sample@example.invalid", "sample-password", out,
                                     [sys.executable, "-c", script], False, DEFAULT_URL, stop_file=stop)
            finally:
                thread.join(timeout=6)
            self.assertTrue(result["stopped"])
            self.assertLess(time.monotonic() - started, 6)
            self.assertTrue((out / "browser-private.log").exists())
            self.assertTrue((out / "STOP").exists())
            pid = int((out / "ready").read_text())
            if os.name == "posix":
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    def test_repeated_communicate_waits_preserve_json_input_and_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "attempt"
            script = (
                "import json,sys,time; data=json.load(sys.stdin); "
                "time.sleep(0.6); "
                "print(json.dumps({'authenticated':False,'emailStatus':200 if data['email']=='sample@example.invalid' else 400}))"
            )
            result = run_account("sample@example.invalid", "sample-password", out,
                                 [sys.executable, "-c", script], False, DEFAULT_URL)
            self.assertEqual(result["emailStatus"], 200)
            self.assertFalse(result.get("stopped"))

    def test_websocket_driver_crash_preserves_progress_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "attempt"
            script = (
                "import json,pathlib,sys; data=json.load(sys.stdin); "
                "pathlib.Path(data['outputDirectory'],'progress.json').write_text(json.dumps({"
                "'stage':'wait_com_return','emailStatus':200,'passwordStatus':200,'exchangeCOMObserved':True,"
                "'email':data['email'],'password':data['password']})); "
                "sys.stderr.write('Error: Assertion error\\n    at FFPage._onWebSocketOpened (coreBundle.js:45124:9)\\n'); "
                "sys.exit(1)"
            )
            result = run_account("sample@example.invalid", "sample-secret", out,
                                 [sys.executable, "-c", script], False, DEFAULT_URL)
            self.assertFalse(result["authenticated"])
            self.assertEqual(result["failureStage"], "browser_driver")
            self.assertEqual(result["errorType"], "PlaywrightWebSocketAssertion")
            self.assertEqual(result["lastStage"], "wait_com_return")
            self.assertEqual((result["emailStatus"], result["passwordStatus"], result["childExitCode"]), (200, 200, 1))
            saved = (out / "runner-error.json").read_text()
            self.assertNotIn("sample@example.invalid", saved)
            self.assertNotIn("sample-secret", saved)
            self.assertFalse((out / "cookie-header.txt").exists())

    def test_invalid_child_output_and_partial_progress_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            for i, value in enumerate(("not-json", "[]")):
                out = Path(temporary) / f"attempt-{i}"
                script = (
                    "import json,pathlib,sys; data=json.load(sys.stdin); "
                    "pathlib.Path(data['outputDirectory'],'progress.json').write_text('{'); "
                    f"print({value!r})"
                )
                result = run_account("sample@example.invalid", "sample-secret", out,
                                     [sys.executable, "-c", script], False, DEFAULT_URL)
                self.assertFalse(result["authenticated"])
                self.assertEqual(result["failureStage"], "browser_process")
                self.assertEqual(result["childExitCode"], 0)
                self.assertNotIn("emailStatus", result)


if __name__ == "__main__":
    unittest.main()
