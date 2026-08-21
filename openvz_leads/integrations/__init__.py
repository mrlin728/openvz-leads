"""External integrations: page reading, CRM sync, Instantly, LinkedIn, email.

Submodules are intentionally NOT imported here so that optional heavy
dependencies (playwright, dnspython, aiosmtplib, crawl4ai, browser-use) are
only loaded by the agents that actually use them.
"""

__all__ = [
    "crawler",
    "crm",
    "instantly",
    "linkedin",
    "email_finder",
    "calendar",
]
