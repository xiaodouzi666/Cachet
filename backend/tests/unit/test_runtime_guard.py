"""Runtime adoption, bounded health recovery, and safe startup without real services."""

from copy import deepcopy
import os
from pathlib import Path
import signal
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

from scripts import runtime_guard as guard


@pytest.fixture(autouse=True)
def isolate_production_services(monkeypatch):
    """Every test must explicitly provide any external interaction it exercises."""
    monkeypatch.setattr(guard, "STOP", threading.Event())
    monkeypatch.setattr(guard, "command", lambda *a, **k: pytest.fail("Unexpected runtime command"))
    monkeypatch.setattr(guard, "processes", lambda: {})
    monkeypatch.setattr(guard.os, "killpg", lambda *a: pytest.fail("Unexpected process-group signal"))
    monkeypatch.setattr(guard.httpx, "Client", lambda *a, **k: pytest.fail("Unexpected HTTP request"))


def pane(number=100, dead=False):
    return {"id": "%" + str(number), "pid": number, "dead": dead}


class FakeRuntime:
    def __init__(self):
        self.panes = {name: [pane(i + 100)] for i, name in enumerate(guard.SESSIONS)}
        self.is_paused = False
        self.health_status = 200
        self.actions = []
        self.fail = set()

    def paused(self):
        return self.is_paused

    def inspect(self, name):
        return deepcopy(self.panes[name])

    def start(self, name):
        if name in self.fail:
            raise RuntimeError("Private detail must not reach logs")
        self.actions.append(("start", name))
        self.panes[name] = [pane(200)]

    def recover_dead(self, name, current):
        self.actions.append(("recover_dead", name))
        self.panes[name] = [pane(300)]

    def health(self):
        return self.health_status

    def restart_sglang(self, current):
        self.actions.append(("restart_sglang", current["pid"]))
        self.panes["sglang"] = [pane(400)]


def scenario():
    runtime, now = FakeRuntime(), [0.0]
    return runtime, now, guard.Guard(runtime, clock=lambda: now[0])


def test_adoption_does_not_restart_healthy_existing_processes():
    runtime, now, watcher = scenario()
    for stamp in (0, 20, 500, 10_000):
        now[0] = stamp
        state = watcher.tick()
        assert all(item["state"] == "running" for item in state["services"].values())
    assert runtime.actions == []


@pytest.mark.parametrize("name", guard.SESSIONS)
@pytest.mark.parametrize("dead", [False, True])
def test_missing_or_single_dead_session_is_restored(name, dead):
    runtime, _, watcher = scenario()
    runtime.panes[name] = [pane(dead=True)] if dead else None
    state = watcher.tick()
    assert runtime.actions == [("recover_dead" if dead else "start", name)]
    assert state["services"][name]["state"] == "started"


def test_short_health_failure_and_startup_grace_do_not_restart():
    runtime, now, watcher = scenario()
    runtime.health_status = None
    for stamp in (0, 180, 299):
        now[0] = stamp
        watcher.tick()
    assert runtime.actions == []
    runtime.health_status = 200
    now[0] = 300
    watcher.tick()
    runtime.health_status = 503
    for stamp in (400, 579):
        now[0] = stamp
        watcher.tick()
    assert runtime.actions == []
    now[0] = 580
    assert watcher.tick()["services"]["sglang"]["state"] == "restarted-after-health-failure"
    assert runtime.actions == [("restart_sglang", 100)]


def test_continuous_failure_after_grace_recovers_and_new_pane_gets_new_grace():
    runtime, now, watcher = scenario()
    runtime.health_status = None
    watcher.tick()
    now[0] = 300
    watcher.tick()
    assert runtime.actions == [("restart_sglang", 100)]
    for stamp in (320, 500, 619):
        now[0] = stamp
        watcher.tick()
    assert len(runtime.actions) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_failure_never_restarts_gpu(status):
    runtime, now, watcher = scenario()
    runtime.health_status = status
    for stamp in (0, 500, 50_000):
        now[0] = stamp
        assert watcher.tick()["services"]["sglang"]["state"] == "configuration-error"
    assert runtime.actions == []


