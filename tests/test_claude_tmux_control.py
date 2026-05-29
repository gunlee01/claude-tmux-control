import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import claude_tmux_control as ctc
import ctc_bridge_sessions
import ctc_cli
import ctc_launch
import ctc_pricing
import ctc_state
import ctc_streaming
import ctc_tmux
import ctc_transcripts
import transcript_events


FIXTURES_DIR = Path(__file__).parent / "fixtures"


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.session_exists = False
        self.capture_text = "Done\nclaude> "

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))

        if args[:3] == ["tmux", "has-session", "-t"]:
            if self.session_exists:
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 1, "", "missing")

        if args[:3] == ["tmux", "capture-pane", "-p"]:
            return subprocess.CompletedProcess(args, 0, self.capture_text, "")

        if args[:3] == ["tmux", "list-sessions", "-F"]:
            return subprocess.CompletedProcess(args, 0, "ctc-old\nctc-new\nwork\n", "")

        return subprocess.CompletedProcess(args, 0, "", "")


class TmuxControllerTest(unittest.TestCase):
    def test_start_creates_detached_tmux_session_when_missing(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)

        created = controller.start_session("cc-test", command="claude", cwd="/tmp/project")

        self.assertTrue(created)
        self.assertEqual(
            runner.calls,
            [
                (["tmux", "has-session", "-t", "cc-test"], {"check": False, "capture_output": True, "text": True}),
                (
                    ["tmux", "new-session", "-d", "-s", "cc-test", "-c", "/tmp/project", "claude"],
                    {"check": True},
                ),
            ],
        )

    def test_start_can_inject_oauth_token_environment(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)

        created = controller.start_session(
            "cc-test",
            command="claude",
            cwd="/tmp/project",
            env={"CLAUDE_CODE_OAUTH_TOKEN": "oauth-token"},
        )

        self.assertTrue(created)
        self.assertEqual(
            runner.calls[-1],
            (
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    "cc-test",
                    "-e",
                    "CLAUDE_CODE_OAUTH_TOKEN=oauth-token",
                    "-c",
                    "/tmp/project",
                    "claude",
                ],
                {"check": True},
            ),
        )

    def test_start_reuses_existing_session(self):
        runner = FakeRunner()
        runner.session_exists = True
        controller = ctc.TmuxController(run=runner)

        created = controller.start_session("cc-test")

        self.assertFalse(created)
        self.assertEqual(len(runner.calls), 1)

    def test_run_start_reuses_existing_session_without_parsing_environment(self):
        runner = FakeRunner()
        runner.session_exists = True
        controller = ctc.TmuxController(run=runner)
        args = ctc.parse_args(["start", "cc-test", "--env", "MISSING_SECRET"])

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = ctc._run_command(args, controller)

        self.assertEqual(exit_code, 0)
        self.assertIn("reused session: cc-test", stdout.getvalue())
        self.assertEqual(runner.calls, [(["tmux", "has-session", "-t", "cc-test"], {"check": False, "capture_output": True, "text": True})])

    def test_run_start_reports_invalid_claude_args_without_traceback(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)
        args = ctc.parse_args(["start", "cc-test", "--claude-args", "\"unterminated"])

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = ctc._run_command(args, controller)

        self.assertEqual(exit_code, 2)
        self.assertIn("invalid_claude_args", stderr.getvalue())
        self.assertNotIn("new-session", json.dumps(runner.calls))

    def test_run_chat_reports_invalid_claude_args_without_traceback(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)
        args = ctc.parse_args(["chat", "cc-test", "--claude-args", "\"unterminated"])

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = ctc._run_command(args, controller)

        self.assertEqual(exit_code, 2)
        self.assertIn("invalid_claude_args", stderr.getvalue())
        self.assertNotIn("new-session", json.dumps(runner.calls))

    def test_run_launch_reports_invalid_claude_args_without_pasting(self):
        runner = FakeRunner()
        runner.session_exists = True
        controller = ctc.TmuxController(run=runner)
        args = ctc.parse_args(["launch", "cc-test", "--claude-args", "\"unterminated"])

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = ctc._run_command(args, controller)

        self.assertEqual(exit_code, 2)
        self.assertIn("invalid_claude_args", stderr.getvalue())
        self.assertNotIn("load-buffer", json.dumps(runner.calls))

    def test_run_low_level_stream_rejects_claude_launch_args(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)
        args = ctc.parse_args(["stream", "cc-test", "--model", "opus"])

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = ctc._run_command(args, controller)

        self.assertEqual(exit_code, 2)
        self.assertIn("claude_launch_args_require_cwd", stderr.getvalue())

    def test_send_prompt_pastes_text_and_submits_enter(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)

        with patch("ctc_tmux.time.sleep") as sleep:
            controller.send_prompt("cc-test", "hello Claude")

        self.assertEqual(
            runner.calls,
            [
                (
                    ["tmux", "load-buffer", "-b", "claude-tmux-control", "-"],
                    {"input": "hello Claude", "text": True, "check": True},
                ),
                (
                    ["tmux", "paste-buffer", "-d", "-b", "claude-tmux-control", "-t", "cc-test"],
                    {"check": True},
                ),
                (["tmux", "send-keys", "-t", "cc-test", "Enter"], {"check": True}),
            ],
        )
        sleep.assert_called_once_with(ctc.DEFAULT_PASTE_SUBMIT_DELAY_SECONDS)

    def test_send_prompt_can_submit_enter_twice(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)

        with patch("ctc_tmux.time.sleep") as sleep:
            controller.send_prompt("cc-test", "hello Claude", submit_enters=2)

        self.assertEqual(
            runner.calls,
            [
                (
                    ["tmux", "load-buffer", "-b", "claude-tmux-control", "-"],
                    {"input": "hello Claude", "text": True, "check": True},
                ),
                (
                    ["tmux", "paste-buffer", "-d", "-b", "claude-tmux-control", "-t", "cc-test"],
                    {"check": True},
                ),
                (["tmux", "send-keys", "-t", "cc-test", "Enter"], {"check": True}),
                (["tmux", "send-keys", "-t", "cc-test", "Enter"], {"check": True}),
            ],
        )
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [ctc.DEFAULT_PASTE_SUBMIT_DELAY_SECONDS, ctc.DEFAULT_SECOND_SUBMIT_DELAY_SECONDS],
        )

    def test_send_prompt_uses_bracketed_paste_for_multiline_prompt(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)

        with patch("ctc_tmux.time.sleep") as sleep:
            controller.send_prompt("cc-test", "first line\nsecond line")

        self.assertEqual(
            runner.calls,
            [
                (
                    ["tmux", "load-buffer", "-b", "claude-tmux-control", "-"],
                    {"input": "first line\nsecond line", "text": True, "check": True},
                ),
                (
                    ["tmux", "paste-buffer", "-p", "-d", "-b", "claude-tmux-control", "-t", "cc-test"],
                    {"check": True},
                ),
                (["tmux", "send-keys", "-t", "cc-test", "Enter"], {"check": True}),
            ],
        )
        sleep.assert_called_once_with(ctc.DEFAULT_PASTE_SUBMIT_DELAY_SECONDS)

    def test_send_prompt_uses_bracketed_paste_for_carriage_return_prompt(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)

        controller.send_prompt("cc-test", "first line\rsecond line")

        self.assertEqual(
            runner.calls,
            [
                (
                    ["tmux", "load-buffer", "-b", "claude-tmux-control", "-"],
                    {"input": "first line\rsecond line", "text": True, "check": True},
                ),
                (
                    ["tmux", "paste-buffer", "-p", "-d", "-b", "claude-tmux-control", "-t", "cc-test"],
                    {"check": True},
                ),
                (["tmux", "send-keys", "-t", "cc-test", "Enter"], {"check": True}),
            ],
        )

    def test_send_prompt_can_leave_text_unsubmitted(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)

        with patch("ctc_tmux.time.sleep") as sleep:
            controller.send_prompt("cc-test", "draft only", submit=False)

        self.assertNotIn((["tmux", "send-keys", "-t", "cc-test", "Enter"], {"check": True}), runner.calls)
        sleep.assert_not_called()

    def test_send_escape_sends_escape_key(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)

        controller.send_escape("cc-test")

        self.assertEqual(runner.calls, [(["tmux", "send-keys", "-t", "cc-test", "Escape"], {"check": True})])

    def test_launch_in_existing_session_requires_existing_tmux_session(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)

        with self.assertRaises(ctc.SessionNotFoundError):
            controller.launch_in_existing_session("cc-test")

    def test_launch_in_existing_session_pastes_claude_command(self):
        runner = FakeRunner()
        runner.session_exists = True
        controller = ctc.TmuxController(run=runner)

        controller.launch_in_existing_session("cc-test", command="claude --dangerously-skip-permissions")

        self.assertEqual(
            runner.calls,
            [
                (["tmux", "has-session", "-t", "cc-test"], {"check": False, "capture_output": True, "text": True}),
                (
                    ["tmux", "load-buffer", "-b", "claude-tmux-control", "-"],
                    {"input": "claude --dangerously-skip-permissions", "text": True, "check": True},
                ),
                (
                    ["tmux", "paste-buffer", "-d", "-b", "claude-tmux-control", "-t", "cc-test"],
                    {"check": True},
                ),
                (["tmux", "send-keys", "-t", "cc-test", "Enter"], {"check": True}),
            ],
        )

    def test_capture_screen_reads_rendered_tmux_pane(self):
        runner = FakeRunner()
        runner.capture_text = "Claude answer\n\n"
        controller = ctc.TmuxController(run=runner)

        self.assertEqual(controller.capture_screen("cc-test", height=80), "Claude answer\n")
        self.assertEqual(
            runner.calls[-1],
            (
                ["tmux", "capture-pane", "-p", "-t", "cc-test", "-S", "-80"],
                {"check": True, "capture_output": True, "text": True},
            ),
        )

    def test_pane_current_path_reads_tmux_current_path(self):
        runner = FakeRunner()

        def run(args, **kwargs):
            runner.calls.append((args, kwargs))
            if args[:3] == ["tmux", "display-message", "-p"]:
                return subprocess.CompletedProcess(args, 0, "/tmp/project\n", "")
            return runner(args, **kwargs)

        controller = ctc.TmuxController(run=run)

        self.assertEqual(controller.pane_current_path("cc-test"), Path("/tmp/project"))

    def test_list_sessions_reads_tmux_session_names(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)

        self.assertEqual(controller.list_sessions(), ["ctc-old", "ctc-new", "work"])

    def test_kill_session_checks_existence_then_kills_tmux_session(self):
        runner = FakeRunner()
        runner.session_exists = True
        controller = ctc.TmuxController(run=runner)

        controller.kill_session("ctc-old")

        self.assertEqual(
            runner.calls[-1],
            (["tmux", "kill-session", "-t", "ctc-old"], {"check": True}),
        )

    def test_kill_session_requires_existing_tmux_session(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)

        with self.assertRaises(ctc.SessionNotFoundError):
            controller.kill_session("missing")


