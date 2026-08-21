"""The pipeline stages, and the rules for moving between them.

`status` on a prospect has always been a free string, set from five different
places with no agreement about what the values meant. That was survivable
while the last stage was "we sent something". It stops being survivable the
moment a meeting, a win or a loss has to reach a CRM, because a CRM cares
about the shape of the history, not just where the record ended up.

So the stages are enumerated, the legal moves between them are declared, and
every move goes through `advance()`, which records what happened and offers
it to the CRM.

    new → queued → contacted → replied → meeting → won
                                              ↘        ↘
                                               lost ←──┘

`opted_out` is reachable from anywhere and leaves from nowhere. Someone who
asked not to be contacted has said the last word on the subject; a pipeline
that can move them back out of it is a pipeline that will eventually email
them again.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("openvz_leads.pipeline")

STAGES = (
    "new",
    "queued",
    "contacted",
    "replied",
    "meeting",
    "won",
    "lost",
    "opted_out",
)

# Stages with no way out. `won` is here too: a won deal that moves again is a
# new deal, and merging the two loses the first one.
TERMINAL = frozenset({"won", "lost", "opted_out"})

# A stage only a person may set. The agent can observe the others in a reply
# — "not interested" is a loss in their own words, "here's my calendar" is a
# meeting — but nothing in an inbox says a deal closed, and a product that
# refuses to invent a buying signal should not invent a win either.
HUMAN_ONLY = frozenset({"won"})

_ORDER = {stage: i for i, stage in enumerate(STAGES)}

# What a stage means, in one line. Used by the dashboard and `stage --help`
# so the vocabulary is defined in exactly one place.
DESCRIPTIONS = {
    "new": "Found. Not yet analysed or written to.",
    "queued": "Outreach drafted — waiting on review, or on a send.",
    "contacted": "Something actually went out.",
    "replied": "They answered.",
    "meeting": "A meeting is booked.",
    "won": "Closed. They bought.",
    "lost": "Closed. They did not.",
    "opted_out": "Asked not to be contacted. Final.",
}

# Legacy values that predate this module, mapped to what they meant. Anything
# unrecognised becomes "new", which is the only safe wrong answer: it puts a
# record back at the start of the pipeline rather than skipping it forward
# past an outreach step.
_ALIASES = {
    "": "new",
    "closed": "won",
    "unsubscribed": "opted_out",
    "opted-out": "opted_out",
    "not_interested": "lost",
    "sent": "contacted",
}


def normalize(stage: str) -> str:
    """Coerce any stored status into a known stage."""
    value = (stage or "").strip().lower()
    if value in _ORDER:
        return value
    return _ALIASES.get(value, "new")


def index(stage: str) -> int:
    return _ORDER[normalize(stage)]


def can_move(current: str, target: str) -> tuple[bool, str]:
    """Whether a move is legal, and a sentence explaining it if not."""
    current = normalize(current)
    target = normalize(target)

    if target not in _ORDER:
        return False, f"'{target}' is not a stage. Use one of: {', '.join(STAGES)}."
    if current == target:
        return False, f"Already at '{target}'."
    if target == "opted_out":
        return True, ""  # always reachable, from anywhere
    if current in TERMINAL:
        return False, (
            f"'{current}' is final. Reopening a closed record hides what "
            f"happened the first time — create a new one instead."
        )
    if target == "new":
        return False, "Nothing moves back to 'new'."
    return True, ""


async def advance(
    state,
    prospect_id: str,
    to_stage: str,
    *,
    reason: str = "",
    actor: str = "agent",
    crm=None,
    force: bool = False,
) -> bool:
    """Move a prospect to a stage, record it, and offer it to the CRM.

    Returns False on an illegal move rather than raising: callers are agents
    mid-cycle, and an out-of-order reply (a "thanks, not interested" arriving
    after the record was already closed) is a normal event, not a bug.

    `force` exists for the dashboard, where a person correcting a mistake
    outranks the state machine. It still records the move, so the correction
    is visible in the history rather than looking like it was always so.
    """
    target = normalize(to_stage)
    prospect = await state.get_prospect(prospect_id)
    if prospect is None:
        logger.warning(f"Cannot move {prospect_id}: no such prospect.")
        return False

    current = normalize(prospect.status)
    if target in HUMAN_ONLY and actor != "human" and not force:
        logger.warning(
            f"Refusing to move {prospect_id} to '{target}' — only a person "
            f"can set that. Nothing in an inbox proves it."
        )
        return False

    ok, why = can_move(current, target)
    if not ok and not force:
        logger.info(f"Not moving {prospect_id} {current} → {target}: {why}")
        return False

    await state.set_prospect_stage(
        prospect_id,
        target,
        from_stage=current,
        reason=reason,
        actor=actor,
    )
    logger.info(
        f"Stage: {prospect.full_name() or prospect_id} {current} → {target}"
        + (f" ({reason})" if reason else "")
    )

    if crm is not None and crm.wants(target):
        # Best-effort and inline: one webhook is cheap, and a stage change the
        # CRM hears about immediately is worth more than a tidy queue. What
        # fails here stays unsynced and is retried by sync_pending().
        await _push_one(state, crm, prospect_id)
    return True


async def sync_pending(state, crm, limit: int = 50) -> tuple[int, int]:
    """Retry every stage change the CRM has not acknowledged.

    Returns (sent, failed). Called from the heartbeat, so an outage during a
    stage change costs a delay rather than the record of it.
    """
    if crm is None or not crm.enabled:
        return 0, 0

    events = await state.get_unsynced_stage_events(limit=limit)
    sent = failed = 0
    for event in events:
        if not crm.wants(event.get("to_stage", "")):
            # Not an error — the config says this stage is not the CRM's
            # business. Mark it done so it stops being re-read every cycle.
            await state.mark_stage_event_synced(event["id"], ok=True)
            continue
        if await _deliver(state, crm, event):
            sent += 1
        else:
            failed += 1
    if sent or failed:
        logger.info(f"CRM sync: {sent} sent, {failed} still pending.")
    return sent, failed


async def _push_one(state, crm, prospect_id: str):
    """Deliver this prospect's outstanding events, oldest first.

    Oldest first, and stopping at the first failure, because the alternative
    is a CRM whose history reads backwards: an earlier push that failed would
    be replayed *after* the stage that followed it, so a record would show
    "won" and then "contacted". Order is the only thing a stage history has.
    """
    events = await state.get_unsynced_stage_events(limit=50)
    for event in events:
        if event.get("prospect_id") != prospect_id:
            continue
        if not crm.wants(event.get("to_stage", "")):
            await state.mark_stage_event_synced(event["id"], ok=True)
            continue
        if not await _deliver(state, crm, event):
            return


async def _deliver(state, crm, event: dict) -> bool:
    from openvz_leads.integrations.crm import build_payload

    prospect = await state.get_prospect(event.get("prospect_id", ""))
    if prospect is None:
        # The record is gone; the event can never be delivered meaningfully.
        await state.mark_stage_event_synced(
            event["id"], ok=False, error="prospect no longer exists"
        )
        return False

    company = None
    if getattr(prospect, "company_id", ""):
        try:
            company = await state.get_company(prospect.company_id)
        except Exception:
            company = None

    payload = build_payload(event, prospect, company, prospect.profile())
    ok, error, permanent = await crm.push(payload)
    if ok:
        await state.mark_stage_event_synced(event["id"], ok=True)
        return True

    if permanent:
        logger.error(f"CRM rejected a stage change permanently: {error}")
        await state.mark_stage_event_synced(event["id"], ok=False, error=error)
    else:
        logger.warning(f"CRM sync failed, will retry: {error}")
    return False
