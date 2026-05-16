import importlib.util
import unittest
from pathlib import Path


def load_stream_question_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "stream_question.py"
    spec = importlib.util.spec_from_file_location("stream_question", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StreamQuestionScriptTest(unittest.TestCase):
    def test_build_commands_send_prompt_then_stream_session(self):
        module = load_stream_question_module()

        send_command, stream_command = module.build_commands(
            ctc="./claude_tmux_control.py",
            session="work",
            prompt="hello Claude",
            timeout=300.0,
            idle=2.0,
            interval=0.5,
        )

        self.assertEqual(send_command, ["./claude_tmux_control.py", "send", "work", "hello Claude"])
        self.assertEqual(
            stream_command,
            [
                "./claude_tmux_control.py",
                "stream",
                "work",
                "--timeout",
                "300.0",
                "--idle",
                "2.0",
                "--interval",
                "0.5",
            ],
        )

    def test_format_event_returns_human_readable_lines(self):
        module = load_stream_question_module()

        self.assertEqual(module.format_event({"event": "thinking", "text": "plan"}), "[thinking] plan")
        self.assertEqual(
            module.format_event({"event": "tool_use", "id": "toolu_1", "caller": "assistant", "name": "Task"}),
            "[tool_use] Task id=toolu_1 caller=assistant",
        )
        self.assertEqual(
            module.format_event(
                {"event": "tool_result", "tool_use_id": "toolu_1", "is_error": True, "text": "failed"}
            ),
            "[tool_result] tool_use_id=toolu_1 error=true\nfailed",
        )
        self.assertEqual(module.format_event({"event": "assistant_text", "text": "answer"}), "answer")
        self.assertEqual(module.format_event({"event": "done", "answer": "answer"}), "\n[done]\nanswer")


if __name__ == "__main__":
    unittest.main()
