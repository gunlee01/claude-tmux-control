import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def load_web_chat_client_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "web_chat_client.py"
    spec = importlib.util.spec_from_file_location("web_chat_client", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class WebChatClientScriptTest(unittest.TestCase):
    def test_build_stream_command_includes_session_and_integration_options(self):
        module = load_web_chat_client_module()
        args = module.parse_args(
            [
                "--ctc",
                "./claude_tmux_control.py",
                "--cwd",
                "/tmp/project",
                "--prompt",
                "hello",
                "--session-id",
                "550e8400-e29b-41d4-a716-446655440000",
                "--oauth-token-env",
                "ACCOUNT_A_TOKEN",
                "--state-dir",
                "/tmp/state",
                "--root",
                "/tmp/claude",
                "--log",
                "/tmp/log.jsonl",
                "--timeout",
                "120",
            ]
        )

        self.assertEqual(
            module.build_stream_command(args),
            [
                "./claude_tmux_control.py",
                "stream",
                "--cwd",
                "/tmp/project",
                "--timeout",
                "120.0",
                "--interval",
                "2.0",
                "--session-id",
                "550e8400-e29b-41d4-a716-446655440000",
                "--oauth-token-env",
                "ACCOUNT_A_TOKEN",
                "--state-dir",
                "/tmp/state",
                "--root",
                "/tmp/claude",
                "hello",
            ],
        )

    def test_parse_jsonl_line_returns_dict_or_parse_error(self):
        module = load_web_chat_client_module()

        self.assertEqual(module.parse_jsonl_line('{"event":"done"}\n'), {"event": "done"})
        self.assertEqual(module.parse_jsonl_line("\n"), None)
        self.assertEqual(module.parse_jsonl_line("{")["event"], "client_parse_error")

    def test_validate_event_order_requires_done_before_metrics(self):
        module = load_web_chat_client_module()

        self.assertEqual(module.validate_event_order([{"event": "done"}, {"event": "metrics"}]), [])
        self.assertIn("metrics_before_done", module.validate_event_order([{"event": "metrics"}, {"event": "done"}]))
        self.assertIn("missing_done", module.validate_event_order([{"event": "metrics"}]))
        self.assertIn(
            "client_parse_error",
            module.validate_event_order([{"event": "client_parse_error"}, {"event": "done"}, {"event": "metrics"}]),
        )

    def test_summarize_events_validates_expected_answer(self):
        module = load_web_chat_client_module()
        summary = module.summarize_events(
            [
                {"event": "assistant_text", "session_id": "sid", "turn_id": "turn"},
                {"event": "done", "session_id": "sid", "turn_id": "turn", "answer": "client-ok"},
                {"event": "metrics", "session_id": "sid", "turn_id": "turn", "cost": {"turn_usd": 0.1}},
            ],
            returncode=0,
            expected_answer="client-ok",
        )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["record"], "summary")
        self.assertEqual(summary["session_id"], "sid")
        self.assertEqual(summary["turn_id"], "turn")
        self.assertEqual(summary["answer"], "client-ok")
        self.assertEqual(summary["events_seen"], 3)

    def test_write_log_appends_jsonl_records(self):
        module = load_web_chat_client_module()
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "client.jsonl"

            module.write_log(log, {"record": "request"})
            module.write_log(log, {"record": "summary", "ok": True})

            records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["record"] for record in records], ["request", "summary"])


if __name__ == "__main__":
    unittest.main()