class CliTest(unittest.TestCase):
    def test_top_level_version_prints_package_version(self):
        stdout = io.StringIO()

        with self.assertRaises(SystemExit) as context, redirect_stdout(stdout):
            ctc.parse_args(["--version"])

        self.assertEqual(context.exception.code, 0)
        self.assertEqual(stdout.getvalue(), "ctc 0.7.6\n")

    def test_top_level_help_separates_web_and_low_level_commands(self):
        stdout = io.StringIO()

        with self.assertRaises(SystemExit), redirect_stdout(stdout):
            ctc.parse_args(["--help"])

        output = stdout.getvalue()
        self.assertIn("High-level web/client commands:", output)
        self.assertIn("stream --cwd PATH [--session-id UUID] PROMPT", output)
        self.assertIn("Low-level tmux/debug commands:", output)
        self.assertIn("start TMUX_SESSION --cwd PATH", output)
        self.assertIn("Do not pass ctc-csess-$SESSION_ID to low-level start.", output)
        self.assertIn("WEB: list high-level controlled web sessions.", output)
        self.assertIn("LOW: create/reuse a named tmux session", output)
        self.assertIn("debugging.", output)
        self.assertIn("docs/quickstart-web-client.md", output)

    def test_parse_start_defaults_to_claude_options(self):
        args = ctc.parse_args(["start", "work"])

        self.assertEqual(args.command_name, "start")
        self.assertEqual(args.session, "work")
        self.assertIsNone(args.model)
        self.assertIsNone(args.claude_args_string)
        self.assertEqual(Path(args.cwd), Path.cwd())
        self.assertEqual(args.oauth_token_env, "CLAUDE_CODE_OAUTH_TOKEN")

    def test_parse_start_accepts_model_and_claude_args(self):
        args = ctc.parse_args(["start", "work", "--cwd", "/tmp/project", "--model", "opus", "--claude-args", "--add-dir ../other"])

        self.assertEqual(args.command_name, "start")
        self.assertEqual(args.cwd, "/tmp/project")
        self.assertEqual(args.model, "opus")
        self.assertEqual(args.claude_args_string, "--add-dir ../other")

    def test_parse_start_rejects_unknown_claude_options(self):
        with self.assertRaises(SystemExit):
            ctc.parse_args(["start", "work", "--cwd", "/tmp/project", "--add-dir", "../other"])

    def test_parse_status_rejects_unknown_args(self):
        with self.assertRaises(SystemExit):
            ctc.parse_args(["status", "work", "--model", "opus"])

    def test_parse_send_accepts_multi_word_prompt(self):
        args = ctc.parse_args(["send", "work", "hello", "Claude"])

        self.assertEqual(args.command_name, "send")
        self.assertEqual(args.prompt, ["hello", "Claude"])

    def test_parse_launch_defaults_to_claude_options(self):
        args = ctc.parse_args(["launch", "work"])

        self.assertEqual(args.command_name, "launch")
        self.assertEqual(args.session, "work")
        self.assertIsNone(args.model)
        self.assertIsNone(args.claude_args_string)

    def test_parse_launch_accepts_model_and_claude_args(self):
        args = ctc.parse_args(["launch", "work", "--model", "opus", "--claude-args", "--add-dir ../shared"])

        self.assertEqual(args.command_name, "launch")
        self.assertEqual(args.session, "work")
        self.assertEqual(args.model, "opus")
        self.assertEqual(args.claude_args_string, "--add-dir ../shared")

    def test_parse_chat_accepts_model_and_claude_args(self):
        args = ctc.parse_args(["chat", "work", "--cwd", "/tmp/project", "--model", "opus", "--claude-args", "--add-dir ../shared"])

        self.assertEqual(args.command_name, "chat")
        self.assertEqual(args.session, "work")
        self.assertEqual(args.cwd, "/tmp/project")
        self.assertEqual(args.model, "opus")
        self.assertEqual(args.claude_args_string, "--add-dir ../shared")

    def test_parse_stream_and_ask_accept_model_and_claude_args(self):
        stream_args = ctc.parse_args(["stream", "--cwd", "/tmp/project", "--model", "opus", "--claude-args", "--add-dir ../shared", "hello"])
        ask_args = ctc.parse_args(["ask", "--cwd", "/tmp/project", "--model", "sonnet", "--claude-args", "--add-dir ../shared", "hello"])

        self.assertEqual(stream_args.model, "opus")
        self.assertEqual(stream_args.claude_args_string, "--add-dir ../shared")
        self.assertEqual(ask_args.model, "sonnet")
        self.assertEqual(ask_args.claude_args_string, "--add-dir ../shared")

    def test_parse_rejects_command_on_claude_launch_commands(self):
        for argv in (
            ["start", "work", "--command", "claude --model opus"],
            ["launch", "work", "--command", "claude --model opus"],
            ["chat", "work", "--command", "claude --model opus"],
            ["stream", "--cwd", "/tmp/project", "--command", "claude --model opus", "hello"],
            ["ask", "--cwd", "/tmp/project", "--command", "claude --model opus", "hello"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    ctc.parse_args(argv)

    def test_build_claude_command_adds_dangerous_skip_permissions(self):
        self.assertEqual(
            ctc.build_claude_command(["--model", "opus"]),
            "claude --model opus --dangerously-skip-permissions",
        )

    def test_claude_args_from_options_appends_model_after_claude_args(self):
        args = ctc.parse_args(["start", "work", "--model", "opus", "--claude-args", "--add-dir ../other"])

        self.assertEqual(ctc.claude_args_from_options(args), ["--add-dir", "../other", "--model", "opus"])

    def test_claude_args_from_options_rejects_duplicate_model_forms(self):
        with self.assertRaisesRegex(ValueError, "duplicate_model"):
            ctc.claude_args_from_options(ctc.parse_args(["start", "work", "--model", "sonnet", "--claude-args", "--model opus"]))
        with self.assertRaisesRegex(ValueError, "duplicate_model"):
            ctc.claude_args_from_options(ctc.parse_args(["start", "work", "--model", "sonnet", "--claude-args", "--model=opus"]))

    def test_claude_args_from_options_rejects_malformed_args(self):
        with self.assertRaisesRegex(ValueError, "invalid_claude_args"):
            ctc.claude_args_from_options(ctc.parse_args(["start", "work", "--claude-args", "\"unterminated"]))

    def test_build_claude_command_appends_claude_args(self):
        self.assertEqual(
            ctc.build_claude_command(["--model", "opus", "--add-dir", "../other"]),
            "claude --model opus --add-dir ../other --dangerously-skip-permissions",
        )

    def test_build_claude_command_does_not_duplicate_permission_flag(self):
        self.assertEqual(
            ctc.build_claude_command(["--dangerously-skip-permissions"]),
            "claude --dangerously-skip-permissions",
        )
        self.assertEqual(
            ctc.build_claude_command(["--permission-mode", "bypassPermissions"]),
            "claude --permission-mode bypassPermissions",
        )
        self.assertEqual(
            ctc.build_claude_command(["--permission-mode=plan"]),
            "claude --permission-mode=plan",
        )

    def test_oauth_environment_reads_configured_source_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ctc.parse_args(["start", "work", "--cwd", tmp, "--oauth-token-env", "ACCOUNT_A_TOKEN"])

            self.assertEqual(
                ctc.claude_environment_from_args(args, environ={"ACCOUNT_A_TOKEN": "oauth-token"}),
                {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-token"},
            )

    def test_oauth_environment_is_empty_when_source_env_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ctc.parse_args(["start", "work", "--cwd", tmp, "--oauth-token-env", "ACCOUNT_A_TOKEN"])

            self.assertEqual(ctc.claude_environment_from_args(args, environ={}), {})

    def test_parse_accepts_explicit_environment_options(self):
        args = ctc.parse_args(
            [
                "stream",
                "--cwd",
                "/tmp/project",
                "--env-file",
                "/tmp/project/.ctc.env",
                "--env",
                "SERVICE_API_KEY",
                "hello",
            ]
        )

        self.assertEqual(args.env_files, [Path("/tmp/project/.ctc.env")])
        self.assertEqual(args.env_names, ["SERVICE_API_KEY"])

    def test_claude_environment_reads_env_file_and_process_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "custom.env"
            env_file.write_text(
                "\n".join(
                    [
                        "# comment",
                        "SERVICE_BASE_URL=https://api.example.test",
                        "export QUOTED='hello world'",
                        'DOUBLE_QUOTED="yes"',
                    ]
                ),
                encoding="utf-8",
            )
            args = ctc.parse_args(
                [
                    "stream",
                    "--cwd",
                    tmp,
                    "--env-file",
                    str(env_file),
                    "--env",
                    "SERVICE_API_KEY",
                    "hello",
                ]
            )

            env = ctc.claude_environment_from_args(args, environ={"SERVICE_API_KEY": "secret"})

            self.assertEqual(env["SERVICE_BASE_URL"], "https://api.example.test")
            self.assertEqual(env["QUOTED"], "hello world")
            self.assertEqual(env["DOUBLE_QUOTED"], "yes")
            self.assertEqual(env["SERVICE_API_KEY"], "secret")

    def test_claude_environment_uses_default_cwd_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".ctc.env").write_text("SERVICE_BASE_URL=https://default.example.test\n", encoding="utf-8")
            args = ctc.parse_args(["stream", "--cwd", tmp, "hello"])

            self.assertEqual(
                ctc.claude_environment_from_args(args, environ={}),
                {"SERVICE_BASE_URL": "https://default.example.test"},
            )

    def test_claude_environment_rejects_malformed_env_file_without_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "bad.env"
            env_file.write_text("SERVICE_API_KEY secret-value\n", encoding="utf-8")
            args = ctc.parse_args(["start", "work", "--cwd", tmp, "--env-file", str(env_file)])

            with self.assertRaisesRegex(ValueError, r"invalid_env_file: .*:1"):
                ctc.claude_environment_from_args(args, environ={})

            try:
                ctc.claude_environment_from_args(args, environ={})
            except ValueError as error:
                self.assertNotIn("secret-value", str(error))

    def test_claude_environment_rejects_reserved_oauth_from_env_file_and_env_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "bad.env"
            env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=file-token\n", encoding="utf-8")
            file_args = ctc.parse_args(["start", "work", "--cwd", tmp, "--env-file", str(env_file)])
            env_args = ctc.parse_args(["start", "work", "--cwd", tmp, "--env", "CLAUDE_CODE_OAUTH_TOKEN"])

            with self.assertRaisesRegex(ValueError, "reserved_env: CLAUDE_CODE_OAUTH_TOKEN"):
                ctc.claude_environment_from_args(file_args, environ={})
            with self.assertRaisesRegex(ValueError, "reserved_env: CLAUDE_CODE_OAUTH_TOKEN"):
                ctc.claude_environment_from_args(env_args, environ={"CLAUDE_CODE_OAUTH_TOKEN": "token"})

    def test_claude_environment_fails_closed_when_whitelisted_env_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ctc.parse_args(["start", "work", "--cwd", tmp, "--env", "SERVICE_API_KEY"])

            with self.assertRaisesRegex(ValueError, "missing_env: SERVICE_API_KEY"):
                ctc.claude_environment_from_args(args, environ={})

    def test_claude_environment_process_env_overrides_env_file_for_non_oauth_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "custom.env"
            env_file.write_text("SERVICE_API_KEY=file-value\n", encoding="utf-8")
            args = ctc.parse_args(["start", "work", "--cwd", tmp, "--env-file", str(env_file), "--env", "SERVICE_API_KEY"])

            self.assertEqual(
                ctc.claude_environment_from_args(args, environ={"SERVICE_API_KEY": "process-value"}),
                {"SERVICE_API_KEY": "process-value"},
            )

    def test_claude_environment_applies_oauth_after_extra_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "custom.env"
            env_file.write_text("SERVICE_API_KEY=file-value\n", encoding="utf-8")
            args = ctc.parse_args(
                ["start", "work", "--cwd", tmp, "--env-file", str(env_file), "--oauth-token-env", "ACCOUNT_A_TOKEN"]
            )

            self.assertEqual(
                ctc.claude_environment_from_args(args, environ={"ACCOUNT_A_TOKEN": "oauth-token"}),
                {"SERVICE_API_KEY": "file-value", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token"},
            )

    def test_preflight_requires_tmux(self):
        args = ctc.parse_args(["send", "work", "hello"])

        error = ctc.check_runtime_dependencies(args, which=lambda command: None)

        self.assertIn("tmux not found in PATH", error)
        self.assertIn("sudo yum install -y tmux", error)

    def test_preflight_requires_claude_for_launch_commands(self):
        args = ctc.parse_args(["start", "work"])

        def which(command):
            return "/usr/bin/tmux" if command == "tmux" else None

        error = ctc.check_runtime_dependencies(args, which=which)

        self.assertIn("Claude Code executable not found in PATH: claude", error)
        self.assertIn("curl -fsSL https://claude.ai/install.sh | bash", error)

    def test_preflight_does_not_require_claude_for_non_launch_commands(self):
        args = ctc.parse_args(["events", "work"])

        error = ctc.check_runtime_dependencies(args, which=lambda command: "/usr/bin/tmux" if command == "tmux" else None)

        self.assertIsNone(error)

    def test_preflight_checks_fixed_claude_executable(self):
        args = ctc.parse_args(["start", "work", "--claude-args", "--model opus"])

        def which(command):
            return "/usr/bin/tmux" if command == "tmux" else None

        error = ctc.check_runtime_dependencies(args, which=which)

        self.assertIn("Claude Code executable not found in PATH: claude", error)

    def test_parse_events_accepts_optional_session_name(self):
        args = ctc.parse_args(["events", "work", "--tail", "5"])

        self.assertEqual(args.command_name, "events")
        self.assertEqual(args.session, "work")
        self.assertEqual(args.tail, 5)

    def test_parse_answer_requires_session_name(self):
        args = ctc.parse_args(["answer", "work", "--tail", "3"])

        self.assertEqual(args.command_name, "answer")
        self.assertEqual(args.session, "work")
        self.assertEqual(args.count, 3)

    def test_parse_turn_requires_session_name(self):
        args = ctc.parse_args(["turn", "work", "--count", "2"])

        self.assertEqual(args.command_name, "turn")
        self.assertEqual(args.session, "work")
        self.assertEqual(args.count, 2)

    def test_answer_does_not_fallback_to_latest_transcript_for_unknown_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "latest.jsonl").write_text(
                json_line({"type": "user", "message": {"content": "real prompt"}})
                + json_line({"type": "assistant", "message": {"content": [{"type": "text", "text": "real answer"}]}}),
                encoding="utf-8",
            )
            args = ctc.parse_args(["answer", "any", "--root", str(root)])
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = ctc._print_latest_answer(args, ctc.TmuxController(run=FakeRunner()))

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("no transcript found", stderr.getvalue())

    def test_turn_does_not_fallback_to_latest_transcript_for_unknown_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "latest.jsonl").write_text(
                json_line({"type": "user", "message": {"content": "real prompt"}})
                + json_line({"type": "assistant", "message": {"content": [{"type": "text", "text": "real answer"}]}}),
                encoding="utf-8",
            )
            args = ctc.parse_args(["turn", "any", "--root", str(root)])
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = ctc._print_latest_turn(args, ctc.TmuxController(run=FakeRunner()))

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("no transcript found", stderr.getvalue())

    def test_parse_info_accepts_session_id_and_json(self):
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        args = ctc.parse_args(["info", session_id, "--json"])

        self.assertEqual(args.command_name, "info")
        self.assertEqual(args.session_id, session_id)
        self.assertEqual(args.state_dir, ctc.DEFAULT_STATE_DIR)
        self.assertTrue(args.json)

    def test_parse_list_accepts_json(self):
        args = ctc.parse_args(["list", "--json"])

        self.assertEqual(args.command_name, "list")
        self.assertEqual(args.state_dir, ctc.DEFAULT_STATE_DIR)
        self.assertTrue(args.json)

    def test_parse_ask_accepts_high_level_prompt(self):
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        args = ctc.parse_args(["ask", "--session-id", session_id, "--cwd", "/tmp/project", "hello", "Claude"])

        self.assertEqual(args.command_name, "ask")
        self.assertEqual(args.session_id, session_id)
        self.assertEqual(args.cwd, Path("/tmp/project"))
        self.assertEqual(ctc._high_level_prompt_from_args(args), "hello Claude")
        self.assertEqual(args.tool_result_limit, 100)
        self.assertEqual(args.submit_enters, 2)

    def test_parse_stream_requires_session_name(self):
        args = ctc.parse_args(["stream", "work", "--timeout", "300", "--idle", "1.5"])

        self.assertEqual(args.command_name, "stream")
        self.assertEqual(args.session, "work")
        self.assertEqual(args.timeout, 300)
        self.assertEqual(args.idle, 1.5)
        self.assertEqual(args.tool_result_limit, 100)

    def test_parse_stream_defaults_to_three_point_five_idle_seconds(self):
        args = ctc.parse_args(["stream", "work"])

        self.assertEqual(args.idle, 3.5)

    def test_parse_stream_accepts_tool_result_limit(self):
        args = ctc.parse_args(["stream", "work", "--tool-result-limit", "240"])

        self.assertEqual(args.tool_result_limit, 240)

    def test_parse_high_level_stream_accepts_session_id_cwd_and_prompt(self):
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        args = ctc.parse_args(["stream", "--session-id", session_id, "--cwd", "/tmp/project", "hello", "Claude"])

        self.assertEqual(args.command_name, "stream")
        self.assertEqual(args.session_id, session_id)
        self.assertEqual(args.cwd, Path("/tmp/project"))
        self.assertEqual(ctc._high_level_prompt_from_args(args), "hello Claude")
        self.assertEqual(args.idle, 3.5)
        self.assertEqual(args.submit_enters, 2)
        self.assertTrue(ctc._is_high_level_stream_args(args))

    def test_parse_high_level_stream_accepts_submit_enters_override(self):
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        args = ctc.parse_args(["stream", "--session-id", session_id, "--cwd", "/tmp/project", "--submit-enters", "1", "hello"])

        self.assertEqual(args.submit_enters, 1)

    def test_parse_high_level_stream_accepts_attach_without_cwd(self):
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        args = ctc.parse_args(["stream", "--attach", "--session-id", session_id])

        self.assertEqual(args.command_name, "stream")
        self.assertTrue(args.attach)
        self.assertEqual(args.session_id, session_id)
        self.assertTrue(ctc._is_high_level_stream_args(args))

    def test_parse_kill_requires_session_name(self):
        args = ctc.parse_args(["kill", "ctc-old"])

        self.assertEqual(args.command_name, "kill")
        self.assertEqual(args.session, "ctc-old")

    def test_parse_reap_defaults_to_ctc_prefix(self):
        args = ctc.parse_args(["reap", "--idle-seconds", "1800", "--dry-run"])

        self.assertEqual(args.command_name, "reap")
        self.assertEqual(args.idle_seconds, 1800)
        self.assertEqual(args.prefix, "ctc-")
        self.assertTrue(args.dry_run)


class RefactorCompatibilityContractTest(unittest.TestCase):
    def test_console_scripts_stay_on_facade_main(self):
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('ctc = "claude_tmux_control:main"', pyproject)
        self.assertIn('claude-tmux-control = "claude_tmux_control:main"', pyproject)

    def test_packaging_keeps_current_public_modules(self):
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn(
            'py-modules = ["claude_tmux_control", "transcript_events", "ctc_tmux", "ctc_launch", "ctc_transcripts", "ctc_pricing", "ctc_state", "ctc_streaming", "ctc_bridge_sessions", "ctc_cli"]',
            pyproject,
        )

    def test_transcript_event_types_keep_canonical_identity(self):
        self.assertIs(ctc.ScreenStatus, transcript_events.ScreenStatus)
        self.assertIs(ctc.TranscriptRecord, transcript_events.TranscriptRecord)

    def test_transcript_event_helpers_keep_canonical_identity(self):
        helper_names = [
            "analyze_transcript_status",
            "analyze_turn_status",
            "extract_latest_answer_text",
            "extract_answer_texts",
            "format_latest_turn",
            "format_latest_turns",
            "target_turn_events",
            "normalize_stream_events",
            "read_transcript_records",
            "normalize_stream_record",
            "latest_usage",
            "normalize_usage",
            "latest_context",
            "latest_model",
        ]

        for name in helper_names:
            with self.subTest(name=name):
                self.assertIs(getattr(ctc, name), getattr(transcript_events, name))

    def test_tmux_adapter_symbols_keep_canonical_identity(self):
        symbol_names = [
            "ScreenCaptureController",
            "SessionNotFoundError",
            "RenderedScreenFollower",
            "TmuxController",
            "follow_until_idle",
            "DEFAULT_BUFFER_NAME",
            "DEFAULT_PASTE_SUBMIT_DELAY_SECONDS",
            "DEFAULT_SECOND_SUBMIT_DELAY_SECONDS",
        ]

        for name in symbol_names:
            with self.subTest(name=name):
                self.assertIs(getattr(ctc, name), getattr(ctc_tmux, name))

    def test_launch_helpers_keep_canonical_identity(self):
        symbol_names = [
            "CLAUDE_OAUTH_TOKEN_ENV",
            "CLAUDE_DANGEROUS_SKIP_PERMISSIONS_FLAG",
            "CLAUDE_LAUNCH_COMMANDS",
            "CLAUDE_EXECUTABLE",
            "DEFAULT_ENV_FILE_NAME",
            "ENV_NAME_PATTERN",
            "RESERVED_ENV_NAMES",
            "add_claude_launch_args",
            "add_environment_args",
            "claude_args_from_options",
            "build_claude_command",
            "build_initial_claude_command",
            "claude_environment_from_args",
            "preseed_claude_project_trust",
            "read_env_file",
            "check_runtime_dependencies",
            "_normalize_claude_args_option_values",
            "_shell_ansi_c_quote",
            "_shell_join",
        ]

        for name in symbol_names:
            with self.subTest(name=name):
                self.assertIs(getattr(ctc, name), getattr(ctc_launch, name))

    def test_transcript_path_helpers_keep_canonical_identity(self):
        symbol_names = [
            "extract_transcript_session_id",
            "find_latest_transcript",
            "project_transcript_dir",
            "read_transcript_events",
            "resolve_high_level_transcript",
            "resolve_session_transcript_path",
            "resolve_status_transcript_path",
            "resolve_transcript_path",
            "transcript_file_state",
            "transcript_identity",
            "transcript_matches_or_omits_session_id",
            "transcript_matches_session_id",
        ]

        for name in symbol_names:
            with self.subTest(name=name):
                self.assertIs(getattr(ctc, name), getattr(ctc_transcripts, name))

    def test_pricing_helpers_keep_canonical_identity(self):
        symbol_names = [
            "DEFAULT_PRICING_TABLE",
            "DEFAULT_INSTALLED_PRICING_TABLE",
            "add_session_cost_to_turn_cost",
            "aggregate_turn_usage",
            "cost_totals_from_completed_turns",
            "count_turn_usage_calls",
            "estimate_turn_cost",
            "load_pricing_table",
            "resolve_pricing_table_path",
            "result_total_cost",
            "select_pricing_model",
            "usage_totals_from_completed_turns",
            "_extract_context",
            "_extract_usage",
            "_numeric_value",
            "_turn_cost_for_completed_record",
        ]

        for name in symbol_names:
            with self.subTest(name=name):
                self.assertIs(getattr(ctc, name), getattr(ctc_pricing, name))

    def test_state_helpers_keep_canonical_identity(self):
        symbol_names = [
            "DEFAULT_STATE_DIR",
            "DEFAULT_WEB_SESSION_PREFIX",
            "STATE_SCHEMA_VERSION",
            "SessionState",
            "StateGenerationConflict",
            "StreamRuntime",
            "build_pending_turn_state",
            "break_stale_lock",
            "exclusive_file_lock",
            "mutate_high_level_state",
            "process_exists",
            "read_bridge_state",
            "read_session_state",
            "session_state_path",
            "state_write_lock_path",
            "validate_or_create_session_id",
            "web_session_lock_path",
            "web_session_state_path",
            "web_tmux_session_name",
            "write_session_state",
            "_write_high_level_state",
        ]

        for name in symbol_names:
            with self.subTest(name=name):
                self.assertIs(getattr(ctc, name), getattr(ctc_state, name))

    def test_streaming_helpers_keep_canonical_identity(self):
        symbol_names = [
            "DEFAULT_STREAM_SUBMIT_ENTERS",
            "DEFAULT_TOOL_RESULT_TEXT_LIMIT",
            "DEFAULT_TRANSCRIPT_ROOT",
            "UNANCHORED_SUBMIT_RETRY_SECONDS",
            "analyze_combined_status",
            "analyze_screen_status",
            "high_level_done_payload",
            "high_level_metrics_payload",
            "stream_transcript_until_done",
            "_bottom_screen_area",
            "_capture_screen_status",
            "_compact_payload",
            "_elapsed_ms",
            "_event_content",
            "_extract_text_blocks",
            "_format_user_content",
            "_is_anchor_user_record",
            "_is_external_user_record",
            "_is_interruption_user_content",
            "_is_internal_user_content",
            "_is_metadata_event",
            "_is_tool_result_content",
            "_maybe_retry_unanchored_submit",
            "_normalize_match_whitespace",
            "_text_contains_with_normalized_whitespace",
            "_write_jsonl",
        ]

        for name in symbol_names:
            with self.subTest(name=name):
                self.assertIs(getattr(ctc, name), getattr(ctc_streaming, name))

    def test_bridge_session_helpers_keep_canonical_identity(self):
        symbol_names = [
            "active_turn_recovery_payload",
            "build_session_info_payload",
            "build_session_list_payload",
            "completed_turn_transcript_path",
            "replay_completed_turn_payloads",
            "replay_metrics_payload",
            "transcript_replaced_since_baseline",
            "validated_stored_transcript_path",
            "wait_for_high_level_transcript",
            "_attach_transcript_path",
            "_int_or_none",
            "_session_info_transcript_path",
        ]

        for name in symbol_names:
            with self.subTest(name=name):
                self.assertIs(getattr(ctc, name), getattr(ctc_bridge_sessions, name))

    def test_cli_helpers_keep_canonical_identity(self):
        symbol_names = [
            "DEFAULT_CONTROLLED_PREFIX",
            "PACKAGE_NAME",
            "package_version",
            "parse_args",
        ]

        for name in symbol_names:
            with self.subTest(name=name):
                self.assertIs(getattr(ctc, name), getattr(ctc_cli, name))

    def test_facade_exposes_refactor_contract_symbols(self):
        required_symbols = [
            "main",
            "parse_args",
            "_run_command",
            "TmuxController",
            "RenderedScreenFollower",
            "SessionNotFoundError",
            "claude_args_from_options",
            "build_claude_command",
            "build_initial_claude_command",
            "claude_environment_from_args",
            "preseed_claude_project_trust",
            "find_latest_transcript",
            "resolve_high_level_transcript",
            "project_transcript_dir",
            "read_transcript_events",
            "read_bridge_state",
            "_write_high_level_state",
            "mutate_high_level_state",
            "build_pending_turn_state",
            "transcript_file_state",
            "estimate_turn_cost",
            "stream_high_level_transcript_until_done",
            "prepare_high_level_stream",
            "run_high_level_turn",
            "run_high_level_cancel",
            "run_high_level_replay",
            "reap_idle_sessions",
        ]

        missing = [name for name in required_symbols if not hasattr(ctc, name)]
        self.assertEqual(missing, [])


class FollowUntilIdleTest(unittest.TestCase):
    def test_follow_until_idle_prints_changed_screens_until_stable(self):
        controller = Mock()
        controller.capture_screen.side_effect = ["one\n", "two\n", "two\n", "two\n"]
        writes = []
        current_time = 0.0

        def fake_now():
            return current_time

        def fake_sleep(seconds):
            nonlocal current_time
            current_time += seconds

        ctc.follow_until_idle(
            controller,
            "work",
            height=50,
            interval=1.0,
            idle_seconds=2.0,
            write=writes.append,
            sleep=fake_sleep,
            now=fake_now,
        )

        self.assertEqual(writes, ["one\n", "two\n"])


class ReapTest(unittest.TestCase):
    def _write_high_level_active_reap_fixture(
        self,
        state_dir: Path,
        root: Path,
        cwd: Path,
        session_id: str,
        *,
        prompt: str = "old prompt",
        answer: str | None = None,
    ) -> tuple[str, Path]:
        tmux_session = ctc.web_tmux_session_name(session_id)
        state_path = ctc.web_session_state_path(session_id, state_dir)
        if answer is not None:
            transcript_dir = ctc.project_transcript_dir(root, cwd)
            transcript_dir.mkdir(parents=True)
            transcript_path = transcript_dir / f"{session_id}.jsonl"
            transcript_path.write_text(
                json_line({"sessionId": session_id, "type": "user", "message": {"content": prompt}})
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": answer}]},
                    }
                ),
                encoding="utf-8",
            )
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": session_id,
                    "tmux_session": tmux_session,
                    "cwd": str(cwd),
                    "active_turn": {
                        "turn_id": f"turn_{session_id[:4]}",
                        "claude_state": "working",
                        "stream_state": "active",
                        "heartbeat_at": "2026-05-24T00:00:00Z",
                        "before_send_wall_time_utc": "2026-05-24T00:00:00Z",
                        "prompt_preview": prompt,
                        "anchor_start_offset": 0 if answer is not None else None,
                        "read_offset": 0,
                        "replay_start_offset": 0,
                    },
                }
            ),
            encoding="utf-8",
        )
        os.utime(state_path, (1000.0, 1000.0))
        return tmux_session, state_path

    def test_reap_idle_sessions_kills_only_prefixed_sessions_older_than_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            ctc.write_session_state(ctc.session_state_path("ctc-old", state_dir), "ctc-old", "", "/tmp/project")
            ctc.write_session_state(ctc.session_state_path("ctc-new", state_dir), "ctc-new", "", "/tmp/project")
            os.utime(ctc.session_state_path("ctc-old", state_dir), (1000.0, 1000.0))
            os.utime(ctc.session_state_path("ctc-new", state_dir), (1900.0, 1900.0))
            runner = FakeRunner()
            runner.session_exists = True
            runner.capture_text = "Done\nclaude> "
            controller = ctc.TmuxController(run=runner)

            results = ctc.reap_idle_sessions(
                controller,
                idle_seconds=600,
                prefix="ctc-",
                dry_run=False,
                state_dir=state_dir,
                root=root,
                now=lambda: 2000.0,
            )

            self.assertEqual([result["session"] for result in results], ["ctc-old"])
            self.assertIn((["tmux", "kill-session", "-t", "ctc-old"], {"check": True}), runner.calls)
            self.assertNotIn((["tmux", "kill-session", "-t", "ctc-new"], {"check": True}), runner.calls)
            self.assertNotIn((["tmux", "kill-session", "-t", "work"], {"check": True}), runner.calls)

    def test_reap_idle_sessions_dry_run_does_not_kill(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            ctc.write_session_state(ctc.session_state_path("ctc-old", state_dir), "ctc-old", "", "/tmp/project")
            os.utime(ctc.session_state_path("ctc-old", state_dir), (1000.0, 1000.0))
            runner = FakeRunner()
            runner.session_exists = True
            runner.capture_text = "Done\nclaude> "
            controller = ctc.TmuxController(run=runner)

            results = ctc.reap_idle_sessions(
                controller,
                idle_seconds=600,
                prefix="ctc-",
                dry_run=True,
                state_dir=state_dir,
                root=root,
                now=lambda: 2000.0,
            )

            self.assertEqual(results[0]["action"], "would-kill")
            self.assertNotIn((["tmux", "kill-session", "-t", "ctc-old"], {"check": True}), runner.calls)

    def test_reap_idle_sessions_uses_high_level_session_state_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            tmux_session = ctc.web_tmux_session_name(session_id)
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": tmux_session,
                        "cwd": "/tmp/project",
                        "last_turn": {"claude_state": "ready"},
                    }
                ),
                encoding="utf-8",
            )
            os.utime(state_path, (1000.0, 1000.0))
            controller = Mock()
            controller.list_sessions.return_value = [tmux_session]
            controller.capture_screen.return_value = "Done\nclaude> "

            results = ctc.reap_idle_sessions(
                controller,
                idle_seconds=600,
                prefix="ctc-",
                dry_run=True,
                state_dir=state_dir,
                root=root,
                now=lambda: 2000.0,
            )

            self.assertEqual(results, [{"session": tmux_session, "idle_seconds": 1000.0, "action": "would-kill"}])

    def test_reap_idle_sessions_skips_working_transcript_even_when_input_is_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            project = root / "projects" / "-tmp-project"
            project.mkdir(parents=True)
            (project / "session.jsonl").write_text(
                json_line({"type": "user", "message": {"content": "old prompt"}})
                + json_line({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Task"}]}}),
                encoding="utf-8",
            )
            ctc.write_session_state(ctc.session_state_path("ctc-old", state_dir), "ctc-old", "old prompt", "/tmp/project")
            os.utime(ctc.session_state_path("ctc-old", state_dir), (1000.0, 1000.0))
            runner = FakeRunner()
            runner.session_exists = True
            runner.capture_text = "Done\nclaude> "
            controller = ctc.TmuxController(run=runner)

            results = ctc.reap_idle_sessions(
                controller,
                idle_seconds=600,
                prefix="ctc-",
                dry_run=False,
                state_dir=state_dir,
                root=root,
                now=lambda: 2000.0,
            )

            self.assertEqual(results, [])
            self.assertNotIn((["tmux", "kill-session", "-t", "ctc-old"], {"check": True}), runner.calls)

    def test_reap_idle_sessions_skips_confirmation_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            ctc.write_session_state(ctc.session_state_path("ctc-old", state_dir), "ctc-old", "", "/tmp/project")
            os.utime(ctc.session_state_path("ctc-old", state_dir), (1000.0, 1000.0))
            runner = FakeRunner()
            runner.session_exists = True
            runner.capture_text = "Allow tool execution?\nYes / No\n"
            controller = ctc.TmuxController(run=runner)

            results = ctc.reap_idle_sessions(
                controller,
                idle_seconds=600,
                prefix="ctc-",
                dry_run=False,
                state_dir=state_dir,
                root=root,
                now=lambda: 2000.0,
            )

            self.assertEqual(results, [])
            self.assertNotIn((["tmux", "kill-session", "-t", "ctc-old"], {"check": True}), runner.calls)

    def test_reap_idle_sessions_skips_unknown_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            ctc.write_session_state(ctc.session_state_path("ctc-old", state_dir), "ctc-old", "", "/tmp/project")
            os.utime(ctc.session_state_path("ctc-old", state_dir), (1000.0, 1000.0))
            runner = FakeRunner()
            runner.session_exists = True
            runner.capture_text = "unrecognized pane text\n"
            controller = ctc.TmuxController(run=runner)

            results = ctc.reap_idle_sessions(
                controller,
                idle_seconds=600,
                prefix="ctc-",
                dry_run=False,
                state_dir=state_dir,
                root=root,
                now=lambda: 2000.0,
            )

            self.assertEqual(results, [])
            self.assertNotIn((["tmux", "kill-session", "-t", "ctc-old"], {"check": True}), runner.calls)

    def test_reap_idle_sessions_dry_run_reports_recoverable_high_level_active_turn_without_state_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            tmux_session, state_path = self._write_high_level_active_reap_fixture(
                state_dir,
                root,
                cwd,
                session_id,
                answer="old answer",
            )
            controller = Mock()
            controller.list_sessions.return_value = [tmux_session]
            controller.session_exists.return_value = True
            controller.capture_screen.return_value = "Done\nclaude> "
            before_state = state_path.read_text(encoding="utf-8")
            before_mtime = state_path.stat().st_mtime

            results = ctc.reap_idle_sessions(
                controller,
                idle_seconds=600,
                prefix="ctc-csess-",
                dry_run=True,
                state_dir=state_dir,
                root=root,
                now=lambda: 2000.0,
            )

            self.assertEqual(results, [{"session": tmux_session, "idle_seconds": 1000.0, "action": "would-kill"}])
            self.assertEqual(state_path.read_text(encoding="utf-8"), before_state)
            self.assertEqual(state_path.stat().st_mtime, before_mtime)
            state = json.loads(before_state)
            self.assertEqual(state["active_turn"]["turn_id"], "turn_550e")
            controller.kill_session.assert_not_called()

    def test_reap_idle_sessions_recovers_stale_high_level_active_turn_before_killing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            session_id = "550e8400-e29b-41d4-a716-446655440002"
            tmux_session, state_path = self._write_high_level_active_reap_fixture(
                state_dir,
                root,
                cwd,
                session_id,
                answer="old answer",
            )
            controller = Mock()
            controller.list_sessions.return_value = [tmux_session]
            controller.session_exists.return_value = True
            controller.capture_screen.return_value = "Done\nclaude> "

            results = ctc.reap_idle_sessions(
                controller,
                idle_seconds=600,
                prefix="ctc-csess-",
                dry_run=False,
                state_dir=state_dir,
                root=root,
                now=lambda: 2000.0,
            )

            self.assertEqual(results, [{"session": tmux_session, "idle_seconds": 1000.0, "action": "killed"}])
            recovered = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIsNone(recovered["active_turn"])
            self.assertEqual(recovered["last_turn"]["turn_id"], "turn_550e")
            self.assertEqual(recovered["last_turn"]["stream_state"], "done")
            controller.kill_session.assert_called_once_with(tmux_session)

    def test_reap_idle_sessions_recovers_active_turn_with_skill_context_user_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            session_id = "550e8400-e29b-41d4-a716-446655440003"
            tmux_session, state_path = self._write_high_level_active_reap_fixture(state_dir, root, cwd, session_id)
            transcript_dir = ctc.project_transcript_dir(root, cwd)
            transcript_dir.mkdir(parents=True)
            (transcript_dir / f"{session_id}.jsonl").write_text(
                json_line({"sessionId": session_id, "type": "user", "message": {"content": "old prompt"}})
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "assistant",
                        "message": {"content": [{"type": "tool_use", "name": "Skill"}]},
                    }
                )
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_skill",
                                    "content": "Launching skill: zetta",
                                }
                            ]
                        },
                    }
                )
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "user",
                        "message": {"content": [{"type": "text", "text": "Base directory for this skill: /tmp/skill\n\n# Skill"}]},
                    }
                )
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "old answer"}]},
                    }
                ),
                encoding="utf-8",
            )
            os.utime(state_path, (1000.0, 1000.0))
            controller = Mock()
            controller.list_sessions.return_value = [tmux_session]
            controller.session_exists.return_value = True
            controller.capture_screen.return_value = "Done\nclaude> "

            results = ctc.reap_idle_sessions(
                controller,
                idle_seconds=600,
                prefix="ctc-csess-",
                dry_run=True,
                state_dir=state_dir,
                root=root,
                now=lambda: 2000.0,
            )

            self.assertEqual(results, [{"session": tmux_session, "idle_seconds": 1000.0, "action": "would-kill"}])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["active_turn"]["turn_id"], "turn_550e")

    def test_reap_idle_sessions_keeps_stale_high_level_active_turn_when_screen_is_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            session_id = "550e8400-e29b-41d4-a716-446655440001"
            tmux_session, state_path = self._write_high_level_active_reap_fixture(state_dir, root, cwd, session_id)
            controller = Mock()
            controller.list_sessions.return_value = [tmux_session]
            controller.session_exists.return_value = True
            controller.capture_screen.return_value = "Thinking...\nEsc to interrupt"

            results = ctc.reap_idle_sessions(
                controller,
                idle_seconds=1,
                prefix="ctc-csess-",
                dry_run=False,
                state_dir=state_dir,
                root=root,
                now=lambda: 2000.0,
            )

            self.assertEqual(results, [])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["active_turn"]["turn_id"], "turn_550e")
            controller.kill_session.assert_not_called()

    def test_reap_idle_sessions_kills_unrecoverable_high_level_active_turn_when_screen_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            session_id = "550e8400-e29b-41d4-a716-446655440004"
            tmux_session, state_path = self._write_high_level_active_reap_fixture(state_dir, root, cwd, session_id)
            controller = Mock()
            controller.list_sessions.return_value = [tmux_session]
            controller.session_exists.return_value = True
            controller.capture_screen.return_value = "Done\nclaude> "

            results = ctc.reap_idle_sessions(
                controller,
                idle_seconds=600,
                prefix="ctc-csess-",
                dry_run=False,
                state_dir=state_dir,
                root=root,
                now=lambda: 2000.0,
            )

            self.assertEqual(results, [{"session": tmux_session, "idle_seconds": 1000.0, "action": "killed"}])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["active_turn"]["turn_id"], "turn_550e")
            controller.kill_session.assert_called_once_with(tmux_session)

    def test_reap_idle_sessions_dry_run_reports_unrecoverable_high_level_active_turn_when_screen_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            session_id = "550e8400-e29b-41d4-a716-446655440006"
            tmux_session, state_path = self._write_high_level_active_reap_fixture(state_dir, root, cwd, session_id)
            controller = Mock()
            controller.list_sessions.return_value = [tmux_session]
            controller.session_exists.return_value = True
            controller.capture_screen.return_value = "Done\nclaude> "
            before_state = state_path.read_text(encoding="utf-8")
            before_mtime = state_path.stat().st_mtime

            results = ctc.reap_idle_sessions(
                controller,
                idle_seconds=600,
                prefix="ctc-csess-",
                dry_run=True,
                state_dir=state_dir,
                root=root,
                now=lambda: 2000.0,
            )

            self.assertEqual(results, [{"session": tmux_session, "idle_seconds": 1000.0, "action": "would-kill"}])
            self.assertEqual(state_path.read_text(encoding="utf-8"), before_state)
            self.assertEqual(state_path.stat().st_mtime, before_mtime)
            state = json.loads(before_state)
            self.assertEqual(state["active_turn"]["turn_id"], "turn_550e")
            controller.kill_session.assert_not_called()

    def test_reap_idle_sessions_ignores_other_session_transcript_with_same_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            session_id = "550e8400-e29b-41d4-a716-446655440007"
            tmux_session, state_path = self._write_high_level_active_reap_fixture(state_dir, root, cwd, session_id)
            transcript_dir = ctc.project_transcript_dir(root, cwd)
            transcript_dir.mkdir(parents=True)
            (transcript_dir / "other-session.jsonl").write_text(
                json_line(
                    {
                        "sessionId": "550e8400-e29b-41d4-a716-446655449999",
                        "type": "user",
                        "message": {"content": "old prompt"},
                    }
                )
                + json_line(
                    {
                        "sessionId": "550e8400-e29b-41d4-a716-446655449999",
                        "type": "assistant",
                        "message": {"content": [{"type": "tool_use", "name": "Task"}]},
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.list_sessions.return_value = [tmux_session]
            controller.session_exists.return_value = True
            controller.capture_screen.return_value = "Done\nclaude> "

            results = ctc.reap_idle_sessions(
                controller,
                idle_seconds=600,
                prefix="ctc-csess-",
                dry_run=True,
                state_dir=state_dir,
                root=root,
                now=lambda: 2000.0,
            )

            self.assertEqual(results, [{"session": tmux_session, "idle_seconds": 1000.0, "action": "would-kill"}])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["active_turn"]["turn_id"], "turn_550e")
            controller.kill_session.assert_not_called()

    def test_reap_idle_sessions_does_not_recover_from_other_session_explicit_transcript_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            session_id = "550e8400-e29b-41d4-a716-446655440008"
            tmux_session, state_path = self._write_high_level_active_reap_fixture(state_dir, root, cwd, session_id)
            transcript_dir = ctc.project_transcript_dir(root, cwd)
            transcript_dir.mkdir(parents=True)
            other_transcript = transcript_dir / "other-session.jsonl"
            other_transcript.write_text(
                json_line(
                    {
                        "sessionId": "550e8400-e29b-41d4-a716-446655449999",
                        "type": "user",
                        "message": {"content": "old prompt"},
                    }
                )
                + json_line(
                    {
                        "sessionId": "550e8400-e29b-41d4-a716-446655449999",
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "other answer"}]},
                    }
                ),
                encoding="utf-8",
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["transcript"] = ctc.transcript_file_state(other_transcript, other_transcript.stat().st_size)
            state["active_turn"]["before_send_transcript"] = ctc.transcript_file_state(other_transcript, 0)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            os.utime(state_path, (1000.0, 1000.0))
            controller = Mock()
            controller.list_sessions.return_value = [tmux_session]
            controller.session_exists.return_value = True
            controller.capture_screen.return_value = "Done\nclaude> "

            results = ctc.reap_idle_sessions(
                controller,
                idle_seconds=600,
                prefix="ctc-csess-",
                dry_run=False,
                state_dir=state_dir,
                root=root,
                now=lambda: 2000.0,
            )

            self.assertEqual(results, [{"session": tmux_session, "idle_seconds": 1000.0, "action": "killed"}])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["active_turn"]["turn_id"], "turn_550e")
            self.assertNotIn("last_turn", state)
            controller.kill_session.assert_called_once_with(tmux_session)

    def test_reap_idle_sessions_keeps_unrecoverable_high_level_active_turn_when_screen_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            session_id = "550e8400-e29b-41d4-a716-446655440005"
            tmux_session, state_path = self._write_high_level_active_reap_fixture(state_dir, root, cwd, session_id)
            controller = Mock()
            controller.list_sessions.return_value = [tmux_session]
            controller.session_exists.return_value = True
            controller.capture_screen.return_value = "Thinking...\nEsc to interrupt"

            results = ctc.reap_idle_sessions(
                controller,
                idle_seconds=600,
                prefix="ctc-csess-",
                dry_run=False,
                state_dir=state_dir,
                root=root,
                now=lambda: 2000.0,
            )

            self.assertEqual(results, [])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["active_turn"]["turn_id"], "turn_550e")
            controller.kill_session.assert_not_called()


