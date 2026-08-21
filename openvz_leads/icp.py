"""Turning a sentence into an ICP.

The rest of this product runs on a structured ICP: industries, size, titles,
geography. Writing that by hand is a form filled in before you know what you
want, and it is the reason prospecting tools feel like databases rather than
colleagues. This module accepts what a person would actually say —

    帮我找美国牙科诊所
    Find dental clinics in California with outdated websites and 5-50 employees

— and produces the structure, plus an honest account of what it inferred that
you never said.

Three things this deliberately does NOT do:

- **Guess silently.** Anything not present in the request lands in
  ``assumptions`` and is shown before it is saved. A geography invented on
  your behalf sends the Scout somewhere you did not ask about, and you would
  never find out from the results.
- **Discard the qualifier.** "with outdated websites" is not an industry, a
  size or a place, so a four-field parse throws it away and returns clinics
  that are perfectly fine. It goes to ``keywords``, which reach both the
  search queries and the account analysis.
- **Require a model.** ``heuristic_parse`` runs when there is no model, the
  call fails, or the answer is unusable. It is worse — and says so, with
  ``confidence: low`` — but the box still does something when you type in it.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("openvz_leads.icp")

# Roughly a sentence or two. Past this it is a document, and the parse gets
# worse rather than better because the request stops being a single request.
MAX_REQUEST_CHARS = 2000

CONFIDENCE_LEVELS = ("low", "medium", "high")


def _unquote(text: str) -> str:
    """Drop quotes that wrap the whole value, keep quotes inside it.

    A model returning `"SaaS"` means SaaS. A model returning
    `"Outdated website" is checked during analysis` means that whole sentence —
    stripping quote characters blindly eats the first word's opening quote and
    leaves the reader with a stray one.
    """
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1].strip()
    return text


def _as_list(value, limit: int = 12, max_len: int = 80) -> list[str]:
    """Coerce whatever the model returned into a clean list of short strings.

    Runs in "before" mode, ahead of type coercion. A model that answers with a
    comma-joined string, a null, or a list containing a dict must not raise a
    ValidationError — that would throw away an otherwise good parse over
    formatting, and the whole point of the fallback path is that we degrade
    instead of failing.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;、，]", value)
    elif isinstance(value, (list, tuple, set)):
        parts = []
        for item in value:
            if isinstance(item, dict):
                # Some models answer [{"name": "SaaS"}] no matter how you ask.
                item = item.get("name") or item.get("value") or ""
            parts.append(str(item))
    else:
        parts = [str(value)]

    out: list[str] = []
    seen = set()
    for part in parts:
        cleaned = _unquote(str(part).strip())
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned[:max_len])
        if len(out) >= limit:
            break
    return out