def test_authentication_error_resets_the_continuous_failure_window():
    runtime, now, watcher = scenario()
    runtime.health_status = 503
    watcher.tick()
    runtime.health_status = 401
    now[0] = 250
    watcher.tick()
    runtime.health_status = 503
    for stamp in (300, 479):
        now[0] = stamp
        watcher.tick()
    assert runtime.actions == []
    now[0] = 480
    watcher.tick()
    assert runtime.actions == [("restart_sglang", 100)]


def test_pause_suppresses_agent_recovery_and_keeps_other_services_independent():
    runtime, _, watcher = scenario()
    runtime.is_paused = True
    runtime.panes["gpu-agent"] = None
    runtime.panes["app-checker"] = None
    state = watcher.tick()
    assert state["services"]["gpu-agent"]["state"] == "paused"
    assert runtime.actions == [("start", "app-checker")]
    assert runtime.is_paused


def test_one_helper_failure_does_not_skip_other_recovery_or_expose_details(capsys):
    runtime, _, watcher = scenario()
    runtime.panes = dict.fromkeys(guard.SESSIONS)
    runtime.fail.add("sglang")
    state = watcher.tick()
    assert state["services"]["sglang"]["state"] == "error"
    assert runtime.actions == [("start", "gpu-agent"), ("start", "app-checker")]
    assert "Private detail" not in capsys.readouterr().out


def test_multiple_panes_are_left_for_operator_review():
    runtime, _, watcher = scenario()
    runtime.panes["sglang"] = [pane(dead=True), pane(101)]
    assert watcher.tick()["services"]["sglang"]["state"] == "manual-review"
    assert runtime.actions == []


def test_checker_start_waits_for_docker_without_starting_or_claiming(monkeypatch, tmp_path):
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        raise RuntimeError("Daemon unavailable")

    monkeypatch.setattr(guard, "command", command)
    runtime = guard.Runtime(tmp_path)
    with pytest.raises(guard.NotReady):
        runtime.start("app-checker")
    assert len(calls) == 1 and calls[0][:3] == ["docker", "image", "inspect"]


