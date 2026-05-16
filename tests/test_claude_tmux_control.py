import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import claude_tmux_control as ctc


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.session_exists = False
        self.capture_text = ""

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

    def test_send_prompt_pastes_text_and_submits_enter(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)

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

    def test_send_prompt_can_leave_text_unsubmitted(self):
        runner = FakeRunner()
        controller = ctc.TmuxController(run=runner)

        controller.send_prompt("cc-test", "draft only", submit=False)

        self.assertNotIn((["tmux", "send-keys", "-t", "cc-test", "Enter"], {"check": True}), runner.calls)

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
    def test_parse_start_defaults_to_claude_command(self):
        args = ctc.parse_args(["start", "work"])

        self.assertEqual(args.command_name, "start")
        self.assertEqual(args.session, "work")
        self.assertEqual(args.claude_command, "claude")
        self.assertEqual(Path(args.cwd), Path.cwd())
        self.assertEqual(args.oauth_token_env, "CLAUDE_CODE_OAUTH_TOKEN")
        self.assertEqual(args.claude_args, [])

    def test_parse_start_collects_unknown_args_as_claude_args(self):
        args = ctc.parse_args(["start", "work", "--cwd", "/tmp/project", "--model", "opus", "--add-dir", "../other"])

        self.assertEqual(args.command_name, "start")
        self.assertEqual(args.cwd, "/tmp/project")
        self.assertEqual(args.claude_args, ["--model", "opus", "--add-dir", "../other"])

    def test_parse_start_strips_passthrough_separator(self):
        args = ctc.parse_args(["start", "work", "--cwd", "/tmp/project", "--", "--model", "opus"])

        self.assertEqual(args.claude_args, ["--model", "opus"])

    def test_parse_status_rejects_unknown_args(self):
        with self.assertRaises(SystemExit):
            ctc.parse_args(["status", "work", "--model", "opus"])

    def test_parse_send_accepts_multi_word_prompt(self):
        args = ctc.parse_args(["send", "work", "hello", "Claude"])

        self.assertEqual(args.command_name, "send")
        self.assertEqual(args.prompt, ["hello", "Claude"])

    def test_parse_launch_defaults_to_claude_command(self):
        args = ctc.parse_args(["launch", "work"])

        self.assertEqual(args.command_name, "launch")
        self.assertEqual(args.session, "work")
        self.assertEqual(args.claude_command, "claude")
        self.assertEqual(args.claude_args, [])

    def test_parse_launch_collects_unknown_args_as_claude_args(self):
        args = ctc.parse_args(["launch", "work", "--model", "opus"])

        self.assertEqual(args.command_name, "launch")
        self.assertEqual(args.session, "work")
        self.assertEqual(args.claude_args, ["--model", "opus"])

    def test_parse_chat_collects_unknown_args_as_claude_args(self):
        args = ctc.parse_args(["chat", "work", "--cwd", "/tmp/project", "--model", "opus"])

        self.assertEqual(args.command_name, "chat")
        self.assertEqual(args.session, "work")
        self.assertEqual(args.cwd, "/tmp/project")
        self.assertEqual(args.claude_args, ["--model", "opus"])

    def test_build_claude_command_adds_dangerous_skip_permissions(self):
        self.assertEqual(
            ctc.build_claude_command("claude --model opus"),
            "claude --model opus --dangerously-skip-permissions",
        )

    def test_build_claude_command_appends_passthrough_args(self):
        self.assertEqual(
            ctc.build_claude_command("claude", ["--model", "opus", "--add-dir", "../other"]),
            "claude --model opus --add-dir ../other --dangerously-skip-permissions",
        )

    def test_build_claude_command_does_not_duplicate_permission_flag(self):
        self.assertEqual(
            ctc.build_claude_command("claude --dangerously-skip-permissions"),
            "claude --dangerously-skip-permissions",
        )
        self.assertEqual(
            ctc.build_claude_command("claude --permission-mode bypassPermissions"),
            "claude --permission-mode bypassPermissions",
        )
        self.assertEqual(
            ctc.build_claude_command("claude", ["--permission-mode", "bypassPermissions"]),
            "claude --permission-mode bypassPermissions",
        )

    def test_oauth_environment_reads_configured_source_env(self):
        args = ctc.parse_args(["start", "work", "--oauth-token-env", "ACCOUNT_A_TOKEN"])

        self.assertEqual(
            ctc.claude_environment_from_args(args, environ={"ACCOUNT_A_TOKEN": "oauth-token"}),
            {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-token"},
        )

    def test_oauth_environment_is_empty_when_source_env_is_missing(self):
        args = ctc.parse_args(["start", "work", "--oauth-token-env", "ACCOUNT_A_TOKEN"])

        self.assertEqual(ctc.claude_environment_from_args(args, environ={}), {})

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

    def test_preflight_checks_custom_command_executable(self):
        args = ctc.parse_args(["start", "work", "--command", "custom-claude --flag"])

        def which(command):
            return "/usr/bin/tmux" if command == "tmux" else None

        error = ctc.check_runtime_dependencies(args, which=which)

        self.assertIn("custom-claude", error)

    def test_extract_shell_command_executable_skips_env_assignments(self):
        self.assertEqual(ctc._extract_shell_command_executable("TERM=xterm-256color claude --model opus"), "claude")
        self.assertEqual(ctc._extract_shell_command_executable("env TOKEN=abc claude --model opus"), "claude")

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

    def test_parse_stream_requires_session_name(self):
        args = ctc.parse_args(["stream", "work", "--timeout", "300", "--idle", "1.5"])

        self.assertEqual(args.command_name, "stream")
        self.assertEqual(args.session, "work")
        self.assertEqual(args.timeout, 300)
        self.assertEqual(args.idle, 1.5)

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

    def test_ignores_old_prompt_glyph_outside_bottom_status_area(self):
        screen = "\n".join(
            [
                "❯ 이전에 보낸 프롬프트",
                "Claude is still producing a long answer.",
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
            old.touch()
            new.touch()

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
        cwd = Path("/home/user/app")

        self.assertEqual(ctc.project_transcript_dir(root, cwd), Path("/home/user/.claude/projects/-home-user-app"))

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

    def test_format_latest_turn_ignores_internal_session_summary_prompt(self):
        events = [
            {"type": "user", "message": {"content": "real question"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "real answer"}]}},
            {"type": "user", "message": {"content": "당신은 Claude Code 세션 활동 요약 작성자입니다."}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": '{"skip": true}'}]}},
        ]

        self.assertEqual(ctc.format_latest_turn(events), "[user]\nreal question\n\n[assistant]\nreal answer")


class StreamTest(unittest.TestCase):
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
                {"event": "thinking", "timestamp": "t1", "text": "plan"},
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
                    "result": "done summary",
                },
                {"event": "assistant_text", "timestamp": "t4", "text": "final"},
            ],
        )

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


def json_line(payload):
    return json.dumps(payload, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    unittest.main()