class ICPDraft(BaseModel):
    """A parsed request, before anyone has agreed to it.

    Every field is optional and every validator is permissive, because this
    object's job is to survive contact with a model's output. Validation that
    matters happens when it is written into the config, which is typed.
    """

    industries: list[str] = Field(default_factory=list)
    company_size: str = ""
    titles: list[str] = Field(default_factory=list)
    geography: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    # What the parser supplied that the request did not contain. Shown before
    # saving — this is the difference between a helpful default and a lie.
    assumptions: list[str] = Field(default_factory=list)
    confidence: str = "medium"
    # A sentence back to the user in their own words, for the confirmation
    # step: "Dental practices in California, 5-50 staff, decision-makers."
    summary: str = ""
    request: str = ""
    # Which path produced this: "model" or "heuristic".
    via: str = "model"

    @field_validator(
        "industries", "titles", "geography", "keywords", "exclusions",
        mode="before",
    )
    @classmethod
    def _listify(cls, v):
        return _as_list(v)

    @field_validator("assumptions", mode="before")
    @classmethod
    def _listify_sentences(cls, v):
        """Assumptions are sentences addressed to the user, so they get room.

        They are also the one field that must not be split on commas — "You
        did not name titles, so I inferred them" is one assumption, not two.
        """
        if isinstance(v, str):
            v = [v]
        return _as_list(v, limit=8, max_len=300)

    @field_validator("company_size", "summary", "request", mode="before")
    @classmethod
    def _stringify(cls, v):
        if v is None:
            return ""
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        return str(v).strip()

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, v):
        """A model asked for low/medium/high will eventually answer '7/10'."""
        text = str(v or "").strip().lower()
        if text in CONFIDENCE_LEVELS:
            return text
        if text in ("very high", "certain", "sure"):
            return "high"
        if text in ("very low", "unsure", "unknown", "none", ""):
            return "low"
        return "medium"

    def is_usable(self) -> bool:
        """Enough to search on. Industry or keywords is the real minimum."""
        return bool(self.industries or self.keywords)

    def describe(self) -> str:
        """Multi-line, human-readable. Used by the CLI and the dashboard."""
        lines = []
        if self.summary:
            lines.append(self.summary)
            lines.append("")
        rows = [
            ("Industries", ", ".join(self.industries) or "—"),
            ("Company size", self.company_size or "—"),
            ("Geography", ", ".join(self.geography) or "—"),
            ("Titles", ", ".join(self.titles) or "—"),
        ]
        if self.keywords:
            rows.append(("Must also match", ", ".join(self.keywords)))
        if self.exclusions:
            rows.append(("Rules out", ", ".join(self.exclusions)))
        width = max(len(label) for label, _ in rows)
        for label, value in rows:
            lines.append(f"{label.ljust(width)}  {value}")
        lines.append("")
        lines.append(f"Confidence: {self.confidence} (parsed by {self.via})")
        if self.assumptions:
            lines.append("")
            lines.append("Filled in for you — you did not say these:")
            for item in self.assumptions:
                lines.append(f"  · {item}")
        return "\n".join(lines)

    def to_config_dict(self) -> dict:
        """The `icp:` block as it should appear in openvz-leads.yaml."""
        return {
            "industries": self.industries,
            "company_size": self.company_size,
            "titles": self.titles,
            "geography": self.geography,
            "keywords": self.keywords,
            "exclusions": self.exclusions,
            "request": self.request,
        }


# ── Parsing ───────────────────────────────────────────────────────────

_PROMPT_FALLBACK = """\
Turn this prospecting request into a structured Ideal Customer Profile.

REQUEST
{request}

Return JSON with exactly these keys:
- industries: string[]  — what kind of business, in the words a search engine
  would find them by ("dental clinic", not "healthcare")
- company_size: string  — an employee range like "5-50 employees", or "" if
  the request does not say
- titles: string[]      — job titles worth reaching. If the request names no
  titles, infer the ones who decide for a company of this kind and shape
- geography: string[]   — countries, states or cities named in the request
- keywords: string[]    — qualifiers that are none of the above ("outdated
  website", "recently funded", "hiring"). Do not invent these
- exclusions: string[]  — what the request rules out, if anything
- assumptions: string[] — every field you filled in that the request did not
  state, one short sentence each, written to the user in second person
- confidence: "low" | "medium" | "high"
- summary: string       — one sentence restating the target, in the same
  language as the request

Rules:
- Never invent a geography. If the request names no place, leave it empty and
  say so in assumptions.
- titles is the one field you may infer freely, because a request almost
  never names them and searching with none is worse. Say that you did.
- Keep the user's language for summary and assumptions.
"""


async def parse_request(brain, text: str, current=None) -> ICPDraft:
    """Parse a natural-language request into an ICP draft.

    Falls back to ``heuristic_parse`` when there is no usable model answer, so
    this function always returns a draft — check ``is_usable()`` and ``via``.
    """
    request = (text or "").strip()[:MAX_REQUEST_CHARS]
    if not request:
        return ICPDraft(request="", confidence="low", via="heuristic")

    prompt = ""
    if brain is not None:
        prompt = brain.load_prompt("icp", request=request)
    if not prompt:
        prompt = _PROMPT_FALLBACK.format(request=request)

    if current is not None:
        # Context, not a default: an existing ICP tells the parser what kind
        # of business this seller is in, which sharpens title inference.
        prompt += (
            "\n\nFor context only — the ICP currently configured. Do not copy "
            "it unless the request implies the same thing:\n"
            f"- industries: {', '.join(getattr(current, 'industries', []) or []) or '—'}\n"
            f"- company_size: {getattr(current, 'company_size', '') or '—'}\n"
            f"- titles: {', '.join(getattr(current, 'titles', []) or []) or '—'}\n"
            f"- geography: {', '.join(getattr(current, 'geography', []) or []) or '—'}\n"
        )

    parsed = None
    if brain is not None:
        try:
            parsed = await brain.think_json(prompt, session_id="leads-icp")
        except Exception as e:
            logger.warning(f"ICP parse failed: {e}")

    if isinstance(parsed, list) and parsed:
        parsed = parsed[0]
    if isinstance(parsed, dict):
        try:
            draft = ICPDraft(**{**parsed, "request": request, "via": "model"})
        except Exception as e:
            logger.warning(f"ICP parse returned an unusable shape: {e}")
            draft = None
        if draft is not None and draft.is_usable():
            return _post_process(draft)
        if draft is not None:
            logger.info("ICP parse produced nothing searchable; using heuristics.")

    fallback = heuristic_parse(request)
    fallback.assumptions.insert(
        0,
        "Parsed without a model — check every field below before saving.",
    )
    return _post_process(fallback)