class HighLevelStreamSetupTest(unittest.TestCase):
    def setUp(self):
        self._home_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._home_tmp.cleanup)
        self._home_patch = patch.object(ctc.Path, "home", return_value=Path(self._home_tmp.name))
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)

    def test_validate_session_id_rejects_non_uuid_before_path_use(self):
        with self.assertRaisesRegex(ValueError, "invalid_session_id"):
            ctc.web_session_state_path("../../bad", Path("/tmp/state"))

    def test_build_initial_claude_command_inlines_prompt_for_new_tmux_session(self):
        command = ctc.build_initial_claude_command(
            ["--model", "opus"],
            "550e8400-e29b-41d4-a716-446655440000",
            resume=True,
            prompt="안녕?\n니 이름은?",
        )

        self.assertEqual(
            command,
            "claude --model opus --resume 550e8400-e29b-41d4-a716-446655440000 --dangerously-skip-permissions -- $'안녕?\\n니 이름은?'",
        )

    def test_build_initial_claude_command_respects_permission_mode_equals(self):
        command = ctc.build_initial_claude_command(
            ["--permission-mode=plan"],
            "550e8400-e29b-41d4-a716-446655440000",
            resume=False,
            prompt="--help를 설명해줘",
        )

        self.assertEqual(
            command,
            "claude --permission-mode=plan --session-id 550e8400-e29b-41d4-a716-446655440000 -- $'--help를 설명해줘'",
        )

    def test_build_initial_claude_command_escapes_inline_prompt_for_shell(self):
        command = ctc.build_initial_claude_command(
            [],
            "550e8400-e29b-41d4-a716-446655440000",
            resume=False,
            prompt="it's \\ ok\nnext",
        )

        self.assertEqual(
            command,
            "claude --session-id 550e8400-e29b-41d4-a716-446655440000 --dangerously-skip-permissions -- $'it\\'s \\\\ ok\\nnext'",
        )

    def test_prepare_high_level_stream_fails_closed_on_cwd_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps({"schema_version": 1, "session_id": session_id, "cwd": "/tmp/other"}),
                encoding="utf-8",
            )
            controller = ctc.TmuxController(run=FakeRunner())

            with self.assertRaisesRegex(ValueError, "session_cwd_mismatch"):
                ctc.prepare_high_level_stream(
                    controller=controller,
                    cwd=Path(tmp),
                    prompt="hello",
                    root=Path(tmp) / "claude",
                    state_dir=state_dir,
                    session_id=session_id,
                )

    def test_prepare_high_level_stream_starts_new_generated_session_with_inline_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner()
            controller = ctc.TmuxController(run=runner)

            runtime = ctc.prepare_high_level_stream(
                controller=controller,
                cwd=Path(tmp),
                prompt="hello Claude",
                root=Path(tmp) / "claude",
                state_dir=Path(tmp) / "state",
            )

            self.assertTrue(runtime.tmux_session.startswith("ctc-csess-"))
            uuid.UUID(runtime.session_id)
            self.assertIn(
                (
                    [
                        "tmux",
                        "new-session",
                        "-d",
                        "-s",
                        runtime.tmux_session,
                        "-c",
                        str(Path(tmp).resolve()),
                        f"claude --session-id {runtime.session_id} --dangerously-skip-permissions -- $'hello Claude'",
                    ],
                    {"check": True},
                ),
                runner.calls,
            )
            self.assertFalse(any(call[0][:2] == ["tmux", "load-buffer"] for call in runner.calls))
            self.assertFalse(any(call[0][:2] == ["tmux", "paste-buffer"] for call in runner.calls))
            self.assertFalse(any(call[0][:2] == ["tmux", "send-keys"] for call in runner.calls))
            state = ctc.read_bridge_state(runtime.state_path)
            self.assertEqual(state["active_turn"]["claude_state"], "starting")
            self.assertTrue(state["active_turn"]["no_transcript_baseline"])

    def test_preseed_claude_project_trust_registers_cwd_and_preserves_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cwd = Path(tmp) / "project"
            config_dir = Path(tmp) / "config"
            cwd.mkdir()
            home.mkdir()
            global_config = home / ".claude.json"
            global_config.write_text(
                json.dumps(
                    {
                        "theme": "dark",
                        "projects": {
                            str(cwd.resolve()): {
                                "allowedTools": ["Bash"],
                                "custom": "keep",
                                "projectOnboardingSeenCount": 2,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            ctc.preseed_claude_project_trust(cwd, env={"CLAUDE_CONFIG_DIR": str(config_dir)}, home=home)

            updated = json.loads(global_config.read_text(encoding="utf-8"))
            project = updated["projects"][str(cwd.resolve())]
            self.assertTrue(updated["hasCompletedOnboarding"])
            self.assertEqual(updated["theme"], "dark")
            self.assertEqual(project["allowedTools"], ["Bash"])
            self.assertEqual(project["custom"], "keep")
            self.assertTrue(project["hasTrustDialogAccepted"])
            self.assertTrue(project["hasCompletedProjectOnboarding"])
            self.assertEqual(project["projectOnboardingSeenCount"], 4)
            settings = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
            self.assertTrue(settings["skipDangerousModePermissionPrompt"])

    def test_prepare_high_level_stream_preseeds_only_for_new_claude_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            runner = FakeRunner()
            controller = ctc.TmuxController(run=runner)

            ctc.prepare_high_level_stream(
                controller=controller,
                cwd=Path(tmp),
                prompt="hello",
                root=Path(tmp) / "claude",
                state_dir=Path(tmp) / "state",
                env={"CLAUDE_CONFIG_DIR": str(Path(tmp) / "claude-config")},
                preseed_project_trust=lambda cwd, env: calls.append((cwd, env)),
            )

            self.assertEqual(calls, [(Path(tmp).resolve(), {"CLAUDE_CONFIG_DIR": str(Path(tmp) / "claude-config")})])

            calls.clear()
            runner.session_exists = True
            runner.capture_text = "Done\nclaude> "
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            ctc.prepare_high_level_stream(
                controller=ctc.TmuxController(run=runner),
                cwd=Path(tmp),
                prompt="second",
                root=Path(tmp) / "claude",
                state_dir=Path(tmp) / "state2",
                session_id=session_id,
                preseed_project_trust=lambda cwd, env: calls.append((cwd, env)),
            )

            self.assertEqual(calls, [])

    def test_prepare_high_level_stream_preseed_integration_writes_temp_home_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "project"
            config_dir = Path(tmp) / "claude-config"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"

            ctc.prepare_high_level_stream(
                controller=ctc.TmuxController(run=FakeRunner()),
                cwd=cwd,
                prompt="hello",
                root=Path(tmp) / "claude",
                state_dir=Path(tmp) / "state",
                session_id=session_id,
                env={"CLAUDE_CONFIG_DIR": str(config_dir)},
            )

            global_config = json.loads((Path(self._home_tmp.name) / ".claude.json").read_text(encoding="utf-8"))
            settings = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
            project = global_config["projects"][str(cwd.resolve())]
            self.assertTrue(global_config["hasCompletedOnboarding"])
            self.assertTrue(project["hasTrustDialogAccepted"])
            self.assertTrue(project["hasCompletedProjectOnboarding"])
            self.assertEqual(project["projectOnboardingSeenCount"], 4)
            self.assertTrue(settings["skipDangerousModePermissionPrompt"])

    def test_prepare_high_level_stream_applies_model_to_new_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runner = FakeRunner()
            controller = ctc.TmuxController(run=runner)
            args = ctc.parse_args(["stream", "--cwd", tmp, "--session-id", session_id, "--model", "opus", "hello"])

            runtime = ctc.prepare_high_level_stream(
                controller=controller,
                cwd=args.cwd,
                prompt="hello",
                root=Path(tmp) / "claude",
                state_dir=Path(tmp) / "state",
                session_id=session_id,
                claude_args_builder=lambda: ctc.claude_args_from_options(args),
            )

            self.assertIn(
                (
                    [
                        "tmux",
                        "new-session",
                        "-d",
                        "-s",
                        runtime.tmux_session,
                        "-c",
                        str(Path(tmp).resolve()),
                        "claude --model opus --session-id 550e8400-e29b-41d4-a716-446655440000 "
                        "--dangerously-skip-permissions -- $'hello'",
                    ],
                    {"check": True},
                ),
                runner.calls,
            )

    def test_prepare_high_level_stream_injects_default_cwd_env_file_for_new_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".ctc.env").write_text("SERVICE_BASE_URL=https://default.example.test\n", encoding="utf-8")
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runner = FakeRunner()
            controller = ctc.TmuxController(run=runner)
            args = ctc.parse_args(["stream", "--cwd", tmp, "--session-id", session_id, "hello"])

            runtime = ctc.prepare_high_level_stream(
                controller=controller,
                cwd=args.cwd,
                prompt="hello",
                root=Path(tmp) / "claude",
                state_dir=Path(tmp) / "state",
                session_id=session_id,
                env_builder=lambda: ctc.claude_environment_from_args(args, environ={}),
            )

            self.assertIn(
                (
                    [
                        "tmux",
                        "new-session",
                        "-d",
                        "-s",
                        runtime.tmux_session,
                        "-e",
                        "SERVICE_BASE_URL=https://default.example.test",
                        "-c",
                        str(Path(tmp).resolve()),
                        "claude --session-id 550e8400-e29b-41d4-a716-446655440000 "
                        "--dangerously-skip-permissions -- $'hello'",
                    ],
                    {"check": True},
                ),
                runner.calls,
            )

    def test_prepare_high_level_stream_reuses_existing_session_without_parsing_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".ctc.env").write_text("not valid\n", encoding="utf-8")
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runner = FakeRunner()
            runner.session_exists = True
            runner.capture_text = "Done\nclaude> "
            controller = ctc.TmuxController(run=runner)
            args = ctc.parse_args(["stream", "--cwd", tmp, "--session-id", session_id, "--env", "MISSING_SECRET", "next"])

            runtime = ctc.prepare_high_level_stream(
                controller=controller,
                cwd=args.cwd,
                prompt="next",
                root=Path(tmp) / "claude",
                state_dir=Path(tmp) / "state",
                session_id=session_id,
                env_builder=lambda: ctc.claude_environment_from_args(args, environ={}),
            )

            self.assertEqual(runtime.tmux_session, "ctc-csess-550e8400-e29b-41d4-a716-446655440000")
            self.assertIn(
                (
                    ["tmux", "load-buffer", "-b", "claude-tmux-control", "-"],
                    {"input": "next", "text": True, "check": True},
                ),
                runner.calls,
            )

    def test_prepare_high_level_stream_reuses_existing_session_without_parsing_claude_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runner = FakeRunner()
            runner.session_exists = True
            runner.capture_text = "Done\nclaude> "
            controller = ctc.TmuxController(run=runner)
            args = ctc.parse_args(["stream", "--cwd", tmp, "--session-id", session_id, "--claude-args", "\"unterminated", "next"])

            runtime = ctc.prepare_high_level_stream(
                controller=controller,
                cwd=args.cwd,
                prompt="next",
                root=Path(tmp) / "claude",
                state_dir=Path(tmp) / "state",
                session_id=session_id,
                claude_args_builder=lambda: ctc.claude_args_from_options(args),
            )

            self.assertEqual(runtime.tmux_session, "ctc-csess-550e8400-e29b-41d4-a716-446655440000")
            self.assertIn(
                (
                    ["tmux", "load-buffer", "-b", "claude-tmux-control", "-"],
                    {"input": "next", "text": True, "check": True},
                ),
                runner.calls,
            )

    def test_prepare_high_level_stream_env_failure_does_not_write_state_for_new_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_dir = Path(tmp) / "state"
            args = ctc.parse_args(["stream", "--cwd", tmp, "--session-id", session_id, "--env", "MISSING_SECRET", "hello"])

            with self.assertRaisesRegex(ValueError, "missing_env: MISSING_SECRET"):
                ctc.prepare_high_level_stream(
                    controller=ctc.TmuxController(run=FakeRunner()),
                    cwd=args.cwd,
                    prompt="hello",
                    root=Path(tmp) / "claude",
                    state_dir=state_dir,
                    session_id=session_id,
                    env_builder=lambda: ctc.claude_environment_from_args(args, environ={}),
                )

            self.assertFalse(ctc.web_session_state_path(session_id, state_dir).exists())

    def test_prepare_high_level_stream_claude_args_failure_does_not_write_state_for_new_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_dir = Path(tmp) / "state"
            args = ctc.parse_args(["stream", "--cwd", tmp, "--session-id", session_id, "--claude-args", "\"unterminated", "hello"])

            with self.assertRaisesRegex(ValueError, "invalid_claude_args"):
                ctc.prepare_high_level_stream(
                    controller=ctc.TmuxController(run=FakeRunner()),
                    cwd=args.cwd,
                    prompt="hello",
                    root=Path(tmp) / "claude",
                    state_dir=state_dir,
                    session_id=session_id,
                    claude_args_builder=lambda: ctc.claude_args_from_options(args),
                )

            self.assertFalse(ctc.web_session_state_path(session_id, state_dir).exists())

    def test_build_pending_turn_state_preserves_completed_turn_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=ctc.web_tmux_session_name(session_id),
                state_path=Path(tmp) / "state.json",
                state_dir=Path(tmp),
                cwd=Path(tmp),
                prompt="next",
                turn_id="turn_next",
                before_send_offset=10,
                replay_start_offset=10,
            )
            previous = {
                "generation": 3,
                "completed_turns": [{"turn_id": "turn_old", "cost": {"currency": "USD", "turn_usd": 0.1}}],
                "usage_totals": {"input_tokens": 10},
                "cost_totals": {"currency": "USD", "session_usd": 0.1},
            }

            state = ctc.build_pending_turn_state(previous, runtime, transcript=None, wall_time=1000.0)

            self.assertEqual(state["completed_turns"], previous["completed_turns"])
            self.assertEqual(state["usage_totals"], previous["usage_totals"])
            self.assertEqual(state["cost_totals"], previous["cost_totals"])

    def test_prepare_high_level_stream_starts_new_supplied_session_with_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runner = FakeRunner()
            controller = ctc.TmuxController(run=runner)

            runtime = ctc.prepare_high_level_stream(
                controller=controller,
                cwd=Path(tmp),
                prompt="first prompt",
                root=Path(tmp) / "claude",
                state_dir=Path(tmp) / "state",
                session_id=session_id,
            )

            self.assertEqual(runtime.session_id, session_id)
            self.assertIn(
                (
                    [
                        "tmux",
                        "new-session",
                        "-d",
                        "-s",
                        runtime.tmux_session,
                        "-c",
                        str(Path(tmp).resolve()),
                        "claude --session-id 550e8400-e29b-41d4-a716-446655440000 "
                        "--dangerously-skip-permissions -- $'first prompt'",
                    ],
                    {"check": True},
                ),
                runner.calls,
            )

    def test_prepare_high_level_stream_blocks_when_active_turn_is_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "cwd": str(Path(tmp).resolve()),
                        "active_turn": {"claude_state": "working"},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "turn_in_progress"):
                ctc.prepare_high_level_stream(
                    controller=ctc.TmuxController(run=FakeRunner()),
                    cwd=Path(tmp),
                    prompt="second prompt",
                    root=Path(tmp) / "claude",
                    state_dir=state_dir,
                    session_id=session_id,
                )

    def test_prepare_high_level_stream_recovers_stale_timeout_when_tmux_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "cwd": str(Path(tmp).resolve()),
                        "active_turn": {
                            "turn_id": "timed-out",
                            "claude_state": "working",
                            "stream_state": "timeout",
                            "heartbeat_at": "2000-01-01T00:00:00Z",
                        },
                    }
                ),
                encoding="utf-8",
            )

            runtime = ctc.prepare_high_level_stream(
                controller=ctc.TmuxController(run=FakeRunner()),
                cwd=Path(tmp),
                prompt="recover",
                root=Path(tmp) / "claude",
                state_dir=state_dir,
                session_id=session_id,
            )

            self.assertEqual(runtime.session_id, session_id)
            state = ctc.read_bridge_state(state_path)
            self.assertEqual(state["active_turn"]["prompt_preview"], "recover")
            self.assertEqual(state["last_turn"]["turn_id"], "timed-out")

    def test_prepare_high_level_stream_recovers_stale_active_when_tmux_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "cwd": str(Path(tmp).resolve()),
                        "active_turn": {
                            "turn_id": "crashed-active",
                            "claude_state": "working",
                            "stream_state": "active",
                            "heartbeat_at": "2000-01-01T00:00:00Z",
                        },
                    }
                ),
                encoding="utf-8",
            )
            runner = FakeRunner()
            runner.session_exists = True
            runner.capture_text = "Done\nclaude> "

            runtime = ctc.prepare_high_level_stream(
                controller=ctc.TmuxController(run=runner),
                cwd=Path(tmp),
                prompt="after crash",
                root=Path(tmp) / "claude",
                state_dir=state_dir,
                session_id=session_id,
            )

            self.assertEqual(runtime.session_id, session_id)
            state = ctc.read_bridge_state(state_path)
            self.assertEqual(state["active_turn"]["prompt_preview"], "after crash")
            self.assertEqual(state["last_turn"]["turn_id"], "crashed-active")

    def test_prepare_high_level_stream_auto_finalizes_recent_timeout_when_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            root = Path(tmp) / "claude"
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd.resolve()) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            user_line = json_line({"type": "user", "timestamp": "t0", "message": {"content": "old prompt"}})
            transcript.write_text(
                user_line
                + json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t1",
                        "message": {"content": [{"type": "text", "text": "old answer"}]},
                        "model": "claude-sonnet-4-6",
                        "usage": {"input_tokens": 10, "cache_read_input_tokens": 2, "output_tokens": 5},
                    }
                ),
                encoding="utf-8",
            )
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd.resolve()),
                        "transcript": ctc.transcript_file_state(transcript, 0),
                        "active_turn": {
                            "turn_id": "old-turn",
                            "claude_state": "working",
                            "stream_state": "timeout",
                            "heartbeat_at": ctc._utc_timestamp(4102444800.0),
                            "prompt_preview": "old prompt",
                            "before_send_wall_time_utc": "2026-01-01T00:00:00Z",
                            "before_send_transcript": ctc.transcript_file_state(transcript, 0),
                            "anchor_start_offset": 0,
                            "anchor_end_offset": len(user_line.encode("utf-8")),
                            "replay_start_offset": len(user_line.encode("utf-8")),
                            "read_offset": transcript.stat().st_size,
                        },
                        "completed_turns": [],
                    }
                ),
                encoding="utf-8",
            )
            runner = FakeRunner()
            runner.session_exists = True
            runner.capture_text = "old answer\nclaude> "

            runtime = ctc.prepare_high_level_stream(
                controller=ctc.TmuxController(run=runner),
                cwd=cwd,
                prompt="new prompt",
                root=root,
                state_dir=state_dir,
                session_id=session_id,
            )

            state = ctc.read_bridge_state(state_path)
            self.assertEqual(runtime.session_id, session_id)
            self.assertEqual(state["last_turn"]["turn_id"], "old-turn")
            self.assertEqual(state["last_turn"]["stream_state"], "done")
            self.assertEqual(state["last_turn"]["answer"], "old answer")
            self.assertEqual(state["completed_turns"][0]["turn_id"], "old-turn")
            self.assertEqual(state["completed_turns"][0]["usage"]["cache_read_tokens"], 2)
            self.assertEqual(state["active_turn"]["prompt_preview"], "new prompt")
            self.assertTrue(any(call[0][:2] == ["tmux", "load-buffer"] for call in runner.calls))

    def test_prepare_high_level_stream_keeps_recent_timeout_when_transcript_is_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            root = Path(tmp) / "claude"
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            user_line = json_line({"type": "user", "timestamp": "t0", "message": {"content": "old prompt"}})
            transcript.write_text(user_line, encoding="utf-8")
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd.resolve()),
                        "transcript": ctc.transcript_file_state(transcript, 0),
                        "active_turn": {
                            "turn_id": "old-turn",
                            "claude_state": "working",
                            "stream_state": "timeout",
                            "heartbeat_at": ctc._utc_timestamp(4102444800.0),
                            "prompt_preview": "old prompt",
                            "before_send_transcript": ctc.transcript_file_state(transcript, 0),
                            "anchor_start_offset": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            runner = FakeRunner()
            runner.session_exists = True
            runner.capture_text = "claude> "

            with self.assertRaisesRegex(RuntimeError, "turn_in_progress"):
                ctc.prepare_high_level_stream(
                    controller=ctc.TmuxController(run=runner),
                    cwd=cwd,
                    prompt="new prompt",
                    root=root,
                    state_dir=state_dir,
                    session_id=session_id,
                )

    def test_prepare_high_level_stream_does_not_auto_finalize_fresh_active_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            root = Path(tmp) / "claude"
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            user_line = json_line({"type": "user", "timestamp": "t0", "message": {"content": "old prompt"}})
            transcript.write_text(
                user_line
                + json_line({"type": "assistant", "message": {"content": [{"type": "text", "text": "old answer"}]}}),
                encoding="utf-8",
            )
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd.resolve()),
                        "transcript": ctc.transcript_file_state(transcript, 0),
                        "active_turn": {
                            "turn_id": "old-turn",
                            "claude_state": "working",
                            "stream_state": "active",
                            "heartbeat_at": ctc._utc_timestamp(4102444800.0),
                            "prompt_preview": "old prompt",
                            "before_send_transcript": ctc.transcript_file_state(transcript, 0),
                            "anchor_start_offset": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            runner = FakeRunner()
            runner.session_exists = True
            runner.capture_text = "old answer\nclaude> "

            with self.assertRaisesRegex(RuntimeError, "turn_in_progress"):
                ctc.prepare_high_level_stream(
                    controller=ctc.TmuxController(run=runner),
                    cwd=cwd,
                    prompt="new prompt",
                    root=root,
                    state_dir=state_dir,
                    session_id=session_id,
                )

    def test_recovery_finalize_stops_before_next_external_user_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            root = Path(tmp) / "claude"
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            first_user = json_line({"type": "user", "timestamp": "t0", "message": {"content": "old prompt"}})
            transcript.write_text(
                first_user
                + json_line(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "old answer"}]},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    }
                )
                + json_line({"type": "user", "timestamp": "t2", "message": {"content": "later prompt"}})
                + json_line(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "later answer"}]},
                        "usage": {"input_tokens": 10, "output_tokens": 20},
                    }
                ),
                encoding="utf-8",
            )
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd.resolve()),
                        "transcript": ctc.transcript_file_state(transcript, 0),
                        "active_turn": {
                            "turn_id": "old-turn",
                            "claude_state": "working",
                            "stream_state": "timeout",
                            "heartbeat_at": ctc._utc_timestamp(4102444800.0),
                            "prompt_preview": "old prompt",
                            "before_send_transcript": ctc.transcript_file_state(transcript, 0),
                            "anchor_start_offset": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            runner = FakeRunner()
            runner.session_exists = True
            runner.capture_text = "later answer\nclaude> "

            ctc.prepare_high_level_stream(
                controller=ctc.TmuxController(run=runner),
                cwd=cwd,
                prompt="new prompt",
                root=root,
                state_dir=state_dir,
                session_id=session_id,
            )

            state = ctc.read_bridge_state(state_path)
            self.assertEqual(state["last_turn"]["answer"], "old answer")
            self.assertEqual(state["completed_turns"][0]["usage"]["input_tokens"], 1)

    def test_prepare_high_level_stream_breaks_stale_send_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            lock_path = ctc.web_session_lock_path(session_id, state_dir)
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(json.dumps({"pid": 99999999, "started_at": 946684800.0}), encoding="utf-8")
            os.utime(lock_path, (946684800.0, 946684800.0))

            runtime = ctc.prepare_high_level_stream(
                controller=ctc.TmuxController(run=FakeRunner()),
                cwd=Path(tmp),
                prompt="after stale lock",
                root=Path(tmp) / "claude",
                state_dir=state_dir,
                session_id=session_id,
            )

            self.assertEqual(runtime.session_id, session_id)
            self.assertFalse(lock_path.exists())

    def test_prepare_high_level_stream_breaks_stale_malformed_send_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            lock_path = ctc.web_session_lock_path(session_id, state_dir)
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text("{", encoding="utf-8")
            os.utime(lock_path, (946684800.0, 946684800.0))

            runtime = ctc.prepare_high_level_stream(
                controller=ctc.TmuxController(run=FakeRunner()),
                cwd=Path(tmp),
                prompt="after malformed lock",
                root=Path(tmp) / "claude",
                state_dir=state_dir,
                session_id=session_id,
            )

            self.assertEqual(runtime.session_id, session_id)
            self.assertFalse(lock_path.exists())

    def test_prepare_high_level_stream_breaks_stale_schema_malformed_send_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            lock_path = ctc.web_session_lock_path(session_id, state_dir)
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text("{}", encoding="utf-8")
            os.utime(lock_path, (946684800.0, 946684800.0))

            runtime = ctc.prepare_high_level_stream(
                controller=ctc.TmuxController(run=FakeRunner()),
                cwd=Path(tmp),
                prompt="after schema malformed lock",
                root=Path(tmp) / "claude",
                state_dir=state_dir,
                session_id=session_id,
            )

            self.assertEqual(runtime.session_id, session_id)
            self.assertFalse(lock_path.exists())

    def test_prepare_high_level_stream_resumes_inactive_existing_session_with_inline_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "cwd": str(Path(tmp).resolve()),
                        "last_turn": {"turn_id": "old", "claude_state": "ready"},
                    }
                ),
                encoding="utf-8",
            )
            runner = FakeRunner()
            controller = ctc.TmuxController(run=runner)

            runtime = ctc.prepare_high_level_stream(
                controller=controller,
                cwd=Path(tmp),
                prompt="resume please",
                root=Path(tmp) / "claude",
                state_dir=state_dir,
                session_id=session_id,
            )

            self.assertIn(
                (
                    [
                        "tmux",
                        "new-session",
                        "-d",
                        "-s",
                        runtime.tmux_session,
                        "-c",
                        str(Path(tmp).resolve()),
                        "claude --resume 550e8400-e29b-41d4-a716-446655440000 --dangerously-skip-permissions -- $'resume please'",
                    ],
                    {"check": True},
                ),
                runner.calls,
            )
            self.assertFalse(any(call[0][:2] == ["tmux", "capture-pane"] for call in runner.calls))
            self.assertFalse(any(call[0][:2] == ["tmux", "load-buffer"] for call in runner.calls))
            self.assertFalse(any(call[0][:2] == ["tmux", "paste-buffer"] for call in runner.calls))
            self.assertFalse(any(call[0][:2] == ["tmux", "send-keys"] for call in runner.calls))

    def test_prepare_high_level_stream_does_not_wait_for_ready_after_new_session_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "cwd": str(Path(tmp).resolve()),
                        "last_turn": {"turn_id": "old", "claude_state": "ready"},
                    }
                ),
                encoding="utf-8",
            )
            runner = FakeRunner()
            controller = ctc.TmuxController(run=runner)

            runtime = ctc.prepare_high_level_stream(
                controller=controller,
                cwd=Path(tmp),
                prompt="resume please",
                root=Path(tmp) / "claude",
                state_dir=state_dir,
                session_id=session_id,
            )

            self.assertEqual(runtime.session_id, session_id)
            self.assertFalse(any(call[0][:2] == ["tmux", "load-buffer"] for call in runner.calls))
            state = ctc.read_bridge_state(state_path)
            self.assertEqual(state["active_turn"]["prompt_preview"], "resume please")

    def test_prepare_high_level_stream_reuses_active_tmux_and_sends_two_enters_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runner = FakeRunner()
            runner.session_exists = True
            runner.capture_text = "Done\nclaude> "
            controller = ctc.TmuxController(run=runner)

            runtime = ctc.prepare_high_level_stream(
                controller=controller,
                cwd=Path(tmp),
                prompt="next turn",
                root=Path(tmp) / "claude",
                state_dir=Path(tmp) / "state",
                session_id=session_id,
            )

            self.assertEqual(runtime.tmux_session, "ctc-csess-550e8400-e29b-41d4-a716-446655440000")
            self.assertIn(
                (
                    ["tmux", "load-buffer", "-b", "claude-tmux-control", "-"],
                    {"input": "next turn", "text": True, "check": True},
                ),
                runner.calls,
            )
            self.assertEqual(
                [call for call in runner.calls if call[0] == ["tmux", "send-keys", "-t", runtime.tmux_session, "Enter"]],
                [
                    (["tmux", "send-keys", "-t", runtime.tmux_session, "Enter"], {"check": True}),
                    (["tmux", "send-keys", "-t", runtime.tmux_session, "Enter"], {"check": True}),
                ],
            )

    def test_prepare_high_level_stream_can_send_one_enter_for_active_tmux(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runner = FakeRunner()
            runner.session_exists = True
            runner.capture_text = "Done\nclaude> "
            controller = ctc.TmuxController(run=runner)

            runtime = ctc.prepare_high_level_stream(
                controller=controller,
                cwd=Path(tmp),
                prompt="next turn",
                root=Path(tmp) / "claude",
                state_dir=Path(tmp) / "state",
                session_id=session_id,
                submit_enters=1,
            )

            self.assertEqual(
                [call for call in runner.calls if call[0] == ["tmux", "send-keys", "-t", runtime.tmux_session, "Enter"]],
                [(["tmux", "send-keys", "-t", runtime.tmux_session, "Enter"], {"check": True})],
            )

    def test_prepare_high_level_stream_refuses_active_tmux_when_ready_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runner = FakeRunner()
            runner.session_exists = True
            runner.capture_text = "still rendering\n"

            with self.assertRaisesRegex(RuntimeError, "turn_in_progress"):
                ctc.prepare_high_level_stream(
                    controller=ctc.TmuxController(run=runner),
                    cwd=Path(tmp),
                    prompt="unsafe interleave",
                    root=Path(tmp) / "claude",
                    state_dir=Path(tmp) / "state",
                    session_id=session_id,
                )

            self.assertNotIn(
                (
                    ["tmux", "load-buffer", "-b", "claude-tmux-control", "-"],
                    {"input": "unsafe interleave", "text": True, "check": True},
                ),
                runner.calls,
            )

    def test_prepare_high_level_stream_clears_active_turn_when_tmux_start_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"

            def failing_run(args, **kwargs):
                if args[:3] == ["tmux", "has-session", "-t"]:
                    return subprocess.CompletedProcess(args, 1, "", "missing")
                if args[:3] == ["tmux", "new-session", "-d"]:
                    raise subprocess.CalledProcessError(1, args)
                return subprocess.CompletedProcess(args, 0, "", "")

            state_dir = Path(tmp) / "state"
            with self.assertRaises(subprocess.CalledProcessError):
                ctc.prepare_high_level_stream(
                    controller=ctc.TmuxController(run=failing_run),
                    cwd=Path(tmp),
                    prompt="start fail",
                    root=Path(tmp) / "claude",
                    state_dir=state_dir,
                    session_id=session_id,
                )

            state = ctc.read_bridge_state(ctc.web_session_state_path(session_id, state_dir))
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["last_turn"]["stream_state"], "failed")

    def test_prepare_high_level_stream_marks_interrupted_when_send_is_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"

            def interrupted_run(args, **kwargs):
                if args[:3] == ["tmux", "has-session", "-t"]:
                    return subprocess.CompletedProcess(args, 1, "", "missing")
                if args[:3] == ["tmux", "new-session", "-d"]:
                    raise KeyboardInterrupt
                return subprocess.CompletedProcess(args, 0, "", "")

            state_dir = Path(tmp) / "state"
            with self.assertRaises(KeyboardInterrupt):
                ctc.prepare_high_level_stream(
                    controller=ctc.TmuxController(run=interrupted_run),
                    cwd=Path(tmp),
                    prompt="start interrupted",
                    root=Path(tmp) / "claude",
                    state_dir=state_dir,
                    session_id=session_id,
                )

            state = ctc.read_bridge_state(ctc.web_session_state_path(session_id, state_dir))
            self.assertEqual(state["active_turn"]["stream_state"], "interrupted")
            self.assertEqual(state["active_turn"]["claude_state"], "working")

    def test_prepare_high_level_stream_recovers_fresh_interrupted_when_tmux_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "cwd": str(Path(tmp).resolve()),
                        "active_turn": {
                            "turn_id": "interrupted-start",
                            "claude_state": "working",
                            "stream_state": "interrupted",
                            "heartbeat_at": ctc._utc_timestamp(4102444800.0),
                        },
                    }
                ),
                encoding="utf-8",
            )

            runtime = ctc.prepare_high_level_stream(
                controller=ctc.TmuxController(run=FakeRunner()),
                cwd=Path(tmp),
                prompt="retry after interrupted start",
                root=Path(tmp) / "claude",
                state_dir=state_dir,
                session_id=session_id,
            )

            state = ctc.read_bridge_state(state_path)
            self.assertEqual(runtime.session_id, session_id)
            self.assertEqual(state["last_turn"]["turn_id"], "interrupted-start")
            self.assertEqual(state["active_turn"]["prompt_preview"], "retry after interrupted start")

    def test_late_timeout_does_not_overwrite_new_active_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = Path(tmp) / "state" / "sessions" / f"{session_id}.json"
            old_runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=ctc.web_tmux_session_name(session_id),
                state_path=state_path,
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="old prompt",
                turn_id="old-turn",
                before_send_offset=0,
                replay_start_offset=0,
            )
            ctc._write_high_level_state(
                state_path,
                {
                    "schema_version": 1,
                    "session_id": session_id,
                    "active_turn": {
                        "turn_id": "new-turn",
                        "claude_state": "starting",
                        "stream_state": "active",
                        "prompt_preview": "new prompt",
                    },
                },
            )

            ctc._mark_turn_timeout(old_runtime)

            state = ctc.read_bridge_state(state_path)
            self.assertEqual(state["active_turn"]["turn_id"], "new-turn")
            self.assertEqual(state["active_turn"]["claude_state"], "starting")
            self.assertEqual(state["active_turn"]["stream_state"], "active")

    def test_late_timeout_cancel_does_not_overwrite_new_active_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = Path(tmp) / "state" / "sessions" / f"{session_id}.json"
            old_runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=ctc.web_tmux_session_name(session_id),
                state_path=state_path,
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="old prompt",
                turn_id="old-turn",
                before_send_offset=0,
                replay_start_offset=0,
            )
            ctc._write_high_level_state(
                state_path,
                {
                    "schema_version": 1,
                    "session_id": session_id,
                    "active_turn": {
                        "turn_id": "new-turn",
                        "claude_state": "starting",
                        "stream_state": "active",
                        "prompt_preview": "new prompt",
                    },
                },
            )

            ctc._mark_turn_timeout_cancelled(old_runtime)

            state = ctc.read_bridge_state(state_path)
            self.assertEqual(state["active_turn"]["turn_id"], "new-turn")
            self.assertEqual(state["active_turn"]["claude_state"], "starting")
            self.assertEqual(state["active_turn"]["stream_state"], "active")

    def test_stale_state_write_does_not_resurrect_completed_active_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = Path(tmp) / "state" / "sessions" / f"{session_id}.json"
            active_state = {
                "schema_version": 1,
                "session_id": session_id,
                "generation": 0,
                "active_turn": {
                    "turn_id": "turn-1",
                    "claude_state": "working",
                    "stream_state": "active",
                    "prompt_preview": "hello",
                },
                "completed_turns": [],
            }
            ctc._write_high_level_state(state_path, active_state)
            stale_snapshot = ctc.read_bridge_state(state_path)

            completed_state = dict(stale_snapshot)
            completed_state["last_turn"] = dict(stale_snapshot["active_turn"])
            completed_state["last_turn"]["completed_offset"] = 120
            completed_state["active_turn"] = None
            completed_state["completed_turns"] = [{"turn_id": "turn-1", "completed_offset": 120}]
            ctc._write_high_level_state(state_path, completed_state)

            stale_snapshot["active_turn"]["read_offset"] = 80
            with self.assertRaisesRegex(ctc.StateGenerationConflict, "state_generation_conflict"):
                ctc._write_high_level_state(state_path, stale_snapshot)

            state = ctc.read_bridge_state(state_path)
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["completed_turns"], [{"turn_id": "turn-1", "completed_offset": 120}])

    def test_stream_interrupt_marks_turn_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text("", encoding="utf-8")
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=ctc.web_tmux_session_name(session_id),
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="old prompt",
                turn_id="old-turn",
                before_send_offset=0,
                replay_start_offset=0,
            )
            ctc._write_high_level_state(
                runtime.state_path,
                ctc.build_pending_turn_state({}, runtime, transcript, wall_time=1000.0),
            )
            args = ctc.parse_args(["stream", "--cwd", str(Path(tmp)), "--session-id", session_id, "old prompt"])

            with patch.object(ctc, "stream_high_level_transcript_until_done", side_effect=KeyboardInterrupt):
                result = ctc._stream_prepared_high_level_turn(args, Mock(), runtime, transcript, lambda _line: None)

            state = ctc.read_bridge_state(runtime.state_path)
            self.assertEqual(result["exit_code"], 130)
            self.assertEqual(result["status"].state, "interrupted")
            self.assertEqual(state["active_turn"]["turn_id"], "old-turn")
            self.assertEqual(state["active_turn"]["stream_state"], "interrupted")

    def test_stream_interrupt_while_waiting_for_transcript_marks_turn_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            args = ctc.parse_args(["stream", "--cwd", str(Path(tmp)), "--session-id", session_id, "old prompt"])
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=ctc.web_tmux_session_name(session_id),
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="old prompt",
                turn_id="old-turn",
                before_send_offset=0,
                replay_start_offset=0,
            )
            ctc._write_high_level_state(
                runtime.state_path,
                ctc.build_pending_turn_state({}, runtime, transcript=None, wall_time=1000.0),
            )

            with patch.object(ctc, "prepare_high_level_stream", return_value=runtime), patch.object(
                ctc, "wait_for_high_level_transcript", side_effect=KeyboardInterrupt
            ):
                result = ctc.run_high_level_turn(args, Mock(), lambda _line: None)

            state = ctc.read_bridge_state(runtime.state_path)
            self.assertEqual(result["exit_code"], 130)
            self.assertEqual(state["active_turn"]["turn_id"], "old-turn")
            self.assertEqual(state["active_turn"]["stream_state"], "interrupted")