def test_checker_starts_only_after_existing_verifier_image_is_ready(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(guard, "command", lambda args, **kwargs: calls.append(args))
    guard.Runtime(tmp_path).start("app-checker")
    assert calls[0][:3] == ["docker", "image", "inspect"]
    assert calls[1] == [str(tmp_path / ".venv/bin/python"), str(tmp_path / "scripts/application_check_worker.py"), "start"]


def test_waiting_boot_dependency_is_reported_and_retried_next_tick():
    runtime, _, watcher = scenario()
    runtime.panes["app-checker"] = None
    original = runtime.start
    runtime.start = lambda name: (_ for _ in ()).throw(guard.NotReady())
    assert watcher.tick()["services"]["app-checker"]["state"] == "waiting-for-dependency"
    runtime.start = original
    assert watcher.tick()["services"]["app-checker"]["state"] == "started"


def process(pid, ppid=0, pgid=100, sid=100, start=1):
    return {"pid": pid, "ppid": ppid, "pgid": pgid, "sid": sid,
            "start": start, "state": "S", "uid": os.getuid()}


def test_process_group_requires_exact_project_pane_and_all_descendants(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "project_process", lambda *a, **k: True)
    snapshot = {100: process(100), 101: process(101, ppid=100)}
    assert guard.verified_group(snapshot, 100, tmp_path) == {100: 1, 101: 1}
    with pytest.raises(RuntimeError):
        guard.verified_group({**snapshot, 999: process(999)}, 100, tmp_path)
    with pytest.raises(RuntimeError):
        guard.verified_group({**snapshot, 102: process(102, ppid=100, pgid=102)}, 100, tmp_path)
    monkeypatch.setattr(guard, "project_process", lambda *a, **k: False)
    with pytest.raises(RuntimeError):
        guard.verified_group(snapshot, 100, tmp_path)


@pytest.mark.parametrize("replacement", [
    {100: process(100, start=2)}, {999: process(999)},
    {101: process(101, pgid=101)},
])
def test_escalation_refuses_reused_foreign_or_escaped_processes(monkeypatch, replacement):
    monkeypatch.setattr(guard, "processes", lambda: replacement)
    with pytest.raises(RuntimeError):
        guard.Runtime.remaining(100, {100: 1, 101: 1})


def test_health_recovery_retains_pane_until_verified_group_exits(monkeypatch, tmp_path):
    runtime = guard.Runtime(tmp_path)
    inspected = iter([[pane()], [pane()], [pane(dead=True)]])
    actions = []
    monkeypatch.setattr(runtime, "inspect", lambda name: next(inspected))
    monkeypatch.setattr(guard, "processes", lambda: {})
    monkeypatch.setattr(guard, "verified_group", lambda *a: {100: 1, 101: 2})
    monkeypatch.setattr(guard, "command", lambda args, **kwargs: actions.append(tuple(args)))
    monkeypatch.setattr(guard.os, "killpg", lambda pid, sig: actions.append(("signal", pid, sig)))
    monkeypatch.setattr(runtime, "wait_group", lambda *a: actions.append(("wait",)))
    monkeypatch.setattr(runtime, "remaining", lambda *a: {})
    monkeypatch.setattr(runtime, "no_engine_residue", lambda: actions.append(("check-residue",)))
    monkeypatch.setattr(runtime, "start", lambda name: actions.append(("start", name)))
    runtime.restart_sglang(pane())
    assert actions == [
        ("tmux", "set-option", "-p", "-t", "%100", "remain-on-exit", "on"),
        ("signal", 100, signal.SIGTERM), ("wait",), ("check-residue",),
        ("tmux", "kill-session", "-t", "=cachet-sglang"), ("start", "sglang"),
    ]


def test_unproven_orphan_blocks_dead_pane_replacement(monkeypatch, tmp_path):
    runtime = guard.Runtime(tmp_path)
    monkeypatch.setattr(runtime, "no_engine_residue", lambda: (_ for _ in ()).throw(RuntimeError("Orphan")))
    monkeypatch.setattr(guard, "command", lambda *a, **k: pytest.fail("Must not signal or launch"))
    with pytest.raises(RuntimeError):
        runtime.recover_dead("sglang", pane(dead=True))


def test_single_instance_and_private_state(tmp_path):
    path = tmp_path / "guard.lock"
    with guard.instance_lock(path):
        with pytest.raises(BlockingIOError):
            with guard.instance_lock(path):
                pytest.fail("Duplicate guard")
    guard.private_write(tmp_path / "status.json", "{}\n")
    assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "status.json").stat().st_mode & 0o777 == 0o600


def test_unit_uses_absolute_escaped_paths_and_preserves_tmux():
    text = guard.unit_text(Path('/tmp/a b"$x%name'))
    assert 'WorkingDirectory=/tmp/a b"$x%%name\n' in text
    assert 'ExecStart=/usr/bin/env -- "/tmp/a b\\"$$x%%name/.venv/bin/python"' in text
    for directive in ("Restart=always", "RestartSec=5", "StartLimitIntervalSec=0",
                      "KillMode=process", "UMask=0077", "WantedBy=default.target"):
        assert directive in text
    assert "ExecStop" not in text and "EnvironmentFile" not in text
    with pytest.raises(ValueError):
        guard.unit_word("/tmp/invalid\npath")


