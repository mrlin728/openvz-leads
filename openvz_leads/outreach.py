"""Turning a written email into the one this person receives.

The Writer produces copy with merge variables in it — `{{first_name}}`,
`{{company}}`, `{{title}}` — because the same sequence goes to many people.
Instantly substituted them at send time. Gmail will not: it takes a finished
message and delivers it, so if this module does not do the substitution the
prospect receives an email containing the literal text `{{first_name}}`.

That is the single most embarrassing thing this product could send, and it is
one forgotten step away at all times.

## What happens when a value is missing

Not all variables fail the same way, so they are not treated the same way.

- **first_name** has a safe fallback. "Hi there," is a normal thing for a
  person to write; nobody reads it as a mistake.
- **company** and **title** do not. "I saw 's site" and "as a at Acme" are
  not sentences, and there is no substitution that makes them into one. A
  message that needs one and cannot get it is *not sent* — the queued step is
  cancelled with the reason recorded, and the prospect keeps the record of
  why nothing went out.

Refusing to send is the right failure here. The alternative is a cold email
that visibly came out of a machine with a hole in it, sent to someone whose
first impression of the company this is done in the name of is that email.
"""

from __future__ import annotations

import re

# The variables the Writer is told it may use. Anything else in double braces
# is a typo, and is reported rather than silently left in the text.
KNOWN_VARIABLES = ("first_name", "company", "title")

# Only where the sentence survives it. See the module docstring.
FALLBACKS = {"first_name": "there"}

_VARIABLE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def values_for(prospect) -> dict[str, str]:
    """The substitutions for one prospect, before fallbacks."""
    return {
        "first_name": (getattr(prospect, "first_name", "") or "").strip(),
        "company": (getattr(prospect, "company", "") or "").strip(),
        "title": (getattr(prospect, "title", "") or "").strip(),
    }


def render(text: str, prospect) -> tuple[str, list[str]]:
    """Substitute merge variables. Returns (text, blockers).

    `blockers` names the variables that were needed, had no value, and have
    no safe fallback. A non-empty list means this message must not be sent.
    Unknown variables are reported too — `{{prodcut_name}}` reaching a
    prospect is the same class of failure as an empty one.
    """
    if not text:
        return "", []

    values = values_for(prospect)
    blockers: list[str] = []

    def substitute(match: re.Match) -> str:
        name = match.group(1)
        if name not in KNOWN_VARIABLES:
            blockers.append(f"unknown variable {{{{{name}}}}}")
            return match.group(0)
        value = values.get(name, "")
        if value:
            return value
        if name in FALLBACKS:
            return FALLBACKS[name]
        blockers.append(f"{{{{{name}}}}} has no value for this prospect")
        return match.group(0)

    rendered = _VARIABLE.sub(substitute, text)
    # Dedupe while keeping order: the same variable appearing four times is
    # one problem, and a reason field listing it four times reads as four.
    seen = set()
    unique = [b for b in blockers if not (b in seen or seen.add(b))]
    return rendered, unique


def render_email(
    subject: str, body: str, prospect, footer: str = ""
) -> tuple[str, str, list[str]]:
    """Render a whole message. Returns (subject, body, blockers).

    The footer is appended verbatim and is never templated: it carries the
    opt-out line and a postal address, and a merge variable failing inside a
    legal notice would be a legal notice with a hole in it.
    """
    rendered_subject, subject_blockers = render(subject, prospect)
    rendered_body, body_blockers = render(body, prospect)

    blockers = subject_blockers + [b for b in body_blockers if b not in subject_blockers]

    if footer:
        rendered_body = f"{rendered_body.rstrip()}\n\n--\n{footer.strip()}"

    return rendered_subject.strip(), rendered_body, blockers


def follow_up_subject(subject: str) -> str:
    """The subject a threaded follow-up should carry.

    A reply in a thread keeps the original subject with `Re:` in front, and
    that is what makes a mail client show one conversation. A follow-up with
    its own new subject line is a second cold email that happens to be from
    the same person.
    """
    cleaned = (subject or "").strip()
    if not cleaned:
        return ""
    if cleaned[:3].lower() == "re:":
        return cleaned
    return f"Re: {cleaned}"
