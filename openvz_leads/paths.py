"""Where things live.

Three different situations have to resolve to sensible directories:

  * a git checkout or an unpacked archive — everything sits next to the
    package, which is what `pip install -e .` users expect;
  * a frozen desktop build — the code is inside a read-only .app bundle, so
    anything writable (database, config, .env) and anything the user is meant
    to edit (prompts, skills) has to live in their own Application Support
    directory instead;
  * a deliberate override — one install driving several workspaces.

Everything funnels through `workspace()` so the answer is decided in one
place rather than eleven copies of `Path(__file__).parent.parent`.
"""

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "OpenVZ Leads"

# Directories the user is expected to open and edit. In a frozen build these
# ship read-only inside the bundle and are copied out on first run — burying
# them in a .app would break the one promise that "changing how it sells needs
# no code" rests on.
SEEDED_DIRS = ("prompts", "skills")
SEEDED_FILES = ("openvz-leads.yaml",)


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def bundle_root() -> Path:
    """Read-only resources shipped with the app.

    In a frozen build this is PyInstaller's extraction directory; otherwise
    it is the project root, where prompts/ and skills/ already are.
    """
    if is_frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def default_workspace() -> Path:
    """The per-user workspace for a frozen build."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        base = os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    base = os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "openvz-leads"


def workspace() -> Path:
    """The directory holding config, data, prompts and skills.

    OPENVZ_LEADS_HOME wins, so one install can drive several workspaces
    (different products, different ICPs) without copying the app.
    """
    override = os.getenv("OPENVZ_LEADS_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if is_frozen():
        return default_workspace()
    # Checkout or unpacked archive: behave exactly as before.
    return Path(__file__).resolve().parent.parent


def ensure_workspace() -> Path:
    """Create the workspace and seed the editable files into it.

    Copies are made only when the destination is missing, so a user's edited
    prompt is never overwritten by an upgrade. New files added in a later
    version do appear, which is the behaviour you want from a seed.
    """
    ws = workspace()
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "data").mkdir(exist_ok=True)

    src_root = bundle_root()
    if src_root.resolve() == ws.resolve():
        return ws  # running from a checkout — nothing to copy

    for name in SEEDED_DIRS:
        src, dst = src_root / name, ws / name
        if not src.is_dir():
            continue
        dst.mkdir(exist_ok=True)
        for item in src.iterdir():
            if item.is_file() and not (dst / item.name).exists():
                shutil.copy2(item, dst / item.name)

    for name in SEEDED_FILES:
        src, dst = src_root / name, ws / name
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)

    return ws


def static_dir() -> Path:
    """Read-only web assets shipped with the package.

    These are not seeded into the workspace the way prompts/ and skills/ are:
    the dashboard page is code, not something a user is invited to fork, and a
    stale copy left behind by an upgrade would be a page whose markup no longer
    matches the API serving it. Under PyInstaller the package directory is
    extracted beneath _MEIPASS, so the normal path resolves; the bundle_root()
    fallback covers a spec that places the assets at the archive root instead.
    """
    beside_package = Path(__file__).resolve().parent / "static"
    if beside_package.is_dir():
        return beside_package
    return bundle_root() / "openvz_leads" / "static"


def static_file(name: str) -> Path:
    return static_dir() / name


# ── Convenience accessors ────────────────────────────────────────────

def prompts_dir() -> Path:
    return workspace() / "prompts"


def skills_dir() -> Path:
    return workspace() / "skills"


def config_file() -> Path:
    return workspace() / "openvz-leads.yaml"


def env_file() -> Path:
    return workspace() / ".env"


def data_dir() -> Path:
    return workspace() / "data"
