"""Recheck generated examples by real HTTP in isolated, read-only Docker containers."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from backend.app.agents.tasks import TASKS, app_code
from backend.app.config import ROOT

IMAGE = "cachet-generated-app-check:v2"


def run_http(task, answer, *, full=True, draft=False):
    """Both modes check rejected-request recovery; full also waits a real 60 seconds."""
    source = app_code(answer, allow_prose=True)
    with TemporaryDirectory(prefix="cachet-public-code-") as temp:
        sandbox = Path(temp)
        # The model sees only its generated source, this verifier and installed public packages.
        (sandbox / "generated_app.py").write_text(source)
        (sandbox / "verify.py").write_bytes(
            (ROOT / "scripts/generated_app_http.py").read_bytes()
        )
        os.chmod(sandbox, 0o755)
        for p in sandbox.iterdir():
            p.chmod(0o444)
        name = "cachet-app-check-" + uuid4().hex
        try:
            process = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    "48",
                    "--memory",
                    "256m",
                    "--cpus",
                    "1",
                    "--user",
                    "65534:65534",
                    "--mount",
                    f"type=bind,src={sandbox},dst=/case,readonly",
                    IMAGE,
                    "python",
                    "-B",
                    "/case/verify.py",
                    task,
                    "full" if full else "quick",
                ],
                capture_output=True,
                text=True,
                timeout=80,
            )
            if process.returncode in (125, 126, 127):
                raise RuntimeError(
                    "Docker could not start the isolated verifier. Build the check image first."
                )
            if process.returncode:
                reason = (
                    process.stderr[-1500:]
                    or f"Container exit code {process.returncode}"
                )
                raise ValueError(
                    "Generated app failed its isolated HTTP contract: " + reason
                )
            result = json.loads(process.stdout)
        finally:
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, timeout=10
            )
    return {
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "http": result,
    }


def proposal_feedback(task, source):
    try:
        observed = run_http(task, source, full=False)["http"]
        return "HTTP smoke passed: " + json.dumps(observed, sort_keys=True)
    except (ValueError, subprocess.TimeoutExpired) as exc:
        lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
        # Feed only concise actual failure lines to the public recording.
        errors = [
            line
            for line in lines
            if line.startswith(
                (
                    "AssertionError",
                    "NameError",
                    "ImportError",
                    "KeyError",
                    "AttributeError",
                    "TypeError",
                    "SyntaxError",
                    "ModuleNotFoundError",
                    "RuntimeError",
                    "ValueError",
                )
            )
        ]
        return (
            "HTTP smoke failed. " + (" | ".join(errors) if errors else lines[-1])[:350]
        )


def assess(task, answer, *, draft=False):
    receipt = {"answer_sha256": hashlib.sha256(answer.encode()).hexdigest()}
    try:
        app_code(answer)
        receipt["requested_format_met"] = True
    except ValueError:
        receipt["requested_format_met"] = False
    try:
        receipt.update(run_http(task, answer, draft=draft), status="passed")
    except (ValueError, subprocess.TimeoutExpired) as exc:
        reason = str(exc).splitlines()
        receipt.update(
            status="failed", reason=(reason[-1] if reason else type(exc).__name__)[:400]
        )
    return receipt


def assess_record(track):
    task, turns = track["task_id"], track["turns"]
    final = assess(task, turns[-1]["text"])
    if final["status"] == "passed":
        return {"application_check": final, "verified_application_turn": len(turns)}
    for turn in reversed(turns[:-1]):
        if turn.get("http_feedback", "").startswith("HTTP smoke passed:"):
            prior = assess(task, turn["text"], draft=True)
            if prior["status"] == "passed":
                return {
                    "application_check": final,
                    "verified_application_turn": turn["turn"],
                    "earlier_application_check": prior,
                }
    return {"application_check": final, "verified_application_turn": None}


def reproduce(task, buy):
    from backend.app.replay.recordings import load

    track = load(task, buy)
    evidence = assess_record(track)
    for key, observed in evidence.items():
        recorded = track.get(key)
        if isinstance(observed, dict) and observed.get("status") == "failed":
            if (
                recorded.get("status") == "failed"
                and observed["answer_sha256"] == recorded["answer_sha256"]
            ):
                continue
        if observed != recorded:
            raise ValueError("Reproduced HTTP evidence differs from the recording")
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build",
        action="store_true",
        help="Build the public dependency image before checking",
    )
    parser.add_argument(
        "--require-passed",
        action="store_true",
        help="Require every final module to pass its full HTTP check and match the recording.",
    )
    args = parser.parse_args()
    if args.build:
        subprocess.run(
            ["docker", "build", "--tag", IMAGE, "-"],
            input=(ROOT / "scripts/generated_app.Dockerfile").read_bytes(),
            check=True,
        )
    from backend.app.replay.recordings import DIRECTORY

    manifest = DIRECTORY / "sha256.json"
    initial_index = manifest.read_bytes() if args.require_passed else None
    failures = []
    for task in TASKS:
        for buy in (True, False):
            decision = "buy" if buy else "compute"
            try:
                evidence = reproduce(task, buy)
            except ValueError as exc:
                if not args.require_passed:
                    raise
                failures.append(f"{task}/{decision}: {exc}")
                continue
            print(task, decision, json.dumps(evidence), flush=True)
            final = evidence["application_check"]
            if args.require_passed and final["status"] != "passed":
                failures.append(f"{task}/{decision}: {final['reason']}")
    if args.require_passed and manifest.read_bytes() != initial_index:
        failures.append("The recording index changed during the HTTP checks. Rerun on a stable archive.")
    if failures:
        parser.exit(1, "Final HTTP acceptance failed:\n" + "\n".join(failures) + "\n")


if __name__ == "__main__":
    main()
