# Module Extraction Refactor Plan

> Status: accepted planning draft pending implementation.
> Scope: plan only. Do not change runtime behavior while executing this plan.

## Goal

Split the current `claude_tmux_control.py` monolith into realistic Python modules without changing the CLI contract, JSONL event contract, state schema, tmux session naming, or import compatibility.

The target is not a speculative framework. The target is a smaller set of modules that match today's responsibilities:

- tmux process control
- Claude launch and environment handling
- transcript parsing and transcript path resolution
- local bridge state and locks
- streaming and completion detection
- high-level web session orchestration
- CLI parsing and dispatch

## Evidence From Current Code

`claude_tmux_control.py` is currently about 4.7k lines and contains multiple responsibilities in one file.

| Responsibility | Current symbols |
| --- | --- |
| CLI and dispatch | `parse_args`, `main`, `_run_command`, `_print_*` |
| tmux adapter | `TmuxController`, `ScreenCaptureController`, `RenderedScreenFollower` |
| Claude launch/env | `claude_args_from_options`, `build_claude_command`, `build_initial_claude_command`, `claude_environment_from_args`, `preseed_claude_project_trust` |
| high-level web session | `prepare_high_level_stream`, `run_high_level_turn`, `prepare_high_level_attach`, `run_high_level_cancel`, `run_high_level_replay` |
| local state and locks | `read_bridge_state`, `_write_high_level_state`, `mutate_high_level_state`, `exclusive_file_lock` |
| transcript paths and reads | `resolve_high_level_transcript`, `project_transcript_dir`, `read_transcript_events` |
| stream normalization | already partly extracted to `transcript_events.py` and re-exported by `_use_transcript_events_module()` |
| pricing and usage | `estimate_turn_cost`, `load_pricing_table`, `aggregate_turn_usage`, `cost_totals_from_completed_turns` |

The current tests import `claude_tmux_control as ctc` and directly call many public and underscored symbols. That test shape is treated as a compatibility inventory for this refactor.

## Non-Goals

- Do not convert this repository to a package directory in the first extraction.
- Do not remove `claude_tmux_control.py`.
- Do not change the console script targets from `claude_tmux_control:main`.
- Do not redesign the state schema.
- Do not change event names, payload keys, event ordering, offsets, or `event_id` construction.
- Do not rewrite parser behavior while moving leaf helpers.
- Do not combine module extraction with new feature work.

## Compatibility Contract

These surfaces must stay stable through every phase:

| Surface | Must remain stable |
| --- | --- |
| Console scripts | `ctc` and `claude-tmux-control` still resolve to `claude_tmux_control:main` |
| Script execution | `python claude_tmux_control.py ...` still works |
| Import facade | `import claude_tmux_control as ctc` still exposes the currently tested symbols |
| CLI behavior | command names, flags, help behavior, stderr strings, and exit codes remain behavior-compatible |
| High-level JSONL stream | `done` is emitted before `metrics`; `event_id`, `source_offset`, `source_end_offset`, and `block_index` stay stable |
| State paths | `~/.cache/claude-tmux-control/sessions/*.json` and `locks/*.lock` remain unchanged |
| tmux names | high-level sessions remain `ctc-csess-<session_id>` |
| Transcript resolution | high-level transcript lookup does not fall back to unrelated global latest transcripts |
| Claude launch | default permission behavior, `--model`, `--claude-args`, `.ctc.env`, `--env-file`, `--env`, and `--oauth-token-env` behavior remain unchanged |
| Dataclass identity | `ScreenStatus` and `TranscriptRecord` do not split into competing class identities |

## Target Module Shape

Keep sibling modules at the repository root during this refactor. A package conversion can be considered later only after the facade and packaging contract are explicitly migrated.

| Module | Responsibility |
| --- | --- |
| `claude_tmux_control.py` | thin entrypoint and compatibility re-export facade |
| `ctc_cli.py` | `parse_args`, `main`, command dispatch, stdout/stderr formatting |
| `ctc_types.py` | shared protocols, exceptions, and stable constants that are not domain-specific |
| `ctc_tmux.py` | `TmuxController`, rendered screen following, low-level tmux helpers |
| `ctc_launch.py` | Claude command construction, launch args, env files, dependency checks, project trust preseed |
| `transcript_events.py` | transcript event parsing and normalization helpers already extracted today |
| `ctc_transcripts.py` | transcript discovery, project transcript directory encoding, JSONL file reads, session-id matching |
| `ctc_state.py` | bridge/high-level session ids, state paths, file locks, bridge state read/write/mutate, turn state records |
| `ctc_pricing.py` | pricing table loading, pricing model selection, cost totals |
| `ctc_streaming.py` | screen readiness, transcript streaming loops, done/metrics payload builders |
| `ctc_bridge_sessions.py` | bridge-owned high-level client session orchestration: stream/ask/attach/cancel/replay and active-turn recovery |
| `ctc_reap.py` | idle-session reap logic once state and transcript helpers are separated |

