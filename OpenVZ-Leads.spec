# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the macOS app bundle.

prompts/ and skills/ are bundled read-only and copied into the user's
Application Support directory on first run (see openvz_leads/paths.py) —
they are meant to be edited, and nothing inside a .app is.

Playwright is excluded on purpose: it is only used for LinkedIn prospecting,
which ships off because automating LinkedIn breaks their terms of service,
and bundling it plus a browser would multiply the download for a feature
most people never turn on.
"""

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
    excludes=['playwright', 'tkinter', 'pytest', 'IPython', 'matplotlib', 'numpy'],
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
    console=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name='OpenVZ Leads',
)
app = BUNDLE(
    coll,
    name='OpenVZ Leads.app',
    icon=None,
    bundle_identifier='com.openvzai.leads',
    version='1.0.0',
    info_plist={
        'CFBundleName': 'OpenVZ Leads',
        'CFBundleDisplayName': 'OpenVZ Leads',
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1.0.0',
        'NSHighResolutionCapable': True,
        # No dock-less mode: people need to be able to quit it.
        'LSUIElement': False,
        'NSHumanReadableCopyright': 'MIT. Derived from Harvey by Ethan Rogers.',
    },
)