@pytest.mark.parametrize("failure", ["linger", "verify", "enabled", "active", None])
def test_installer_verifies_linger_and_unit_before_activation(tmp_path, failure):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "loginctl":
            return SimpleNamespace(returncode=0, stdout="no" if failure == "linger" else "yes", stderr="")
        if args[0] == "systemd-analyze":
            assert Path(args[-1]).stat().st_mode & 0o777 == 0o600
            return SimpleNamespace(returncode=int(failure == "verify"), stdout="", stderr="Synthetic verifier error")
        result = "disabled" if failure == "enabled" else "enabled"
        active = "inactive" if failure == "active" else "active"
        return SimpleNamespace(returncode=0, stdout=active if "is-active" in args else result, stderr="")

    if failure:
        with pytest.raises(RuntimeError):
            guard.install(tmp_path / "repo", tmp_path / "home", run=run)
    else:
        observed = guard.install(tmp_path / "repo", tmp_path / "home", run=run)
        assert observed["boot_restoration_configured"] is True and observed["reboot_tested"] is False
    activation = [args for args in calls if args[0] == "systemctl"]
    assert bool(activation) is (failure not in {"linger", "verify"})
    if activation:
        assert calls[1][0] == "systemd-analyze"
        assert activation[0] == ["systemctl", "--user", "daemon-reload"]


def test_stop_cancels_cycle_before_missing_services_or_health():
    runtime, _, watcher = scenario()
    runtime.panes = dict.fromkeys(guard.SESSIONS)
    guard.STOP.set()
    assert watcher.tick()["services"] == {}
    assert runtime.actions == []


@pytest.mark.parametrize("name", guard.SESSIONS)
def test_stop_prevents_any_new_start(name, tmp_path):
    guard.STOP.set()
    with pytest.raises(RuntimeError, match="stopping"):
        guard.Runtime(tmp_path).start(name)


@pytest.mark.parametrize("stop_phase", ["term", "wait"])
def test_stop_during_recovery_cannot_escalate_or_start(monkeypatch, tmp_path, stop_phase):
    runtime, actions = guard.Runtime(tmp_path), []
    monkeypatch.setattr(runtime, "inspect", lambda name: [pane()])
    monkeypatch.setattr(guard, "verified_group", lambda *a: {100: 1})
    monkeypatch.setattr(guard, "command", lambda args, **kwargs: actions.append(tuple(args)))
    monkeypatch.setattr(runtime, "remaining", lambda *a: {100: process(100)})
    monkeypatch.setattr(runtime, "start", lambda name: pytest.fail("Stopped guard must not restart"))

    def signal_group(group, sig):
        actions.append(("signal", group, sig))
        if stop_phase == "term":
            guard.STOP.set()

    monkeypatch.setattr(guard.os, "killpg", signal_group)
    if stop_phase == "wait":
        monkeypatch.setattr(runtime, "wait_group", lambda *a: guard.STOP.set())
    with pytest.raises(RuntimeError, match="stopping"):
        runtime.restart_sglang(pane())
    assert [action for action in actions if action[0] == "signal"] == [("signal", 100, signal.SIGTERM)]
    assert not any("kill-session" in action for action in actions)


def test_group_identity_change_before_term_refuses_signal(monkeypatch, tmp_path):
    runtime, identities = guard.Runtime(tmp_path), iter([{100: 1}, {100: 2}])
    monkeypatch.setattr(runtime, "inspect", lambda name: [pane()])
    monkeypatch.setattr(guard, "verified_group", lambda *a: next(identities))
    monkeypatch.setattr(guard, "command", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="changed before signalling"):
        runtime.restart_sglang(pane())


@pytest.mark.parametrize("field,value", [("uid", -1), ("pgid", 999), ("sid", 999)])
def test_group_leader_must_match_uid_pgid_and_session(monkeypatch, tmp_path, field, value):
    monkeypatch.setattr(guard, "project_process", lambda *a, **k: True)
    leader = process(100)
    leader[field] = value
    with pytest.raises(RuntimeError):
        guard.verified_group({100: leader}, 100, tmp_path)


