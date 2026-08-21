"""Every text file this product touches is UTF-8, on every platform.

This exists because of a bug that shipped in v1.0.0 and stayed invisible for
a release.

On Windows, `open(path)` and `Path.read_text()` use the *locale* encoding —
cp1252 on an English install — not UTF-8. Every file this product reads and
writes has non-ASCII in it: the shipped `openvz-leads.yaml` is full of `──`
section rules and Chinese comments, the prompts and skills are prose, the
account briefs can be written in any language, and the exports carry company
names. So on Windows, reading the config raised UnicodeDecodeError.

It did not crash. `get_setup_status` wraps that read in `except Exception`,
so the dashboard simply reported "product not configured" to someone whose
product was configured, and the installer shipped green.

The fix is one keyword argument in about twenty places. Keeping it there is
what this file is for: a Windows-only failure found by a Windows-only CI run
is found at the latest possible moment, so the rule is enforced here, on
every platform, in a second.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "openvz_leads"

# Binary modes carry no encoding, and passing one is a TypeError.
_BINARY = ("b",)

# `open()` for a subprocess's stdout hands Popen a file descriptor; the text
# wrapper's encoding is never consulted for the child's output. It is spelled
# out anyway, so this list stays empty and nobody has to remember exceptions.
ALLOWED_WITHOUT_ENCODING: set[tuple[str, int]] = set()


def _python_files():
    return sorted(PACKAGE.rglob("*.py"))


def _is_binary_mode(node: ast.Call) -> bool:
    """True when open() was given a mode containing 'b'."""
    mode = None
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = node.args[1].value
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return isinstance(mode, str) and any(b in mode for b in _BINARY)


def _has_encoding(node: ast.Call) -> bool:
    return any(kw.arg == "encoding" for kw in node.keywords)


def _call_name(node: ast.Call) -> str:
    """'open', 'read_text', 'write_text', or '' for anything else."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _offenders_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in ("open", "read_text", "write_text"):
            continue
        if name == "open":
            # webbrowser.open, os.open and friends are not file-text calls.
            if isinstance(node.func, ast.Attribute) and not isinstance(
                node.func.value, ast.Name
            ):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.value.id not in (
                "io",
            ):
                # `something.open(...)` — webbrowser.open, Path.open, etc.
                # Path.open takes encoding too, so only skip the known
                # non-file ones.
                if node.func.value.id in ("webbrowser", "os", "subprocess"):
                    continue
            if _is_binary_mode(node):
                continue
        if _has_encoding(node):
            continue
        rel = path.relative_to(PACKAGE.parent).as_posix()
        if (rel, node.lineno) in ALLOWED_WITHOUT_ENCODING:
            continue
        found.append(f"{rel}:{node.lineno}  {name}(...) without encoding=")
    return found


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_text_io_declares_utf8(path):
    offenders = _offenders_in(path)
    assert not offenders, (
        "These read or write a text file without saying which encoding.\n"
        "On Windows that means cp1252, and every file here has non-ASCII in "
        "it.\nAdd encoding=\"utf-8\":\n  " + "\n  ".join(offenders)
    )


def test_the_shipped_config_is_not_ascii():
    """Guards the premise of the rule above.

    If the shipped config were ever reduced to plain ASCII, the tests would
    still pass on Windows for the wrong reason, and the next comment added in
    Chinese would break an install rather than a build.
    """
    config = PACKAGE.parent / "openvz-leads.yaml"
    if not config.exists():
        pytest.skip("running outside a checkout")
    text = config.read_text(encoding="utf-8")
    assert not text.isascii(), (
        "openvz-leads.yaml is now pure ASCII. That is fine, but it means the "
        "Windows encoding bug this suite guards against would no longer "
        "reproduce — keep the rule, and delete this test deliberately."
    )


def test_a_config_full_of_box_drawing_survives_a_round_trip(tmp_path):
    """The actual failure, end to end.

    Not a Windows-only test: it passes everywhere. Its value is that it uses
    the real shipped config, so if someone reintroduces a bare read_text()
    inside apply_to_file, this is the test whose name says what broke.
    """
    import yaml

    from openvz_leads.icp import ICPDraft, apply_to_file

    source = PACKAGE.parent / "openvz-leads.yaml"
    if not source.exists():
        pytest.skip("running outside a checkout")

    target = tmp_path / "openvz-leads.yaml"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    apply_to_file(
        ICPDraft(industries=["牙科诊所"], request="帮我找美国牙科诊所"), target
    )

    written = target.read_text(encoding="utf-8")
    assert "──" in written, "the section rules were lost"
    parsed = yaml.safe_load(written)
    assert parsed["icp"]["industries"] == ["牙科诊所"]
    assert parsed["icp"]["request"] == "帮我找美国牙科诊所"
