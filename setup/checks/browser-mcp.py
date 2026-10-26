"""Open a page through the browser MCP server exactly as Bulava will start it.

Run as the user the runner runs as:  python3 -I browser-mcp.py
Exit 0 only if Chrome rendered the page and the snapshot shows its text. Finding the
binaries is not enough: this proves the preinstalled chrome-devtools-mcp starts Chrome
under this user without npx or network downloads.
"""

import json
import re
import subprocess
import sys

CONFIG = "/opt/bulava-local/mcp.json"
PAGE = "data:text/html,<title>arceus-check</title><h1>browser works</h1>"


def main() -> int:
    server = json.load(open(CONFIG))["mcpServers"]["browser"]
    proc = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        proc.stdin.flush()

    def call(i: int, method: str, params: dict | None = None) -> dict:
        send({"id": i, "method": method, "params": params or {}})
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError(f"MCP server exited during {method}")
            msg = json.loads(line)
            if msg.get("id") == i:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg["result"]

    try:
        call(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "arceus-setup-check", "version": "1"}})
        send({"method": "notifications/initialized"})
        opened = call(2, "tools/call", {"name": "new_page", "arguments": {"url": PAGE}})["content"][0]["text"]
        selected = re.findall(r"(\d+): [^\n]*\[selected\]", opened)
        if not selected:
            print(f"new_page did not report a selected page: {opened[:300]}")
            return 1
        snap = call(3, "tools/call", {"name": "take_snapshot", "arguments": {"pageId": int(selected[-1])}})
        text = snap["content"][0]["text"]
    finally:
        proc.terminate()
        proc.wait(timeout=20)
    if 'heading "browser works"' in text:
        print("browser MCP: Chrome opened the page and the snapshot shows it")
        return 0
    print(f"snapshot does not show the page: {text[:300]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