def test_dead_pane_replacement_refuses_changed_session(monkeypatch, tmp_path):
    runtime = guard.Runtime(tmp_path)
    monkeypatch.setattr(runtime, "inspect", lambda name: [pane(999)])
    with pytest.raises(RuntimeError, match="changed during recovery"):
        runtime.recover_dead("app-checker", pane(dead=True))


def test_pause_marker_is_read_without_deletion(tmp_path):
    marker = tmp_path / "logs/gpu-agent/paused"
    marker.parent.mkdir(parents=True)
    marker.write_text("Operator maintenance marker\n")
    assert guard.Runtime(tmp_path).paused()
    assert marker.read_text() == "Operator maintenance marker\n"


def fake_process_tree(monkeypatch, tmp_path):
    """A realistic proc filesystem fixture; never inspects or signals the GPU."""
    root = tmp_path / "repository"
    legacy = root / ".cache/v3-integrate"
    executable = tmp_path / "python-test-binary"
    executable.write_bytes(b"not an executable")
    for checkout in (root, legacy):
        (checkout / "scripts").mkdir(parents=True)
        (checkout / "scripts/serve_sglang.py").write_text("# identical trusted launcher\n")
        (checkout / ".venv-sglang/bin").mkdir(parents=True)
        (checkout / ".venv-sglang/bin/python").symlink_to(executable)
    proc = tmp_path / "proc"
    item = proc / "100"
    item.mkdir(parents=True)
    (item / "cwd").symlink_to(legacy, target_is_directory=True)
    (item / "exe").symlink_to(executable)
    (item / "cmdline").write_bytes(b"python\0-u\0scripts/serve_sglang.py\0--engine\0")
    (item / "comm").write_text("python\n")
    original_path = Path
    monkeypatch.setattr(guard, "Path", lambda arg: proc if str(arg) == "/proc" else original_path(arg))
    return root, legacy, item


def test_explicit_legacy_launcher_is_adopted_only_with_matching_script_and_executable(monkeypatch, tmp_path):
    root, legacy, item = fake_process_tree(monkeypatch, tmp_path)
    assert guard.project_process(100, root, launcher=True)
    (legacy / "scripts/serve_sglang.py").write_text("# changed legacy launcher\n")
    assert not guard.project_process(100, root, launcher=True)
    shutil.copyfile(root / "scripts/serve_sglang.py", legacy / "scripts/serve_sglang.py")
    (item / "exe").unlink()
    (item / "exe").symlink_to(tmp_path / "other-python")
    assert not guard.project_process(100, root, launcher=True)


def test_arbitrary_checkout_and_symlinked_legacy_are_not_adopted(monkeypatch, tmp_path):
    root, legacy, item = fake_process_tree(monkeypatch, tmp_path)
    unrelated = root / ".cache/other-worktree"
    unrelated.mkdir()
    (item / "cwd").unlink()
    (item / "cwd").symlink_to(unrelated, target_is_directory=True)
    assert not guard.project_process(100, root, launcher=True)
    moved = tmp_path / "relocated-legacy"
    legacy.rename(moved)
    legacy.symlink_to(moved, target_is_directory=True)
    (item / "cwd").unlink()
    (item / "cwd").symlink_to(legacy, target_is_directory=True)
    assert not guard.project_process(100, root, launcher=True)


