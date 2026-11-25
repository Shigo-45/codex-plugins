#!/usr/bin/env python3
"""Start the packaged MCP-only plugin in a local Fiji with Java 21."""

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


ENDPOINT = "http://127.0.0.1:3001/mcp"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SESSION_PATH = Path.home() / ".local/state/image-analysis-mcp/session.json"
OWNER_PROPERTY = "image.analysis.launcher.token"


def owned_process(session):
    """Check PID reuse as well as the per-launch JVM marker."""
    pid = session.get("pid")
    token = session.get("token")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not token:
        raise RuntimeError("Invalid managed Fiji session record")
    result = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                            capture_output=True, text=True, check=False)
    return (result.returncode == 0
            and "-D%s=%s" % (OWNER_PROPERTY, token) in shlex.split(result.stdout))


def probe():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "image-analysis-launcher", "version": "0.1.0"}}}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    try:
        with OPENER.open(req, timeout=2) as response:
            body = response.read().decode()
            messages = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
            if not messages:
                messages = [body]
            for message in messages:
                info = json.loads(message).get("result", {}).get("serverInfo", {})
                if info.get("name") == "ImageAnalysisMCP":
                    session = response.headers.get("Mcp-Session-Id")
                    if session:
                        close = urllib.request.Request(ENDPOINT, method="DELETE", headers={"Mcp-Session-Id": session})
                        try:
                            OPENER.open(close, timeout=2).close()
                        except urllib.error.URLError:
                            pass
                    return True
            raise RuntimeError("Port 3001 belongs to a different MCP server")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Port 3001 returned HTTP {exc.code}; inspect the existing service") from exc
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return False


def java_home(fiji):
    local = Path.home() / ".local/tools/jdk-21.0.13+9/Contents/Home"
    if (local / "bin/java").is_file():
        return local
    candidates = sorted((fiji.parent / "java").glob("**/Contents/Home/release"))
    for release in candidates:
        if 'JAVA_VERSION="21.' in release.read_text():
            return release.parent
    raise RuntimeError("Java 21 was not found; pass --java-home")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fiji", type=Path, default=Path(os.environ.get("IMAGE_ANALYSIS_FIJI", "/Applications/Fiji/fiji")))
    parser.add_argument("--java-home", type=Path)
    args = parser.parse_args()
    if probe():
        print(f"Image Analysis is ready at {ENDPOINT}")
        return 0
    root = Path(__file__).resolve().parents[1]
    jars = sorted((root / "runtime").glob("*.jar"))
    if not any(p.name.startswith("ImageAnalysisMCP-") for p in jars):
        raise RuntimeError("Packaged runtime is missing; run scripts/package_codex_plugin.py from the source checkout")
    if not args.fiji.is_file():
        raise RuntimeError(f"Fiji launcher is missing: {args.fiji}; pass --fiji")
    jdk = args.java_home or java_home(args.fiji)
    state = Path.home() / ".local/state/image-analysis-mcp"
    state.mkdir(parents=True, exist_ok=True)
    log = state / "fiji.log"
    command = [str(args.fiji), "--java-home", str(jdk), "--no-splash", "--no-python",
               "--allow-multiple", "--class-path", os.pathsep.join(map(str, jars)),
               "--add-opens=java.desktop/com.apple.eawt=ALL-UNNAMED",
               "--main-class", "copilotj.McpOnlyService"]
    # Ask Fiji to resolve its complete runtime configuration, then launch the
    # selected Java executable directly. Native JVM loading on this macOS setup
    # can reject Java 21 options even when Jaunch reports the correct Java home.
    resolved = subprocess.run(command + ["--dry-run"], cwd=root, capture_output=True,
                              text=True, check=True)
    lines = (resolved.stdout + "\n" + resolved.stderr).splitlines()
    launch = next((line[len("[DRY-RUN] "):] for line in lines
                   if line.startswith("[DRY-RUN] ")), None)
    if launch is None:
        raise RuntimeError("Fiji did not return its Java launch command")
    command = shlex.split(launch)
    if not command or Path(command[0]).resolve() != (jdk / "bin/java").resolve():
        raise RuntimeError("Fiji resolved a different Java executable; inspect --java-home")
    token = uuid.uuid4().hex
    command.insert(1, "-D%s=%s" % (OWNER_PROPERTY, token))
    with log.open("ab") as output:
        process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=output, start_new_session=True)
    temporary = SESSION_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": process.pid, "token": token}))
    temporary.chmod(0o600)
    temporary.replace(SESSION_PATH)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if probe():
            print(f"Image Analysis is ready at {ENDPOINT}\nFiji log: {log}")
            return 0
        if process.poll() is not None:
            raise RuntimeError(f"Fiji exited with code {process.returncode}; inspect {log}")
        time.sleep(0.5)
    raise RuntimeError(f"Fiji did not open its MCP endpoint within 60 seconds; inspect {log}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