`ScreenStatus` and `TranscriptRecord` have one canonical owner during this plan: `transcript_events.py`. If another module needs those names, it must import or re-export the canonical classes instead of defining new dataclasses.

## Dependency Direction

```text
claude_tmux_control.py
  -> ctc_cli
  -> compatibility re-exports

ctc_cli
  -> ctc_bridge_sessions
  -> ctc_reap
  -> ctc_tmux
  -> ctc_launch
  -> ctc_state
  -> ctc_streaming
  -> ctc_transcripts
  -> ctc_pricing

ctc_bridge_sessions
  -> ctc_launch
  -> ctc_state
  -> ctc_streaming
  -> ctc_transcripts
  -> ctc_tmux

ctc_streaming
  -> transcript_events
  -> ctc_state
  -> ctc_transcripts
  -> ctc_pricing
  -> ctc_types

ctc_reap
  -> ctc_state
  -> ctc_transcripts
  -> ctc_streaming
  -> ctc_tmux

ctc_state
  -> ctc_types

ctc_transcripts
  -> transcript_events
  -> ctc_types

ctc_tmux / ctc_launch / ctc_pricing / transcript_events
  -> no ctc_cli import
  -> no ctc_bridge_sessions import
```

Lower-level modules must not import `ctc_cli` or `ctc_bridge_sessions`. If a cycle appears between `ctc_state`, `ctc_streaming`, and `ctc_bridge_sessions`, do not solve it by making `ctc_state` depend on runtime streaming, controller, transcript anchoring, or metrics code. Keep `ctc_state` limited to persistence primitives, and put richer active-turn transitions in `ctc_streaming`, `ctc_bridge_sessions`, or a later `ctc_turn_state.py` transition module with explicit dependencies.

## Extraction Sequence

Each phase should be behavior-neutral and small enough to review independently.

## Test-First Refactor Safety Net

Before moving implementation code, add and run contract tests that describe the behavior the refactor must preserve. These tests are not new feature tests. They are characterization gates for the existing system.

Required pre-refactor additions:

| Gate | Purpose | Evidence |
| --- | --- | --- |
| Facade contract unit tests | Prove `import claude_tmux_control as ctc` still exposes the tested symbols after extraction | tests assert re-export presence and canonical class identity |
| Entrypoint/package contract tests | Prove `ctc` and `claude-tmux-control` still resolve to `claude_tmux_control:main` | tests read `pyproject.toml` |
| Refactor contract runner | Provide one command that runs the local no-Claude refactor safety gate | `scripts/refactor_contract_check.py --phase all` |
| Docker no-auth contract runner | Prove the installed package in the Docker image exposes the same CLI/import/test contract without requiring live Claude auth | `scripts/docker_refactor_contract_check.sh` |
| Optional Docker live smoke | Prove the Docker image can still run a real high-level stream when auth is available | `CTC_DOCKER_LIVE_SMOKE=1 scripts/docker_refactor_contract_check.sh` |
| Import-boundary checker | Prevent lower modules from importing `ctc_cli`, `ctc_bridge_sessions`, or the facade | `scripts/check_import_boundaries.py` |

