import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

ROOT = Path('/repo')
CTC = str(ROOT / 'claude_tmux_control.py')
WORK = ROOT
STATE = Path('/tmp/ctc-docker-state')
PREFIX = 'ctc-csess-'
ENV = {**os.environ, 'TERM': 'xterm-256color'}

STATE.mkdir(parents=True, exist_ok=True)
results = []
created_sessions = set()
stop_watchdog = False

def preseed_claude_config():
    config = Path.home() / '.claude.json'
    existing = {}
    if config.exists():
        try:
            existing = json.loads(config.read_text())
        except json.JSONDecodeError:
            existing = {}
    projects = existing.get('projects') if isinstance(existing.get('projects'), dict) else {}
    project = projects.get(str(WORK)) if isinstance(projects.get(str(WORK)), dict) else {}
    project.update({
        'allowedTools': project.get('allowedTools') or [],
        'hasTrustDialogAccepted': True,
        'hasCompletedProjectOnboarding': True,
        'projectOnboardingSeenCount': max(int(project.get('projectOnboardingSeenCount') or 0), 4),
    })
    projects[str(WORK)] = project
    existing.update({
        'hasCompletedOnboarding': True,
        'lastOnboardingVersion': '2.1.147',
        'projects': projects,
    })
    config.write_text(json.dumps(existing, ensure_ascii=False, indent=2))

    settings_dir = Path.home() / '.claude'
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_file = settings_dir / 'settings.json'
    settings = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except json.JSONDecodeError:
            settings = {}
    settings['skipDangerousModePermissionPrompt'] = True
    settings_file.write_text(json.dumps(settings, ensure_ascii=False, indent=2))

    # Claude Code skips managed-settings approval in non-interactive print mode.
    # Running it once fills ~/.claude/remote-settings.json so the later tmux TUI
    # sees the same settings as already cached and does not prompt.
    preflight = run(['claude', '-p', 'Reply with exactly: ctc-docker-preflight-ok'], timeout=90)
    if preflight.returncode != 0 or 'ctc-docker-preflight-ok' not in preflight.stdout:
        raise RuntimeError(
            'Claude Code preflight failed: '
            f'rc={preflight.returncode}, stdout={preflight.stdout[:200]!r}, stderr={preflight.stderr[:200]!r}'
        )
    remote_settings = Path.home() / '.claude' / 'remote-settings.json'
    if not remote_settings.exists():
        print('[WARN] remote-settings cache was not created by preflight', flush=True)

def run(args, timeout=180):
    return subprocess.run(args, cwd=ROOT, env=ENV, text=True, capture_output=True, timeout=timeout)

def parse_jsonl(text):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({'_raw': line})
    return rows

def summarize(rows):
    return {
        'events': [r.get('event') or r.get('_raw', '?') for r in rows],
        'answers': [r.get('answer') for r in rows if r.get('event') == 'done' and 'answer' in r],
        'done': sum(1 for r in rows if r.get('event') == 'done'),
        'metrics': sum(1 for r in rows if r.get('event') == 'metrics'),
        'tools': [r.get('name') for r in rows if r.get('event') == 'tool_use'],
    }

def record(name, ok, detail):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

def kill_all_ctc_tmux():
    out = subprocess.run("tmux list-sessions -F '#{session_name}' 2>/dev/null || true", shell=True, text=True, capture_output=True).stdout
    for session in out.splitlines():
        if session.startswith(PREFIX):
            subprocess.run(['tmux', 'kill-session', '-t', session], text=True, capture_output=True)

def trust_prompt_watchdog():
    while not stop_watchdog:
        out = subprocess.run("tmux list-sessions -F '#{session_name}' 2>/dev/null || true", shell=True, text=True, capture_output=True).stdout
        for session in out.splitlines():
            if not session.startswith(PREFIX):
                continue
            screen = subprocess.run(['tmux', 'capture-pane', '-p', '-t', session, '-S', '-80'], text=True, capture_output=True).stdout
            if 'Yes, I trust this folder' in screen or 'Enter to confirm' in screen:
                subprocess.run(['tmux', 'send-keys', '-t', session, 'Enter'], text=True, capture_output=True)
        time.sleep(0.5)

def stream(session_id, prompt, timeout=180, extra=None):
    args = [CTC, 'stream', '--cwd', str(WORK), '--state-dir', str(STATE), '--session-id', session_id, '--interval', '2', '--timeout', str(timeout)]
    if extra:
        args.extend(extra)
    args.append(prompt)
    return run(args, timeout=timeout + 60)

def last(session_id, n=1, timeout=120):
    return run([CTC, 'last', session_id, '--state-dir', str(STATE), '--last', str(n), '--interval', '2', '--timeout', str(timeout)], timeout=timeout + 60)

