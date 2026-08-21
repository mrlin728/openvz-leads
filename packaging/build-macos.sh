#!/usr/bin/env bash
#
# Build the macOS .app and wrap it in a .dmg.
#
# The Windows installer has had a workflow since the first release; the Mac
# side was a sequence of commands somebody remembered. That asymmetry is the
# reason this exists: a release step that lives only in someone's shell
# history is a step that gets done differently every time, and the one thing
# a download must be is the same thing twice.
#
#   ./packaging/build-macos.sh              # version read from pyproject.toml
#   ./packaging/build-macos.sh 1.1.0        # or state it
#
# It must run on an Apple-silicon Mac. PyInstaller freezes the interpreter it
# is running under, so the architecture of this machine is the architecture of
# the artefact — there is no cross-compiling, which is also why the Windows
# build runs on a Windows runner.
#
# The result is UNSIGNED. We have no code-signing certificate, so Gatekeeper
# refuses the first launch and the user has to right-click → Open. The DMG
# carries a note saying so; do not remove it, because the alternative is a
# user who concludes the download is broken.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

APP_NAME="OpenVZ Leads"
PYTHON="${PYTHON:-.venv/bin/python}"

# ── Version ───────────────────────────────────────────────────────────

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
  VERSION=$("$PYTHON" - <<'PY'
import re, pathlib
text = pathlib.Path("pyproject.toml").read_text()
print(re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1))
PY
)
fi

ARCH=$(uname -m)
if [[ "$ARCH" != "arm64" ]]; then
  echo "This machine is $ARCH. The artefact would be named arm64 and would not"
  echo "run on Apple silicon. Build on an Apple-silicon Mac." >&2
  exit 1
fi

DMG_NAME="OpenVZ-Leads-${VERSION}-${ARCH}.dmg"
DIST_DIR="dist"
STAGE_DIR="build/dmg-stage"
OUT_DIR="dist-installer"

echo "==> Building ${APP_NAME} ${VERSION} (${ARCH})"

# ── 1. Tests, before freezing anything ────────────────────────────────
#
# A frozen build of broken code is a slower way to find out.

echo "==> Running the test suite"
"$PYTHON" -m pytest -q

# ── 2. Freeze ─────────────────────────────────────────────────────────

echo "==> Freezing with PyInstaller"
"$PYTHON" -m PyInstaller --noconfirm --clean OpenVZ-Leads.spec

APP_PATH="${DIST_DIR}/${APP_NAME}.app"
[[ -d "$APP_PATH" ]] || { echo "PyInstaller produced no .app at ${APP_PATH}" >&2; exit 1; }

# ── 3. Smoke test ─────────────────────────────────────────────────────
#
# Mirrors the Windows workflow, and for the same reason: the failure modes
# here — a hidden import PyInstaller did not see, a data file that did not get
# bundled — are invisible until runtime. Answering on the port is not enough
# either. If prompts/ and skills/ did not make it in, every agent silently
# falls back to a terse inline prompt and the build ships degraded but green.

echo "==> Smoke-testing the frozen app"
SMOKE_HOME="$(mktemp -d)"
trap 'rm -rf "$SMOKE_HOME"' EXIT

OPENVZ_LEADS_HOME="$SMOKE_HOME" "${APP_PATH}/Contents/MacOS/${APP_NAME}" \
  > "${SMOKE_HOME}/stdout.log" 2>&1 &
APP_PID=$!

answered=0
for _ in $(seq 1 40); do
  sleep 1
  if curl -fsS --max-time 3 http://127.0.0.1:5555/api/setup-status > /dev/null 2>&1; then
    answered=1
    break
  fi
done

kill "$APP_PID" 2>/dev/null || true
wait "$APP_PID" 2>/dev/null || true

if [[ "$answered" -ne 1 ]]; then
  echo "--- app log ---" >&2
  cat "${SMOKE_HOME}/stdout.log" >&2 || true
  cat "${SMOKE_HOME}/data/app.log" 2>/dev/null >&2 || true
  echo "The frozen app never served the dashboard." >&2
  exit 1