class HighLevelSessionInfoTest(unittest.TestCase):
    def test_build_session_info_reads_state_tmux_and_transcript_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / "session.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json_line({"sessionId": session_id, "type": "user", "message": {"content": "hello"}}),
                encoding="utf-8",
            )
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd),
                        "transcript": {"path": str(transcript)},
                        "active_turn": {"turn_id": "turn_active"},
                        "last_turn": {"turn_id": "turn_last"},
                        "completed_turns": [{"turn_id": "turn_last"}],
                        "usage_totals": {"input_tokens": 10},
                        "cost_totals": {"currency": "USD", "session_usd": 0.01},
                    }
                ),
                encoding="utf-8",
            )
            os.utime(state_path, (2000.0, 2000.0))
            controller = Mock()
            controller.session_exists.return_value = True

            payload = ctc.build_session_info_payload(session_id, state_dir, root, controller, now=lambda: 2500.0)

            self.assertEqual(payload["event"], "info")
            self.assertEqual(payload["session_id"], session_id)
            self.assertEqual(payload["tmux_session"], ctc.web_tmux_session_name(session_id))
            self.assertTrue(payload["tmux_active"])
            self.assertEqual(payload["state_mtime"], 2000.0)
            self.assertEqual(payload["idle_seconds"], 500.0)
            self.assertEqual(payload["transcript_path"], str(transcript))
            self.assertEqual(payload["claude_transcript_session_id"], session_id)
            self.assertEqual(payload["active_turn"]["turn_id"], "turn_active")
            self.assertEqual(payload["last_turn"]["turn_id"], "turn_last")
            self.assertEqual(payload["completed_turn_count"], 1)
            self.assertEqual(payload["usage_totals"]["input_tokens"], 10)
            self.assertEqual(payload["cost_totals"]["session_usd"], 0.01)

    def test_build_session_info_reports_inactive_when_tmux_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps({"schema_version": 1, "session_id": session_id, "cwd": str(Path(tmp))}),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = False

            payload = ctc.build_session_info_payload(session_id, state_dir, root, controller)

            self.assertFalse(payload["tmux_active"])
            self.assertTrue(payload["state_exists"])

    def test_build_session_info_omits_state_timing_when_state_file_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            controller = Mock()
            controller.session_exists.return_value = True

            payload = ctc.build_session_info_payload(session_id, state_dir, root, controller, now=lambda: 2500.0)

            self.assertTrue(payload["tmux_active"])
            self.assertFalse(payload["state_exists"])
            self.assertNotIn("state_mtime", payload)
            self.assertNotIn("idle_seconds", payload)

    def test_build_session_info_includes_recovery_actions_for_timeout_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "cwd": str(Path(tmp)),
                        "active_turn": {
                            "turn_id": "turn_timeout",
                            "stream_state": "timeout",
                            "claude_state": "working",
                        },
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = True

            payload = ctc.build_session_info_payload(session_id, state_dir, root, controller)

            self.assertEqual(
                payload["active_turn_recovery"],
                {
                    "state": "timeout",
                    "can_attach": True,
                    "can_cancel": True,
                    "can_send_new_prompt": False,
                    "recommended_action": "attach_or_retry",
                    "description": "completion is unconfirmed; attach or retry with the same session before sending a new prompt",
                },
            )

    def test_build_session_info_includes_recovery_actions_for_failed_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "cwd": str(Path(tmp)),
                        "active_turn": {
                            "turn_id": "turn_failed",
                            "stream_state": "failed",
                            "claude_state": "unknown",
                        },
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = True

            payload = ctc.build_session_info_payload(session_id, state_dir, root, controller)

            self.assertEqual(payload["active_turn_recovery"]["recommended_action"], "inspect_or_kill")
            self.assertFalse(payload["active_turn_recovery"]["can_attach"])
            self.assertFalse(payload["active_turn_recovery"]["can_send_new_prompt"])

    def test_build_stats_payload_reads_model_usage_and_context_from_transcript(self):
        transcript = FIXTURES_DIR / "transcripts" / "basic_tool_flow.jsonl"

        payload = ctc.build_stats_payload(transcript, session_id="550e8400-e29b-41d4-a716-446655440000")

        self.assertEqual(payload["event"], "stats")
        self.assertEqual(payload["session_id"], "550e8400-e29b-41d4-a716-446655440000")
        self.assertEqual(payload["transcript_path"], str(transcript))
        self.assertEqual(payload["model"], "claude-sonnet-4-6")
        self.assertEqual(
            payload["usage"],
            {
                "input_tokens": 10,
                "cache_read_tokens": 20,
                "cache_write_tokens": 30,
                "output_tokens": 40,
            },
        )
        self.assertEqual(payload["event_count"], 5)
        self.assertEqual(payload["read_offset"], transcript.stat().st_size)

    def test_run_stats_with_explicit_transcript_prints_json(self):
        transcript = FIXTURES_DIR / "transcripts" / "basic_tool_flow.jsonl"
        args = ctc.parse_args(["stats", "--transcript", str(transcript), "--json"])
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = ctc._run_command(args, Mock())

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["event"], "stats")
        self.assertEqual(payload["model"], "claude-sonnet-4-6")

    def test_run_stats_with_session_id_resolves_state_transcript_and_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json_line({"sessionId": session_id, "type": "user", "message": {"content": "stats context"}})
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "done"}]},
                        "usage": {"input_tokens": 11, "cache_read_input_tokens": 22, "output_tokens": 33},
                        "context_window": {"remaining": 12345, "used": "678"},
                        "model": "claude-sonnet-4-6",
                    }
                ),
                encoding="utf-8",
            )
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "cwd": str(cwd),
                        "transcript": ctc.transcript_file_state(transcript, transcript.stat().st_size),
                    }
                ),
                encoding="utf-8",
            )
            args = ctc.parse_args(["stats", session_id, "--state-dir", str(state_dir), "--root", str(root), "--json"])
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = ctc._run_command(args, Mock())

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["session_id"], session_id)
            self.assertEqual(payload["transcript_path"], str(transcript))
            self.assertEqual(payload["usage"]["input_tokens"], 11)
            self.assertEqual(payload["context"], {"remaining": 12345, "used": "678"})

    def test_build_session_list_includes_state_and_tmux_only_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            state_session = "550e8400-e29b-41d4-a716-446655440000"
            tmux_only = "660e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(state_session, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps({"schema_version": 1, "session_id": state_session, "cwd": str(Path(tmp))}),
                encoding="utf-8",
            )
            os.utime(state_path, (2000.0, 2000.0))
            (state_path.parent / "not-a-uuid.json").write_text("{}", encoding="utf-8")
            controller = Mock()
            controller.list_sessions.return_value = [
                ctc.web_tmux_session_name(tmux_only),
                "work",
                "ctc-csess-not-a-uuid",
            ]
            controller.session_exists.side_effect = lambda name: name == ctc.web_tmux_session_name(tmux_only)

            payload = ctc.build_session_list_payload(state_dir, root, controller, now=lambda: 2600.0)

            self.assertEqual(payload["event"], "list")
            self.assertEqual(payload["count"], 2)
            self.assertEqual(
                sorted(item["session_id"] for item in payload["sessions"]),
                [state_session, tmux_only],
            )
            by_id = {item["session_id"]: item for item in payload["sessions"]}
            self.assertFalse(by_id[state_session]["tmux_active"])
            self.assertEqual(by_id[state_session]["state_mtime"], 2000.0)
            self.assertEqual(by_id[state_session]["idle_seconds"], 600.0)
            self.assertTrue(by_id[tmux_only]["tmux_active"])
            self.assertNotIn("state_mtime", by_id[tmux_only])
            self.assertNotIn("idle_seconds", by_id[tmux_only])


class HighLevelAskTest(unittest.TestCase):
    def test_run_high_level_ask_prints_final_answer_and_metrics(self):
        args = ctc.parse_args(["ask", "--cwd", "/tmp/project", "hello"])
        result = {
            "exit_code": 0,
            "session_id": "550e8400-e29b-41d4-a716-446655440000",
            "turn_id": "turn_test",
            "status": ctc.ScreenStatus("ready", "done"),
            "events": [{"event": "assistant_text"}, {"event": "done"}, {"event": "metrics"}],
            "done": {"event": "done", "answer": "final answer"},
            "metrics": {"event": "metrics", "elapsed_ms": 1000},
        }
        stdout = io.StringIO()

        with patch.object(ctc, "run_high_level_turn", return_value=result), redirect_stdout(stdout):
            exit_code = ctc._run_high_level_ask(args, Mock())

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["event"], "ask_result")
        self.assertEqual(payload["session_id"], result["session_id"])
        self.assertEqual(payload["turn_id"], "turn_test")
        self.assertEqual(payload["answer"], "final answer")
        self.assertEqual(payload["metrics"]["elapsed_ms"], 1000)
        self.assertEqual(payload["events_seen"], 3)

    def test_run_high_level_ask_non_ready_does_not_emit_answer(self):
        args = ctc.parse_args(["ask", "--cwd", "/tmp/project", "hello"])
        result = {
            "exit_code": 3,
            "session_id": "550e8400-e29b-41d4-a716-446655440000",
            "turn_id": "turn_test",
            "status": ctc.ScreenStatus("timeout", "not ready"),
            "events": [{"event": "timeout"}],
        }
        stdout = io.StringIO()

        with patch.object(ctc, "run_high_level_turn", return_value=result), redirect_stdout(stdout):
            exit_code = ctc._run_high_level_ask(args, Mock())

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 3)
        self.assertEqual(payload["event"], "ask_result")
        self.assertEqual(payload["state"], "timeout")
        self.assertEqual(payload["reason"], "not ready")
        self.assertNotIn("answer", payload)
        self.assertEqual(payload["events_seen"], 1)