def cancel(session_id):
    return run([CTC, 'cancel', session_id, '--state-dir', str(STATE)], timeout=30)

def kill_session(session_id):
    return run([CTC, 'kill', PREFIX + session_id], timeout=30)

def state_path(session_id):
    return STATE / 'sessions' / f'{session_id}.json'

def backdate_active_heartbeat(session_id):
    path = state_path(session_id)
    payload = json.loads(path.read_text())
    active = payload.get('active_turn') or {}
    active['heartbeat_at'] = '2000-01-01T00:00:00Z'
    payload['active_turn'] = active
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

watchdog = threading.Thread(target=trust_prompt_watchdog, daemon=True)
watchdog.start()
kill_all_ctc_tmux()
preseed_claude_config()

try:
    # 1. normal stream
    sid = str(uuid.uuid4()); created_sessions.add(sid)
    p = stream(sid, 'Reply with exactly: docker-one-ok')
    rows = parse_jsonl(p.stdout); s = summarize(rows)
    record('normal stream new session', p.returncode == 0 and s['answers'] and 'docker-one-ok' in s['answers'][-1] and s['metrics'] == 1, f'rc={p.returncode}, {s}, stderr={p.stderr[:180]}')

    # 2. same session next turn
    p = stream(sid, 'Reply with exactly: docker-two-ok')
    rows = parse_jsonl(p.stdout); s = summarize(rows)
    record('same session second stream', p.returncode == 0 and s['answers'] and 'docker-two-ok' in s['answers'][-1], f'rc={p.returncode}, {s}, stderr={p.stderr[:180]}')

    # 3. replay last turns
    p = last(sid, 2)
    rows = parse_jsonl(p.stdout); s = summarize(rows)
    record('last --last 2 completed replay', p.returncode == 0 and s['done'] == 2 and s['metrics'] == 2 and 'docker-one-ok' in ''.join(map(str, s['answers'])) and 'docker-two-ok' in ''.join(map(str, s['answers'])), f'rc={p.returncode}, {s}, stderr={p.stderr[:180]}')

    # 4. crash client process, attach with last, and block concurrent prompt
    crash_prompt = 'Use Bash tool to run this exact command: sleep 8; echo docker-crash-attach-ok. Do not answer until the command finishes. Then reply with exactly the command output.'
    proc = subprocess.Popen([CTC, 'stream', '--cwd', str(WORK), '--state-dir', str(STATE), '--session-id', sid, '--interval', '2', '--timeout', '90', crash_prompt], cwd=ROOT, env=ENV, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(4)
    proc.kill()
    out, err = proc.communicate(timeout=10)
    p_block = stream(sid, 'Reply with exactly: should-not-send-during-crash-active', timeout=20)
    record('new prompt blocked after crashed client while active', p_block.returncode != 0 and 'turn_in_progress' in p_block.stderr, f'rc={p_block.returncode}, stderr={p_block.stderr[:180]}')
    p = last(sid, 1, timeout=90)
    rows = parse_jsonl(p.stdout); s = summarize(rows)
    record('last attaches after crashed client', p.returncode == 0 and s['answers'] and 'docker-crash-attach-ok' in s['answers'][-1], f'crash_rc={proc.returncode}, last_rc={p.returncode}, {s}, stderr={p.stderr[:180]}')

    # 5. SIGINT client disconnect then last attach
    int_prompt = 'Use Bash tool to run this exact command: sleep 8; echo docker-sigint-attach-ok. Do not answer until the command finishes. Then reply with exactly the command output.'
    proc = subprocess.Popen([CTC, 'stream', '--cwd', str(WORK), '--state-dir', str(STATE), '--session-id', sid, '--interval', '2', '--timeout', '90', int_prompt], cwd=ROOT, env=ENV, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(4)
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=15)
    p = last(sid, 1, timeout=90)
    rows = parse_jsonl(p.stdout); s = summarize(rows)
    record('last attaches after SIGINT disconnect', proc.returncode == 130 and p.returncode == 0 and s['answers'] and 'docker-sigint-attach-ok' in s['answers'][-1], f'int_rc={proc.returncode}, last_rc={p.returncode}, {s}, stderr={p.stderr[:180]}')

    # 6. cancel active tool turn then continue same session
    cancel_sid = str(uuid.uuid4()); created_sessions.add(cancel_sid)
    proc = subprocess.Popen([CTC, 'stream', '--cwd', str(WORK), '--state-dir', str(STATE), '--session-id', cancel_sid, '--interval', '2', '--timeout', '90', 'Use Bash tool to run this exact command: sleep 30; echo docker-cancel-should-not-finish. Do not answer until the command finishes.'], cwd=ROOT, env=ENV, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(5)
    pc = cancel(cancel_sid)
    try:
        out, err = proc.communicate(timeout=45)
        rows = parse_jsonl(out); s = summarize(rows); rc = proc.returncode; stderr = err
    except subprocess.TimeoutExpired:
        proc.kill(); out, err = proc.communicate(timeout=10); rows = []; s = summarize(rows); rc = proc.returncode; stderr = err
    if rc != 0 or not any(r.get('event') == 'done' for r in rows):
        p = last(cancel_sid, 1, timeout=90)
        rows = parse_jsonl(p.stdout); s = summarize(rows); rc = p.returncode; stderr = p.stderr
    combined_answers = '\n'.join(answer or '' for answer in s['answers'])
    record(
        'cancel active turn reaches done metrics without command output',
        pc.returncode == 0
        and rc == 0
        and s['done'] == 1
        and s['metrics'] == 1
        and 'docker-cancel-should-not-finish' not in combined_answers,
        f'cancel_rc={pc.returncode}, final_rc={rc}, {s}, stderr={stderr[:180]}',
    )
    p = stream(cancel_sid, 'Reply with exactly: docker-after-cancel-ok')
    rows = parse_jsonl(p.stdout); s = summarize(rows)
    record('same session continues after cancel', p.returncode == 0 and s['answers'] and 'docker-after-cancel-ok' in s['answers'][-1], f'rc={p.returncode}, {s}, stderr={p.stderr[:180]}')

    # 7. kill completed tmux then resume
    pk = kill_session(cancel_sid)
    p = stream(cancel_sid, 'Reply with exactly: docker-after-kill-ok')
    rows = parse_jsonl(p.stdout); s = summarize(rows)
    record('kill completed tmux then resume same session', p.returncode == 0 and s['answers'] and 'docker-after-kill-ok' in s['answers'][-1], f'kill_rc={pk.returncode}, stream_rc={p.returncode}, {s}, stderr={p.stderr[:180]}')

    # 8. reap completed tmux then resume
    reap_sid = str(uuid.uuid4()); created_sessions.add(reap_sid)
    p = stream(reap_sid, 'Reply with exactly: docker-before-reap-ok')
    rows = parse_jsonl(p.stdout); s = summarize(rows)
    ok_first = p.returncode == 0 and s['answers'] and 'docker-before-reap-ok' in s['answers'][-1]
    pr = run([CTC, 'reap', '--state-dir', str(STATE), '--idle-seconds', '0', '--prefix', PREFIX], timeout=30)
    p = stream(reap_sid, 'Reply with exactly: docker-after-reap-ok')
    rows = parse_jsonl(p.stdout); s = summarize(rows)
    record('reap completed tmux then resume same session', ok_first and pr.returncode == 0 and p.returncode == 0 and s['answers'] and 'docker-after-reap-ok' in s['answers'][-1], f'reap_rc={pr.returncode}, stream_rc={p.returncode}, {s}, reap_out={pr.stdout[:180]}, stderr={p.stderr[:180]}')

    # 9. tmux killed mid-turn: immediate request blocked, stale backdate recovers
    stale_sid = str(uuid.uuid4()); created_sessions.add(stale_sid)
    proc = subprocess.Popen([CTC, 'stream', '--cwd', str(WORK), '--state-dir', str(STATE), '--session-id', stale_sid, '--interval', '2', '--timeout', '90', 'Use Bash tool to run this exact command: sleep 30; echo docker-killed-active. Do not answer until the command finishes.'], cwd=ROOT, env=ENV, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(5)
    subprocess.run(['tmux', 'kill-session', '-t', PREFIX + stale_sid], text=True, capture_output=True)
    proc.kill(); proc.communicate(timeout=10)
    p_immediate = stream(stale_sid, 'Reply with exactly: should-block-after-fresh-killed-tmux', timeout=20)
    backdate_active_heartbeat(stale_sid)
    p = stream(stale_sid, 'Reply with exactly: docker-stale-recovered-ok', timeout=180)
    rows = parse_jsonl(p.stdout); s = summarize(rows)
    record('fresh killed active blocks then stale state recovers', p_immediate.returncode != 0 and 'turn_in_progress' in p_immediate.stderr and p.returncode == 0 and s['answers'] and 'docker-stale-recovered-ok' in s['answers'][-1], f'immediate_rc={p_immediate.returncode}, recovery_rc={p.returncode}, {s}, immediate_err={p_immediate.stderr[:160]}, recovery_err={p.stderr[:160]}')

finally:
    stop_watchdog = True
    kill_all_ctc_tmux()

print('\nSUMMARY')
for name, ok, _ in results:
    print(f"- {'PASS' if ok else 'FAIL'} {name}")
print('STATE=' + str(STATE))
if not all(ok for _, ok, _ in results):
    raise SystemExit(1)
