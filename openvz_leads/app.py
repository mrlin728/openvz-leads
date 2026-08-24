"""Desktop entry point — what the .app bundle actually runs.

The CLI assumes a terminal: a shell that already has PATH set up, a working
directory next to the code, and someone to read stderr. A double-clicked app
has none of those, so this module supplies them before handing over to the
dashboard.

The dashboard itself is a local web app. It used to be shown by handing the
URL to the user's browser, which worked but never felt like a program: the
window carried someone else's chrome, the tab was lost among thirty others,
and quitting the browser looked like quitting OpenVZ Leads. It now runs in a
window of its own via pywebview — the system web view, so no second browser
engine is downloaded — and falls back to the browser when that is missing.
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

# The window opens at this size when there is no saved geometry. Wide enough
# for the sidebar plus a table that does not wrap, short enough to fit a
# 1366x768 laptop with the dock showing.
WINDOW_W, WINDOW_H = 1280, 840
MIN_W, MIN_H = 960, 640

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


def wait_for_port(port: int, timeout: float = 12.0) -> bool:
    """Block until the server answers, so nothing shows a connection error."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def open_when_ready(url: str, port: int) -> None:
    """Open the browser once the server answers, not before."""
    wait_for_port(port)
    webbrowser.open(url)  # open regardless; the page explains if it cannot connect


def headless() -> bool:
    """Serve the dashboard without putting anything on screen.

    The release smoke tests start the app, wait for the port to answer and
    kill it. On a CI runner there is no desktop session, so without this the
    window fails, the browser fallback fires, and the check comes to depend on
    how a headless machine happens to handle `webbrowser.open`. An explicit
    switch makes that path deterministic.
    """
    return os.environ.get("OPENVZ_LEADS_NO_WINDOW", "").strip().lower() in {
        "1", "true", "yes",
    }


def run_in_window(url: str, port: int) -> bool:
    """Show the dashboard in a window of its own.

    Returns False if pywebview is unavailable or the platform has no web view
    to give us, so the caller can fall back to the browser rather than leaving
    the user with a running server and nothing on screen.
    """
    try:
        import webview
    except ImportError:
        print("pywebview not installed — falling back to the browser.")
        return False

    if not wait_for_port(port):
        print("Server did not come up in time; showing the window anyway.")

    try:
        webview.create_window(
            "OpenVZ Leads",
            url,
            width=WINDOW_W,
            height=WINDOW_H,
            min_size=(MIN_W, MIN_H),
            # The dashboard paints its own dark background. Without this the
            # web view flashes white for a frame on open, which reads as a
            # bug on a dark app.
            background_color="#08090C",
            text_select=True,
        )
        webview.start()  # blocks until the user closes the window
        return True
    except Exception as exc:  # no GUI toolkit, headless session, etc.
        print(f"Could not open a window ({exc}) — falling back to the browser.")
        return False


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

    from openvz_leads.dashboard import build_server

    server = build_server(host="127.0.0.1", port=port)
    # Daemon, so closing the window ends the process instead of leaving a
    # server with no interface running until the machine reboots.
    threading.Thread(target=server.run, daemon=True).start()

    if headless():
        print("OPENVZ_LEADS_NO_WINDOW set — serving without a window.")
        wait_for_port(port)
    elif run_in_window(url, port):
        print("Window closed — shutting down.")
        return
    else:
        # No window: keep the old behaviour rather than exiting, so the app
        # still works everywhere it worked before.
        open_when_ready(url, port)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Interrupted — shutting down.")


if __name__ == "__main__":
    main()