Minimum pre-refactor verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/refactor_contract_check.py --phase all
scripts/docker_refactor_contract_check.sh
python3 scripts/check_import_boundaries.py
```

The Docker contract runner must skip Claude Code preflight by default and must not require token env. Live Claude stream smoke is valuable, but optional because it depends on external auth and network state.

Behavior-preserving acceptance criteria:

- `python claude_tmux_control.py --version`, `ctc --version`, and `claude-tmux-control --version` still work.
- `ctc --help` and major command help surfaces remain compatible.
- `import claude_tmux_control as ctc` exposes the moved-symbol inventory through the facade.
- `ctc.ScreenStatus is transcript_events.ScreenStatus` and `ctc.TranscriptRecord is transcript_events.TranscriptRecord`.
- high-level stream still emits `done` before `metrics`.
- stream progress events keep stable `event_id`, `source_offset`, `source_end_offset`, and `block_index`.
- fake-runner tests continue to cover new stream, session reuse, attach, cancel, replay/last, info/list/stats, reap, stale locks, timeout recovery, cwd mismatch, transcript scoping, and tool-result-not-final behavior.
- installed artifact smoke imports `claude_tmux_control`, `transcript_events`, and every new `ctc_*.py` module from outside the repository cwd.
- import-boundary smoke prevents lower modules from depending on `ctc_cli`, `ctc_bridge_sessions`, or `claude_tmux_control`.
- no-auth Docker smoke proves build/install/import/entrypoint basics.
- authenticated Docker live smoke proves real `ctc stream` when auth env is available.

### Phase 0: Baseline And Facade Contract

- Confirm the current test baseline before extraction.
- Add explicit compatibility tests for facade re-exports before moving more code.
- Keep `pyproject.toml` console scripts pointing at `claude_tmux_control:main`.

Verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/refactor_contract_check.py --phase 0
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
python3 -m py_compile claude_tmux_control.py scripts/stream_question.py scripts/web_chat_client.py scripts/check_docs.py
python3 scripts/check_docs.py
```

### Phase 1: Finish Transcript Event Boundary

`transcript_events.py` already exists and is re-exported by `_use_transcript_events_module()`. Make this boundary explicit before expanding the module set.

Move or delete duplicate transcript event implementations in `claude_tmux_control.py` only after facade identity tests prove that:

- `ctc.normalize_stream_events is transcript_events.normalize_stream_events`
- `ctc.analyze_turn_status is transcript_events.analyze_turn_status`
- `ctc.ScreenStatus is transcript_events.ScreenStatus`
- `ctc.TranscriptRecord is transcript_events.TranscriptRecord`

Targeted verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_claude_tmux_control.TranscriptTest tests.test_claude_tmux_control.StreamTest
```

### Phase 2: Extract Tmux Adapter

Move:

- `ScreenCaptureController`
- `SessionNotFoundError`
- `RenderedScreenFollower`
- `TmuxController`
- `follow_until_idle`

Keep `claude_tmux_control.py` re-exporting all moved symbols.

Targeted verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_claude_tmux_control.TmuxControllerTest \
  tests.test_claude_tmux_control.FollowUntilIdleTest \
  tests.test_claude_tmux_control.RenderedFollowerTest
```

### Phase 3: Extract Claude Launch And Environment Helpers

Move:

- `claude_args_from_options`
- `build_claude_command`
- `build_initial_claude_command`
- `_shell_ansi_c_quote`
- `_shell_join`
- `claude_environment_from_args`
- `_env_files_from_args`
- `read_env_file`
- `preseed_claude_project_trust`
- `check_runtime_dependencies`

This phase is a good early extraction because most functions are pure or have isolated filesystem/env behavior.

Targeted verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_claude_tmux_control.CliTest tests.test_claude_tmux_control.HighLevelStreamSetupTest
```

### Phase 4: Extract Transcript Path And File Helpers

Move:

- `find_latest_transcript`
- `resolve_transcript_path`
- `resolve_session_transcript_path`
- `resolve_status_transcript_path`
- `resolve_high_level_transcript`
- `project_transcript_dir`
- `transcript_matches_session_id`
- `transcript_matches_or_omits_session_id`
- `extract_transcript_session_id`
- `read_transcript_events`
- transcript file identity helpers

Do not weaken high-level transcript matching. It must stay scoped by cwd and session id.

Targeted verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_claude_tmux_control.TranscriptTest tests.test_claude_tmux_control.StreamTest
```

### Phase 5: Extract Pricing And Usage Helpers

Move:

- `load_pricing_table`
- `resolve_pricing_table_path`
- `select_pricing_model`
- `estimate_turn_cost`
- usage/context/model extraction helpers
- `usage_totals_from_completed_turns`
- `cost_totals_from_completed_turns`

Keep `DEFAULT_PRICING_TABLE`, installed data-file lookup, and cache behavior intact.

