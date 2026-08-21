"""Sending through the user's own Gmail mailbox.

This is the alternative to a sending platform, and the trade is worth stating
plainly. A platform warms domains, rotates inboxes, and tells you when your
deliverability is falling apart. Gmail does none of that: it is a mailbox. A
personal mailbox that suddenly emits fifty cold emails a day is a mailbox
Google will rate-limit, and possibly worse.

What you get back is that the mail comes from a real person at a real
address, it threads properly in the recipient's client because it is a real
thread, replies land in the sender's actual inbox, and no third party holds
the prospect list.

Three things this module owns that the platform used to:

- **Threading.** Follow-ups are `In-Reply-To` the previous message, so they
  land in the same conversation instead of arriving as three unrelated cold
  emails, which is how a sequence reads as harassment.
- **Reply detection.** Nothing else will tell us they answered, and a
  follow-up sent after a reply is the worst failure this product has.
- **The footer.** See config.GmailFooterConfig — commercial mail needs a
  working opt-out and a postal address, and Gmail will not add one for you.

## Talking to Google

Plain HTTP, not `google-api-python-client`. That library and its dependency
tree would add tens of megabytes to a desktop build that currently fits in
21, for four endpoints and an OAuth exchange.

## Credentials

Two separate things, deliberately kept apart:

- The **client** (`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in .env)
  identifies this installation of the software to Google. The user creates
  it once in their own Google Cloud project.
- The **account** is authorised by `openvz-leads gmail login`, which runs the
  loopback OAuth flow in a browser. Its refresh token is written to the
  workspace with 0600 and never leaves the machine.

An installed-app client secret is not really a secret — Google says so — but
the refresh token is, which is why they are stored separately and only one of
them is in the file people paste into bug reports.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import socket
import time
import urllib.parse
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

import httpx

logger = logging.getLogger("openvz_leads.gmail")

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"

SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
READ_SCOPES = {
    # Headers only: enough to see that a reply arrived and stop following up,
    # without the software ever holding the text of someone's reply.
    "metadata": "https://www.googleapis.com/auth/gmail.metadata",
    # Bodies too, so the Handler can classify intent.
    "readonly": "https://www.googleapis.com/auth/gmail.readonly",
    "none": "",
}

TOKEN_FILE_NAME = "gmail-token.json"
HTTP_TIMEOUT = 30.0

# Refresh this far before expiry rather than after a 401. A send that fails on
# an expired token is a send that has to be retried, and a retried send is the
# thing most likely to become a duplicate.
REFRESH_MARGIN_SECONDS = 120


class GmailError(Exception):
    """Something went wrong talking to Gmail. Carries a usable message."""


class GmailNotAuthorised(GmailError):
    """No usable token. The user has to run `openvz-leads gmail login`."""


@dataclass
class SentMessage:
    message_id: str = ""
    thread_id: str = ""
    # The RFC 2822 Message-ID header, which is what a follow-up references.
    rfc_message_id: str = ""


@dataclass
class ThreadReply:
    """A message in our thread that we did not send."""

    message_id: str = ""
    from_address: str = ""
    received_at: int = 0  # epoch ms, as Gmail reports it
    subject: str = ""
    # Empty under the metadata scope, which is the point of that scope.
    body: str = ""


@dataclass
class GmailCredentials:
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    access_token: str = ""
    expires_at: float = 0.0
    scopes: list[str] = field(default_factory=list)
    email_address: str = ""

    def usable(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    def fresh(self) -> bool:
        return bool(self.access_token) and time.time() < (
            self.expires_at - REFRESH_MARGIN_SECONDS
        )


def token_path():
    from openvz_leads import paths

    return paths.data_dir() / TOKEN_FILE_NAME


def scopes_for(read_scope: str) -> list[str]:
    """The scopes to request for a given read setting."""
    scopes = [SEND_SCOPE]
    read = READ_SCOPES.get(read_scope, "")
    if read:
        scopes.append(read)
    return scopes


def load_credentials(env=None) -> GmailCredentials:
    """Read the stored token, merged with the client from .env.

    Never raises on a missing or corrupt file: the caller decides whether not
    being authorised is a problem, and for most of them it is a message
    rather than a crash.
    """
    if env is None:
        from openvz_leads.config import load_env

        env = load_env()

    creds = GmailCredentials(
        client_id=getattr(env, "google_client_id", ""),
        client_secret=getattr(env, "google_client_secret", ""),
    )

    path = token_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return creds
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not read {path}: {e}. Re-run 'openvz-leads gmail login'.")
        return creds

    if not isinstance(data, dict):
        return creds
    creds.refresh_token = str(data.get("refresh_token") or "")
    creds.access_token = str(data.get("access_token") or "")
    try:
        creds.expires_at = float(data.get("expires_at") or 0)
    except (TypeError, ValueError):
        creds.expires_at = 0.0
    scopes = data.get("scopes")
    creds.scopes = [str(s) for s in scopes] if isinstance(scopes, list) else []
    creds.email_address = str(data.get("email_address") or "")
    return creds


def save_credentials(creds: GmailCredentials) -> None:
    """Persist the refresh token, readable only by this user."""
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "refresh_token": creds.refresh_token,
        "access_token": creds.access_token,
        "expires_at": creds.expires_at,
        "scopes": creds.scopes,
        "email_address": creds.email_address,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows, or an exotic filesystem. The token is still inside the
        # user's own profile directory; say nothing rather than alarm them.
        pass


class GmailClient:
    """Four endpoints and a token refresh."""

    def __init__(self, creds: GmailCredentials, read_scope: str = "metadata"):
        self.creds = creds
        self.read_scope = read_scope

    # ── Auth ──

    def authorised(self) -> bool:
        return self.creds.usable()

    def readiness(self) -> tuple[bool, str]:
        """Whether this client can send, and what to do about it if not."""
        if not self.creds.client_id or not self.creds.client_secret:
            return False, (
                "channels.email.provider is 'gmail' but GOOGLE_CLIENT_ID / "
                "GOOGLE_CLIENT_SECRET are not in .env. Create an OAuth client "
                "(Desktop app) in your own Google Cloud project, then run "
                "'openvz-leads gmail login'."
            )
        if not self.creds.refresh_token:
            return False, (
                "No Gmail account is authorised yet. Run: openvz-leads gmail login"
            )
        wanted = set(scopes_for(self.read_scope))
        held = set(self.creds.scopes)
        missing = wanted - held
        if missing:
            return False, (
                "The authorised Gmail account is missing scopes "
                f"({', '.join(sorted(missing))}) — this usually means "
                "read_scope changed after you logged in. Re-run: "
                "openvz-leads gmail login"
            )
        return True, ""

    async def _access_token(self) -> str:
        if self.creds.fresh():
            return self.creds.access_token
        if not self.creds.usable():
            raise GmailNotAuthorised(self.readiness()[1])

        payload = {
            "client_id": self.creds.client_id,
            "client_secret": self.creds.client_secret,
            "refresh_token": self.creds.refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.post(TOKEN_ENDPOINT, data=payload)
        except Exception as e:
            raise GmailError(f"Could not reach Google to refresh the token: {e}")

        if resp.status_code >= 400:
            detail = _error_detail(resp)
            if "invalid_grant" in detail:
                # Revoked, expired after six months idle, or the account's
                # password changed. Nothing retryable about it.
                raise GmailNotAuthorised(
                    "Google rejected the stored refresh token (invalid_grant). "
                    "It was probably revoked or has gone stale. Re-run: "
                    "openvz-leads gmail login"
                )
            raise GmailError(f"Token refresh failed ({resp.status_code}): {detail}")

        data = resp.json()
        self.creds.access_token = data.get("access_token", "")
        self.creds.expires_at = time.time() + float(data.get("expires_in", 3600))
        save_credentials(self.creds)
        return self.creds.access_token

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        token = await self._access_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        url = f"{API_ROOT}{path}"
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.request(method, url, headers=headers, **kwargs)
        except Exception as e:
            raise GmailError(f"Could not reach Gmail: {e}")

        if resp.status_code == 401:
            raise GmailNotAuthorised(
                "Gmail rejected the access token. Re-run: openvz-leads gmail login"
            )
        if resp.status_code == 403:
            detail = _error_detail(resp)
            if "rateLimitExceeded" in detail or "userRateLimitExceeded" in detail:
                raise GmailError(f"Gmail is rate-limiting this account: {detail}")
            raise GmailError(
                f"Gmail refused the request (403): {detail}. If this mentions "
                "a scope, re-run 'openvz-leads gmail login' after changing "
                "channels.email.gmail.read_scope."
            )
        if resp.status_code >= 400:
            raise GmailError(f"Gmail returned {resp.status_code}: {_error_detail(resp)}")
        try:
            return resp.json()
        except Exception:
            return {}

    # ── Who are we ──

    async def profile(self) -> dict:
        return await self._request("GET", "/profile")

    async def address(self) -> str:
        if self.creds.email_address:
            return self.creds.email_address
        data = await self.profile()
        self.creds.email_address = str(data.get("emailAddress") or "")
        if self.creds.email_address:
            save_credentials(self.creds)
        return self.creds.email_address

    # ── Sending ──

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        sender_name: str = "",
        thread_id: str = "",
        in_reply_to: str = "",
    ) -> SentMessage:
        """Send one plain-text message, optionally into an existing thread.

        `in_reply_to` is the previous message's RFC 2822 Message-ID. Both it
        and `thread_id` are needed: the header is what makes a mail client
        show a conversation, and threadId is what makes Gmail agree.
        """
        from_address = await self.address()

        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message["From"] = (
            formataddr((sender_name, from_address)) if sender_name else from_address
        )
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = in_reply_to
        message.set_content(body)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        payload = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id

        data = await self._request(
            "POST",
            "/messages/send",
            json=payload,
            headers={"Content-Type": "application/json"},
        )

        sent = SentMessage(
            message_id=str(data.get("id") or ""),
            thread_id=str(data.get("threadId") or thread_id or ""),
        )
        # The Message-ID header is assigned by Gmail, and the follow-up needs
        # it. It is not in the send response, so read it back.
        if sent.message_id:
            sent.rfc_message_id = await self._rfc_message_id(sent.message_id)
        return sent

    async def _rfc_message_id(self, message_id: str) -> str:
        try:
            data = await self._request(
                "GET",
                f"/messages/{message_id}",
                params={"format": "metadata", "metadataHeaders": "Message-ID"},
            )
        except GmailError as e:
            # Threading degrades to "the follow-up is its own email", which is
            # worse but not wrong. Never fail a completed send over it.
            logger.warning(f"Could not read back the Message-ID: {e}")
            return ""
        return _header(data.get("payload", {}), "Message-ID")

    # ── Reading ──

    async def thread_replies(
        self, thread_id: str, *, our_address: str = "", after_ms: int = 0
    ) -> list[ThreadReply]:
        """Messages in this thread that we did not send.

        Returns [] when read_scope is 'none' — the caller is expected to have
        been stopped by config validation long before this, but a silent
        empty list here would look exactly like "no reply", so it logs.
        """
        if self.read_scope == "none":
            logger.warning(
                "thread_replies called with read_scope 'none'; replies cannot "
                "be seen. This configuration should have been rejected."
            )
            return []
        if not thread_id:
            return []

        fmt = "full" if self.read_scope == "readonly" else "metadata"
        params = {"format": fmt}
        if fmt == "metadata":
            params["metadataHeaders"] = ["From", "Subject", "Message-ID"]

        data = await self._request("GET", f"/threads/{thread_id}", params=params)
        mine = (our_address or await self.address()).lower()

        replies = []
        for message in data.get("messages", []) or []:
            payload = message.get("payload", {}) or {}
            from_header = _header(payload, "From")
            _, from_address = parseaddr(from_header)
            from_address = (from_address or "").lower()
            if not from_address or from_address == mine:
                continue

            try:
                received = int(message.get("internalDate") or 0)
            except (TypeError, ValueError):
                received = 0
            if after_ms and received and received <= after_ms:
                continue

            replies.append(
                ThreadReply(
                    message_id=str(message.get("id") or ""),
                    from_address=from_address,
                    received_at=received,
                    subject=_header(payload, "Subject"),
                    body=_plain_text(payload) if fmt == "full" else "",
                )
            )
        return replies


# ── Helpers ───────────────────────────────────────────────────────────


def _error_detail(resp) -> str:
    try:
        data = resp.json()
    except Exception:
        return resp.text[:300]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error:
            return str(error) + " " + str(data.get("error_description") or "")
    return str(data)[:300]


def _header(payload: dict, name: str) -> str:
    wanted = name.lower()
    for header in payload.get("headers", []) or []:
        if str(header.get("name", "")).lower() == wanted:
            return str(header.get("value") or "")
    return ""


def _plain_text(payload: dict) -> str:
    """Best-effort text/plain out of a MIME tree. Never raises."""
    if not isinstance(payload, dict):
        return ""

    mime = payload.get("mimeType", "")
    body = payload.get("body", {}) or {}
    if mime == "text/plain" and body.get("data"):
        return _decode(body["data"])

    for part in payload.get("parts", []) or []:
        found = _plain_text(part)
        if found:
            return found

    # A message with no text/plain part at all: fall back to whatever the
    # top-level body holds rather than reporting an empty reply, which the
    # Handler would read as "nothing to classify".
    if body.get("data"):
        return _decode(body["data"])
    return ""


def _decode(data: str) -> str:
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return ""


# ── The login flow ────────────────────────────────────────────────────
#
# Loopback, not copy-paste: Google turned off the out-of-band flow in 2022,
# so a desktop app has to catch the redirect on 127.0.0.1. PKCE is used
# because an installed app cannot keep its client secret secret, which Google
# states outright.


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")
    import hashlib

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_auth_url(client_id: str, redirect_uri: str, scopes: list[str], challenge: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        # Without both of these Google returns no refresh token on a repeat
        # authorisation, and the login appears to succeed while leaving
        # nothing that survives an hour.
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def run_login(env, read_scope: str = "metadata", open_browser: bool = True) -> GmailCredentials:
    """Run the loopback OAuth flow. Blocking, and meant for the CLI.

    Returns credentials with a refresh token, already saved. Raises GmailError
    with something actionable on every failure path.
    """
    import http.server
    import threading
    import webbrowser

    client_id = getattr(env, "google_client_id", "")
    client_secret = getattr(env, "google_client_secret", "")
    if not client_id or not client_secret:
        raise GmailError(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not in .env.\n"
            "Create an OAuth client of type 'Desktop app' in your own Google "
            "Cloud project, enable the Gmail API on it, then put the two "
            "values in .env."
        )

    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/"
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    scopes = scopes_for(read_scope)
    auth_url = build_auth_url(client_id, redirect_uri, scopes, challenge, state)

    received: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib naming)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            received["code"] = (query.get("code") or [""])[0]
            received["state"] = (query.get("state") or [""])[0]
            received["error"] = (query.get("error") or [""])[0]
            body = (
                "<html><body style=\"font-family:system-ui;padding:3rem\">"
                "<h2>OpenVZ Leads</h2><p>"
                + (
                    "Authorisation failed: " + received["error"]
                    if received["error"]
                    else "Authorised. You can close this tab and go back to the terminal."
                )
                + "</p></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            done.set()

        def log_message(self, *args):
            pass  # the stdlib logs every request to stderr otherwise

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("\n  Opening your browser to authorise Gmail.")
    print("  If it does not open, paste this into a browser yourself:\n")
    print(f"  {auth_url}\n")
    if open_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

    try:
        if not done.wait(timeout=300):
            raise GmailError("Timed out waiting for the browser to come back.")
    finally:
        server.shutdown()
        server.server_close()

    if received.get("error"):
        raise GmailError(f"Google returned an error: {received['error']}")
    if received.get("state") != state:
        # Someone hit the loopback port with a forged redirect.
        raise GmailError("The authorisation response did not match this request.")
    code = received.get("code") or ""
    if not code:
        raise GmailError("Google did not return an authorisation code.")

    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.post(
            TOKEN_ENDPOINT,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
    if resp.status_code >= 400:
        raise GmailError(f"Exchanging the code failed: {_error_detail(resp)}")

    data = resp.json()
    refresh_token = data.get("refresh_token", "")
    if not refresh_token:
        raise GmailError(
            "Google returned no refresh token. This happens when the account "
            "has authorised this client before — remove OpenVZ Leads at "
            "https://myaccount.google.com/permissions and try again."
        )

    creds = GmailCredentials(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        access_token=data.get("access_token", ""),
        expires_at=time.time() + float(data.get("expires_in", 3600)),
        scopes=(data.get("scope") or " ".join(scopes)).split(),
    )

    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.get(
            f"{API_ROOT}/profile",
            headers={"Authorization": f"Bearer {creds.access_token}"},
        )
    if resp.status_code < 400:
        creds.email_address = str(resp.json().get("emailAddress") or "")

    save_credentials(creds)
    return creds