class HighLevelCancelTest(unittest.TestCase):
    def test_cancel_active_turn_resets_state_after_tmux_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "active_turn": {"turn_id": "turn_active", "cancel_count": 1},
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = True
            args = ctc.parse_args(["cancel", session_id, "--state-dir", str(state_dir)])

            result = ctc.run_high_level_cancel(args, controller)

            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["event"], "cancel")
            self.assertEqual(result["active_turn_id"], "turn_active")
            self.assertTrue(result["reset_applied"])
            self.assertEqual(result["moved_turn_id"], "turn_active")
            self.assertEqual(result["state_after"], {"active_turn": None})
            controller.send_escape.assert_called_once_with(ctc.web_tmux_session_name(session_id))
            controller.kill_session.assert_called_once_with(ctc.web_tmux_session_name(session_id))
            state = ctc.read_bridge_state(state_path)
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["last_turn"]["turn_id"], "turn_active")
            self.assertEqual(state["last_turn"]["cancel_count"], 1)

    def test_cancel_reset_moves_active_turn_to_last_turn_after_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            active_turn = {"turn_id": "turn_active", "claude_state": "working", "stream_state": "active"}
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "active_turn": active_turn,
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = True
            args = ctc.parse_args(["cancel", session_id, "--reset", "--state-dir", str(state_dir)])

            result = ctc.run_high_level_cancel(args, controller)

            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(result["reset_requested"])
            self.assertTrue(result["reset_applied"])
            self.assertEqual(result["moved_turn_id"], "turn_active")
            self.assertEqual(result["state_after"], {"active_turn": None})
            controller.send_escape.assert_called_once_with(ctc.web_tmux_session_name(session_id))
            controller.kill_session.assert_called_once_with(ctc.web_tmux_session_name(session_id))
            state = ctc.read_bridge_state(state_path)
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["last_turn"], active_turn)

    def test_cancel_reset_is_noop_when_active_turn_is_already_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "active_turn": None,
                        "last_turn": {"turn_id": "done"},
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = True
            args = ctc.parse_args(["cancel", "--session-id", session_id, "--reset", "--state-dir", str(state_dir)])

            result = ctc.run_high_level_cancel(args, controller)

            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(result["reset_requested"])
            self.assertFalse(result["reset_applied"])
            self.assertIsNone(result["moved_turn_id"])
            self.assertEqual(result["state_after"], {"active_turn": None})
            controller.send_escape.assert_called_once_with(ctc.web_tmux_session_name(session_id))
            controller.kill_session.assert_called_once_with(ctc.web_tmux_session_name(session_id))
            state = ctc.read_bridge_state(state_path)
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["last_turn"], {"turn_id": "done"})

    def test_cancel_cleans_state_when_tmux_session_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            active_turn = {"turn_id": "turn_missing", "claude_state": "working"}
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "active_turn": active_turn,
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = False
            args = ctc.parse_args(["cancel", session_id, "--state-dir", str(state_dir)])

            result = ctc.run_high_level_cancel(args, controller)

            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(result["tmux_session_missing"])
            self.assertTrue(result["reset_applied"])
            self.assertEqual(result["moved_turn_id"], "turn_missing")
            controller.send_escape.assert_not_called()
            controller.kill_session.assert_not_called()
            state = ctc.read_bridge_state(state_path)
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["last_turn"], active_turn)

    def test_cancel_does_not_mutate_state_when_escape_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            original = {
                "schema_version": 1,
                "session_id": session_id,
                "tmux_session": ctc.web_tmux_session_name(session_id),
                "active_turn": {"turn_id": "turn_active", "claude_state": "working"},
            }
            state_path.write_text(json.dumps(original), encoding="utf-8")
            controller = Mock()
            controller.session_exists.return_value = True
            controller.send_escape.side_effect = subprocess.CalledProcessError(1, ["tmux", "send-keys"])
            args = ctc.parse_args(["cancel", session_id, "--state-dir", str(state_dir)])

            result = ctc.run_high_level_cancel(args, controller)

            self.assertEqual(result["exit_code"], 5)
            self.assertEqual(result["error"], "cancel_failed")
            controller.kill_session.assert_not_called()
            state = ctc.read_bridge_state(state_path)
            self.assertEqual(state, original)

    def test_cancel_does_not_mutate_state_when_kill_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            original = {
                "schema_version": 1,
                "session_id": session_id,
                "tmux_session": ctc.web_tmux_session_name(session_id),
                "active_turn": {"turn_id": "turn_active", "claude_state": "working"},
            }
            state_path.write_text(json.dumps(original), encoding="utf-8")
            controller = Mock()
            controller.session_exists.return_value = True
            controller.kill_session.side_effect = subprocess.CalledProcessError(1, ["tmux", "kill-session"])
            args = ctc.parse_args(["cancel", session_id, "--state-dir", str(state_dir)])

            result = ctc.run_high_level_cancel(args, controller)

            self.assertEqual(result["exit_code"], 5)
            self.assertEqual(result["error"], "cancel_failed")
            controller.send_escape.assert_called_once_with(ctc.web_tmux_session_name(session_id))
            controller.kill_session.assert_called_once_with(ctc.web_tmux_session_name(session_id))
            state = ctc.read_bridge_state(state_path)
            self.assertEqual(state, original)

    def test_cancel_without_turn_id_does_not_clear_new_active_turn_after_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "active_turn": {"claude_state": "working"},
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = True

            def send_escape(_session: str) -> None:
                state = ctc.read_bridge_state(state_path)
                state["active_turn"] = {"turn_id": "new_turn", "claude_state": "working"}
                ctc._write_high_level_state(state_path, state)

            controller.send_escape.side_effect = send_escape
            args = ctc.parse_args(["cancel", session_id, "--state-dir", str(state_dir)])

            result = ctc.run_high_level_cancel(args, controller)

            self.assertEqual(result["exit_code"], 0)
            self.assertFalse(result["reset_applied"])
            self.assertIsNone(result["moved_turn_id"])
            controller.kill_session.assert_called_once_with(ctc.web_tmux_session_name(session_id))
            state = ctc.read_bridge_state(state_path)
            self.assertEqual(state["active_turn"], {"turn_id": "new_turn", "claude_state": "working"})
            self.assertNotIn("last_turn", state)

    def test_cancel_reset_is_idempotent_when_called_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "active_turn": {"turn_id": "turn_active"},
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = True
            args = ctc.parse_args(["cancel", session_id, "--reset", "--state-dir", str(state_dir)])

            first = ctc.run_high_level_cancel(args, controller)
            second = ctc.run_high_level_cancel(args, controller)

            self.assertEqual(first["exit_code"], 0)
            self.assertTrue(first["reset_applied"])
            self.assertEqual(second["exit_code"], 0)
            self.assertFalse(second["reset_applied"])
            self.assertEqual(controller.kill_session.call_count, 2)
            state = ctc.read_bridge_state(state_path)
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["last_turn"]["turn_id"], "turn_active")

    def test_cancel_without_state_still_uses_default_web_tmux_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            controller = Mock()
            controller.session_exists.return_value = True
            args = ctc.parse_args(["cancel", "--session-id", session_id, "--state-dir", str(state_dir)])

            result = ctc.run_high_level_cancel(args, controller)

            self.assertEqual(result["exit_code"], 0)
            self.assertFalse(result["state_exists"])
            self.assertFalse(result["active_turn_present"])
            controller.send_escape.assert_called_once_with(ctc.web_tmux_session_name(session_id))
            controller.kill_session.assert_called_once_with(ctc.web_tmux_session_name(session_id))

    def test_cancel_missing_tmux_session_is_state_cleanup_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            controller = Mock()
            controller.session_exists.return_value = False
            args = ctc.parse_args(["cancel", session_id, "--state-dir", str(Path(tmp) / "state")])

            result = ctc.run_high_level_cancel(args, controller)

            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["event"], "cancel")
            self.assertTrue(result["tmux_session_missing"])
            self.assertFalse(result["reset_applied"])
            self.assertFalse(controller.send_escape.called)
            controller.kill_session.assert_not_called()

    def test_cancel_reports_send_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            controller = Mock()
            controller.session_exists.return_value = True
            controller.send_escape.side_effect = subprocess.CalledProcessError(1, ["tmux", "send-keys"])
            args = ctc.parse_args(["cancel", session_id, "--state-dir", str(Path(tmp) / "state")])

            result = ctc.run_high_level_cancel(args, controller)

            self.assertEqual(result["exit_code"], 5)
            self.assertEqual(result["error"], "cancel_failed")
            controller.kill_session.assert_not_called()

    def test_cancel_does_not_resurrect_active_turn_completed_during_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "active_turn": {"turn_id": "turn_active", "cancel_count": 0},
                    }
                ),
                encoding="utf-8",
            )

            def complete_during_escape(_session):
                state_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "session_id": session_id,
                            "tmux_session": ctc.web_tmux_session_name(session_id),
                            "active_turn": None,
                            "completed_turns": [{"turn_id": "turn_active", "answer": None}],
                        }
                    ),
                    encoding="utf-8",
                )

            controller = Mock()
            controller.session_exists.return_value = True
            controller.send_escape.side_effect = complete_during_escape
            args = ctc.parse_args(["cancel", session_id, "--state-dir", str(state_dir)])

            result = ctc.run_high_level_cancel(args, controller)

            self.assertEqual(result["exit_code"], 0)
            state = ctc.read_bridge_state(state_path)
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["completed_turns"][0]["turn_id"], "turn_active")

    def test_cancel_allows_next_high_level_stream_through_resume_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(Path(tmp).resolve()),
                        "active_turn": {"turn_id": "turn_cancel", "claude_state": "working"},
                    }
                ),
                encoding="utf-8",
            )
            cancel_controller = Mock()
            cancel_controller.session_exists.return_value = True
            cancel_args = ctc.parse_args(["cancel", session_id, "--state-dir", str(state_dir)])

            cancel_result = ctc.run_high_level_cancel(cancel_args, cancel_controller)

            self.assertEqual(cancel_result["exit_code"], 0)
            self.assertTrue(cancel_result["reset_applied"])
            runner = FakeRunner()
            stream_controller = ctc.TmuxController(run=runner)
            runtime = ctc.prepare_high_level_stream(
                controller=stream_controller,
                cwd=Path(tmp),
                prompt="next prompt",
                root=Path(tmp) / "claude",
                state_dir=state_dir,
                session_id=session_id,
            )

            self.assertEqual(runtime.session_id, session_id)
            self.assertTrue(
                any(
                    "--resume " + session_id in call[0][-1]
                    for call in runner.calls
                    if call[0][:2] == ["tmux", "new-session"]
                )
            )


class HighLevelAttachTest(unittest.TestCase):
    def test_prepare_high_level_attach_reuses_active_turn_without_sending_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / "session.jsonl"
            transcript.parent.mkdir(parents=True)
            user_line = json_line({"sessionId": session_id, "type": "user", "message": {"content": "question"}})
            transcript.write_text(
                user_line
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "final answer"}]},
                    }
                ),
                encoding="utf-8",
            )
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd),
                        "transcript": {"path": str(transcript)},
                        "active_turn": {
                            "turn_id": "turn_active",
                            "stream_state": "active",
                            "claude_state": "working",
                            "prompt_preview": "question",
                            "before_send_wall_time_utc": "2026-05-18T00:00:00Z",
                            "before_send_transcript": {"path": str(transcript)},
                            "anchor_start_offset": 0,
                            "anchor_end_offset": len(user_line),
                            "replay_start_offset": 0,
                            "read_offset": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = True

            runtime, attached_transcript = ctc.prepare_high_level_attach(controller, session_id, state_dir, root)

            self.assertEqual(runtime.turn_id, "turn_active")
            self.assertEqual(runtime.prompt, "question")
            self.assertEqual(runtime.before_send_offset, 0)
            self.assertEqual(attached_transcript, transcript)
            self.assertFalse(controller.send_prompt.called)

    def test_prepare_high_level_attach_accepts_interrupted_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / "session.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json_line({"sessionId": session_id, "type": "user", "message": {"content": "question"}}),
                encoding="utf-8",
            )
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd),
                        "transcript": {"path": str(transcript)},
                        "active_turn": {
                            "turn_id": "turn_interrupted",
                            "stream_state": "interrupted",
                            "claude_state": "working",
                            "prompt_preview": "question",
                            "before_send_transcript": {"path": str(transcript)},
                        },
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = True

            runtime, attached_transcript = ctc.prepare_high_level_attach(controller, session_id, state_dir, root)

            self.assertEqual(runtime.turn_id, "turn_interrupted")
            self.assertEqual(attached_transcript, transcript)

    def test_run_high_level_turn_attach_streams_existing_turn_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / "session.jsonl"
            transcript.parent.mkdir(parents=True)
            user_line = json_line({"sessionId": session_id, "type": "user", "message": {"content": "question"}})
            transcript.write_text(
                user_line
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "final answer"}]},
                        "model": "claude-sonnet-4-6",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    }
                ),
                encoding="utf-8",
            )
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd),
                        "transcript": {"path": str(transcript)},
                        "active_turn": {
                            "turn_id": "turn_active",
                            "stream_state": "active",
                            "claude_state": "working",
                            "prompt_preview": "question",
                            "before_send_wall_time_utc": "2026-05-18T00:00:00Z",
                            "before_send_transcript": {"path": str(transcript)},
                            "anchor_start_offset": 0,
                            "anchor_end_offset": len(user_line),
                            "replay_start_offset": 0,
                            "read_offset": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = True
            controller.capture_screen.return_value = "Done\nclaude> "
            args = ctc.parse_args(
                [
                    "stream",
                    "--attach",
                    "--session-id",
                    session_id,
                    "--state-dir",
                    str(state_dir),
                    "--root",
                    str(root),
                    "--interval",
                    "0",
                    "--idle",
                    "0",
                    "--timeout",
                    "1",
                ]
            )
            writes = []

            result = ctc.run_high_level_turn(args, controller, writes.append)

            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["turn_id"], "turn_active")
            self.assertEqual(result["done"]["answer"], "final answer")
            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual([payload["event"] for payload in payloads], ["user", "assistant_text", "done", "metrics"])
            self.assertTrue(all(payload["turn_id"] == "turn_active" for payload in payloads))
            self.assertFalse(controller.send_prompt.called)

    def test_prepare_high_level_attach_rejects_missing_active_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"session_id": session_id}), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "no_active_turn"):
                ctc.prepare_high_level_attach(Mock(), session_id, state_dir, Path(tmp) / "claude")

    def test_prepare_high_level_attach_rejects_missing_tmux_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "cwd": str(Path(tmp)),
                        "active_turn": {"turn_id": "turn", "stream_state": "active"},
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = False

            with self.assertRaisesRegex(RuntimeError, "tmux_session_missing"):
                ctc.prepare_high_level_attach(controller, session_id, state_dir, Path(tmp) / "claude")


