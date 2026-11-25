#!/usr/bin/env python3
"""Run bounded DS tasks in independent sessions of one DSH SDK subprocess."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid


class DispatchError(Exception):
    pass


def launcher(explicit):
    candidate = explicit or os.environ.get("DSH_SUBAGENT_BIN")
    if candidate:
        found = shutil.which(candidate) or str(Path(candidate).expanduser().absolute())
        if not os.path.isfile(found) or not os.access(found, os.X_OK):
            raise DispatchError("Selected DSH launcher is not executable")
        return found
    found = shutil.which("dsh")
    if found:
        return found
    desktop = "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
    if os.access(desktop, os.X_OK):
        return desktop
    raise DispatchError("No DSH launcher found; pass --dsh-bin or set DSH_SUBAGENT_BIN")


def emit(report, output):
    data = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if not output:
        sys.stdout.write(data)
        return
    path = Path(output).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ds-result-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


class Runtime:
    def __init__(self, binary, cwd, patch):
        self.binary, self.cwd, self.patch = binary, cwd, patch
        self.proc = self.reader = self.diagnostics = None
        self.pending, self.sessions = {}, {}
        self.sequence = 0
        self.failure = None

    async def start(self):
        env = dict(os.environ)
        env["DSH_MAX_TOKENS_AS_SUCCESS"] = "false"
        self.proc = await asyncio.create_subprocess_exec(
            self.binary, "--profile", "sdk", "--patch", self.patch,
            cwd=self.cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, limit=32 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self.read())
        self.diagnostics = asyncio.create_task(self.read_stderr())

    async def read_stderr(self):
        # Drain diagnostics so a chatty plugin cannot block the runtime.
        # Do not copy arbitrary plugin diagnostics or reasoning into the report.
        while await self.proc.stderr.readline():
            pass

    async def read(self):
        try:
            while line := await self.proc.stdout.readline():
                frame = json.loads(line)
                if frame.get("jsonrpc") != "2.0":
                    raise DispatchError("DSH stdout contains a non-JSON-RPC frame")
                if "id" in frame and "method" in frame:
                    reply = {"jsonrpc": "2.0", "id": frame["id"],
                             "error": {"code": -32601, "message": "Client method unavailable"}}
                    self.proc.stdin.write((json.dumps(reply) + "\n").encode())
                    await self.proc.stdin.drain()
                elif "id" in frame:
                    future = self.pending.get(frame["id"])
                    if future is not None and not future.done():
                        if "error" in frame:
                            future.set_exception(DispatchError(str(frame["error"].get("message", "SDK error"))))
                        else:
                            future.set_result(frame.get("result"))
                elif frame.get("method") == "session.event":
                    params = frame.get("params", {})
                    state = self.sessions.get(params.get("sessionId"))
                    if state is None:
                        continue
                    event = params.get("event", {})
                    data = event.get("data", {})
                    if event.get("type") == "assistant/message":
                        text = "".join(b.get("text", "") for b in data.get("message", {}).get("content", [])
                                       if b.get("type") == "text")
                        if text.strip():
                            state["text"] = text
                    elif event.get("type") == "turn/end":
                        state["stop_reason"] = data.get("reason")
            raise DispatchError("DSH SDK closed its output")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failure = DispatchError(str(error))
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(self.failure)

    async def request(self, method, params=None):
        if self.failure:
            raise self.failure
        self.sequence += 1
        identity = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            frame = {"jsonrpc": "2.0", "id": identity, "method": method}
            if params is not None:
                frame["params"] = params
            self.proc.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode())
            await self.proc.stdin.drain()
            return await future
        finally:
            self.pending.pop(identity, None)

    async def close(self):
        if self.proc is None:
            return
        if self.proc.returncode is None and not self.failure:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.request("shutdown"), 5)
        # Reap the entire group, including tool processes after an interrupted run.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, signal.SIGTERM)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.proc.wait(), 5)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, signal.SIGKILL)
        await self.proc.wait()
        for task in (self.reader, self.diagnostics):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


def tasks_from(args):
    if args.task is not None:
        tasks = [{"id": "task-1", "prompt": args.task}]
    else:
        with open(args.tasks, encoding="utf-8") as stream:
            payload = json.load(stream)
        tasks = payload.get("tasks") if isinstance(payload, dict) else payload
    if not isinstance(tasks, list) or not tasks:
        raise DispatchError("Tasks must be a non-empty array or an object with a tasks array")
    ids = set()
    for task in tasks:
        if not isinstance(task, dict) or set(task) - {"id", "prompt"}:
            raise DispatchError("Each task accepts only id and prompt")
        if not isinstance(task.get("id"), str) or not task["id"].strip() or task["id"] in ids:
            raise DispatchError("Task IDs must be unique non-empty strings")
        if not isinstance(task.get("prompt"), str) or not task["prompt"].strip():
            raise DispatchError("Each task requires a non-empty prompt")
        ids.add(task["id"])
    return tasks


async def dispatch(args, binary, cwd, tasks):
    started = time.monotonic()
    results = [None] * len(tasks)
    report = {"schema_version": 1, "ok": False, "backend": "dsh-sdk",
              "provider": args.provider, "model": args.model, "cwd": str(cwd),
              "permission_mode": args.permission_mode, "tasks": results}
    patch = [{"id": "sandbox-policy", "config": {"mode": args.permission_mode, "workspaceRoot": str(cwd)}},
             {"id": "tool-subagent", "disabled": True},
             {"id": "tool-subagent-fork", "disabled": True}]
    with tempfile.TemporaryDirectory(prefix="ds-dispatch-") as scratch:
        path = Path(scratch) / "overlay.yml"
        path.write_text(json.dumps(patch), encoding="utf-8")
        runtime = Runtime(binary, cwd, str(path))
        semaphore = asyncio.Semaphore(args.max_parallel)
        stopping = asyncio.Event()

        async def run_one(index, task):
            async with semaphore:
                if stopping.is_set():
                    return
                session = "ds-worker-" + uuid.uuid4().hex
                state = {"text": "", "stop_reason": None}
                runtime.sessions[session] = state
                result = {"id": task["id"], "session_id": session, "ok": False}
                results[index] = result
                began = time.monotonic()
                print("DS task started: " + task["id"], file=sys.stderr, flush=True)
                prompt = ("You are a delegated worker. Complete only the assignment below. "
                          "Do not spawn other agents, message people, or expand the assignment. "
                          "Return the requested output, supporting evidence, and any uncertainty. "
                          "Permitted filesystem mode: " + args.permission_mode + ".\n\n" + task["prompt"])
                try:
                    async def turn():
                        receipt = await runtime.request("session/prompt", {"sessionId": session,
                            "contentBlocks": [{"type": "text", "text": prompt}]})
                        result["message_id"] = receipt["messageId"]
                        await runtime.request("session/wait", {"sessionId": session})
                    await asyncio.wait_for(turn(), args.timeout)
                    result.update(state)
                    result["ok"] = (isinstance(state["stop_reason"], dict)
                                    and state["stop_reason"].get("kind") == "completed"
                                    and bool(state["text"].strip()))
                    if not result["ok"]:
                        result["error"] = "No completed turn with committed assistant text"
                except asyncio.TimeoutError:
                    stopping.set()
                    result.update(state)
                    result["error"] = "Task timed out; shared runtime must stop"
                    raise
                except asyncio.CancelledError:
                    stopping.set()
                    result.update(state)
                    result["error"] = "Interrupted when the batch stopped"
                    raise
                except Exception as error:
                    result.update(state)
                    result["error"] = str(error)
                finally:
                    result["elapsed_seconds"] = round(time.monotonic() - began, 3)
                    print("DS task finished: " + task["id"] + " ok=" + str(result["ok"]), file=sys.stderr, flush=True)

        jobs = []
        try:
            await runtime.start()
            params = {"provider": args.provider, "model": args.model, "cwd": str(cwd)}
            if args.reasoning_effort:
                params["reasoningEffort"] = args.reasoning_effort
            if args.max_tokens:
                params["maxTokens"] = args.max_tokens
            report["server"] = await asyncio.wait_for(runtime.request("initialize", params), args.startup_timeout)
            report["startup_seconds"] = round(time.monotonic() - started, 3)
            jobs = [asyncio.create_task(run_one(i, task)) for i, task in enumerate(tasks)]
            await asyncio.gather(*jobs)
        except asyncio.TimeoutError:
            report["error"] = "Startup or task timeout; unfinished work interrupted"
        except Exception as error:
            report["error"] = str(error)
        finally:
            for job in jobs:
                if not job.done():
                    job.cancel()
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)
            await runtime.close()
            for i, result in enumerate(results):
                if result is None:
                    results[i] = {"id": tasks[i]["id"], "ok": False, "error": "Task was not started"}
            report["ok"] = not report.get("error") and all(r["ok"] for r in results)
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Must be a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check launcher without sending model requests")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--tasks", help="UTF-8 task JSON file")
    inputs.add_argument("--task", help="Single task prompt")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--dsh-bin")
    parser.add_argument("--provider", default=os.environ.get("DSH_SUBAGENT_PROVIDER", "deepseek-official"))
    parser.add_argument("--model", default=os.environ.get("DSH_SUBAGENT_MODEL", "deepseek-flash"))
    parser.add_argument("--reasoning-effort", default=os.environ.get("DSH_SUBAGENT_REASONING_EFFORT", "max"))
    parser.add_argument("--max-tokens", type=positive)
    parser.add_argument("--max-parallel", type=positive, default=3)
    parser.add_argument("--timeout", type=positive, default=300)
    parser.add_argument("--startup-timeout", type=positive, default=45)
    parser.add_argument("--permission-mode", choices=["read-only", "workspace-write"], default="read-only")
    parser.add_argument("--output", help="Atomic JSON report destination; default stdout")
    args = parser.parse_args()
    try:
        binary = launcher(args.dsh_bin)
        if args.check:
            version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15, check=True)
            sdk = subprocess.run([binary, "sdk", "--help"], capture_output=True, text=True, timeout=30, check=True)
            if "sdk" not in sdk.stdout.lower():
                raise DispatchError("SDK profile help could not be verified")
            emit({"ok": True, "launcher": binary, "version": version.stdout.strip(),
                  "sdk_available": True, "model_request_sent": False}, args.output)
            return 0
        if args.tasks is None and args.task is None:
            parser.error("Provide --tasks, --task, or --check")
        cwd = Path(args.cwd).expanduser().resolve(strict=True)
        if not cwd.is_dir():
            raise DispatchError("--cwd must be a directory")
        if args.permission_mode == "workspace-write" and args.max_parallel != 1:
            raise DispatchError("Writer batches require --max-parallel 1; use separate workspaces for parallel edits")
        tasks = tasks_from(args)
        report = asyncio.run(dispatch(args, binary, cwd, tasks))
        emit(report, args.output)
        return 0 if report["ok"] else 1
    except KeyboardInterrupt:
        print("DS dispatch interrupted", file=sys.stderr)
        return 130
    except Exception as error:
        emit({"schema_version": 1, "ok": False, "error": str(error)}, args.output)
        return 1


if __name__ == "__main__":
    sys.exit(main())
