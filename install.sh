#!/usr/bin/env bash
#
# OpenVZ Leads installer.
#
# Creates a virtualenv beside this script, installs the package into it, and
# leaves prompts/ and skills/ where you can edit them — they are read from this
# directory at runtime, which is why the tool ships as an archive you unpack
# rather than something you `pip install` from an index.
#
# Safe to re-run: an existing .venv is reused.

set -euo pipefail

cd "$(dirname "$0")"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
say()  { printf '\n  %s%s%s\n' "$BOLD" "$1" "$OFF"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$OFF" "$1"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$OFF" "$1"; }
die()  { printf '\n  %s✗%s %s\n\n' "$RED" "$OFF" "$1" >&2; exit 1; }

printf '\n  %sOpenVZ Leads%s — 找得到 · 看得懂 · 写得出\n' "$BOLD" "$OFF"
printf '  %sFind them. Understand them. Reach them.%s\n' "$DIM" "$OFF"

# ── 1. Python ────────────────────────────────────────────────────────
# 3.11 is the floor: the codebase uses `X | None` in runtime-evaluated
# pydantic annotations, which 3.9 and 3.10 reject at import time.
say "[1/4] Looking for Python 3.11+"

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    found="$(python3 --version 2>&1 || echo 'none')"
    die "Python 3.11 or newer is required (found: $found).
     macOS:  brew install python@3.12
     Linux:  apt install python3.12 python3.12-venv
     Or:     https://www.python.org/downloads/"
fi
ok "$PYTHON ($("$PYTHON" --version 2>&1 | cut -d' ' -f2))"

# ── 2. Virtualenv ────────────────────────────────────────────────────
say "[2/4] Setting up the virtualenv"
if [ -d .venv ]; then
    ok ".venv already exists — reusing it"
else
    "$PYTHON" -m venv .venv || die "Could not create .venv.
     On Debian/Ubuntu this usually means the venv module is missing:
     apt install python3-venv"
    ok "created .venv"
fi

# ── 3. Dependencies ──────────────────────────────────────────────────
say "[3/4] Installing dependencies"
./.venv/bin/python -m pip install --quiet --upgrade pip setuptools wheel
./.venv/bin/python -m pip install --quiet -e . || die "Dependency install failed. Scroll up for the reason."
ok "installed"

# ── 4. Chromium (optional) ───────────────────────────────────────────
# Only LinkedIn prospecting uses Playwright, and that is off by default
# because automating LinkedIn breaks their terms of service. A failure here
# must not fail the install.
say "[4/4] Chromium for LinkedIn prospecting (optional)"
if ./.venv/bin/python -m playwright install chromium >/dev/null 2>&1; then
    ok "installed"
else
    warn "skipped — only needed if you enable LinkedIn prospecting."
    printf '    %sRun later with: ./.venv/bin/python -m playwright install chromium%s\n' "$DIM" "$OFF"
fi

# ── Prerequisite check, not a failure ────────────────────────────────
printf '\n'
if command -v claude >/dev/null 2>&1; then
    ok "Claude Code CLI found"
else
    warn "Claude Code CLI not found — it is the thinking engine, and nothing"
    printf '    runs without it. Install from https://claude.ai/download,\n'
    printf '    then run: claude login\n'
fi

cat <<EOF

  ${BOLD}Installed.${OFF} Next:

    ${BOLD}source .venv/bin/activate${OFF}

    ${BOLD}openvz-leads train https://your-company.com${OFF}   learn your product from your site
    ${BOLD}openvz-leads setup${OFF}                            or answer a few questions instead

    ${BOLD}openvz-leads run${OFF}                              find, analyse and draft, on a loop
    ${BOLD}openvz-leads dashboard${OFF}                        http://localhost:5555

  ${DIM}Nothing is sent by default. Outreach is drafted and queued for your
  review; approve it with 'openvz-leads review list', or take it elsewhere
  with 'openvz-leads export'. Read README.md before your first campaign.${OFF}

EOF