class HighLevelReplayTest(unittest.TestCase):
    def test_replay_completed_turns_outputs_jsonl_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd.resolve()) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)

            q1 = json_line({"sessionId": session_id, "type": "user", "message": {"content": "q1"}})
            a1 = json_line(
                {
                    "sessionId": session_id,
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "a1"}]},
                }
            )
            q2 = json_line({"sessionId": session_id, "type": "user", "message": {"content": "q2"}})
            a2 = json_line(
                {
                    "sessionId": session_id,
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "a2"}]},
                }
            )
            transcript.write_text(q1 + a1 + q2 + a2, encoding="utf-8")
            q1_start = 0
            q1_done = len((q1 + a1).encode("utf-8"))
            q2_start = q1_done
            q2_done = len((q1 + a1 + q2 + a2).encode("utf-8"))
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "cwd": str(cwd),
                        "transcript": ctc.transcript_file_state(transcript, q2_done),
                        "completed_turns": [
                            {
                                "turn_id": "turn1",
                                "session_id": session_id,
                                "answer": "a1",
                                "anchor_start_offset": q1_start,
                                "completed_offset": q1_done,
                                "transcript": ctc.transcript_file_state(transcript, q1_done),
                                "cost": {"estimated": True, "currency": "USD", "turn_usd": 0.1},
                            },
                            {
                                "turn_id": "turn2",
                                "session_id": session_id,
                                "answer": "a2",
                                "anchor_start_offset": q2_start,
                                "completed_offset": q2_done,
                                "transcript": ctc.transcript_file_state(transcript, q2_done),
                                "cost": {"estimated": True, "currency": "USD", "turn_usd": 0.2},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = ctc.parse_args(["replay", session_id, "--state-dir", str(state_dir), "--root", str(root), "--last", "2"])
            writes = []

            result = ctc.run_high_level_replay(args, Mock(), writes.append)

            self.assertEqual(result["exit_code"], 0)
            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual(
                [payload["event"] for payload in payloads],
                ["user", "assistant_text", "done", "metrics", "user", "assistant_text", "done", "metrics"],
            )
            self.assertEqual(payloads[0]["turn_id"], "turn1")
            self.assertEqual(payloads[4]["turn_id"], "turn2")
            self.assertEqual(payloads[7]["cost"]["session_usd"], 0.3)

            last_args = ctc.parse_args(["last", session_id, "--state-dir", str(state_dir), "--root", str(root)])
            last_writes = []
            last_result = ctc.run_high_level_replay(last_args, Mock(), last_writes.append)
            last_payloads = [json.loads(line) for line in "".join(last_writes).splitlines()]

            self.assertEqual(last_result["exit_code"], 0)
            self.assertEqual(last_payloads[0]["turn_id"], "turn2")

    def test_replay_last_turn_attaches_active_turn_until_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            user_line = json_line({"sessionId": session_id, "type": "user", "message": {"content": "active q"}})
            transcript.write_text(
                user_line
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "active a"}]},
                    }
                ),
                encoding="utf-8",
            )
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd),
                        "transcript": ctc.transcript_file_state(transcript, 0),
                        "active_turn": {
                            "turn_id": "turn_active",
                            "stream_state": "active",
                            "claude_state": "working",
                            "prompt_preview": "active q",
                            "before_send_wall_time_utc": "2026-05-18T00:00:00Z",
                            "before_send_transcript": ctc.transcript_file_state(transcript, 0),
                            "anchor_start_offset": 0,
                            "anchor_end_offset": len(user_line.encode("utf-8")),
                            "replay_start_offset": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = True
            controller.capture_screen.return_value = "Done\nclaude> "
            args = ctc.parse_args(
                [
                    "replay",
                    session_id,
                    "--state-dir",
                    str(state_dir),
                    "--root",
                    str(root),
                    "--last",
                    "1",
                    "--interval",
                    "0",
                    "--idle",
                    "0",
                    "--timeout",
                    "1",
                ]
            )
            writes = []

            result = ctc.run_high_level_replay(args, controller, writes.append)

            self.assertEqual(result["exit_code"], 0)
            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual([payload["event"] for payload in payloads], ["user", "assistant_text", "done", "metrics"])
            self.assertTrue(all(payload["turn_id"] == "turn_active" for payload in payloads))
            self.assertFalse(controller.send_prompt.called)

    def test_replay_last_two_outputs_completed_then_active_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            q1 = json_line({"sessionId": session_id, "type": "user", "message": {"content": "done q"}})
            a1 = json_line({"sessionId": session_id, "type": "assistant", "message": {"content": [{"type": "text", "text": "done a"}]}})
            q2 = json_line({"sessionId": session_id, "type": "user", "message": {"content": "active q"}})
            a2 = json_line({"sessionId": session_id, "type": "assistant", "message": {"content": [{"type": "text", "text": "active a"}]}})
            transcript.write_text(q1 + a1 + q2 + a2, encoding="utf-8")
            q1_done = len((q1 + a1).encode("utf-8"))
            q2_start = q1_done
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd),
                        "transcript": ctc.transcript_file_state(transcript, q1_done),
                        "completed_turns": [
                            {
                                "turn_id": "turn_done",
                                "session_id": session_id,
                                "answer": "done a",
                                "anchor_start_offset": 0,
                                "completed_offset": q1_done,
                                "transcript": ctc.transcript_file_state(transcript, q1_done),
                            }
                        ],
                        "active_turn": {
                            "turn_id": "turn_active",
                            "stream_state": "active",
                            "claude_state": "working",
                            "prompt_preview": "active q",
                            "before_send_wall_time_utc": "2026-05-18T00:00:00Z",
                            "before_send_transcript": ctc.transcript_file_state(transcript, q1_done),
                            "anchor_start_offset": q2_start,
                            "anchor_end_offset": q2_start + len(q2.encode("utf-8")),
                            "replay_start_offset": q2_start,
                        },
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = True
            controller.capture_screen.return_value = "Done\nclaude> "
            args = ctc.parse_args(
                [
                    "last",
                    session_id,
                    "--state-dir",
                    str(state_dir),
                    "--root",
                    str(root),
                    "--last",
                    "2",
                    "--interval",
                    "0",
                    "--idle",
                    "0",
                    "--timeout",
                    "1",
                ]
            )
            writes = []

            result = ctc.run_high_level_replay(args, controller, writes.append)

            self.assertEqual(result["exit_code"], 0)
            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual(
                [payload["event"] for payload in payloads],
                ["user", "assistant_text", "done", "metrics", "user", "assistant_text", "done", "metrics"],
            )
            self.assertEqual(payloads[0]["turn_id"], "turn_done")
            self.assertEqual(payloads[4]["turn_id"], "turn_active")
            self.assertIsNone(ctc.read_bridge_state(state_path)["active_turn"])

    def test_replay_recovers_when_active_turn_completes_before_attach(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            q1 = json_line({"sessionId": session_id, "type": "user", "message": {"content": "done q"}})
            a1 = json_line({"sessionId": session_id, "type": "assistant", "message": {"content": [{"type": "text", "text": "done a"}]}})
            q2 = json_line({"sessionId": session_id, "type": "user", "message": {"content": "racing q"}})
            a2 = json_line({"sessionId": session_id, "type": "assistant", "message": {"content": [{"type": "text", "text": "racing a"}]}})
            transcript.write_text(q1 + a1 + q2 + a2, encoding="utf-8")
            q1_done = len((q1 + a1).encode("utf-8"))
            q2_start = q1_done
            q2_done = len((q1 + a1 + q2 + a2).encode("utf-8"))
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            initial_state = {
                "schema_version": 1,
                "session_id": session_id,
                "tmux_session": ctc.web_tmux_session_name(session_id),
                "cwd": str(cwd),
                "transcript": ctc.transcript_file_state(transcript, q1_done),
                "completed_turns": [
                    {
                        "turn_id": "turn_done",
                        "session_id": session_id,
                        "answer": "done a",
                        "anchor_start_offset": 0,
                        "completed_offset": q1_done,
                        "transcript": ctc.transcript_file_state(transcript, q1_done),
                    }
                ],
                "active_turn": {
                    "turn_id": "turn_racing",
                    "stream_state": "active",
                    "claude_state": "working",
                    "prompt_preview": "racing q",
                    "before_send_wall_time_utc": "2026-05-18T00:00:00Z",
                    "before_send_transcript": ctc.transcript_file_state(transcript, q1_done),
                    "anchor_start_offset": q2_start,
                    "anchor_end_offset": q2_start + len(q2.encode("utf-8")),
                    "replay_start_offset": q2_start,
                },
            }
            state_path.write_text(json.dumps(initial_state), encoding="utf-8")

            def complete_before_attach(**_kwargs):
                completed_state = dict(initial_state)
                completed_state["active_turn"] = None
                completed_state["completed_turns"] = [
                    *initial_state["completed_turns"],
                    {
                        "turn_id": "turn_racing",
                        "session_id": session_id,
                        "answer": "racing a",
                        "anchor_start_offset": q2_start,
                        "completed_offset": q2_done,
                        "transcript": ctc.transcript_file_state(transcript, q2_done),
                    },
                ]
                ctc._write_high_level_state(state_path, completed_state)
                raise RuntimeError("no_active_turn")

            args = ctc.parse_args(
                [
                    "last",
                    session_id,
                    "--state-dir",
                    str(state_dir),
                    "--root",
                    str(root),
                    "--last",
                    "2",
                ]
            )
            writes = []

            with patch.object(ctc, "prepare_high_level_attach", side_effect=complete_before_attach):
                result = ctc.run_high_level_replay(args, Mock(), writes.append)

            self.assertEqual(result["exit_code"], 0)
            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual(
                [payload["event"] for payload in payloads],
                ["user", "assistant_text", "done", "metrics", "user", "assistant_text", "done", "metrics"],
            )
            self.assertEqual(payloads[0]["turn_id"], "turn_done")
            self.assertEqual(payloads[4]["turn_id"], "turn_racing")
            self.assertEqual(payloads[6]["answer"], "racing a")

    def test_replay_does_not_emit_completed_turns_when_active_attach_preflight_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            q1 = json_line({"sessionId": session_id, "type": "user", "message": {"content": "done q"}})
            a1 = json_line({"sessionId": session_id, "type": "assistant", "message": {"content": [{"type": "text", "text": "done a"}]}})
            transcript.write_text(q1 + a1, encoding="utf-8")
            q1_done = len((q1 + a1).encode("utf-8"))
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd),
                        "transcript": ctc.transcript_file_state(transcript, q1_done),
                        "completed_turns": [
                            {
                                "turn_id": "turn_done",
                                "session_id": session_id,
                                "answer": "done a",
                                "anchor_start_offset": 0,
                                "completed_offset": q1_done,
                                "transcript": ctc.transcript_file_state(transcript, q1_done),
                            }
                        ],
                        "active_turn": {
                            "turn_id": "turn_active",
                            "stream_state": "active",
                            "claude_state": "working",
                        },
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = False
            args = ctc.parse_args(["last", session_id, "--state-dir", str(state_dir), "--root", str(root), "--last", "2"])
            writes = []

            result = ctc.run_high_level_replay(args, controller, writes.append)

            self.assertEqual(result["exit_code"], 5)
            self.assertEqual(result["error"], "tmux_session_missing")
            self.assertEqual(writes, [])
            self.assertEqual(result["events"], [])

    def test_replay_interrupted_active_cancelled_turn_until_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            q1 = json_line({"sessionId": session_id, "type": "user", "message": {"content": "run slow command"}})
            transcript.write_text(
                q1
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "assistant",
                        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "sleep 45"}}]},
                    }
                )
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "user",
                        "toolUseResult": "User rejected tool use",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_1",
                                    "is_error": True,
                                    "content": "The user doesn't want to proceed with this tool use. The tool use was rejected.",
                                }
                            ]
                        },
                    }
                )
                + json_line({"sessionId": session_id, "type": "user", "message": {"content": "[Request interrupted by user for tool use]"}}),
                encoding="utf-8",
            )
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd),
                        "transcript": ctc.transcript_file_state(transcript, 0),
                        "active_turn": {
                            "turn_id": "turn_cancelled",
                            "stream_state": "interrupted",
                            "claude_state": "working",
                            "prompt_preview": "run slow command",
                            "before_send_wall_time_utc": "2026-05-18T00:00:00Z",
                            "before_send_transcript": ctc.transcript_file_state(transcript, 0),
                            "anchor_start_offset": 0,
                            "anchor_end_offset": len(q1.encode("utf-8")),
                            "replay_start_offset": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.session_exists.return_value = True
            controller.capture_screen.return_value = "Done\nclaude> "
            args = ctc.parse_args(
                [
                    "last",
                    session_id,
                    "--state-dir",
                    str(state_dir),
                    "--root",
                    str(root),
                    "--interval",
                    "0",
                    "--idle",
                    "0",
                    "--timeout",
                    "1",
                ]
            )
            writes = []

            result = ctc.run_high_level_replay(args, controller, writes.append)

            self.assertEqual(result["exit_code"], 0)
            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual([payload["event"] for payload in payloads], ["user", "tool_use", "tool_result", "user", "done", "metrics"])
            self.assertEqual(payloads[-2]["state"], "ready")
            self.assertNotIn("answer", payloads[-2])
            self.assertIsNone(ctc.read_bridge_state(state_path)["active_turn"])

    def test_replay_completed_turn_without_transcript_still_returns_done_and_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "cwd": str(Path(tmp) / "missing-project"),
                        "completed_turns": [
                            {
                                "turn_id": "turn_without_transcript",
                                "session_id": session_id,
                                "answer": "stored answer",
                                "completed_offset": 123,
                                "cost": {"estimated": True, "currency": "USD", "turn_usd": 0.01},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = ctc.parse_args(["last", session_id, "--state-dir", str(state_dir), "--root", str(Path(tmp) / "claude")])
            writes = []

            result = ctc.run_high_level_replay(args, Mock(), writes.append)

            self.assertEqual(result["exit_code"], 0)
            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual([payload["event"] for payload in payloads], ["done", "metrics"])
            self.assertEqual(payloads[0]["answer"], "stored answer")
            self.assertEqual(payloads[1]["cost"]["session_usd"], 0.01)

    def test_replay_stale_transcript_identity_returns_only_stored_done_and_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            original_user = json_line({"sessionId": session_id, "type": "user", "message": {"content": "original q"}})
            original_answer = json_line(
                {
                    "sessionId": session_id,
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "original a"}]},
                }
            )
            transcript.write_text(original_user + original_answer, encoding="utf-8")
            completed_offset = len((original_user + original_answer).encode("utf-8"))
            stored_transcript = ctc.transcript_file_state(transcript, completed_offset)

            transcript.unlink()
            transcript.write_text(
                json_line({"sessionId": session_id, "type": "user", "message": {"content": "wrong q"}})
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "wrong a"}]},
                    }
                ),
                encoding="utf-8",
            )
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "cwd": str(cwd),
                        "transcript": stored_transcript,
                        "completed_turns": [
                            {
                                "turn_id": "turn_done",
                                "session_id": session_id,
                                "answer": "stored answer",
                                "anchor_start_offset": 0,
                                "completed_offset": completed_offset,
                                "transcript": stored_transcript,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = ctc.parse_args(["replay", session_id, "--state-dir", str(state_dir), "--root", str(root)])
            writes = []

            result = ctc.run_high_level_replay(args, Mock(), writes.append)

            self.assertEqual(result["exit_code"], 0)
            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual([payload["event"] for payload in payloads], ["done", "metrics"])
            self.assertEqual(payloads[0]["answer"], "stored answer")
            self.assertNotIn("wrong a", "".join(writes))

    def test_last_error_messages_use_last_command_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            no_session_args = ctc.parse_args(["last", "--state-dir", str(Path(tmp) / "state")])
            missing_result = ctc.run_high_level_replay(no_session_args, Mock(), lambda _line: None)
            self.assertEqual(missing_result["error"], "last requires SESSION_ID or --session-id")

            bad_count_args = ctc.parse_args(
                [
                    "last",
                    "550e8400-e29b-41d4-a716-446655440000",
                    "--state-dir",
                    str(Path(tmp) / "state"),
                    "--last",
                    "0",
                ]
            )
            bad_count_result = ctc.run_high_level_replay(bad_count_args, Mock(), lambda _line: None)
            self.assertEqual(bad_count_result["error"], "last --last must be >= 1")


class ScreenStatusTest(unittest.TestCase):
    def test_detects_working_screen_from_interrupt_hint(self):
        status = ctc.analyze_screen_status("Thinking...\nPress Esc to interrupt\n")

        self.assertEqual(status.state, "working")

    def test_detects_confirmation_screen(self):
        status = ctc.analyze_screen_status("Allow tool execution?\nYes / No\n")

        self.assertEqual(status.state, "needs_confirmation")

    def test_detects_ready_screen_from_prompt_marker(self):
        status = ctc.analyze_screen_status("Done.\nclaude> ")

        self.assertEqual(status.state, "ready")

    def test_detects_ready_screen_from_claude_code_prompt_glyph(self):
        screen = "\n".join(
            [
                "  Context ██░░░ 15%",
                "────────────────────────────────",
                "❯ B로 해줘",
                "────────────────────────────────",
                "  Sonnet 4.6 high [Team] Cache:1h",
                "  ⏵⏵ auto mode on (shift+tab to cycle)",
            ],
        )

        status = ctc.analyze_screen_status(screen)

        self.assertEqual(status.state, "ready")

    def test_detects_working_status_line_even_when_prompt_glyph_is_visible(self):
        screen = "\n".join(
            [
                "● Searching for 1 pattern… (ctrl+o to expand)",
                "",
                "✽ Moseying… (1m 20s · ↑ 3.5k tokens)",
                "  ⎿  Tip: Use /btw to ask a quick side question without interrupting Claude's current work",
                "",
                "● How is Claude doing this session? (optional)",
                "  1: Bad    2: Fine   3: Good   0: Dismiss",
                "",
                "────────────────────────────────",
                "❯ ",
            ],
        )

        status = ctc.analyze_screen_status(screen)

        self.assertEqual(status.state, "working")

    def test_detects_haiku_working_status_line_with_elapsed_seconds(self):
        screen = "\n".join(
            [
                '⏺ Bash(python3 -c \'import time; time.sleep(8); print("SLEPT")\')',
                "",
                "✢ Slithering… (8s · ↓ 249 tokens)",
                "────────────────────────────────",
                "❯ ",
            ],
        )

        status = ctc.analyze_screen_status(screen)

        self.assertEqual(status.state, "working")

    def test_does_not_treat_completed_elapsed_summary_as_working(self):
        screen = "\n".join(
            [
                "⏺ TOOL-DONE",
                "",
                "✻ Cooked for 16s",
                "────────────────────────────────",
                "❯ ",
            ],
        )

        status = ctc.analyze_screen_status(screen)

        self.assertEqual(status.state, "ready")

    def test_detects_prompt_glyph_within_bottom_fifteen_status_lines(self):
        screen = "\n".join(
            [
                "❯ 현재 프롬프트",
                "line 1",
                "line 2",
                "line 3",
                "line 4",
                "line 5",
                "line 6",
                "line 7",
                "line 8",
                "line 9",
                "line 10",
                "  Sonnet 4.6 high [Team] Cache:1h",
                "  Context ██░░░░░░░░ 22% (44k/200k)",
                "  ⏵⏵ auto mode on (shift+tab to cycle)",
            ],
        )

        status = ctc.analyze_screen_status(screen)

        self.assertEqual(status.state, "ready")

    def test_ignores_old_prompt_glyph_outside_bottom_status_area(self):
        screen = "\n".join(
            [
                "❯ 이전에 보낸 프롬프트",
                "Claude is still producing a long answer.",
                "line -4",
                "line -3",
                "line -2",
                "line -1",
                "line 0",
                "line 1",
                "line 2",
                "line 3",
                "line 4",
                "line 5",
                "line 6",
                "line 7",
                "line 8",
                "line 9",
                "line 10",
                "  Sonnet 4.6 high [Team] Cache:1h",
                "  Context ██░░░░░░░░ 22% (44k/200k)",
                "  ⏵⏵ auto mode on (shift+tab to cycle)",
            ],
        )

        status = ctc.analyze_screen_status(screen)

        self.assertEqual(status.state, "unknown")

    def test_uses_unknown_when_no_prompt_or_working_hint_is_visible(self):
        status = ctc.analyze_screen_status("Last answer only\n")

        self.assertEqual(status.state, "unknown")


class RenderedFollowerTest(unittest.TestCase):
    def test_append_changed_screen_writes_only_suffix_when_possible(self):
        follower = ctc.RenderedScreenFollower()

        self.assertEqual(follower.diff("hello\n"), "hello\n")
        self.assertEqual(follower.diff("hello\nworld\n"), "world\n")
        self.assertEqual(follower.diff("reset\n"), "\n--- screen changed ---\nreset\n")
        self.assertEqual(follower.diff("reset\n"), "")


class TranscriptTest(unittest.TestCase):
    def test_find_latest_transcript_prefers_newest_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "old.jsonl"
            new = root / "new.jsonl"
            old.write_text("", encoding="utf-8")
            new.write_text("", encoding="utf-8")
            os.utime(old, (1000.0, 1000.0))
            os.utime(new, (2000.0, 2000.0))

            self.assertEqual(ctc.find_latest_transcript(root), new)

    def test_find_latest_transcript_searches_claude_projects_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "projects" / "-tmp-project" / "session.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("", encoding="utf-8")

            self.assertEqual(ctc.find_latest_transcript(root), transcript)

    def test_project_transcript_dir_encodes_cwd_like_claude_code(self):
        root = Path("/home/user/.claude")
        cwd = Path("/home/user/app_tmp")

        self.assertEqual(ctc.project_transcript_dir(root, cwd), Path("/home/user/.claude/projects/-home-user-app-tmp"))
        self.assertEqual(
            ctc.project_transcript_dir(root, Path("/home/user/app/.workspace")),
            Path("/home/user/.claude/projects/-home-user-app--workspace"),
        )

    def test_resolve_transcript_prefers_file_containing_last_sent_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "projects" / "-tmp-project"
            project.mkdir(parents=True)
            wrong = project / "wrong.jsonl"
            right = project / "right.jsonl"
            wrong.write_text('{"type":"user","message":{"content":"other"}}\n', encoding="utf-8")
            right.write_text('{"type":"user","message":{"content":"target prompt"}}\n', encoding="utf-8")

            state = ctc.SessionState(session="work", last_prompt="target prompt", cwd="/tmp/project")

            self.assertEqual(ctc.resolve_transcript_path(root=root, state=state), right)

    def test_resolve_transcript_falls_back_to_project_latest_when_prompt_state_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "projects" / "-tmp-project"
            other = root / "projects" / "-tmp-other"
            project.mkdir(parents=True)
            other.mkdir(parents=True)
            project_transcript = project / "project.jsonl"
            other_transcript = other / "other.jsonl"
            project_transcript.write_text("", encoding="utf-8")
            other_transcript.write_text("", encoding="utf-8")
            other_transcript.touch()

            state = ctc.SessionState(session="work", last_prompt="", cwd="/tmp/project")

            self.assertEqual(ctc.resolve_transcript_path(root=root, state=state), project_transcript)

    def test_resolve_status_transcript_reports_pending_until_prompt_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "projects" / "-tmp-project"
            project.mkdir(parents=True)
            (project / "old.jsonl").write_text('{"type":"assistant"}\n', encoding="utf-8")
            state = ctc.SessionState(session="work", last_prompt="new prompt", cwd="/tmp/project")

            transcript, pending = ctc.resolve_status_transcript_path(root, state)

            self.assertIsNone(transcript)
            self.assertTrue(pending)

    def test_format_transcript_event_summarizes_tool_use(self):
        event = {
            "type": "tool_use",
            "timestamp": "2026-05-15T01:02:03Z",
            "tool_name": "read",
            "tool_input": {"filePath": "/tmp/a.txt", "limit": 10},
        }

        self.assertEqual(
            ctc.format_transcript_event(event),
            "2026-05-15T01:02:03Z tool_use read input_keys=filePath,limit",
        )

    def test_format_transcript_event_includes_usage_when_available(self):
        event = {
            "type": "assistant",
            "timestamp": "2026-05-15T01:02:03Z",
            "message": {"usage": {"input_tokens": 3, "output_tokens": 5}},
        }

        self.assertEqual(
            ctc.format_transcript_event(event),
            "2026-05-15T01:02:03Z assistant usage=input_tokens=3,output_tokens=5",
        )

    def test_format_transcript_event_includes_thinking_content_type_when_available(self):
        event = {
            "type": "assistant",
            "timestamp": "2026-05-15T01:02:03Z",
            "message": {"role": "assistant", "content": [{"type": "thinking"}, {"type": "text"}]},
        }

        self.assertEqual(
            ctc.format_transcript_event(event),
            "2026-05-15T01:02:03Z assistant content=thinking,text",
        )

    def test_format_transcript_event_includes_context_when_available(self):
        event = {
            "type": "assistant",
            "timestamp": "2026-05-15T01:02:03Z",
            "context": {"remaining": 1000, "window": 200000},
        }

        self.assertEqual(
            ctc.format_transcript_event(event),
            "2026-05-15T01:02:03Z assistant context=remaining=1000,window=200000",
        )

    def test_read_transcript_events_starts_after_byte_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            first = '{"type":"user","timestamp":"t1"}\n'
            second = '{"type":"tool_use","timestamp":"t2","tool_name":"bash"}\n'
            path.write_text(first + second, encoding="utf-8")

            events, offset = ctc.read_transcript_events(path, offset=len(first))

            self.assertEqual([event["timestamp"] for event in events], ["t2"])
            self.assertEqual(offset, len(first) + len(second))

    def test_transcript_status_is_working_when_latest_turn_has_only_user(self):
        status = ctc.analyze_transcript_status(
            [
                {"type": "assistant", "message": {"content": [{"type": "text"}]}},
                {"type": "user", "message": {"content": "long answer please"}},
            ]
        )

        self.assertEqual(status.state, "working")

    def test_transcript_status_is_working_while_latest_assistant_is_thinking(self):
        status = ctc.analyze_transcript_status(
            [
                {"type": "user", "message": {"content": "long answer please"}},
                {"type": "assistant", "message": {"content": [{"type": "thinking"}]}},
            ]
        )

        self.assertEqual(status.state, "working")

    def test_transcript_status_is_ready_after_latest_assistant_text(self):
        status = ctc.analyze_transcript_status(
            [
                {"type": "user", "message": {"content": "long answer please"}},
                {"type": "assistant", "message": {"content": [{"type": "text"}]}},
            ]
        )

        self.assertEqual(status.state, "ready")

    def test_transcript_status_stays_working_when_assistant_text_has_tool_use_stop_reason(self):
        status = ctc.analyze_transcript_status(
            [
                {"type": "user", "message": {"content": "inspect files"}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "I will inspect it."}],
                        "stop_reason": "tool_use",
                    },
                },
            ]
        )

        self.assertEqual(status.state, "working")

    def test_transcript_status_is_ready_when_assistant_text_has_end_turn_stop_reason(self):
        status = ctc.analyze_transcript_status(
            [
                {"type": "user", "message": {"content": "answer"}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "final answer"}],
                        "stop_reason": "end_turn",
                    },
                },
            ]
        )

        self.assertEqual(status.state, "ready")

    def test_transcript_status_ignores_mode_metadata_after_ready_answer(self):
        events = [
            {"type": "user", "message": {"content": "answer"}},
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "final answer"}],
                    "stop_reason": "end_turn",
                },
            },
            {"type": "mode", "mode": "default"},
        ]

        self.assertEqual(ctc.analyze_transcript_status(events).state, "ready")
        self.assertEqual(transcript_events.analyze_transcript_status(events).state, "ready")

    def test_transcript_status_stays_working_when_assistant_text_has_null_stop_reason(self):
        status = ctc.analyze_transcript_status(
            [
                {"type": "user", "message": {"content": "inspect files"}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "I will inspect it."}],
                        "stop_reason": None,
                    },
                },
            ]
        )

        self.assertEqual(status.state, "working")

    def test_transcript_status_stays_working_when_assistant_text_has_top_level_null_stop_reason(self):
        status = ctc.analyze_transcript_status(
            [
                {"type": "user", "message": {"content": "inspect files"}},
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "I will inspect it."}]},
                    "stop_reason": None,
                },
            ]
        )

        self.assertEqual(status.state, "working")

    def test_transcript_status_stays_working_when_assistant_text_has_pause_turn_stop_reason(self):
        status = ctc.analyze_transcript_status(
            [
                {"type": "user", "message": {"content": "continue later"}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "I need to pause."}],
                        "stop_reason": "pause_turn",
                    },
                },
            ]
        )

        self.assertEqual(status.state, "working")

    def test_transcript_status_keeps_legacy_ready_when_assistant_text_has_no_stop_reason(self):
        status = ctc.analyze_transcript_status(
            [
                {"type": "user", "message": {"content": "answer"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "final answer"}]}},
            ]
        )

        self.assertEqual(status.state, "ready")

    def test_transcript_status_prioritizes_tool_use_content_over_terminal_stop_reason(self):
        status = ctc.analyze_transcript_status(
            [
                {"type": "user", "message": {"content": "inspect files"}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "I will inspect it."},
                            {"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}},
                        ],
                        "stop_reason": "end_turn",
                    },
                },
            ]
        )

        self.assertEqual(status.state, "working")

    def test_transcript_status_stays_working_when_assistant_text_also_requests_tool(self):
        status = ctc.analyze_transcript_status(
            [
                {"type": "user", "message": {"content": "inspect files"}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "I will inspect it."},
                            {"type": "tool_use", "name": "Task", "input": {"prompt": "subagent work"}},
                        ]
                    },
                },
            ]
        )

        self.assertEqual(status.state, "working")

    def test_transcript_status_is_ready_after_user_interrupts_tool_use(self):
        status = ctc.analyze_transcript_status(
            [
                {"type": "user", "message": {"content": "run slow command"}},
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "sleep 45"}}]},
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "is_error": True,
                                "content": "The user doesn't want to proceed with this tool use. The tool use was rejected.",
                            }
                        ]
                    },
                    "toolUseResult": "User rejected tool use",
                },
                {"type": "user", "message": {"content": "[Request interrupted by user for tool use]"}},
            ]
        )

        self.assertEqual(status.state, "ready")

    def test_transcript_status_stays_working_after_rejected_tool_result_without_interrupt_marker(self):
        status = ctc.analyze_transcript_status(
            [
                {"type": "user", "message": {"content": "run slow command"}},
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "sleep 45"}}]},
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "is_error": True,
                                "content": "The user doesn't want to proceed with this tool use. The tool use was rejected.",
                            }
                        ]
                    },
                    "toolUseResult": "User rejected tool use",
                },
            ]
        )

        self.assertEqual(status.state, "working")

    def test_transcript_status_treats_interruption_phrase_inside_user_prompt_as_normal_user_text(self):
        status = ctc.analyze_transcript_status(
            [
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "old answer"}]}},
                {"type": "user", "message": {"content": "Explain the phrase Request interrupted by user for tool use"}},
            ]
        )

        self.assertEqual(status.state, "working")

    def test_extract_latest_answer_text_returns_last_assistant_text_after_latest_user(self):
        events = [
            {"type": "user", "message": {"content": "first"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "old answer"}]}},
            {"type": "user", "message": {"content": "second"}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hidden"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "new answer"}]}},
        ]

        self.assertEqual(ctc.extract_latest_answer_text(events), "new answer")

    def test_extract_latest_answer_text_joins_multiple_text_blocks(self):
        events = [
            {"type": "user", "message": {"content": "question"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}]},
            },
        ]

        self.assertEqual(ctc.extract_latest_answer_text(events), "part one\npart two")

    def test_extract_latest_answer_text_returns_none_without_completed_answer(self):
        events = [
            {"type": "user", "message": {"content": "question"}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hidden"}]}},
        ]

        self.assertIsNone(ctc.extract_latest_answer_text(events))

    def test_extract_answer_texts_returns_recent_completed_answers(self):
        events = [
            {"type": "user", "message": {"content": "first"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer one"}]}},
            {"type": "user", "message": {"content": "second"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer two"}]}},
            {"type": "user", "message": {"content": "third"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer three"}]}},
        ]

        self.assertEqual(ctc.extract_answer_texts(events, count=2), ["answer two", "answer three"])

    def test_extract_answer_texts_ignores_internal_session_summary_prompt(self):
        events = [
            {"type": "user", "message": {"content": "real question"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "real answer"}]}},
            {"type": "user", "message": {"content": "당신은 Claude Code 세션 활동 요약 작성자입니다."}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": '{"skip": true}'}]}},
        ]

        self.assertEqual(ctc.extract_latest_answer_text(events), "real answer")

    def test_format_latest_turn_includes_thinking_tool_and_text(self):
        events = [
            {"type": "user", "message": {"content": "old"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "old answer"}]}},
            {"type": "user", "message": {"content": "new"}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "plan"}]}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}}]},
            },
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "/tmp/project"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
        ]

        self.assertEqual(
            ctc.format_latest_turn(events),
            "\n".join(
                [
                    "[user]",
                    "new",
                    "",
                    "[thinking]",
                    "plan",
                    "",
                    "[tool_use] Bash",
                    '{"command": "pwd"}',
                    "",
                    "[tool_result]",
                    "/tmp/project",
                    "",
                    "[assistant]",
                    "done",
                ]
            ),
        )

    def test_format_latest_turns_returns_recent_turns(self):
        events = [
            {"type": "user", "message": {"content": "first"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer one"}]}},
            {"type": "user", "message": {"content": "second"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer two"}]}},
            {"type": "user", "message": {"content": "third"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer three"}]}},
        ]

        self.assertEqual(
            ctc.format_latest_turns(events, count=2),
            "--- turn 1/2 ---\n\n[user]\nsecond\n\n[assistant]\nanswer two"
            "\n\n--- turn 2/2 ---\n\n[user]\nthird\n\n[assistant]\nanswer three",
        )

    def test_latest_turn_helpers_keep_minimum_count_of_one(self):
        events = [
            {"type": "user", "message": {"content": "first"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer one"}]}},
            {"type": "user", "message": {"content": "second"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer two"}]}},
        ]

        self.assertEqual(ctc.extract_answer_texts(events, count=0), ["answer two"])
        self.assertEqual(ctc.format_latest_turns(events, count=0), "[user]\nsecond\n\n[assistant]\nanswer two")

    def test_format_latest_turn_ignores_internal_session_summary_prompt(self):
        events = [
            {"type": "user", "message": {"content": "real question"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "real answer"}]}},
            {"type": "user", "message": {"content": "당신은 Claude Code 세션 활동 요약 작성자입니다."}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": '{"skip": true}'}]}},
        ]

        self.assertEqual(ctc.format_latest_turn(events), "[user]\nreal question\n\n[assistant]\nreal answer")


class StreamTest(unittest.TestCase):
    def test_transcript_event_module_is_public(self):
        import transcript_events

        self.assertIs(ctc.normalize_stream_events, transcript_events.normalize_stream_events)
        self.assertIs(ctc.analyze_turn_status, transcript_events.analyze_turn_status)

    def test_transcript_compatibility_fixtures_match_normalized_contract(self):
        fixture_dir = FIXTURES_DIR / "transcripts"
        cases = [
            "basic_tool_flow",
            "signature_thinking",
            "top_level_tool_events",
            "interrupted_turn",
        ]
        for case in cases:
            with self.subTest(case=case):
                transcript = fixture_dir / f"{case}.jsonl"
                expected = json.loads((fixture_dir / f"{case}.normalized.json").read_text(encoding="utf-8"))
                records, offset = ctc.read_transcript_records(transcript)

                self.assertEqual(offset, transcript.stat().st_size)
                self.assertEqual(
                    [payload for record in records for payload in ctc.normalize_stream_events([record.event])],
                    expected,
                )

    def test_normalize_stream_events_includes_thinking_tool_result_and_text(self):
        events = [
            {"type": "user", "timestamp": "t0", "message": {"content": "new"}},
            {"type": "assistant", "timestamp": "t1", "message": {"content": [{"type": "thinking", "thinking": "plan"}]}},
            {
                "type": "assistant",
                "timestamp": "t2",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "caller": "assistant",
                            "name": "Task",
                            "input": {"prompt": "subagent"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "timestamp": "t3",
                "toolUseResult": "done summary",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": False, "content": "done"}
                    ]
                },
            },
            {"type": "assistant", "timestamp": "t4", "message": {"content": [{"type": "text", "text": "final"}]}},
        ]

        self.assertEqual(
            ctc.normalize_stream_events(events),
            [
                {"event": "user", "timestamp": "t0", "text": "new"},
                {
                    "event": "thinking",
                    "timestamp": "t1",
                    "text": "plan",
                    "text_available": True,
                    "has_signature": False,
                },
                {
                    "event": "tool_use",
                    "timestamp": "t2",
                    "id": "toolu_1",
                    "caller": "assistant",
                    "name": "Task",
                    "input": {"prompt": "subagent"},
                },
                {
                    "event": "tool_result",
                    "timestamp": "t3",
                    "tool_use_id": "toolu_1",
                    "is_error": False,
                    "text": "done",
                    "result_preview": "done summary",
                    "result_preview_truncated": False,
                    "result_preview_full_length": 12,
                },
                {"event": "assistant_text", "timestamp": "t4", "text": "final"},
            ],
        )

    def test_normalize_stream_events_reports_signature_only_thinking_metadata(self):
        payloads = ctc.normalize_stream_events(
            [
                {
                    "type": "assistant",
                    "timestamp": "t1",
                    "message": {
                        "content": [{"type": "thinking", "thinking": "", "signature": "signed-thinking-block"}]
                    },
                }
            ]
        )

        self.assertEqual(
            payloads,
            [
                {
                    "event": "thinking",
                    "timestamp": "t1",
                    "text": "",
                    "text_available": False,
                    "has_signature": True,
                    "note": "thinking text unavailable; signature present",
                }
            ],
        )

    def test_normalize_stream_events_truncates_tool_result_text_and_result_preview(self):
        payloads = ctc.normalize_stream_events(
            [
                {
                    "type": "user",
                    "timestamp": "t3",
                    "toolUseResult": {"stdout": "abcdefghijklmnopqrstuvwxyz"},
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "is_error": False,
                                "content": "abcdefghijklmnopqrstuvwxyz",
                            }
                        ]
                    },
                }
            ],
            tool_result_limit=10,
        )

        self.assertEqual(payloads[0]["text"], "abcdefghij...")
        self.assertTrue(payloads[0]["text_truncated"])
        self.assertEqual(payloads[0]["text_full_length"], 26)
        self.assertTrue(payloads[0]["result_preview_truncated"])
        self.assertTrue(payloads[0]["result_preview"].endswith("..."))

    def test_stream_transcript_until_done_emits_jsonl_and_exits_after_final_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text(
                json_line({"type": "user", "timestamp": "t0", "message": {"content": "question"}}),
                encoding="utf-8",
            )
            pending_lines = [
                json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t1",
                        "message": {"content": [{"type": "thinking", "thinking": "plan"}]},
                    }
                ),
                json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t2",
                        "message": {"content": [{"type": "tool_use", "name": "Task", "input": {"prompt": "work"}}]},
                    }
                ),
                json_line(
                    {
                        "type": "user",
                        "timestamp": "t3",
                        "message": {"content": [{"type": "tool_result", "content": "subagent done"}]},
                    }
                ),
                json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t4",
                        "message": {"content": [{"type": "text", "text": "final answer"}]},
                    }
                ),
            ]
            controller = Mock()
            controller.capture_screen.side_effect = [
                "Thinking\n",
                "Thinking\n",
                "Running\n",
                "Waiting\n",
                "Done\nclaude> ",
                "Done\nclaude> ",
                "Done\nclaude> ",
            ]
            writes = []
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                if pending_lines:
                    with transcript.open("a", encoding="utf-8") as file:
                        file.write(pending_lines.pop(0))
                current_time += seconds

            status = ctc.stream_transcript_until_done(
                transcript,
                ctc.SessionState(session="work", last_prompt="question", cwd=None),
                controller,
                "work",
                interval=1.0,
                timeout=10.0,
                idle_seconds=2.0,
                write=writes.append,
                sleep=fake_sleep,
                now=fake_now,
            )

            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual(status.state, "ready")
            self.assertEqual(
                [payload["event"] for payload in payloads],
                ["user", "thinking", "tool_use", "tool_result", "assistant_text", "done"],
            )
            self.assertEqual(payloads[-1]["answer"], "final answer")

    def test_stream_transcript_until_done_does_not_finish_while_tool_result_waits_for_final_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text(
                json_line({"type": "user", "timestamp": "t0", "message": {"content": "question"}})
                + json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t1",
                        "message": {"content": [{"type": "tool_use", "name": "Task", "input": {"prompt": "work"}}]},
                    }
                )
                + json_line(
                    {
                        "type": "user",
                        "timestamp": "t2",
                        "message": {"content": [{"type": "tool_result", "content": "subagent done"}]},
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.capture_screen.return_value = "Looks idle\nclaude> "
            writes = []
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                current_time += seconds

            status = ctc.stream_transcript_until_done(
                transcript,
                ctc.SessionState(session="work", last_prompt="question", cwd=None),
                controller,
                "work",
                interval=1.0,
                timeout=3.0,
                idle_seconds=1.0,
                write=writes.append,
                sleep=fake_sleep,
                now=fake_now,
            )

            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual(status.state, "timeout")
            self.assertNotIn("done", [payload["event"] for payload in payloads])

    def test_stream_transcript_until_done_does_not_finish_when_assistant_text_stops_for_tool_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text(
                json_line({"type": "user", "timestamp": "t0", "message": {"content": "question"}})
                + json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t1",
                        "message": {
                            "content": [{"type": "text", "text": "I will inspect it."}],
                            "stop_reason": "tool_use",
                        },
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.capture_screen.return_value = "Looks idle\nclaude> "
            writes = []
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                current_time += seconds

            status = ctc.stream_transcript_until_done(
                transcript,
                ctc.SessionState(session="work", last_prompt="question", cwd=None),
                controller,
                "work",
                interval=1.0,
                timeout=3.0,
                idle_seconds=1.0,
                write=writes.append,
                sleep=fake_sleep,
                now=fake_now,
            )

            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual(status.state, "timeout")
            self.assertNotIn("done", [payload["event"] for payload in payloads])

    def test_stream_transcript_until_done_does_not_finish_when_assistant_text_has_null_stop_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text(
                json_line({"type": "user", "timestamp": "t0", "message": {"content": "question"}})
                + json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t1",
                        "message": {
                            "content": [{"type": "text", "text": "I will inspect it."}],
                            "stop_reason": None,
                        },
                    }
                ),
                encoding="utf-8",
            )
            controller = Mock()
            controller.capture_screen.return_value = "Looks idle\nclaude> "
            writes = []
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                current_time += seconds

            status = ctc.stream_transcript_until_done(
                transcript,
                ctc.SessionState(session="work", last_prompt="question", cwd=None),
                controller,
                "work",
                interval=1.0,
                timeout=3.0,
                idle_seconds=1.0,
                write=writes.append,
                sleep=fake_sleep,
                now=fake_now,
            )

            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual(status.state, "timeout")
            self.assertNotIn("done", [payload["event"] for payload in payloads])

    def test_stream_transcript_until_done_waits_through_null_stop_reason_partial_text_until_end_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text(
                json_line({"type": "user", "timestamp": "t0", "message": {"content": "question"}}),
                encoding="utf-8",
            )
            pending_lines = [
                json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t1",
                        "message": {"content": [{"type": "thinking", "thinking": "plan"}], "stop_reason": None},
                    }
                ),
                json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t2",
                        "message": {
                            "content": [{"type": "text", "text": "I will inspect it."}],
                            "stop_reason": None,
                        },
                    }
                ),
                json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t3",
                        "message": {
                            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}}],
                            "stop_reason": "tool_use",
                        },
                    }
                ),
                json_line(
                    {
                        "type": "user",
                        "timestamp": "t4",
                        "message": {"content": [{"type": "tool_result", "content": "/tmp/project"}]},
                    }
                ),
                json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t5",
                        "message": {
                            "content": [{"type": "text", "text": "final answer"}],
                            "stop_reason": "end_turn",
                        },
                    }
                ),
            ]
            controller = Mock()
            controller.capture_screen.return_value = "Looks idle\nclaude> "
            writes = []
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                if pending_lines:
                    with transcript.open("a", encoding="utf-8") as file:
                        file.write(pending_lines.pop(0))
                current_time += seconds

            status = ctc.stream_transcript_until_done(
                transcript,
                ctc.SessionState(session="work", last_prompt="question", cwd=None),
                controller,
                "work",
                interval=1.0,
                timeout=10.0,
                idle_seconds=1.0,
                write=writes.append,
                sleep=fake_sleep,
                now=fake_now,
            )

            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual(status.state, "ready")
            self.assertEqual(
                [payload["event"] for payload in payloads],
                ["user", "thinking", "assistant_text", "tool_use", "tool_result", "assistant_text", "done"],
            )
            self.assertEqual(payloads[-1]["answer"], "final answer")

    def test_high_level_stream_finishes_cancelled_tool_use_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text(
                json_line({"type": "user", "timestamp": "t0", "message": {"content": "run slow command"}})
                + json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t1",
                        "message": {
                            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "sleep 45"}}]
                        },
                    }
                )
                + json_line(
                    {
                        "type": "user",
                        "timestamp": "t2",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_1",
                                    "is_error": True,
                                    "content": "The user doesn't want to proceed with this tool use. The tool use was rejected.",
                                }
                            ]
                        },
                        "toolUseResult": "User rejected tool use",
                    }
                )
                + json_line(
                    {
                        "type": "user",
                        "timestamp": "t3",
                        "message": {"content": "[Request interrupted by user for tool use]"},
                    }
                ),
                encoding="utf-8",
            )
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=f"ctc-csess-{session_id}",
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="run slow command",
                turn_id="turn_cancelled",
                before_send_offset=0,
                replay_start_offset=0,
            )
            ctc._write_high_level_state(
                runtime.state_path,
                ctc.build_pending_turn_state({}, runtime, transcript, wall_time=1000.0),
            )
            controller = Mock()
            controller.capture_screen.return_value = "Done\nclaude> "
            writes = []
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                current_time += seconds

            status = ctc.stream_high_level_transcript_until_done(
                transcript,
                runtime,
                controller,
                interval=1.0,
                timeout=5.0,
                idle_seconds=1.0,
                write=writes.append,
                sleep=fake_sleep,
                now=fake_now,
            )

            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual(status.state, "ready")
            self.assertEqual(
                [payload["event"] for payload in payloads],
                ["user", "tool_use", "tool_result", "user", "done", "metrics"],
            )
            self.assertEqual(payloads[-2]["reason"], "matched (^|\\n)\\s*claude>\\s*$; transcript ready")
            self.assertNotIn("answer", payloads[-2])
            state = ctc.read_bridge_state(runtime.state_path)
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["completed_turns"][0]["turn_id"], "turn_cancelled")

    def test_high_level_stream_emits_stable_ids_done_and_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text(
                json_line({"type": "user", "timestamp": "t0", "message": {"content": "question"}})
                + json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t1",
                        "message": {"content": [{"type": "text", "text": "final answer"}]},
                        "model": "claude-sonnet-4-6",
                        "usage": {
                            "input_tokens": 10,
                            "cache_read_input_tokens": 3,
                            "cache_creation_input_tokens": 2,
                            "output_tokens": 5,
                        },
                    }
                ),
                encoding="utf-8",
            )
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=f"ctc-csess-{session_id}",
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="question",
                turn_id="turn_test",
                before_send_offset=0,
                replay_start_offset=0,
            )
            ctc._write_high_level_state(
                runtime.state_path,
                ctc.build_pending_turn_state({}, runtime, transcript, wall_time=1000.0),
            )
            controller = Mock()
            controller.capture_screen.return_value = "Done\nclaude> "
            writes = []
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                current_time += seconds

            status = ctc.stream_high_level_transcript_until_done(
                transcript,
                runtime,
                controller,
                interval=1.0,
                timeout=5.0,
                idle_seconds=1.0,
                write=writes.append,
                sleep=fake_sleep,
                now=fake_now,
            )

            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            completed_offset = transcript.stat().st_size
            self.assertEqual(status.state, "ready")
            self.assertEqual([payload["event"] for payload in payloads], ["user", "assistant_text", "done", "metrics"])
            for payload in payloads:
                self.assertEqual(payload["session_id"], session_id)
                self.assertEqual(payload["turn_id"], "turn_test")
                self.assertIn("event_id", payload)
            self.assertEqual(payloads[-2]["event_id"], f"turn_test:done:{completed_offset}")
            self.assertEqual(payloads[-2]["source_offset"], completed_offset)
            self.assertEqual(payloads[-2]["block_index"], -1)
            self.assertEqual(payloads[-1]["event_id"], f"turn_test:metrics:{completed_offset}")
            self.assertEqual(payloads[-1]["usage"]["cache_read_tokens"], 3)
            self.assertEqual(payloads[-1]["usage"]["cache_write_tokens"], 2)
            self.assertEqual(payloads[-1]["elapsed_ms"], 1000)
            self.assertNotIn("context", payloads[-1])
            self.assertTrue(payloads[-1]["cost"]["estimated"])
            self.assertEqual(payloads[-1]["cost"]["model"], "claude-sonnet-4.6")
            self.assertEqual(payloads[-1]["cost"]["cache_write_ttl"], "1h")
            self.assertEqual(payloads[-1]["cost"]["turn_usd"], 0.0001179)
            self.assertEqual(payloads[-1]["cost"]["session_usd"], 0.0001179)
            state = ctc.read_bridge_state(runtime.state_path)
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["last_turn"]["completed_offset"], completed_offset)
            self.assertEqual(state["transcript"]["path"], str(transcript))
            self.assertEqual(state["last_turn"]["answer"], "final answer")
            self.assertEqual(state["last_turn"]["elapsed_ms"], 1000)
            self.assertNotIn("context", state["last_turn"])
            self.assertEqual(state["completed_turns"][0]["turn_id"], "turn_test")
            self.assertEqual(state["completed_turns"][0]["transcript"]["path"], str(transcript))
            self.assertEqual(state["completed_turns"][0]["answer"], "final answer")
            self.assertEqual(state["completed_turns"][0]["usage"]["cache_read_tokens"], 3)
            self.assertNotIn("context", state["completed_turns"][0])
            self.assertEqual(state["usage_totals"]["input_tokens"], 10)
            self.assertEqual(state["usage_totals"]["cache_read_tokens"], 3)
            self.assertEqual(state["usage_totals"]["cache_write_tokens"], 2)
            self.assertEqual(state["usage_totals"]["output_tokens"], 5)
            self.assertEqual(state["cost_totals"]["currency"], "USD")
            self.assertEqual(state["cost_totals"]["session_usd"], 0.0001179)

    def test_high_level_metrics_aggregates_turn_usage_and_prefers_result_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=f"ctc-csess-{session_id}",
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="question",
                turn_id="turn_test",
                before_send_offset=0,
                replay_start_offset=0,
            )
            turn_events = [
                {"type": "user", "message": {"content": "question"}},
                {
                    "type": "assistant",
                    "model": "claude-sonnet-4-6",
                    "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 3,
                        "cache_creation_input_tokens": 2,
                        "output_tokens": 5,
                    },
                },
                {"type": "user", "message": {"content": [{"type": "tool_result", "content": "done"}]}},
                {
                    "type": "assistant",
                    "model": "claude-sonnet-4-6",
                    "message": {"content": [{"type": "text", "text": "final answer"}]},
                    "usage": {
                        "input_tokens": 20,
                        "cache_read_input_tokens": 7,
                        "cache_creation_input_tokens": 5,
                        "output_tokens": 11,
                    },
                },
                {"type": "result", "total_cost_usd": 0.1234},
            ]

            payload = ctc.high_level_metrics_payload(
                runtime,
                turn_events,
                completed_offset=123,
                state={"cost_totals": {"currency": "USD", "session_usd": 1.0}},
            )

            self.assertEqual(
                payload["usage"],
                {
                    "input_tokens": 30,
                    "cache_read_tokens": 10,
                    "cache_write_tokens": 7,
                    "output_tokens": 16,
                    "api_call_count": 2,
                },
            )
            self.assertFalse(payload["cost"]["estimated"])
            self.assertEqual(payload["cost"]["source"], "claude_result_total_cost_usd")
            self.assertEqual(payload["cost"]["turn_usd"], 0.1234)
            self.assertEqual(payload["cost"]["session_usd"], 1.1234)

    def test_high_level_metrics_deduplicates_repeated_usage_for_one_api_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=f"ctc-csess-{session_id}",
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="question",
                turn_id="turn_test",
                before_send_offset=0,
                replay_start_offset=0,
            )
            first_call_usage = {
                "input_tokens": 10,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
                "output_tokens": 5,
            }
            second_call_usage = {
                "input_tokens": 20,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 5,
                "output_tokens": 11,
            }
            turn_events = [
                {"type": "user", "message": {"content": "question"}},
                {
                    "type": "assistant",
                    "requestId": "req_1",
                    "model": "claude-sonnet-4-6",
                    "message": {
                        "id": "msg_1",
                        "content": [{"type": "thinking", "thinking": "..."}],
                        "usage": first_call_usage,
                    },
                },
                {
                    "type": "assistant",
                    "requestId": "req_1",
                    "model": "claude-sonnet-4-6",
                    "message": {
                        "id": "msg_1",
                        "content": [{"type": "tool_use", "name": "Bash"}],
                        "usage": first_call_usage,
                    },
                },
                {"type": "user", "message": {"content": [{"type": "tool_result", "content": "done"}]}},
                {
                    "type": "assistant",
                    "requestId": "req_2",
                    "model": "claude-sonnet-4-6",
                    "message": {
                        "id": "msg_2",
                        "content": [{"type": "thinking", "thinking": "..."}],
                        "usage": second_call_usage,
                    },
                },
                {
                    "type": "assistant",
                    "requestId": "req_2",
                    "model": "claude-sonnet-4-6",
                    "message": {
                        "id": "msg_2",
                        "content": [{"type": "text", "text": "final answer"}],
                        "usage": second_call_usage,
                    },
                },
            ]

            payload = ctc.high_level_metrics_payload(runtime, turn_events, completed_offset=123)

            self.assertEqual(
                payload["usage"],
                {
                    "input_tokens": 30,
                    "cache_read_tokens": 10,
                    "cache_write_tokens": 7,
                    "output_tokens": 16,
                    "api_call_count": 2,
                },
            )

    def test_completed_turn_totals_deduplicate_by_turn_id(self):
        first = {
            "turn_id": "turn_a",
            "usage": {"input_tokens": 10, "cache_read_tokens": 2},
            "cost": {"estimated": True, "currency": "USD", "turn_usd": 0.1},
        }
        replacement = {
            "turn_id": "turn_a",
            "usage": {"input_tokens": 3, "output_tokens": 4},
            "cost": {"estimated": True, "currency": "USD", "turn_usd": 0.2},
        }
        second = {
            "turn_id": "turn_b",
            "usage": {"input_tokens": 7, "cache_write_tokens": 5},
            "cost": {"estimated": False, "currency": "USD", "turn_usd": 0.3},
        }

        state = ctc.add_completed_turn_to_state({}, first)
        state = ctc.add_completed_turn_to_state(state, replacement)
        state = ctc.add_completed_turn_to_state(state, second)

        self.assertEqual([turn["turn_id"] for turn in state["completed_turns"]], ["turn_a", "turn_b"])
        self.assertEqual(state["usage_totals"]["input_tokens"], 10)
        self.assertEqual(state["usage_totals"]["output_tokens"], 4)
        self.assertEqual(state["usage_totals"]["cache_write_tokens"], 5)
        self.assertEqual(state["cost_totals"]["session_usd"], 0.5)

    def test_estimate_turn_cost_falls_back_to_latest_family_pricing(self):
        usage = {
            "input_tokens": 1_000_000,
            "cache_read_tokens": 1_000_000,
            "cache_write_tokens": 1_000_000,
            "output_tokens": 1_000_000,
        }

        cost = ctc.estimate_turn_cost("claude-sonnet-9-9", usage)

        self.assertTrue(cost["estimated"])
        self.assertEqual(cost["model"], "claude-sonnet-4.6")
        self.assertEqual(cost["model_match"], "family_latest")
        self.assertEqual(cost["turn_usd"], 24.3)

    def test_estimate_turn_cost_treats_hiku_as_haiku_latest(self):
        cost = ctc.estimate_turn_cost(
            "claude-hiku-experimental",
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        )

        self.assertTrue(cost["estimated"])
        self.assertEqual(cost["model"], "claude-haiku-4.5")
        self.assertEqual(cost["model_match"], "family_latest")
        self.assertEqual(cost["turn_usd"], 6.0)

    def test_load_pricing_table_uses_installed_data_file_when_source_file_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed_table = Path(tmp) / "share" / "claude-tmux-control" / "claude_pricing.json"
            installed_table.parent.mkdir(parents=True)
            installed_table.write_text('{"version": "installed-test", "models": {}}', encoding="utf-8")

            with patch.object(ctc_pricing, "DEFAULT_INSTALLED_PRICING_TABLE", installed_table):
                with patch.object(ctc_pricing, "DEFAULT_PRICING_TABLE", Path(tmp) / "missing.json"):
                    ctc_pricing._PRICING_TABLE_CACHE = None
                    self.assertEqual(ctc.load_pricing_table()["version"], "installed-test")
                    ctc_pricing._PRICING_TABLE_CACHE = None

    def test_high_level_stream_starts_from_before_send_offset_for_repeated_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            old_turn = (
                json_line({"type": "user", "timestamp": "old0", "message": {"content": "repeat"}})
                + json_line(
                    {
                        "type": "assistant",
                        "timestamp": "old1",
                        "message": {"content": [{"type": "text", "text": "old answer"}]},
                    }
                )
            )
            transcript.write_text(old_turn, encoding="utf-8")
            before_send_offset = transcript.stat().st_size
            transcript.write_text(
                old_turn
                + json_line({"type": "user", "timestamp": "new0", "message": {"content": "repeat"}})
                + json_line(
                    {
                        "type": "assistant",
                        "timestamp": "new1",
                        "message": {"content": [{"type": "text", "text": "new answer"}]},
                    }
                ),
                encoding="utf-8",
            )
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=f"ctc-csess-{session_id}",
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="repeat",
                turn_id="turn_repeat",
                before_send_offset=before_send_offset,
                replay_start_offset=before_send_offset,
            )
            ctc._write_high_level_state(
                runtime.state_path,
                ctc.build_pending_turn_state({}, runtime, transcript, wall_time=1000.0),
            )
            controller = Mock()
            controller.capture_screen.return_value = "Done\nclaude> "
            writes = []
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                current_time += seconds

            ctc.stream_high_level_transcript_until_done(
                transcript,
                runtime,
                controller,
                interval=1.0,
                timeout=5.0,
                idle_seconds=1.0,
                write=writes.append,
                sleep=fake_sleep,
                now=fake_now,
            )

            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertNotIn("old answer", json.dumps(payloads, ensure_ascii=False))
            self.assertIn("new answer", json.dumps(payloads, ensure_ascii=False))

    def test_high_level_stream_ignores_stray_records_before_anchor_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text(
                json_line(
                    {
                        "type": "assistant",
                        "timestamp": "stray",
                        "message": {"content": [{"type": "text", "text": "stray answer"}]},
                    }
                )
                + json_line({"type": "user", "timestamp": "t0", "message": {"content": "target prompt"}})
                + json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t1",
                        "message": {"content": [{"type": "text", "text": "target answer"}]},
                    }
                ),
                encoding="utf-8",
            )
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=f"ctc-csess-{session_id}",
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="target prompt",
                turn_id="turn_anchor",
                before_send_offset=0,
                replay_start_offset=0,
            )
            ctc._write_high_level_state(
                runtime.state_path,
                ctc.build_pending_turn_state({}, runtime, transcript, wall_time=1000.0),
            )
            controller = Mock()
            controller.capture_screen.return_value = "Done\nclaude> "
            writes = []
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                current_time += seconds

            ctc.stream_high_level_transcript_until_done(
                transcript,
                runtime,
                controller,
                interval=1.0,
                timeout=5.0,
                idle_seconds=1.0,
                write=writes.append,
                sleep=fake_sleep,
                now=fake_now,
            )

            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertNotIn("stray answer", json.dumps(payloads, ensure_ascii=False))
            self.assertIn("target answer", json.dumps(payloads, ensure_ascii=False))

    def test_high_level_stream_matches_anchor_when_prompt_has_trailing_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text(
                json_line({"type": "user", "timestamp": "t0", "message": {"content": "target prompt"}})
                + json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t1",
                        "message": {"content": [{"type": "text", "text": "target answer"}]},
                    }
                ),
                encoding="utf-8",
            )
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=f"ctc-csess-{session_id}",
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="target prompt ",
                turn_id="turn_anchor",
                before_send_offset=0,
                replay_start_offset=0,
            )
            ctc._write_high_level_state(
                runtime.state_path,
                ctc.build_pending_turn_state({}, runtime, transcript, wall_time=1000.0),
            )
            controller = Mock()
            controller.capture_screen.return_value = "Done\nclaude> "
            writes = []
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                current_time += seconds

            status = ctc.stream_high_level_transcript_until_done(
                transcript,
                runtime,
                controller,
                interval=1.0,
                timeout=5.0,
                idle_seconds=1.0,
                write=writes.append,
                sleep=fake_sleep,
                now=fake_now,
            )

            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual(status.state, "ready")
            self.assertIn("target answer", json.dumps(payloads, ensure_ascii=False))

    def test_high_level_stream_matches_anchor_when_transcript_expands_tab_to_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text(
                json_line({"type": "user", "timestamp": "t0", "message": {"content": "Bestie Bingo)    747,936명"}})
                + json_line(
                    {
                        "type": "assistant",
                        "timestamp": "t1",
                        "message": {"content": [{"type": "text", "text": "tab-normalized answer"}]},
                    }
                ),
                encoding="utf-8",
            )
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=f"ctc-csess-{session_id}",
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="Bestie Bingo)\t747,936명",
                turn_id="turn_anchor",
                before_send_offset=0,
                replay_start_offset=0,
            )
            ctc._write_high_level_state(
                runtime.state_path,
                ctc.build_pending_turn_state({}, runtime, transcript, wall_time=1000.0),
            )
            controller = Mock()
            controller.capture_screen.return_value = "Done\nclaude> "
            writes = []
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                current_time += seconds

            status = ctc.stream_high_level_transcript_until_done(
                transcript,
                runtime,
                controller,
                interval=1.0,
                timeout=5.0,
                idle_seconds=1.0,
                write=writes.append,
                sleep=fake_sleep,
                now=fake_now,
            )

            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual(status.state, "ready")
            self.assertIn("tab-normalized answer", json.dumps(payloads, ensure_ascii=False))
            controller.send_enter.assert_not_called()

    def test_high_level_stream_retries_submit_once_when_prompt_never_anchors(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text("", encoding="utf-8")
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=f"ctc-csess-{session_id}",
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="target prompt",
                turn_id="turn_anchor",
                before_send_offset=0,
                replay_start_offset=0,
            )
            ctc._write_high_level_state(
                runtime.state_path,
                ctc.build_pending_turn_state({}, runtime, transcript, wall_time=1000.0),
            )
            controller = Mock()
            controller.capture_screen.return_value = "Done\nclaude> "
            writes = []
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                current_time += seconds

            status = ctc.stream_high_level_transcript_until_done(
                transcript,
                runtime,
                controller,
                interval=0.5,
                timeout=2.0,
                idle_seconds=1.0,
                write=writes.append,
                sleep=fake_sleep,
                now=fake_now,
            )

            self.assertEqual(status.state, "timeout")
            controller.send_enter.assert_called_once_with(runtime.tmux_session)

    def test_high_level_stream_timeout_sends_escape_and_clears_active_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text(
                json_line({"sessionId": "550e8400-e29b-41d4-a716-446655440000", "type": "user", "message": {"content": "question"}})
                + json_line(
                    {
                        "sessionId": "550e8400-e29b-41d4-a716-446655440000",
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "working"}], "stop_reason": "tool_use"},
                    }
                ),
                encoding="utf-8",
            )
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=f"ctc-csess-{session_id}",
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="question",
                turn_id="turn_timeout",
                before_send_offset=0,
                replay_start_offset=0,
            )
            ctc._write_high_level_state(
                runtime.state_path,
                ctc.build_pending_turn_state({}, runtime, transcript, wall_time=1000.0),
            )
            controller = Mock()
            controller.capture_screen.return_value = "Working\n"
            writes = []
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                current_time += seconds

            status = ctc.stream_high_level_transcript_until_done(
                transcript,
                runtime,
                controller,
                interval=1.0,
                timeout=2.0,
                idle_seconds=1.0,
                write=writes.append,
                sleep=fake_sleep,
                now=fake_now,
            )

            self.assertEqual(status.state, "timeout")
            controller.send_escape.assert_called_once_with(runtime.tmux_session)
            controller.kill_session.assert_called_once_with(runtime.tmux_session)
            payloads = [json.loads(line) for line in "".join(writes).splitlines()]
            self.assertEqual(payloads[-1]["event"], "timeout")
            state = ctc.read_bridge_state(runtime.state_path)
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["last_turn"]["turn_id"], runtime.turn_id)
            self.assertEqual(state["last_turn"]["stream_state"], "timeout")

    def test_high_level_stream_retries_submit_when_only_non_anchor_records_arrive(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text(
                json_line({"type": "system", "timestamp": "t0", "cwd": str(Path(tmp))}),
                encoding="utf-8",
            )
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=f"ctc-csess-{session_id}",
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="target prompt",
                turn_id="turn_anchor",
                before_send_offset=0,
                replay_start_offset=0,
            )
            ctc._write_high_level_state(
                runtime.state_path,
                ctc.build_pending_turn_state({}, runtime, transcript, wall_time=1000.0),
            )
            controller = Mock()
            controller.capture_screen.return_value = "Done\nclaude> "
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                current_time += seconds

            ctc.stream_high_level_transcript_until_done(
                transcript,
                runtime,
                controller,
                interval=0.5,
                timeout=2.0,
                idle_seconds=1.0,
                write=lambda _line: None,
                sleep=fake_sleep,
                now=fake_now,
            )

            controller.send_enter.assert_called_once_with(runtime.tmux_session)

    def test_high_level_stream_retries_submit_when_unanchored_screen_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text("", encoding="utf-8")
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=f"ctc-csess-{session_id}",
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp),
                prompt="target prompt",
                turn_id="turn_anchor",
                before_send_offset=0,
                replay_start_offset=0,
            )
            ctc._write_high_level_state(
                runtime.state_path,
                ctc.build_pending_turn_state({}, runtime, transcript, wall_time=1000.0),
            )
            controller = Mock()
            controller.capture_screen.return_value = "pasted text without prompt marker"
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                current_time += seconds

            ctc.stream_high_level_transcript_until_done(
                transcript,
                runtime,
                controller,
                interval=0.5,
                timeout=2.0,
                idle_seconds=1.0,
                write=lambda _line: None,
                sleep=fake_sleep,
                now=fake_now,
            )

            controller.send_enter.assert_called_once_with(runtime.tmux_session)

    def test_high_level_transcript_resolution_never_falls_back_to_global_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "claude"
            root.mkdir()
            global_transcript = root / "global.jsonl"
            global_transcript.write_text(json_line({"type": "user", "message": {"content": "leak"}}), encoding="utf-8")

            self.assertIsNone(ctc.resolve_high_level_transcript(root, Path(tmp) / "project", {}))

    def test_high_level_transcript_resolution_requires_matching_session_id_within_same_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "claude"
            project = ctc.project_transcript_dir(root, Path(tmp) / "project")
            project.mkdir(parents=True)
            wrong = project / "wrong.jsonl"
            right = project / "right.jsonl"
            wrong.write_text(
                json_line({"sessionId": "11111111-1111-4111-8111-111111111111", "type": "user"}),
                encoding="utf-8",
            )
            right.write_text(
                json_line({"sessionId": "550e8400-e29b-41d4-a716-446655440000", "type": "user"}),
                encoding="utf-8",
            )

            selected = ctc.resolve_high_level_transcript(
                root,
                Path(tmp) / "project",
                {"session_id": "550e8400-e29b-41d4-a716-446655440000"},
            )

            self.assertEqual(selected, right)

    def test_wait_for_high_level_transcript_accepts_same_path_inode_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            root = Path(tmp) / "claude"
            project = ctc.project_transcript_dir(root, Path(tmp) / "project")
            project.mkdir(parents=True)
            transcript = project / "session.jsonl"
            transcript.write_text(
                json_line({"sessionId": session_id, "type": "user", "message": {"content": "old"}})
                + ("x" * 300),
                encoding="utf-8",
            )
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=f"ctc-csess-{session_id}",
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp) / "project",
                prompt="new",
                turn_id="turn_wait",
                before_send_offset=transcript.stat().st_size,
                replay_start_offset=transcript.stat().st_size,
                before_send_transcript=transcript,
            )
            ctc._write_high_level_state(
                runtime.state_path,
                ctc.build_pending_turn_state({}, runtime, transcript, wall_time=1000.0),
            )
            transcript.unlink()
            transcript.write_text(
                json_line({"sessionId": session_id, "type": "user", "message": {"content": "new"}}),
                encoding="utf-8",
            )

            self.assertEqual(ctc.wait_for_high_level_transcript(root, runtime, timeout=0.1, interval=0.0), transcript)

    def test_transcript_replaced_since_baseline_detects_shrunk_same_inode_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            state_path = Path(tmp) / "state.json"
            transcript.write_text("new\n", encoding="utf-8")
            stat = transcript.stat()
            ctc._write_high_level_state(
                state_path,
                {
                    "active_turn": {
                        "before_send_transcript": {
                            "path": str(transcript),
                            "st_dev": stat.st_dev,
                            "st_ino": stat.st_ino,
                            "size": stat.st_size + 100,
                            "offset": stat.st_size + 100,
                            "mtime_ns": stat.st_mtime_ns,
                        }
                    }
                },
            )

            self.assertTrue(ctc.transcript_replaced_since_baseline(transcript, state_path))

    def test_wait_for_high_level_transcript_retries_submit_when_no_transcript_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            root = Path(tmp) / "claude"
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=f"ctc-csess-{session_id}",
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=Path(tmp) / "project",
                prompt="target prompt",
                turn_id="turn_missing_transcript",
                before_send_offset=0,
                replay_start_offset=0,
                started_at_monotonic=0.0,
            )
            controller = Mock()
            controller.capture_screen.return_value = "pasted text without prompt marker"
            current_time = 0.0

            def fake_now():
                return current_time

            def fake_sleep(seconds):
                nonlocal current_time
                current_time += seconds

            self.assertIsNone(
                ctc.wait_for_high_level_transcript(
                    root,
                    runtime,
                    timeout=2.0,
                    interval=0.5,
                    controller=controller,
                    sleep=fake_sleep,
                    now=fake_now,
                )
            )
            controller.send_enter.assert_called_once_with(runtime.tmux_session)

    def test_wait_for_high_level_transcript_switches_to_new_resume_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            project = ctc.project_transcript_dir(root, cwd)
            project.mkdir(parents=True)
            old = project / "old.jsonl"
            new = project / "new.jsonl"
            old.write_text(
                json_line({"sessionId": session_id, "type": "user", "message": {"content": "old turn"}}),
                encoding="utf-8",
            )
            os.utime(old, (1000.0, 1000.0))
            runtime = ctc.StreamRuntime(
                session_id=session_id,
                tmux_session=f"ctc-csess-{session_id}",
                state_path=Path(tmp) / "state" / "sessions" / f"{session_id}.json",
                state_dir=Path(tmp) / "state",
                cwd=cwd,
                prompt="resumed prompt",
                turn_id="turn_wait_resume",
                before_send_offset=old.stat().st_size,
                replay_start_offset=old.stat().st_size,
                before_send_transcript=old,
            )
            ctc._write_high_level_state(
                runtime.state_path,
                ctc.build_pending_turn_state({}, runtime, old, wall_time=1000.0),
            )
            new.write_text(
                json_line({"sessionId": session_id, "type": "user", "message": {"content": "resumed prompt"}}),
                encoding="utf-8",
            )
            os.utime(new, (2000.0, 2000.0))

            self.assertEqual(ctc.wait_for_high_level_transcript(root, runtime, timeout=0.1, interval=0.0), new)

    def test_read_transcript_records_does_not_advance_on_partial_json_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            first = json_line({"type": "user", "message": {"content": "question"}})
            partial = '{"type":"assistant","message":{"content":['
            transcript.write_text(first + partial, encoding="utf-8")

            records, offset = ctc.read_transcript_records(transcript, 0)

            self.assertEqual(len(records), 1)
            self.assertEqual(offset, len(first))

            transcript.write_text(
                first
                + json_line(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "done"}]},
                    }
                ),
                encoding="utf-8",
            )
            records, offset = ctc.read_transcript_records(transcript, offset)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].event["message"]["content"][0]["text"], "done")


