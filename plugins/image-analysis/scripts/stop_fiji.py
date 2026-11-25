#!/usr/bin/env python3
"""Quit the launcher-owned Fiji after its task has saved and verified outputs."""

import argparse
import json
import sys
import time
import urllib.request

from start_fiji import ENDPOINT, OPENER, OWNER_PROPERTY, SESSION_PATH, owned_process


class Client:
    def __init__(self):
        self.session = None
        self.protocol = None
        self.request_id = 0

    def call(self, method, params=None, notification=False):
        self.request_id += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = self.request_id
        if params is not None:
            payload["params"] = params
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        request = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(), headers=headers)
        with OPENER.open(request, timeout=20) as response:
            self.session = response.headers.get("Mcp-Session-Id", self.session)
            body = response.read().decode()
        if notification:
            return None
        messages = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        if not messages:
            messages = [body]
        for raw in messages:
            message = json.loads(raw)
            if message.get("id") == self.request_id:
                if "error" in message:
                    raise RuntimeError("Fiji MCP error: " + str(message["error"]))
                return message["result"]
        raise RuntimeError("Fiji MCP response is missing the request result")

    def initialize(self):
        result = self.call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "image-analysis-finish", "version": "0.1.0"}})
        if result.get("serverInfo", {}).get("name") != "ImageAnalysisMCP":
            raise RuntimeError("Port 3001 belongs to a different MCP server")
        self.protocol = result["protocolVersion"]
        self.call("notifications/initialized", notification=True)

    def close(self):
        if self.session:
            request = urllib.request.Request(ENDPOINT, method="DELETE",
                                             headers={"Mcp-Session-Id": self.session})
            try:
                OPENER.open(request, timeout=2).close()
            except OSError:
                pass


def quit_script(token):
    # The JVM marker checks endpoint identity independently of the recorded PID.
    # Swing's one-shot timer lets the MCP result return before Fiji tears down.
    return '''#@output String status
from java.lang import System
from javax.swing import Timer
from java.awt.event import ActionListener
from ij import IJ, WindowManager
if System.getProperty(%s) != %s:
    raise RuntimeError("Endpoint is outside the launcher-owned Fiji session")
unsaved = [i for i in (WindowManager.getIDList() or []) if WindowManager.getImage(i).changes]
if unsaved:
    raise RuntimeError("Save task images before quitting Fiji; unsaved image IDs: " + str(unsaved))
if IJ.getInstance() is None:
    raise RuntimeError("Fiji application window is unavailable")
class QuitFiji(ActionListener):
    def actionPerformed(self, event):
        IJ.getInstance().quit()
timer = Timer(750, QuitFiji())
timer.setRepeats(False)
timer.start()
status = "quit_scheduled"
''' % (json.dumps(OWNER_PROPERTY), json.dumps(token))


def request_quit(session):
    client = Client()
    try:
        client.initialize()
        result = client.call("tools/call", {"name": "run_script", "arguments": {
            "language": "jython", "script": quit_script(session["token"]), "timeout": 15}})
        if result.get("isError"):
            raise RuntimeError("Fiji rejected task completion: " + str(result.get("content")))
        texts = [item["text"] for item in result.get("content", []) if item.get("type") == "text"]
        for value in texts:
            data = json.loads(value)
            if data.get("err"):
                raise RuntimeError("Fiji kept open: " + data["err"])
            if data.get("outputs", {}).get("status") == "quit_scheduled":
                return
        raise RuntimeError("Fiji returned an unconfirmed quit result")
    finally:
        client.close()


def finish(timeout=20):
    if not SESSION_PATH.is_file():
        raise RuntimeError("No launcher-owned Fiji session is recorded; retain the existing instance")
    session = json.loads(SESSION_PATH.read_text())
    if not owned_process(session):
        SESSION_PATH.unlink()
        return "Managed Fiji is already stopped"
    error = None
    try:
        request_quit(session)
    except (OSError, RuntimeError, ValueError) as exc:
        # A quit may tear down HTTP before its response arrives. Inspect the
        # recorded process rather than submitting the same request again.
        error = exc
    deadline = time.monotonic() + timeout
    while owned_process(session):
        if time.monotonic() >= deadline:
            detail = str(error) if error else "Inspect pending dialogs and save remaining task windows"
            raise RuntimeError("Managed Fiji remains open: " + detail)
        time.sleep(0.25)
    # Preserve a replacement launch record if another task started meanwhile.
    if SESSION_PATH.is_file() and json.loads(SESSION_PATH.read_text()) == session:
        SESSION_PATH.unlink()
    return "Managed Fiji exited; task windows are closed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(finish())


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
