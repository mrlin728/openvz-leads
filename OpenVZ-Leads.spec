# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — builds the macOS .app and the Windows folder build.

One spec, two platforms. Each has to be built on its own OS: PyInstaller
freezes the interpreter it is running under, so there is no cross-compiling
a Windows exe from a Mac. The Windows artefact is produced by a
windows-latest runner (see .github/workflows/windows-build.yml).

prompts/ and skills/ are bundled read-only and copied into the user's data
directory on first run (see openvz_leads/paths.py) — they are meant to be
edited, and nothing inside an installed app is.

Playwright is excluded on purpose: it is only used for LinkedIn prospecting,
which ships off because automating LinkedIn breaks their terms of service,
and bundling it plus a browser would multiply the download for a feature
most people never turn on.
"""

import re
import sys
from pathlib import Path

IS_MAC = sys.platform == "darwin"

# Read the version rather than repeating it. It used to appear here three
# times and once in packaging/windows-installer.iss, hand-synced — which is
# how you ship a dmg whose name and whose bundle disagree about what it is.
# The .app value is the one macOS shows in Get Info.
VERSION = re.search(
    r'^version\s*=\s*"([^"]+)"',
    Path("pyproject.toml").read_text(),
    re.M,
).group(1)

datas = [
    ('prompts', 'prompts'),
    ('skills', 'skills'),
    ('openvz-leads.yaml', '.'),
    ('.env.example', '.'),
    ('README.md', '.'),
    ('LICENSE', '.'),
    ('NOTICE.md', '.'),
]

hiddenimports = [
    'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on',
    'aiosqlite', 'dns.resolver', 'aiosmtplib',
]

a = Analysis(
    ['openvz_leads/app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # crawl4ai and browser_use are opt-in extras and each drags in a browser
    # runtime; bundling them would multiply the download for a tier most
    # installs never reach. The crawler detects their absence and uses the
    # basic tier, so the frozen app is fully functional without them.
    excludes=[
        'playwright', 'tkinter', 'pytest', 'IPython', 'matplotlib', 'numpy',
        'crawl4ai', 'browser_use',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='OpenVZ Leads',
    debug=False,
    strip=False,
    upx=False,
    # No console window on either platform: this is a GUI-launched app whose
    # interface is the dashboard in the browser. Logs go to a file instead,
    # because a windowless process has nowhere else to put a traceback.
    console=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name='OpenVZ Leads',
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name='OpenVZ Leads.app',
        icon=None,
        bundle_identifier='com.openvzai.leads',
        version=VERSION,
        info_plist={
            'CFBundleName': 'OpenVZ Leads',
            'CFBundleDisplayName': 'OpenVZ Leads',
            'CFBundleShortVersionString': VERSION,
            'CFBundleVersion': VERSION,
            'NSHighResolutionCapable': True,
            'LSUIElement': False,
            'NSHumanReadableCopyright': 'MIT. Derived from Harvey by Ethan Rogers.',
        },
    )