Targeted verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_claude_tmux_control.StreamTest tests.test_claude_tmux_control.HighLevelSessionInfoTest
```

### Phase 6: Extract State Store And Locking

Move:

- `validate_or_create_session_id`
- `web_tmux_session_name`
- `web_session_state_path`
- `web_session_lock_path`
- `session_state_path`
- low-level session state read/write helpers
- `exclusive_file_lock`
- `break_stale_lock`
- `process_exists`
- `read_bridge_state`
- `_write_high_level_state`
- `mutate_high_level_state`
- `build_pending_turn_state`
- `transcript_file_state`

Do not move runtime/controller/transcript/metrics-aware active-turn transitions in this phase. In particular, keep these outside `ctc_state.py` until streaming and client session orchestration boundaries are ready:

- `_mark_turn_done`
- `_cancel_high_level_timeout_turn`
- `finalize_recoverable_active_turn`
- `collect_recoverable_active_turn`
- recovery helpers that call transcript anchoring, screen status, controller methods, or metrics builders

This is still a high-risk extraction because state writes are shared across the system. Preserve `generation` conflict behavior and stale state protection exactly.

Targeted verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_claude_tmux_control.HighLevelStreamSetupTest
```

### Phase 7: Extract Streaming Loops

Move:

- `analyze_screen_status`
- `analyze_combined_status`
- `wait_until_ready`
- `stream_transcript_until_done`
- `stream_high_level_transcript_until_done`
- `normalize_stream_record` re-export use
- `high_level_done_payload`
- `high_level_metrics_payload`
- low-level stream done payload helpers
- stream-owned active-turn transitions such as `_mark_turn_anchor`, `_mark_turn_working`, `_mark_turn_done`, `_mark_turn_timeout`, `_mark_turn_timeout_cancelled`, `_mark_turn_interrupted`, and offset updates

The completion rule must remain unchanged: final answer requires transcript readiness, screen readiness, and a stable idle window.

Targeted verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_claude_tmux_control.ScreenStatusTest \
  tests.test_claude_tmux_control.StreamTest
```

### Phase 8: Extract Client Session Orchestration And Reap

Move client session orchestration after state and streaming APIs are stable:

- `prepare_high_level_stream`
- `prepare_high_level_attach`
- `run_high_level_turn`
- `run_high_level_cancel`
- `run_high_level_replay`
- `build_session_info_payload`
- `build_session_list_payload`
- `build_stats_payload`
- web-owned recovery helpers such as `recover_stale_active_turn`, `finalize_recoverable_active_turn`, `collect_recoverable_active_turn`, and `recoverable_turn_start_offset`
- replay helpers

Move reap after its shared dependencies are extracted:

- `reap_idle_sessions`
- high-level reap recovery helpers
- `session_is_working`

Targeted verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_claude_tmux_control.HighLevelStreamSetupTest \
  tests.test_claude_tmux_control.HighLevelSessionInfoTest \
  tests.test_claude_tmux_control.HighLevelAskTest \
  tests.test_claude_tmux_control.HighLevelCancelTest \
  tests.test_claude_tmux_control.HighLevelAttachTest \
  tests.test_claude_tmux_control.HighLevelReplayTest \
  tests.test_claude_tmux_control.ReapTest
```

### Phase 9: Extract CLI Parser And Dispatcher

Move `parse_args`, `main`, `_run_command`, and `_print_*` only after lower modules are stable.

`claude_tmux_control.py` remains:

```python
#!/usr/bin/env python3
from ctc_cli import main, parse_args
# Explicitly re-export the compatibility inventory from moved modules.

if __name__ == "__main__":
    raise SystemExit(main())
```

Prefer explicit re-export imports over wildcard once the compatibility inventory is known.

Targeted verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_claude_tmux_control.CliTest tests.test_claude_tmux_control.CliIntegrationTest
```

Final verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
python3 -m py_compile claude_tmux_control.py transcript_events.py ctc_*.py scripts/stream_question.py scripts/web_chat_client.py scripts/check_docs.py
python3 scripts/check_docs.py
```

## Packaging And Versioning

When new sibling modules are added, update `pyproject.toml`:

```toml
[tool.setuptools]
py-modules = [
  "claude_tmux_control",
  "transcript_events",
  "ctc_cli",
  "ctc_types",
  "ctc_tmux",
  "ctc_launch",
  "ctc_transcripts",
  "ctc_state",
  "ctc_pricing",
  "ctc_streaming",
  "ctc_bridge_sessions",
  "ctc_reap",
]
```

Because packaging files change, bump the patch version in `pyproject.toml` in the implementation change.

If command behavior, JSON fields, state schema, or documented user behavior changes, that is no longer a behavior-neutral refactor. Stop and write a separate behavior-change plan with the relevant docs updates.

## Review Rules For Implementation PRs

Each implementation PR should include:

- one extraction phase or a clearly bounded subset of one phase
- no behavior changes unless explicitly called out and separately reviewed
- facade re-export tests for moved public/tested symbols
- targeted tests for the moved area
- final full verification before merge