# Said only when nothing else said it. The model writes its own assumptions,
# in the user's language; these are the floor for the path that cannot.
_FALLBACK_NOTES = (
    ("geography", "No location in the request — searching everywhere."),
    ("titles", "No job titles in the request — the Scout will take whoever it finds."),
    ("company_size", "No size in the request — companies of any size qualify."),
)


def _post_process(draft: ICPDraft) -> ICPDraft:
    """Last-mile tidying that should hold no matter which path produced it.

    The empty-field notes are added only when the parse produced no
    assumptions of its own. A model that already wrote "你没有限定规模" does not
    need an English sentence appended saying the same thing — that is a
    duplicate in the wrong language, and it makes the honest part of this
    screen look automated rather than considered.
    """
    if not draft.assumptions:
        for field, note in _FALLBACK_NOTES:
            if not getattr(draft, field):
                draft.assumptions.append(note)
    if not draft.summary:
        draft.summary = ", ".join(
            part for part in (
                ", ".join(draft.industries),
                draft.company_size,
                ", ".join(draft.geography),
            ) if part
        )
    return draft


# ── The no-model path ─────────────────────────────────────────────────

# Deliberately small. A big lexicon here would be a worse version of the
# model path and would rot; this exists so the input box does something
# reasonable when Claude is not reachable, not to compete with it.
_GEO_LEXICON = {
    "united states": "United States", "usa": "United States", "us": "United States",
    "america": "United States", "美国": "United States",
    "canada": "Canada", "加拿大": "Canada",
    "uk": "United Kingdom", "united kingdom": "United Kingdom",
    "britain": "United Kingdom", "england": "United Kingdom", "英国": "United Kingdom",
    "australia": "Australia", "澳大利亚": "Australia", "澳洲": "Australia",
    "germany": "Germany", "德国": "Germany",
    "france": "France", "法国": "France",
    "japan": "Japan", "日本": "Japan",
    "singapore": "Singapore", "新加坡": "Singapore",
    "china": "China", "中国": "China",
    "california": "California", "加州": "California",
    "texas": "Texas", "new york": "New York", "florida": "Florida",
    "london": "London", "berlin": "Berlin", "sydney": "Sydney",
}

_SIZE_PATTERNS = (
    # "5-50 employees", "5 to 50 employees", "5–50 人"
    re.compile(r"(\d{1,5})\s*(?:-|–|—|to|~|至|到)\s*(\d{1,5})\s*(?:\+)?\s*"
               r"(?:employees|people|staff|headcount|员工|人)?", re.I),
    # "under 200 employees"
    re.compile(r"(?:under|below|fewer than|less than|<|少于|不到)\s*(\d{1,5})", re.I),
    # "200+ employees"
    re.compile(r"(\d{1,5})\s*\+\s*(?:employees|people|staff|员工|人)", re.I),
)

_TITLE_HINTS = (
    "ceo", "cto", "cmo", "cfo", "coo", "founder", "owner", "president",
    "director", "head of", "vp", "vice president", "manager", "partner",
    "principal", "创始人", "老板", "总经理", "负责人", "经理", "总监",
)


def heuristic_parse(text: str) -> ICPDraft:
    """Best-effort structure without a model. Honest about being worse.

    It finds a size range, a place from a short lexicon, and any titles named
    outright. For the industry it keeps the user's own remaining words, which
    is what a search engine wants anyway and is visibly their phrasing rather
    than a guess wearing a field name.
    """
    request = (text or "").strip()[:MAX_REQUEST_CHARS]

    target, qualifiers = _split_qualifier(request)

    geography, target, qualifiers = _take_geography(target, qualifiers)
    company_size, target, qualifiers = _take_size(target, qualifiers)
    titles, target = _take_titles(target)

    industry = _clean_phrase(target)
    keywords = [kw for kw in (_clean_phrase(q) for q in qualifiers) if kw]

    return ICPDraft(
        industries=[industry] if industry else [],
        company_size=company_size,
        titles=titles,
        geography=geography,
        keywords=keywords,
        confidence="low",
        request=request,
        via="heuristic",
        summary=request,
    )