@pytest.mark.parametrize("argv", [
    ["python", "-c", "print('unrelated')", "scripts/serve_sglang.py"],
    ["python", "other.py", "scripts/serve_sglang.py"],
    ["python", "scripts/serve_sglang.py", "--unrelated-mode"],
])
def test_script_name_in_unrelated_arguments_does_not_prove_ownership(monkeypatch, tmp_path, argv):
    root, _, item = fake_process_tree(monkeypatch, tmp_path)
    (item / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
    assert not guard.project_process(100, root, launcher=True)


def test_legacy_residue_blocks_new_engine_before_any_port_or_launch(monkeypatch, tmp_path):
    root, _, _ = fake_process_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(guard, "processes", lambda: {100: process(100)})
    monkeypatch.setattr(guard.socket, "socket", lambda *a: pytest.fail("Must first reject the known orphan"))
    with pytest.raises(RuntimeError, match="existing project inference process"):
        guard.Runtime(root).no_engine_residue()


@pytest.mark.parametrize("untrusted", ["launcher_hash", "executable"])
def test_untrusted_legacy_engine_is_not_safe_to_signal_but_still_blocks_duplicate(monkeypatch, tmp_path, untrusted):
    root, legacy, item = fake_process_tree(monkeypatch, tmp_path)
    if untrusted == "launcher_hash":
        (legacy / "scripts/serve_sglang.py").write_text("# changed legacy source\n")
    else:
        (item / "exe").unlink()
        (item / "exe").symlink_to(tmp_path / "untrusted-python")
    assert not guard.project_process(100, root, launcher=True)
    assert guard.possible_project_engine(100, root)
    monkeypatch.setattr(guard, "processes", lambda: {100: process(100)})
    monkeypatch.setattr(guard.socket, "socket", lambda *a: pytest.fail("Must reject before probing or starting"))
    with pytest.raises(RuntimeError, match="existing project inference process"):
        guard.Runtime(root).no_engine_residue()


def test_occupied_port_blocks_start_even_without_recognized_engine(monkeypatch, tmp_path):
    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, seconds):
            assert seconds == 0.5

        def connect_ex(self, address):
            assert address == ("127.0.0.1", 30000)
            return 0

    monkeypatch.setattr(guard.socket, "socket", Socket)
    with pytest.raises(RuntimeError, match="port is still occupied"):
        guard.Runtime(tmp_path).start("sglang")


def test_missing_credential_never_sends_health_request(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "value", lambda name: "")
    assert guard.Runtime(tmp_path).health() == 401


@pytest.mark.parametrize("existing,gate_exit", [(False, 1), (False, 0), (True, 1)])
def test_shared_sglang_start_entry_enforces_gate_before_tmux_creation(tmp_path, existing, gate_exit):
    """Execute only a copied shell entry with fake Python and fake tmux programs."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    source = Path(__file__).resolve().parents[3] / "scripts/start_sglang.sh"
    shutil.copyfile(source, root / "scripts/start_sglang.sh")
    (root / ".env").write_text("# no secrets or runtime configuration\n")
    scripts = {
        ".venv-sglang/bin/python": '#!/bin/sh\nexit 0\n',
        ".venv/bin/python": '#!/bin/sh\nprintf "gate %s\\n" "$*" >> "$TEST_LOG"\nexit "$GATE_EXIT"\n',
        "bin/tmux": '#!/bin/sh\nif [ "$1" = has-session ]; then exit "$SESSION_RC"; fi\nprintf "tmux %s\\n" "$*" >> "$TEST_LOG"\n',
    }
    for name, body in scripts.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o700)
    log = tmp_path / "actions.log"
    response = subprocess.run(["bash", str(root / "scripts/start_sglang.sh")],
                              env={"PATH": str(root / "bin") + os.pathsep + os.defpath,
                                   "TEST_LOG": str(log), "GATE_EXIT": str(gate_exit),
                                   "SESSION_RC": "0" if existing else "1"},
                              capture_output=True, text=True, timeout=5)
    actions = log.read_text().splitlines() if log.exists() else []
    if existing:
        assert response.returncode == 0 and actions == []
    elif gate_exit:
        assert response.returncode != 0
        assert actions == ["gate scripts/runtime_guard.py check-start"]
    else:
        assert response.returncode == 0
        assert actions[0] == "gate scripts/runtime_guard.py check-start"
        assert len(actions) == 2 and actions[1].startswith("tmux new-session ")