class CliIntegrationTest(unittest.TestCase):
    def test_cli_stream_new_session_preseeds_trust_and_streams_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_log = Path(tmp) / "tmux.log"
            write_fake_tmux(fake_bin)
            write_fake_claude(fake_bin)
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            home = Path(tmp) / "home"
            config = Path(tmp) / "config"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd.resolve()) / f"{session_id}.jsonl"

            result = run_cli(
                [
                    "stream",
                    "--cwd",
                    str(cwd),
                    "--session-id",
                    session_id,
                    "--root",
                    str(root),
                    "--state-dir",
                    str(state_dir),
                    "--timeout",
                    "1",
                    "--interval",
                    "0.01",
                    "--idle",
                    "0",
                    "hello",
                ],
                env=fake_tmux_env(
                    fake_bin,
                    fake_log,
                    HOME=home,
                    CLAUDE_CONFIG_DIR=config,
                    TMUX_FAKE_HAS_SESSION=1,
                    TMUX_FAKE_TRANSCRIPT=transcript,
                    TMUX_FAKE_SESSION_ID=session_id,
                    TMUX_FAKE_ANSWER="streamed answer",
                    TMUX_FAKE_STARTUP_METADATA=1,
                    TMUX_FAKE_INLINE_PROMPT="hello",
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payloads = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([payload["event"] for payload in payloads], ["user", "assistant_text", "done", "metrics"])
            self.assertEqual(payloads[2]["answer"], "streamed answer")
            self.assertEqual(payloads[3]["context"], {"remaining": 12345})
            state = ctc.read_bridge_state(ctc.web_session_state_path(session_id, state_dir))
            self.assertIsNone(state["active_turn"])
            claude_json = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
            self.assertTrue(claude_json["projects"][str(cwd.resolve())]["hasTrustDialogAccepted"])
            settings_json = json.loads((config / "settings.json").read_text(encoding="utf-8"))
            self.assertTrue(settings_json["skipDangerousModePermissionPrompt"])
            fake_log_text = fake_log.read_text(encoding="utf-8")
            self.assertIn("new-session -d -s " + ctc.web_tmux_session_name(session_id), fake_log_text)
            self.assertIn("-- $'hello'", fake_log_text)
            self.assertNotIn("send-keys -t " + ctc.web_tmux_session_name(session_id) + " Enter", fake_log_text)
            self.assertIn('"type":"system"', transcript.read_text(encoding="utf-8"))

    def test_cli_stream_resumes_inactive_session_then_streams_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_log = Path(tmp) / "tmux.log"
            write_fake_tmux(fake_bin)
            write_fake_claude(fake_bin)
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd.resolve()) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json_line({"sessionId": session_id, "type": "user", "message": {"content": "old prompt"}})
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "old answer"}], "stop_reason": "end_turn"},
                    }
                ),
                encoding="utf-8",
            )
            ctc._write_high_level_state(
                ctc.web_session_state_path(session_id, state_dir),
                {
                    "schema_version": 1,
                    "session_id": session_id,
                    "cwd": str(cwd.resolve()),
                    "transcript": ctc.transcript_file_state(transcript),
                    "active_turn": None,
                    "last_turn": {"turn_id": "old", "claude_state": "ready"},
                },
            )

            result = run_cli(
                [
                    "stream",
                    "--cwd",
                    str(cwd),
                    "--session-id",
                    session_id,
                    "--root",
                    str(root),
                    "--state-dir",
                    str(state_dir),
                    "--timeout",
                    "1",
                    "--interval",
                    "0.01",
                    "--idle",
                    "0",
                    "resume prompt",
                ],
                env=fake_tmux_env(
                    fake_bin,
                    fake_log,
                    TMUX_FAKE_HAS_SESSION=1,
                    TMUX_FAKE_TRANSCRIPT=transcript,
                    TMUX_FAKE_SESSION_ID=session_id,
                    TMUX_FAKE_ANSWER="resumed answer",
                    TMUX_FAKE_STARTUP_METADATA=1,
                    TMUX_FAKE_INLINE_PROMPT="resume prompt",
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payloads = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([payload["event"] for payload in payloads], ["user", "assistant_text", "done", "metrics"])
            self.assertEqual(payloads[0]["text"], "resume prompt")
            self.assertEqual(payloads[1]["text"], "resumed answer")
            self.assertEqual(payloads[2]["answer"], "resumed answer")
            self.assertIsNone(ctc.read_bridge_state(ctc.web_session_state_path(session_id, state_dir))["active_turn"])
            fake_log_lines = fake_log.read_text(encoding="utf-8").splitlines()
            new_session_line = next(line for line in fake_log_lines if line.startswith("new-session "))
            self.assertIn("--resume " + session_id, new_session_line)
            self.assertIn("-- $'resume prompt'", new_session_line)
            self.assertNotIn("send-keys -t " + ctc.web_tmux_session_name(session_id) + " Enter", "\n".join(fake_log_lines))
            transcript_text = transcript.read_text(encoding="utf-8")
            self.assertIn('"type":"system"', transcript_text)
            self.assertIn("resume prompt", transcript_text)

    def test_cli_replay_completed_turn_outputs_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            user_line = json_line({"sessionId": session_id, "type": "user", "message": {"content": "q"}})
            assistant_line = json_line(
                {
                    "sessionId": session_id,
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "a"}]},
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                }
            )
            transcript.write_text(user_line + assistant_line, encoding="utf-8")
            completed_offset = len((user_line + assistant_line).encode("utf-8"))
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "cwd": str(cwd),
                        "transcript": ctc.transcript_file_state(transcript, completed_offset),
                        "completed_turns": [
                            {
                                "turn_id": "turn_cli",
                                "session_id": session_id,
                                "answer": "a",
                                "anchor_start_offset": 0,
                                "completed_offset": completed_offset,
                                "transcript": ctc.transcript_file_state(transcript, completed_offset),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(["replay", session_id, "--state-dir", str(state_dir), "--root", str(root), "--last", "1"])

            self.assertEqual(result.returncode, 0, result.stderr)
            payloads = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([payload["event"] for payload in payloads], ["user", "assistant_text", "done", "metrics"])
            self.assertEqual(payloads[2]["answer"], "a")

    def test_cli_last_active_turn_attaches_until_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_log = Path(tmp) / "tmux.log"
            write_fake_tmux(fake_bin)
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            user_line = json_line({"sessionId": session_id, "type": "user", "message": {"content": "active q"}})
            transcript.write_text(
                user_line
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "active a"}]},
                    }
                ),
                encoding="utf-8",
            )
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd),
                        "transcript": ctc.transcript_file_state(transcript, 0),
                        "active_turn": {
                            "turn_id": "turn_active_cli",
                            "stream_state": "active",
                            "claude_state": "working",
                            "prompt_preview": "active q",
                            "before_send_wall_time_utc": "2026-05-18T00:00:00Z",
                            "before_send_transcript": ctc.transcript_file_state(transcript, 0),
                            "anchor_start_offset": 0,
                            "anchor_end_offset": len(user_line.encode("utf-8")),
                            "replay_start_offset": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(
                [
                    "last",
                    session_id,
                    "--state-dir",
                    str(state_dir),
                    "--root",
                    str(root),
                    "--interval",
                    "0",
                    "--idle",
                    "0",
                    "--timeout",
                    "1",
                ],
                env=fake_tmux_env(fake_bin, fake_log),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payloads = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([payload["event"] for payload in payloads], ["user", "assistant_text", "done", "metrics"])
            self.assertTrue(all(payload["turn_id"] == "turn_active_cli" for payload in payloads))
            self.assertIn("has-session -t " + ctc.web_tmux_session_name(session_id), fake_log.read_text(encoding="utf-8"))
            self.assertIsNone(ctc.read_bridge_state(state_path)["active_turn"])

    def test_cli_last_active_turn_does_not_finish_when_assistant_text_stops_for_tool_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_log = Path(tmp) / "tmux.log"
            write_fake_tmux(fake_bin)
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            user_line = json_line({"sessionId": session_id, "type": "user", "message": {"content": "active q"}})
            transcript.write_text(
                user_line
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "assistant",
                        "message": {
                            "content": [{"type": "text", "text": "I will inspect it."}],
                            "stop_reason": "tool_use",
                        },
                    }
                ),
                encoding="utf-8",
            )
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd),
                        "transcript": ctc.transcript_file_state(transcript, 0),
                        "active_turn": {
                            "turn_id": "turn_active_cli_tool_use_stop",
                            "stream_state": "active",
                            "claude_state": "working",
                            "prompt_preview": "active q",
                            "before_send_wall_time_utc": "2026-05-18T00:00:00Z",
                            "before_send_transcript": ctc.transcript_file_state(transcript, 0),
                            "anchor_start_offset": 0,
                            "anchor_end_offset": len(user_line.encode("utf-8")),
                            "replay_start_offset": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(
                [
                    "last",
                    session_id,
                    "--state-dir",
                    str(state_dir),
                    "--root",
                    str(root),
                    "--interval",
                    "0.01",
                    "--idle",
                    "0",
                    "--timeout",
                    "0.05",
                ],
                env=fake_tmux_env(fake_bin, fake_log),
            )

            self.assertEqual(result.returncode, 3, result.stderr)
            payloads = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([payload["event"] for payload in payloads], ["user", "assistant_text", "timeout"])
            self.assertEqual(payloads[-1]["state"], "timeout")
            self.assertNotIn("done", [payload["event"] for payload in payloads])
            state = ctc.read_bridge_state(state_path)
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["last_turn"]["stream_state"], "timeout")
            self.assertIn("send-keys -t " + ctc.web_tmux_session_name(session_id) + " Escape", fake_log.read_text(encoding="utf-8"))

    def test_cli_last_active_turn_does_not_finish_when_assistant_text_has_null_stop_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_log = Path(tmp) / "tmux.log"
            write_fake_tmux(fake_bin)
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            user_line = json_line({"sessionId": session_id, "type": "user", "message": {"content": "active q"}})
            transcript.write_text(
                user_line
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "assistant",
                        "message": {
                            "content": [{"type": "text", "text": "I will inspect it."}],
                            "stop_reason": None,
                        },
                    }
                ),
                encoding="utf-8",
            )
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd),
                        "transcript": ctc.transcript_file_state(transcript, 0),
                        "active_turn": {
                            "turn_id": "turn_active_cli_null_stop_reason",
                            "stream_state": "active",
                            "claude_state": "working",
                            "prompt_preview": "active q",
                            "before_send_wall_time_utc": "2026-05-18T00:00:00Z",
                            "before_send_transcript": ctc.transcript_file_state(transcript, 0),
                            "anchor_start_offset": 0,
                            "anchor_end_offset": len(user_line.encode("utf-8")),
                            "replay_start_offset": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(
                [
                    "last",
                    session_id,
                    "--state-dir",
                    str(state_dir),
                    "--root",
                    str(root),
                    "--interval",
                    "0.01",
                    "--idle",
                    "0",
                    "--timeout",
                    "0.05",
                ],
                env=fake_tmux_env(fake_bin, fake_log),
            )

            self.assertEqual(result.returncode, 3, result.stderr)
            payloads = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([payload["event"] for payload in payloads], ["user", "assistant_text", "timeout"])
            self.assertEqual(payloads[-1]["state"], "timeout")
            self.assertNotIn("done", [payload["event"] for payload in payloads])
            state = ctc.read_bridge_state(state_path)
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["last_turn"]["stream_state"], "timeout")
            self.assertIn("send-keys -t " + ctc.web_tmux_session_name(session_id) + " Escape", fake_log.read_text(encoding="utf-8"))

    def test_cli_cancel_resets_state_after_tmux_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_log = Path(tmp) / "tmux.log"
            write_fake_tmux(fake_bin)
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "active_turn": {"turn_id": "turn_cancel"},
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(
                ["cancel", session_id, "--state-dir", str(state_dir)],
                env=fake_tmux_env(fake_bin, fake_log),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["event"], "cancel")
            self.assertEqual(payload["sent_key"], "Escape")
            self.assertTrue(payload["reset_applied"])
            self.assertEqual(payload["moved_turn_id"], "turn_cancel")
            fake_log_text = fake_log.read_text(encoding="utf-8")
            self.assertIn("send-keys -t " + ctc.web_tmux_session_name(session_id) + " Escape", fake_log_text)
            self.assertIn("kill-session -t " + ctc.web_tmux_session_name(session_id), fake_log_text)
            state = ctc.read_bridge_state(state_path)
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["last_turn"], {"turn_id": "turn_cancel"})

    def test_cli_cancel_reset_moves_active_turn_to_last_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_log = Path(tmp) / "tmux.log"
            write_fake_tmux(fake_bin)
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "active_turn": {"turn_id": "turn_cancel", "claude_state": "working"},
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(
                ["cancel", session_id, "--reset", "--state-dir", str(state_dir)],
                env=fake_tmux_env(fake_bin, fake_log),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["event"], "cancel")
            self.assertTrue(payload["reset_applied"])
            self.assertEqual(payload["moved_turn_id"], "turn_cancel")
            self.assertEqual(payload["state_after"], {"active_turn": None})
            fake_log_text = fake_log.read_text(encoding="utf-8")
            self.assertIn("send-keys -t " + ctc.web_tmux_session_name(session_id) + " Escape", fake_log_text)
            self.assertIn("kill-session -t " + ctc.web_tmux_session_name(session_id), fake_log_text)
            state = ctc.read_bridge_state(state_path)
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["last_turn"], {"turn_id": "turn_cancel", "claude_state": "working"})

    def test_cli_last_cancelled_active_turn_finishes_and_clears_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_log = Path(tmp) / "tmux.log"
            write_fake_tmux(fake_bin)
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            user_line = json_line({"sessionId": session_id, "type": "user", "message": {"content": "run slow command"}})
            transcript.write_text(
                user_line
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "assistant",
                        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "sleep 45"}}]},
                    }
                )
                + json_line(
                    {
                        "sessionId": session_id,
                        "type": "user",
                        "toolUseResult": "User rejected tool use",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_1",
                                    "is_error": True,
                                    "content": "The user doesn't want to proceed with this tool use. The tool use was rejected.",
                                }
                            ]
                        },
                    }
                )
                + json_line({"sessionId": session_id, "type": "user", "message": {"content": "[Request interrupted by user for tool use]"}}),
                encoding="utf-8",
            )
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd),
                        "transcript": ctc.transcript_file_state(transcript, 0),
                        "active_turn": {
                            "turn_id": "turn_cli_cancelled",
                            "stream_state": "interrupted",
                            "claude_state": "working",
                            "prompt_preview": "run slow command",
                            "before_send_wall_time_utc": "2026-05-18T00:00:00Z",
                            "before_send_transcript": ctc.transcript_file_state(transcript, 0),
                            "anchor_start_offset": 0,
                            "anchor_end_offset": len(user_line.encode("utf-8")),
                            "replay_start_offset": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(
                [
                    "last",
                    session_id,
                    "--state-dir",
                    str(state_dir),
                    "--root",
                    str(root),
                    "--interval",
                    "0",
                    "--idle",
                    "0",
                    "--timeout",
                    "1",
                ],
                env=fake_tmux_env(fake_bin, fake_log),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payloads = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([payload["event"] for payload in payloads], ["user", "tool_use", "tool_result", "user", "done", "metrics"])
            self.assertNotIn("answer", payloads[-2])
            state = ctc.read_bridge_state(state_path)
            self.assertIsNone(state["active_turn"])
            self.assertEqual(state["completed_turns"][0]["turn_id"], "turn_cli_cancelled")

    def test_cli_last_two_replays_completed_then_active_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_log = Path(tmp) / "tmux.log"
            write_fake_tmux(fake_bin)
            state_dir = Path(tmp) / "state"
            root = Path(tmp) / "claude"
            cwd = Path(tmp) / "project"
            cwd.mkdir()
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = ctc.project_transcript_dir(root, cwd) / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            q1 = json_line({"sessionId": session_id, "type": "user", "message": {"content": "done q"}})
            a1 = json_line({"sessionId": session_id, "type": "assistant", "message": {"content": [{"type": "text", "text": "done a"}]}})
            q2 = json_line({"sessionId": session_id, "type": "user", "message": {"content": "active q"}})
            a2 = json_line({"sessionId": session_id, "type": "assistant", "message": {"content": [{"type": "text", "text": "active a"}]}})
            transcript.write_text(q1 + a1 + q2 + a2, encoding="utf-8")
            q1_done = len((q1 + a1).encode("utf-8"))
            q2_start = q1_done
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "tmux_session": ctc.web_tmux_session_name(session_id),
                        "cwd": str(cwd),
                        "transcript": ctc.transcript_file_state(transcript, q1_done),
                        "completed_turns": [
                            {
                                "turn_id": "turn_done_cli",
                                "session_id": session_id,
                                "answer": "done a",
                                "anchor_start_offset": 0,
                                "completed_offset": q1_done,
                                "transcript": ctc.transcript_file_state(transcript, q1_done),
                            }
                        ],
                        "active_turn": {
                            "turn_id": "turn_active_cli2",
                            "stream_state": "active",
                            "claude_state": "working",
                            "prompt_preview": "active q",
                            "before_send_wall_time_utc": "2026-05-18T00:00:00Z",
                            "before_send_transcript": ctc.transcript_file_state(transcript, q1_done),
                            "anchor_start_offset": q2_start,
                            "anchor_end_offset": q2_start + len(q2.encode("utf-8")),
                            "replay_start_offset": q2_start,
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(
                [
                    "last",
                    session_id,
                    "--state-dir",
                    str(state_dir),
                    "--root",
                    str(root),
                    "--last",
                    "2",
                    "--interval",
                    "0",
                    "--idle",
                    "0",
                    "--timeout",
                    "1",
                ],
                env=fake_tmux_env(fake_bin, fake_log),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payloads = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(
                [payload["event"] for payload in payloads],
                ["user", "assistant_text", "done", "metrics", "user", "assistant_text", "done", "metrics"],
            )
            self.assertEqual(payloads[0]["turn_id"], "turn_done_cli")
            self.assertEqual(payloads[4]["turn_id"], "turn_active_cli2")
            self.assertIsNone(ctc.read_bridge_state(state_path)["active_turn"])

    def test_cli_replay_no_turns_returns_no_replayable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            state_path = ctc.web_session_state_path(session_id, state_dir)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"schema_version": 1, "session_id": session_id}), encoding="utf-8")

            result = run_cli(["last", session_id, "--state-dir", str(state_dir)])

            self.assertEqual(result.returncode, 4)
            self.assertIn("no_replayable_turns", result.stderr)


def json_line(payload):
    return json.dumps(payload, ensure_ascii=False) + "\n"


def run_cli(args, env=None):
    merged_env = os.environ.copy()
    merged_env["TERM"] = "xterm-256color"
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, str(Path(ctc.__file__).resolve()), *args],
        check=False,
        capture_output=True,
        text=True,
        env=merged_env,
    )