# "dental clinics in California WITH outdated websites AND 5-50 employees":
# everything after the qualifier marker describes traits, not the business.
_QUALIFIER_SPLIT = re.compile(r"\bwith\b|\bthat have\b|\bwho have\b|，且|，有|、且", re.I)
_QUALIFIER_JOIN = re.compile(r"\band\b|[,;、，]", re.I)


def _split_qualifier(request: str) -> tuple[str, list[str]]:
    parts = _QUALIFIER_SPLIT.split(request, maxsplit=1)
    if len(parts) < 2:
        return request, []
    head, tail = parts[0], parts[1]
    return head, [p for p in _QUALIFIER_JOIN.split(tail) if p and p.strip()]


def _strip_all(text: str, needle: str) -> str:
    """Remove a term, word-bounded for ASCII and plain for CJK (no \b there)."""
    if needle.isascii():
        return re.sub(rf"\b{re.escape(needle)}\b", " ", text, flags=re.I)
    return text.replace(needle, " ")


def _take_geography(target: str, qualifiers: list[str]):
    """Pull places out of both halves — "in California" can land either side."""
    found: list[str] = []
    for needle, canonical in _GEO_LEXICON.items():
        hit = (
            re.search(rf"\b{re.escape(needle)}\b", target, re.I)
            if needle.isascii()
            else (needle in target)
        )
        if hit and canonical not in found:
            found.append(canonical)
            target = _strip_all(target, needle)
    return found, target, qualifiers


def _take_size(target: str, qualifiers: list[str]):
    """A size range, from wherever it was written."""
    size = ""
    for pattern in _SIZE_PATTERNS:
        for i, source in enumerate([target] + qualifiers):
            match = pattern.search(source)
            if not match:
                continue
            size = _format_size(match)
            cleaned = pattern.sub(" ", source)
            if i == 0:
                target = cleaned
            else:
                qualifiers[i - 1] = cleaned
            break
        if size:
            break
    # A leftover "employees" with no number is noise in every field.
    qualifiers = [q for q in qualifiers if _clean_phrase(q)]
    return size, target, qualifiers


def _format_size(match: re.Match) -> str:
    groups = [g for g in match.groups() if g]
    if len(groups) == 2:
        return f"{groups[0]}-{groups[1]} employees"
    if not groups:
        return ""
    downward = any(
        word in match.group(0).lower()
        for word in ("under", "below", "fewer", "less", "<", "少于", "不到")
    )
    return f"1-{groups[0]} employees" if downward else f"{groups[0]}+ employees"


def _take_titles(target: str) -> tuple[list[str], str]:
    titles: list[str] = []
    for hint in _TITLE_HINTS:
        hit = (
            re.search(rf"\b{re.escape(hint)}\b", target, re.I)
            if hint.isascii()
            else (hint in target)
        )
        if not hit:
            continue
        titles.append(hint.upper() if len(hint) <= 3 and hint.isascii() else hint.title())
        target = _strip_all(target, hint)
        if len(titles) >= 4:
            break
    return titles, target


# Words that carry no target information in either language. "employees" is
# here because the number beside it has already been taken as company_size.
_STOPWORDS = (
    "find", "search for", "get me", "look for", "companies", "company",
    "businesses", "business", "employees", "people", "staff", "please",
    "and", "or", "in", "the", "for", "me", "a", "an", "of", "some",
)
_CJK_STOPWORDS = ("帮我", "请", "找一些", "找到", "寻找", "找", "一些", "的", "公司", "企业", "员工", "人")


def _clean_phrase(text: str) -> str:
    """Strip filler and punctuation; return what the user actually named."""
    out = text or ""
    for word in _CJK_STOPWORDS:
        out = out.replace(word, " ")
    for word in _STOPWORDS:
        out = re.sub(rf"\b{re.escape(word)}\b", " ", out, flags=re.I)
    out = re.sub(r"[，,。.；;:!?、]+", " ", out)
    out = re.sub(r"\s+", " ", out)
    return out.strip(" -–—")


