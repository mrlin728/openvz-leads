"""Desktop entry point — what the .app bundle actually runs.

The CLI assumes a terminal: a shell that already has PATH set up, a working
directory next to the code, and someone to read stderr. A double-clicked app
has none of those, so this module supplies them before handing over to the
dashboard.
"""

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from openvz_leads import paths

DEFAULT_PORT = 5555
# Ports to try if the default is taken — another copy already running, or an
# unrelated dev server. Failing to start because port 5555 was busy would be a
# baffling way for a desktop app to die.
PORT_RANGE = range(DEFAULT_PORT, DEFAULT_PORT + 20)

# Where `claude` actually installs. A GUI process on macOS inherits launchd's
# PATH (/usr/bin:/bin:/usr/sbin:/sbin), not the shell's, so a perfectly working
# `claude` on the user's terminal is invisible here unless we go looking.
CLI_SEARCH_PATHS = (
    "~/.local/bin",
    "~/.claude/local",
    "~/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
)


def widen_path() -> None:
    """Add the usual install locations to PATH for this process."""
    current = os.environ.get("PATH", "").split(os.pathsep)
    for entry in CLI_SEARCH_PATHS:
        resolved = str(Path(entry).expanduser())
        if resolved not in current and Path(resolved).is_dir():
            current.append(resolved)
    os.environ["PATH"] = os.pathsep.join(current)


def find_free_port() -> int:
    for port in PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return DEFAULT_PORT


def already_running(port: int) -> bool:
    """Is one of our own dashboards already on this port?"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def open_when_ready(url: str, port: int) -> None:
    """Open the browser once the server answers, not before."""
    for _ in range(120):  # ~12s
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                webbrowser.open(url)
                return
        time.sleep(0.1)
    webbrowser.open(url)  # open anyway; the page explains if it cannot connect


def main() -> None:
    widen_path()

    # Create the workspace and copy prompts/, skills/ and the config template
    # out of the read-only bundle so the user can actually edit them.
    workspace = paths.ensure_workspace()

    # Logs have nowhere to go in a windowless process; a file the user can be
    # pointed at beats losing the traceback entirely.
    log_path = paths.data_dir() / "app.log"
    try:
        stream = open(log_path, "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = stream
    except OSError:
        pass

    print(f"\n=== OpenVZ Leads starting {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"Workspace: {workspace}")

    # A second launch should surface the window that already exists rather
    # than starting a rival server against the same SQLite file.
    if already_running(DEFAULT_PORT):
        print("Already running — opening the existing dashboard.")
        webbrowser.open(f"http://127.0.0.1:{DEFAULT_PORT}")
        return

    port = find_free_port()
    url = f"http://127.0.0.1:{port}"
    print(f"Dashboard: {url}")

    threading.Thread(target=open_when_ready, args=(url, port), daemon=True).start()

    from openvz_leads.dashboard import start_dashboard

    start_dashboard(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