Reviewers should mark these as Major:

- removing or bypassing the `claude_tmux_control.py` facade
- changing console script entrypoints
- splitting dataclass identities such as `ScreenStatus` or `TranscriptRecord`
- changing JSONL event schema or `done`/`metrics` ordering
- changing state paths, state schema, generation conflict behavior, or active-turn recovery semantics
- allowing high-level transcript resolution to use unrelated global latest transcripts
- moving `parse_args` or `_run_command` before lower-level modules are stable
- combining feature work with extraction
- moving controller/transcript/metrics-aware active-turn transitions into `ctc_state.py`

## Subagent Review Log

### Round 1: Initial Independent Reviews

Three reviewers inspected the current worktree from different angles.

| Reviewer role | Major position |
| --- | --- |
| Architecture boundary | Keep `claude_tmux_control.py` as a compatibility facade; use sibling modules; prevent lower modules importing CLI/client session orchestration |
| CLI/API compatibility | Preserve console entrypoints, `import claude_tmux_control as ctc`, CLI flags, JSONL events, state paths, and tmux names |
| Testing/migration risk | Extract pure/helper modules first; high-level stream orchestration and CLI dispatch must be last |

Accepted Major constraints from Round 1:

- Do not start with package conversion.
- Do not remove the facade.
- Do not break `ctc.*` import compatibility.
- Do not change JSONL stream/state contracts while extracting modules.
- Do not split `ScreenStatus` or `TranscriptRecord` identity.
- Do not move high-level streaming or CLI dispatch first.

### Round 2: Consensus Synthesis

The accepted synthesis is:

1. Keep root-level sibling modules for this refactor.
2. Make `claude_tmux_control.py` the compatibility facade and script entrypoint.
3. Extract in this order: transcript boundary, tmux, launch/env, transcript paths, pricing, state, streaming, web/reap, CLI.
4. Keep every phase behavior-neutral with facade identity tests.
5. Treat package conversion as a later project, not part of this plan.

Final review results:

| Reviewer role | Result | Action |
| --- | --- | --- |
| Architecture boundary | `NO_MAJOR` | Minor clarification accepted: `ScreenStatus` and `TranscriptRecord` canonical ownership is now explicit |
| CLI/API compatibility | `NO_MAJOR` | No required changes |
| Testing/migration risk | `MAJOR_REMAINS` | Accepted and fixed: Phase 6 now moves only state primitives; active-turn transitions move in Phase 7/8; `ctc_state.py` must not depend on controller/transcript/metrics code |

### Round 3: Final Re-Review

The testing/migration reviewer re-reviewed the accepted fixes and returned `NO_MAJOR`.

Final consensus:

- Phase 6 is limited to state primitives.
- Runtime/controller/transcript/metrics-aware active-turn transitions are explicitly excluded from `ctc_state.py`.
- Active-turn transition ownership is deferred to Phase 7/8.
- `ScreenStatus` and `TranscriptRecord` canonical ownership is explicit.
- Review rules mark violations of these boundaries as Major.

### Round 4: Test-First Gate Re-Review

The user requested stronger proof that functionality moved unchanged before starting module extraction, including Docker verification. Three reviewers re-checked naming, test coverage, packaging, and Docker gates.

Accepted updates:

- Rename the planned high-level session module to `ctc_bridge_sessions.py`.
- Add facade compatibility contract tests for moved-symbol inventory and canonical transcript class identity.
- Add `scripts/refactor_contract_check.py` as the phase/local contract runner.
- Add `scripts/check_package_install.py` for installed-module and pricing data-file smoke.
- Add `scripts/docker_refactor_contract_check.sh` for no-auth Docker build/install/import/preseed/contract smoke.
- Add `scripts/check_import_boundaries.py` to enforce dependency direction with AST import checks.
- Split Docker scenarios into no-auth contract smoke and authenticated live smoke.
- Wire import-boundary and Docker contract checks into CI.

Final re-review results:

| Reviewer role | Result | Action |
| --- | --- | --- |
| Naming/scenario sufficiency | `NO_MAJOR` | Accepted `ctc_bridge_sessions.py`; minor wording cleanup can happen later |
| Docker/packaging | `NO_MAJOR` | No-auth and optional live Docker gates are sufficient |
| Test coverage/import boundaries | `NO_MAJOR` | Remaining import-boundary Major was fixed with `check_import_boundaries.py` and tests |

Consensus status: `NO_MAJOR`.