# ── Writing it back ───────────────────────────────────────────────────

_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z_][\w-]*):")


def _scalar(value: str) -> str:
    """A string as a YAML scalar.

    json.dumps, not yaml.safe_dump: dumping a bare scalar yields a whole YAML
    *document* ("5-50 employees\n...\n"), and splicing that into a mapping
    produces a file that no longer parses. YAML is a superset of JSON, so a
    JSON string is always a valid, correctly-escaped YAML scalar — and
    ensure_ascii=False keeps 牙科诊所 readable in the file.
    """
    import json

    return json.dumps(value or "", ensure_ascii=False)


def render_icp_block(draft: ICPDraft) -> str:
    """The `icp:` block as YAML text, comments included.

    Written by hand rather than dumped, because the config file is meant to be
    read and edited and a round-tripped dump would strip every comment in it.
    The field documentation is re-emitted rather than merely preserved: a save
    replaces these lines, and a file that explains itself before the first
    `target` run and not after is worse than one that never did.
    """

    def emit_list(name: str, values: list[str], comment: list[str]) -> list[str]:
        out = [f"  # {line}" for line in comment]
        if not values:
            out.append(f"  {name}: []")
            return out
        out.append(f"  {name}:")
        out.extend(f"    - {_scalar(value)}" for value in values)
        return out

    lines = ["icp:"]
    if draft.request:
        one_line = " ".join(draft.request.split())[:160]
        lines.append(f"  # Parsed from: {one_line}")
        lines.append(f"  # Confidence: {draft.confidence}, via {draft.via}.")
    lines += emit_list("industries", draft.industries, [])
    lines.append(f"  company_size: {_scalar(draft.company_size)}")
    lines += emit_list("titles", draft.titles, [])
    lines += emit_list("geography", draft.geography, [])
    lines += emit_list(
        "keywords",
        draft.keywords,
        [
            "Traits that are none of the four fields above — \"outdated",
            "website\", \"recently funded\", \"hiring engineers\". No search",
            "engine can filter on these, so they do two things instead:",
            "loosen the search queries, and become criteria the account",
            "analysis is told to check against evidence (confirmed /",
            "contradicted / unknown) rather than assume.",
        ],
    )
    lines += emit_list(
        "exclusions",
        draft.exclusions,
        [
            "What rules an account out. Checked during analysis: an account",
            "the evidence shows matching one of these is scored 1-3, with",
            "the reason recorded.",
        ],
    )
    lines.append("  # The sentence the block above was parsed from, kept verbatim.")
    lines.append("  # The only record of what was actually asked for once")
    lines.append("  # someone hand-edits the fields.")
    lines.append(f"  request: {_scalar(draft.request)}")
    return "\n".join(lines)


def replace_icp_block(config_text: str, block: str) -> str:
    """Swap the `icp:` block in a config file, leaving everything else alone.

    Surgical on purpose. Loading and re-dumping the YAML would work and would
    also delete every comment in the file, which for this product is most of
    the documentation.
    """
    lines = config_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        match = _TOP_LEVEL_KEY.match(line)
        if match and match.group(1) == "icp":
            start = i
            break
    if start is None:
        # No icp block yet — append one rather than failing.
        separator = "" if config_text.endswith("\n") else "\n"
        return f"{config_text}{separator}\n{block}\n"

    end = len(lines)
    for j in range(start + 1, len(lines)):
        match = _TOP_LEVEL_KEY.match(lines[j])
        if match and match.group(1) != "icp":
            end = j
            break

    # Comments and blank lines immediately above the next key are that key's
    # header, not ours — hand them back.
    while end - 1 > start and (
        not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
    ):
        end -= 1

    return "\n".join(lines[:start] + block.splitlines() + lines[end:]) + "\n"


def apply_to_file(draft: ICPDraft, config_path) -> str:
    """Write the draft into openvz-leads.yaml. Returns the path written."""
    from pathlib import Path

    path = Path(config_path)
    original = path.read_text()
    updated = replace_icp_block(original, render_icp_block(draft))

    # Prove it still loads before replacing a working config with it.
    import yaml

    parsed = yaml.safe_load(updated)
    if not isinstance(parsed, dict) or "icp" not in parsed:
        raise ValueError(
            "Rewriting the icp block produced an unreadable config; "
            "nothing was changed."
        )

    path.write_text(updated)
    return str(path)