def write_fake_tmux(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "tmux"
    script.write_text(
        """#!/bin/sh
echo "$@" >> "$TMUX_FAKE_LOG"
if [ "$1" = "has-session" ]; then
  exit "${TMUX_FAKE_HAS_SESSION:-0}"
fi
if [ "$1" = "new-session" ] && [ -n "$TMUX_FAKE_TRANSCRIPT" ] && [ -n "$TMUX_FAKE_STARTUP_METADATA" ]; then
  mkdir -p "$(dirname "$TMUX_FAKE_TRANSCRIPT")"
  printf '{"sessionId":"%s","type":"system","message":{"content":"startup metadata"}}\\n' "$TMUX_FAKE_SESSION_ID" >> "$TMUX_FAKE_TRANSCRIPT"
  if [ -n "$TMUX_FAKE_INLINE_PROMPT" ]; then
    printf '{"sessionId":"%s","type":"user","message":{"content":"%s"}}\\n' "$TMUX_FAKE_SESSION_ID" "$TMUX_FAKE_INLINE_PROMPT" >> "$TMUX_FAKE_TRANSCRIPT"
    printf '{"sessionId":"%s","type":"assistant","message":{"content":[{"type":"text","text":"%s"}],"stop_reason":"end_turn"},"usage":{"input_tokens":1,"output_tokens":2},"context":{"remaining":12345},"model":"claude-sonnet-4-6"}\\n' "$TMUX_FAKE_SESSION_ID" "$TMUX_FAKE_ANSWER" >> "$TMUX_FAKE_TRANSCRIPT"
  fi
fi
if [ "$1" = "capture-pane" ]; then
  printf 'Done\\nclaude> \\n'
  exit 0
fi
if [ "$1" = "load-buffer" ]; then
  cat > "$TMUX_FAKE_LOG.buffer"
  exit 0
fi
if [ "$1" = "send-keys" ]; then
  if [ "$4" = "Enter" ] && [ -n "$TMUX_FAKE_TRANSCRIPT" ]; then
    mkdir -p "$(dirname "$TMUX_FAKE_TRANSCRIPT")"
    prompt="$(cat "$TMUX_FAKE_LOG.buffer" 2>/dev/null)"
    printf '{"sessionId":"%s","type":"user","message":{"content":"%s"}}\\n' "$TMUX_FAKE_SESSION_ID" "$prompt" >> "$TMUX_FAKE_TRANSCRIPT"
    printf '{"sessionId":"%s","type":"assistant","message":{"content":[{"type":"text","text":"%s"}],"stop_reason":"end_turn"},"usage":{"input_tokens":1,"output_tokens":2},"context":{"remaining":12345},"model":"claude-sonnet-4-6"}\\n' "$TMUX_FAKE_SESSION_ID" "$TMUX_FAKE_ANSWER" >> "$TMUX_FAKE_TRANSCRIPT"
  fi
  exit "${TMUX_FAKE_SEND_KEYS_STATUS:-0}"
fi
if [ "$1" = "list-sessions" ]; then
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    script.chmod(0o755)


def write_fake_claude(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)


def fake_tmux_env(fake_bin: Path, fake_log: Path, **extra):
    env = {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}", "TMUX_FAKE_LOG": str(fake_log)}
    env.update({key: str(value) for key, value in extra.items()})
    return env


if __name__ == "__main__":
    unittest.main()
