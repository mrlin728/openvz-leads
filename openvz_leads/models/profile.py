"""Account analysis model — what the Profiler agent produces per prospect.

This is the "understand them" half of the product. Everything here is a
*hypothesis* derived from public evidence, not a fact: `confidence` and
`evidence_gaps` exist so a human can tell the difference at a glance.
"""

from pydantic import BaseModel, Field, field_validator

STRENGTHS = ("high", "medium", "low")
CONTACT_ROLES = (
    "economic_buyer",
    "champion",
    "user",
    "gatekeeper",
    "influencer",
    "unknown",
)


def _clean_list(values, limit: int) -> list[str]:
    """Coerce anything list-ish into a bounded list of non-empty strings."""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    out = []
    for v in values:
        if v is None or isinstance(v, bool):
            continue  # str(None) == "None" would read as real content
        text = str(v).strip()
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


class BuyingSignal(BaseModel):
    signal: str = ""
    evidence: str = ""  # where this came from — keeps the model honest
    strength: str = "low"

    @field_validator("strength", mode="before")
    @classmethod
    def _known_strength(cls, v) -> str:
        normalized = str(v or "").strip().lower()
        return normalized if normalized in STRENGTHS else "low"


class OpeningAngle(BaseModel):
    angle: str = ""
    why_it_lands: str = ""


class CompanySnapshot(BaseModel):
    what_they_do: str = ""
    market: str = ""
    likely_size: str = ""
    positioning: str = ""


class DecisionChain(BaseModel):
    this_contact_role: str = "unknown"
    likely_economic_buyer: str = ""
    likely_champion: str = ""
    likely_blocker: str = ""

    @field_validator("this_contact_role", mode="before")
    @classmethod
    def _known_role(cls, v) -> str:
        normalized = str(v or "").strip().lower().replace(" ", "_")
        return normalized if normalized in CONTACT_ROLES else "unknown"


class AccountProfile(BaseModel):
    fit_score: int = 0  # 1-10, how well this account matches the ICP
    fit_reasons: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    company_snapshot: CompanySnapshot = CompanySnapshot()
    buying_signals: list[BuyingSignal] = Field(default_factory=list)
    pain_hypotheses: list[str] = Field(default_factory=list)
    decision_chain: DecisionChain = DecisionChain()
    opening_angles: list[OpeningAngle] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    confidence: str = "low"
    evidence_gaps: list[str] = Field(default_factory=list)

    # mode="before": the model routinely receives whatever the LLM emitted
    # ("8", 8.0, "high"), so coerce first — the default "after" validator
    # would never run, because type coercion would already have failed.
    @field_validator("fit_score", mode="before")
    @classmethod
    def _clamp_score(cls, v) -> int:
        try:
            return max(0, min(10, int(float(v))))
        except (TypeError, ValueError):
            return 0

    @field_validator("confidence", mode="before")
    @classmethod
    def _known_confidence(cls, v) -> str:
        normalized = str(v or "").strip().lower()
        return normalized if normalized in STRENGTHS else "low"

    @classmethod
    def from_raw(cls, raw) -> "AccountProfile | None":
        """Build a profile from whatever the model returned.

        Tolerant by design: a missing or malformed section is dropped rather
        than failing the whole analysis. Returns None only when there is
        nothing usable at all.
        """
        if not isinstance(raw, dict):
            return None

        snapshot = raw.get("company_snapshot")
        if not isinstance(snapshot, dict):
            snapshot = {}
        chain = raw.get("decision_chain")
        if not isinstance(chain, dict):
            chain = {}

        signals = []
        for item in raw.get("buying_signals") or []:
            if isinstance(item, dict):
                signals.append(BuyingSignal(**{
                    "signal": str(item.get("signal") or "").strip(),
                    "evidence": str(item.get("evidence") or "").strip(),
                    "strength": item.get("strength", "low"),
                }))
            elif isinstance(item, str) and item.strip():
                signals.append(BuyingSignal(signal=item.strip()))
        signals = [s for s in signals if s.signal][:6]

        angles = []
        for item in raw.get("opening_angles") or []:
            if isinstance(item, dict):
                angles.append(OpeningAngle(
                    angle=str(item.get("angle") or "").strip(),
                    why_it_lands=str(item.get("why_it_lands") or "").strip(),
                ))
            elif isinstance(item, str) and item.strip():
                angles.append(OpeningAngle(angle=item.strip()))
        angles = [a for a in angles if a.angle][:4]

        profile = cls(
            fit_score=raw.get("fit_score", 0),
            fit_reasons=_clean_list(raw.get("fit_reasons"), 5),
            risks=_clean_list(raw.get("risks"), 5),
            company_snapshot=CompanySnapshot(
                what_they_do=str(snapshot.get("what_they_do") or "").strip(),
                market=str(snapshot.get("market") or "").strip(),
                likely_size=str(snapshot.get("likely_size") or "").strip(),
                positioning=str(snapshot.get("positioning") or "").strip(),
            ),
            buying_signals=signals,
            pain_hypotheses=_clean_list(raw.get("pain_hypotheses"), 5),
            decision_chain=DecisionChain(
                this_contact_role=chain.get("this_contact_role", "unknown"),
                likely_economic_buyer=str(chain.get("likely_economic_buyer") or "").strip(),
                likely_champion=str(chain.get("likely_champion") or "").strip(),
                likely_blocker=str(chain.get("likely_blocker") or "").strip(),
            ),
            opening_angles=angles,
            avoid=_clean_list(raw.get("avoid"), 5),
            confidence=raw.get("confidence", "low"),
            evidence_gaps=_clean_list(raw.get("evidence_gaps"), 5),
        )

        # An analysis with no substance at all is worse than none: it would
        # read as "we looked and found nothing" when we simply failed.
        if not (
            profile.fit_reasons
            or profile.pain_hypotheses
            or profile.opening_angles
            or profile.company_snapshot.what_they_do
        ):
            return None
        return profile

    def brief(self) -> str:
        """Compact plain-text form, for injecting into the Writer's prompt."""
        lines = [f"Fit {self.fit_score}/10 (confidence: {self.confidence})"]
        if self.company_snapshot.what_they_do:
            lines.append(f"What they do: {self.company_snapshot.what_they_do}")
        if self.pain_hypotheses:
            lines.append("Likely pains: " + "; ".join(self.pain_hypotheses[:3]))
        if self.buying_signals:
            lines.append(
                "Signals: "
                + "; ".join(f"{s.signal} ({s.strength})" for s in self.buying_signals[:3])
            )
        if self.opening_angles:
            lines.append(
                "Opening angles: " + "; ".join(a.angle for a in self.opening_angles[:2])
            )
        if self.avoid:
            lines.append("Do NOT say: " + "; ".join(self.avoid[:3]))
        return "\n".join(lines)
