"""Restore Cachet's tmux services without replacing healthy processes."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.config import value

UNIT = "cachet-runtime-guard.service"
DIRECTORY = ROOT / "logs/runtime-guard"
INTERVAL, STARTUP_GRACE, FAILURE_WINDOW = 20, 300, 180
STOP = threading.Event()
SESSIONS = {"sglang": "cachet-sglang", "gpu-agent": "cachet-gpu-agent", "app-checker": "cachet-app-checker"}


class NotReady(RuntimeError):
    """A boot dependency is not ready yet; leave work queued."""


def check_stopping():
    if STOP.is_set():
        raise RuntimeError("Runtime guard is stopping")


def log(message):
    # No subprocess arguments, response bodies, credentials, or exception text.
    print(datetime.now(timezone.utc).isoformat(timespec="seconds"), message, flush=True)


def command(args, *, timeout=15, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("Runtime command failed")
    return result


def private_write(path, text):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def instance_lock(path):
    with path.open("a") as stream:
        os.chmod(path, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def processes():
    """Read identities without exposing any command line or environment."""
    found = {}
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            text = (directory / "stat").read_text()
            fields = text[text.rindex(")") + 2:].split()
            found[int(directory.name)] = {
                "pid": int(directory.name), "ppid": int(fields[1]),
                "pgid": int(fields[2]), "sid": int(fields[3]),
                "start": int(fields[19]), "state": fields[0],
                "uid": directory.stat().st_uid,
                "comm": text[text.index("(") + 1:text.rindex(")")],
            }
        except (OSError, ValueError, IndexError):
            continue
    return found


def descendants(snapshot, pid):
    owned = {pid}
    while True:
        children = {number for number, item in snapshot.items() if item["ppid"] in owned}
        if children <= owned:
            return owned
        owned.update(children)


def launcher_arguments(argv, cwd):
    scripts = {b"scripts/serve_sglang.py", str(cwd / "scripts/serve_sglang.py").encode()}
    arguments = argv[1:]
    if arguments[:1] == [b"-u"]:
        arguments = arguments[1:]
    return bool(arguments and arguments[0] in scripts and arguments[1:] in ([], [b"--engine"]))


def possible_project_engine(pid, root):
    """Untrusted residue blocks a new engine even when it cannot be safely killed."""
    try:
        directory = Path("/proc") / str(pid)
        cwd = (directory / "cwd").resolve()
        if directory.stat().st_uid != os.getuid() or cwd not in {root, root / ".cache/v3-integrate"}:
            return False
        argv = [argument for argument in (directory / "cmdline").read_bytes().split(b"\0") if argument]
        return launcher_arguments(argv, cwd) or (directory / "comm").read_text().strip().startswith("sglang::")
    except (OSError, ValueError):
        return False


def project_process(pid, root, *, launcher=False):
    try:
        directory = Path("/proc") / str(pid)
        if directory.stat().st_uid != os.getuid():
            return False
        cwd = (directory / "cwd").resolve()
        if cwd != root:
            # The current deployed engine predates the main-checkout launch. Adopt
            # only this verified legacy worktree, never arbitrary child directories.
            legacy = root / ".cache/v3-integrate"
            if cwd != legacy or legacy.resolve() != legacy:
                return False
            entry = "scripts/serve_sglang.py"
            if hashlib.sha256((legacy / entry).read_bytes()).digest() != hashlib.sha256((root / entry).read_bytes()).digest():
                return False
            if (legacy / ".venv-sglang/bin/python").resolve() != (root / ".venv-sglang/bin/python").resolve():
                return False
        if (directory / "exe").resolve() != (root / ".venv-sglang/bin/python").resolve():
            return False
        argv = [argument for argument in (directory / "cmdline").read_bytes().split(b"\0") if argument]
        is_launcher = launcher_arguments(argv, cwd)
        if launcher:
            return is_launcher
        return is_launcher or (directory / "comm").read_text().strip().startswith("sglang::")
    except (OSError, ValueError):
        return False


def verified_group(snapshot, pane_pid, root):
    """Refuse to signal a group unless every member belongs to this exact pane."""
    parent = snapshot.get(pane_pid)
    if (not parent or parent["pgid"] != pane_pid or parent["sid"] != pane_pid
            or parent["uid"] != os.getuid() or not project_process(pane_pid, root, launcher=True)):
        raise RuntimeError("Cannot establish the project's SGLang process identity")
    owned = descendants(snapshot, pane_pid)
    members = {pid for pid, item in snapshot.items() if item["pgid"] == pane_pid}
    if members != owned or any(snapshot[pid]["uid"] != os.getuid() for pid in owned):
        raise RuntimeError("SGLang descendants do not form an isolated process group")
    return {pid: snapshot[pid]["start"] for pid in owned}


class Runtime:
    def __init__(self, root=ROOT, sessions=None):
        self.root = root.resolve()
        self.sessions = dict(SESSIONS if sessions is None else sessions)

    def paused(self):
        return (self.root / "logs/gpu-agent/paused").exists()

    def inspect(self, name):
        session = "=" + self.sessions[name]
        exists = command(["tmux", "has-session", "-t", session], check=False)
        if exists.returncode == 1:
            return None
        if exists.returncode:
            raise RuntimeError("Cannot inspect tmux server")
        result = command(["tmux", "list-panes", "-s", "-t", session, "-F", "#{pane_id} #{pane_pid} #{pane_dead}"])
        panes = []
        for row in result.stdout.splitlines():
            identifier, pid, dead = row.split()
            if not identifier.startswith("%") or not identifier[1:].isdigit() or dead not in {"0", "1"}:
                raise RuntimeError("Unexpected tmux pane metadata")
            panes.append({"id": identifier, "pid": int(pid), "dead": dead == "1"})
        return panes

    def no_engine_residue(self):
        # A lost tmux server can leave an orphan. Never start a duplicate GPU engine.
        for pid, item in processes().items():
            if item["state"] != "Z" and possible_project_engine(pid, self.root):
                raise RuntimeError("An existing project inference process needs operator review")
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", 30000)) == 0:
                raise RuntimeError("Inference port is still occupied")

    def start(self, name):
        check_stopping()
        if name == "sglang":
            self.no_engine_residue()
            args = [str(self.root / "scripts/start_sglang.sh")]
        else:
            if name == "app-checker":
                try:
                    command(["docker", "image", "inspect", "cachet-generated-app-check:v2", "--format", "{{.Id}}"], timeout=8)
                except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                    raise NotReady("Docker and the verifier image must be ready before claiming work") from exc
            script = "gpu_agent.py" if name == "gpu-agent" else "application_check_worker.py"
            args = [str(self.root / ".venv/bin/python"), str(self.root / "scripts" / script), "start"]
        check_stopping()
        command(args, timeout=60)

    def recover_dead(self, name, pane):
        check_stopping()
        if name == "sglang":
            self.no_engine_residue()
        if self.inspect(name) != [pane] or not pane["dead"]:
            raise RuntimeError("The dead pane changed during recovery")
        check_stopping()
        command(["tmux", "kill-session", "-t", "=" + self.sessions[name]])
        self.start(name)

    def health(self):
        key = value("SGLANG_API_KEY")
        if not key:
            return 401
        try:
            with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                return client.get("http://127.0.0.1:30000/health", headers={"Authorization": "Bearer " + key}).status_code
        except httpx.HTTPError:
            return None

    def restart_sglang(self, pane):
        check_stopping()
        if self.inspect("sglang") != [pane] or pane["dead"]:
            raise RuntimeError("SGLang pane changed during health recovery")
        group = pane["pid"]
        identities = verified_group(processes(), group, self.root)
        # Keep a dead pane until all GPU children exit, so gpu_agent cannot start
        # a replacement while the previous scheduler still owns GPU memory.
        check_stopping()
        command(["tmux", "set-option", "-p", "-t", pane["id"], "remain-on-exit", "on"])
        if self.inspect("sglang") != [pane]:
            raise RuntimeError("SGLang pane changed before signalling")
        if verified_group(processes(), group, self.root) != identities:
            raise RuntimeError("SGLang process group changed before signalling")
        check_stopping()
        os.killpg(group, signal.SIGTERM)
        self.wait_group(group, identities, 10)
        if self.remaining(group, identities):
            check_stopping()
            os.killpg(group, signal.SIGKILL)
            self.wait_group(group, identities, 5)
        if self.remaining(group, identities):
            raise RuntimeError("SGLang process group did not exit")
        self.no_engine_residue()
        current = self.inspect("sglang")
        if current is not None:
            if len(current) != 1 or current[0]["id"] != pane["id"] or not current[0]["dead"]:
                raise RuntimeError("Refusing to replace a changed SGLang session")
            check_stopping()
            command(["tmux", "kill-session", "-t", "=" + self.sessions["sglang"]])
        check_stopping()
        self.start("sglang")

    @staticmethod
    def remaining(group, identities):
        snapshot = processes()
        members = {pid: item for pid, item in snapshot.items() if item["pgid"] == group and item["state"] != "Z"}
        if any(pid not in identities or item["start"] != identities[pid] or item["uid"] != os.getuid()
               for pid, item in members.items()):
            raise RuntimeError("Process-group ownership changed; refusing escalation")
        if any(pid in snapshot and snapshot[pid]["state"] != "Z"
               and snapshot[pid]["start"] == start and snapshot[pid]["pgid"] != group
               for pid, start in identities.items()):
            raise RuntimeError("SGLang child escaped its process group")
        return members

    def wait_group(self, group, identities, seconds):
        deadline = time.monotonic() + seconds
        while self.remaining(group, identities) and time.monotonic() < deadline:
            check_stopping()
            time.sleep(0.2)


class Guard:
    def __init__(self, runtime, clock=time.monotonic):
        self.runtime, self.clock = runtime, clock
        self.identity, self.started_at, self.failed_since = None, None, None

    def tick(self):
        now, states, running_pane = self.clock(), {}, None
        for name in SESSIONS:
            if STOP.is_set():
                break
            try:
                if name == "gpu-agent" and self.runtime.paused():
                    states[name] = {"state": "paused"}
                    continue
                panes = self.runtime.inspect(name)
                if panes is None:
                    self.runtime.start(name)
                    states[name] = {"state": "started"}
                    log(name + ": restored missing session")
                elif len(panes) == 1 and panes[0]["dead"]:
                    self.runtime.recover_dead(name, panes[0])
                    states[name] = {"state": "started"}
                    log(name + ": restored dead session")
                elif len(panes) == 1:
                    states[name] = {"state": "running", "pane_pid": panes[0]["pid"]}
                    if name == "sglang":
                        running_pane = panes[0]
                else:
                    states[name] = {"state": "manual-review", "reason": "Unexpected pane count"}
                if name == "sglang" and states[name]["state"] == "started":
                    self.identity, self.started_at, self.failed_since = None, now, None
            except NotReady:
                states[name] = {"state": "waiting-for-dependency"}
                log(name + ": waiting for boot dependency")
            except Exception as exc:
                states[name] = {"state": "error", "error_type": type(exc).__name__}
                log(name + ": recovery deferred (" + type(exc).__name__ + ")")
        if running_pane is not None and not STOP.is_set():
            try:
                identity = (running_pane["id"], running_pane["pid"])
                if identity != self.identity:
                    self.identity, self.started_at, self.failed_since = identity, now, None
                status = self.runtime.health()
                states["sglang"]["health_status"] = status
                if status == 200:
                    self.failed_since = None
                elif status in {401, 403}:
                    self.failed_since = None
                    states["sglang"]["state"] = "configuration-error"
                else:
                    if self.failed_since is None:
                        self.failed_since = now
                    states["sglang"]["continuous_failure_seconds"] = round(now - self.failed_since, 1)
                    if now - self.started_at >= STARTUP_GRACE and now - self.failed_since >= FAILURE_WINDOW:
                        self.runtime.restart_sglang(running_pane)
                        self.identity, self.started_at, self.failed_since = None, now, None
                        states["sglang"]["state"] = "restarted-after-health-failure"
                        log("sglang: restored after sustained health failure")
            except Exception as exc:
                states["sglang"].update(state="error", error_type=type(exc).__name__)
                log("sglang: health recovery deferred (" + type(exc).__name__ + ")")
        return {"at": datetime.now(timezone.utc).isoformat(), "guard_pid": os.getpid(), "services": states}


def unit_word(value, *, execution=False):
    if any(character in value for character in "\n\r\0"):
        raise ValueError("Invalid unit path")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if execution:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def unit_path(value):
    # Path-valued directives do not use ExecStart's argv quoting. Their entire
    # right-hand side is the path; only systemd percent specifiers need escaping.
    if any(character in value for character in "\n\r\0"):
        raise ValueError("Invalid unit path")
    return value.replace("%", "%%")


def unit_text(root):
    root = root.resolve()
    executable = unit_word(str(root / ".venv/bin/python"), execution=True)
    script = unit_word(str(root / "scripts/runtime_guard.py"), execution=True)
    return (
        "[Unit]\nDescription=Cachet tmux runtime guard\nStartLimitIntervalSec=0\n\n"
        "[Service]\nType=simple\n"
        + "WorkingDirectory=" + unit_path(str(root)) + "\n"
        + "ExecStart=/usr/bin/env -- " + executable + " " + script + " serve\n"
        + "Restart=always\nRestartSec=5\nKillMode=process\nTimeoutStopSec=10\nUMask=0077\n"
        + "StandardOutput=" + unit_path("append:" + str(root / "logs/runtime-guard/guard.log")) + "\n"
        + "StandardError=inherit\n\n[Install]\nWantedBy=default.target\n"
    )


def install(root=ROOT, home=None, run=command):
    home = Path.home() if home is None else home
    lingering = run(["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"])
    if lingering.stdout.strip() != "yes":
        raise RuntimeError("User lingering must already be enabled for boot restoration; nothing installed")
    directory = root / "logs/runtime-guard"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    logfile = directory / "guard.log"
    logfile.touch(mode=0o600, exist_ok=True)
    logfile.chmod(0o600)
    unit = unit_text(root)
    with TemporaryDirectory(prefix="cachet-guard-unit-") as temporary:
        candidate = Path(temporary) / UNIT
        private_write(candidate, unit)
        verified = run(["systemd-analyze", "--user", "verify", str(candidate)], check=False)
        if verified.returncode:
            private_write(directory / "unit-verify.log", verified.stderr)
            raise RuntimeError("User unit verification failed; nothing installed")
    target = home / ".config/systemd/user" / UNIT
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_write(target, unit)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", UNIT])
    enabled = run(["systemctl", "--user", "is-enabled", UNIT]).stdout.strip()
    active = run(["systemctl", "--user", "is-active", UNIT]).stdout.strip()
    if enabled != "enabled" or active != "active":
        raise RuntimeError("Runtime guard installation did not become enabled and active")
    return {"unit": UNIT, "enabled": True, "active": True, "linger": True,
            "boot_restoration_configured": True, "reboot_tested": False}


def serve():
    with instance_lock(DIRECTORY / "guard.lock"):
        guard = Guard(Runtime())
        log("Runtime guard started; healthy tmux sessions are retained")
        while not STOP.is_set():
            try:
                private_write(DIRECTORY / "status.json", json.dumps(guard.tick(), indent=2) + "\n")
            except Exception as exc:
                log("Runtime guard cycle deferred (" + type(exc).__name__ + ")")
            STOP.wait(INTERVAL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "serve", "status", "check-start"])
    args = parser.parse_args()
    os.chdir(ROOT)
    os.umask(0o077)
    if args.action == "check-start":
        Runtime().no_engine_residue()
        return
    DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.action == "install":
        print(json.dumps(install(), indent=2))
    elif args.action == "status":
        path = DIRECTORY / "status.json"
        active = command(["systemctl", "--user", "is-active", UNIT], check=False)
        print(json.dumps({"unit": UNIT, "active": active.stdout.strip(),
                          "last_observation": json.loads(path.read_text()) if path.exists() else None}, indent=2))
    else:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: STOP.set())
        serve()


if __name__ == "__main__":
    main()
