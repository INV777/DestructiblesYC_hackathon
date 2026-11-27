"""Run workers as separate processes, so a crash in one is a real crash.

The console never runs the pipeline inside the web server. Each run (and each settlement) is
`python main.py run ...` in its own process, exactly what you would type at a terminal. Killing it
from the console is a real SIGKILL (TerminateProcess on Windows); resuming starts the same command
again, and the checkpointed steps decide what is redone.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "main.py"

MODES = {
    "replay": "Recorded Crustdata responses from 22 Nov 2026. No keys, nothing spent.",
    "fixtures": "Invented test data. Shows every kind of hold; never a real check.",
    "live": "Live Crustdata (and Claude, if a key is set). Spends credits.",
}
# Slow motion, so a viewer can crash the worker mid-run: a delay inside each source call (a kill
# there lands mid-step, and that step is redone) plus a short pause after every step.
SLOW_CALL_SECONDS = 0.8
SLOW_STEP_PAUSE_SECONDS = 0.25


@dataclass
class Paths:
    data: Path

    @property
    def db(self) -> Path:
        return self.data / "console.db"

    @property
    def out(self) -> Path:
        return self.data / "out"

    @property
    def uploads(self) -> Path:
        return self.data / "uploads"

    @property
    def meta(self) -> Path:
        return self.data / "runs"

    @property
    def imports(self) -> Path:
        return self.data / "imports"

    @property
    def provider_db(self) -> Path:
        return self.data / "fake_provider.db"

    def make(self) -> None:
        for p in (self.data, self.out, self.uploads, self.meta, self.imports):
            p.mkdir(parents=True, exist_ok=True)


@dataclass
class Job:
    proc: subprocess.Popen
    kind: str               # "run" | "settle"
    started: float
    log: Any = None


API_KEY_NAMES = ("CRUSTDATA_API_KEY", "ANTHROPIC_API_KEY", "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET")


@dataclass
class Workers:
    paths: Paths
    config_path: Path
    keyless: bool = False  # public demo: workers never see an API key, so they cannot spend anything
    jobs: dict[str, Job] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- run metadata (survives a console restart) -------------------------
    def meta_path(self, run_id: str) -> Path:
        return self.paths.meta / f"{run_id}.json"

    def load_meta(self, run_id: str) -> dict[str, Any] | None:
        p = self.meta_path(run_id)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def save_meta(self, meta: dict[str, Any]) -> None:
        p = self.meta_path(meta["run_id"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)

    def all_meta(self) -> list[dict[str, Any]]:
        out = []
        for p in self.paths.meta.glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda m: m.get("created_at", ""), reverse=True)

    # ---- commands ----------------------------------------------------------
    def command(self, meta: dict[str, Any], settle: bool = False) -> list[str]:
        p = self.paths
        cmd = [sys.executable, str(MAIN), "run", meta["input_path"], "--run-id", meta["run_id"],
               "--db", str(p.db), "--out", str(p.out), "--config", str(self.config_path)]
        mode = meta["mode"]
        if mode == "replay":
            cmd.append("--replay")
        elif mode == "fixtures":
            cmd.append("--fake")
        if self.keyless:
            cmd.append("--no-llm")
        if meta.get("slow") and mode != "live":
            cmd += ["--slow", str(SLOW_CALL_SECONDS), "--pace", str(SLOW_STEP_PAUSE_SECONDS)]
        if settle:
            cmd += ["--pay", "--approver", "console"]
            if meta.get("pay_provider") == "fake":
                cmd += ["--pay-provider", "fake", "--fake-remote-db", str(p.provider_db)]
        return cmd

    def start(self, meta: dict[str, Any], settle: bool = False) -> None:
        with self.lock:
            self._reap()
            job = self.jobs.get(meta["run_id"])
            if job and job.proc.poll() is None:
                raise RuntimeError("this run already has a worker running")
            log = open(self.paths.meta / f"{meta['run_id']}.log", "a", encoding="utf-8")
            log.write(f"\n--- {'settle' if settle else 'run'} started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            log.flush()
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            if self.keyless:  # set to empty so a .env file can't fill them back in either
                env.update({k: "" for k in API_KEY_NAMES})
            proc = subprocess.Popen(self.command(meta, settle), cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, env=env)
            self.jobs[meta["run_id"]] = Job(proc, "settle" if settle else "run", time.time(), log)

    def kill(self, run_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(run_id)
            if not job or job.proc.poll() is not None:
                return False
            job.proc.kill()  # SIGKILL on POSIX, TerminateProcess on Windows: no cleanup runs
            job.proc.wait()
            if job.log:
                job.log.write(f"--- killed from the console {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                job.log.close()
            return True

    def state(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(run_id)
            if not job:
                return {"running": False, "kind": None, "exit_code": None}
            code = job.proc.poll()
            return {"running": code is None, "kind": job.kind, "exit_code": code}

    def running_count(self) -> int:
        with self.lock:
            return sum(1 for j in self.jobs.values() if j.proc.poll() is None)

    def forget(self, run_id: str) -> None:
        """Kill any worker and delete the run's metadata and log (its database rows are the caller's)."""
        self.kill(run_id)
        with self.lock:
            self.jobs.pop(run_id, None)
        for p in (self.meta_path(run_id), self.paths.meta / f"{run_id}.log"):
            p.unlink(missing_ok=True)

    def log_tail(self, run_id: str, chars: int = 1500) -> str:
        p = self.paths.meta / f"{run_id}.log"
        if not p.exists():
            return ""
        text = p.read_text(encoding="utf-8", errors="replace")
        return text[-chars:]

    def _reap(self) -> None:
        for job in self.jobs.values():
            if job.proc.poll() is not None and job.log and not job.log.closed:
                job.log.close()

    def stop_all(self) -> None:
        with self.lock:
            for job in self.jobs.values():
                if job.proc.poll() is None:
                    job.proc.kill()
                    job.proc.wait()
                if job.log and not job.log.closed:
                    job.log.close()