fi

missing=()
for f in prompts/writer.md prompts/profiler.md prompts/icp.md \
         skills/email_frameworks.md openvz-leads.yaml; do
  [[ -e "${SMOKE_HOME}/${f}" ]] || missing+=("$f")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "Bundled resources missing from the seeded workspace: ${missing[*]}" >&2
  exit 1
fi
echo "    Dashboard answered, workspace seeded with all expected resources."

# ── 4. DMG ────────────────────────────────────────────────────────────

echo "==> Building ${DMG_NAME}"
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR" "$OUT_DIR"

cp -R "$APP_PATH" "$STAGE_DIR/"
ln -s /Applications "${STAGE_DIR}/Applications"

# The Gatekeeper note. An unsigned app looks broken on first launch — macOS
# says it "cannot be opened", which reads as a corrupt download rather than a
# missing certificate — and a user who believes the download is broken does
# not try again.
cat > "${STAGE_DIR}/首次打开请先读我.txt" <<'NOTE'
OpenVZ Leads —— 第一次打开

1. 把左边的 OpenVZ Leads 拖到右边的「应用程序」文件夹。
2. 打开「应用程序」，找到 OpenVZ Leads。
3. 【重要】右键点它 → 选「打开」→ 在弹窗里再点一次「打开」。
   直接双击会被系统拦住。

为什么会被拦：我们没有购买 Apple 的代码签名证书，macOS 对所有未签名的
程序都是这个反应，和这个程序本身没有关系。右键打开只需要做一次，之后
双击就正常了。

打开之后浏览器会自动弹出仪表盘：http://127.0.0.1:5555

它还需要你本机装好并登录 Claude Code CLI —— 那是它的大脑，跑在你已有的
订阅上，没有第二份模型账单。应用首屏会检查这一项并给出链接。

你的数据全部留在本机：
  ~/Library/Application Support/OpenVZ Leads/
NOTE

rm -f "${OUT_DIR}/${DMG_NAME}"
hdiutil create \
  -volname "${APP_NAME} ${VERSION}" \
  -srcfolder "$STAGE_DIR" \
  -ov -format UDZO \
  "${OUT_DIR}/${DMG_NAME}" > /dev/null

rm -rf "$STAGE_DIR"

# ── 5. Checksum, and the numbers the website needs ────────────────────

# The checksum file names the artefact without a path, so it verifies with a
# plain `shasum -c` next to the download. Run in a subshell: $PYTHON below is
# relative to the project root, and a cd that outlives this line would break
# it in a way that only shows up on a release day.
( cd "$OUT_DIR" && shasum -a 256 "$DMG_NAME" > "${DMG_NAME}.sha256" )

# One decimal and a space before MB, because the only consumer of this number
# is a copy-paste into the website, which prints "19.7 MB". `du -h` says
# "21M", which is neither that format nor that precision.
SIZE=$("$PYTHON" -c \
  "import os,sys;print(f'{os.path.getsize(sys.argv[1])/1_000_000:.1f} MB')" \
  "${OUT_DIR}/${DMG_NAME}")

echo
echo "==> Done: ${OUT_DIR}/${DMG_NAME}  (${SIZE})"
cat "${OUT_DIR}/${DMG_NAME}.sha256"
echo
# The website prints the size, and that number has to match the file people
# actually download. It lives in lib/leads.ts in the site repo, and the only
# way it goes stale is if nobody is told — so tell them here, at the moment
# the number is known.
echo "Next, in openvzai-brutalist/lib/leads.ts:"
echo "  LEADS_TAG     = 'v${VERSION}'"
echo "  LEADS_VERSION = '${VERSION}'"
echo "  LEADS_SIZE    = '${SIZE}'  # LEADS_WIN_SIZE comes from the Windows build"
echo
echo "The Windows installer is built by a Windows runner, not here:"
echo "  gh workflow run windows-build.yml -f release_tag=v${VERSION}"
