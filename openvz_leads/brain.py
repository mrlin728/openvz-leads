"""Brain — OpenVZ Leads' thinking engine, and the one place a model is called.

Every agent asks the Brain, never a provider. That indirection is what makes
`model.provider` a one-line change instead of a rewrite: the Claude Code CLI
(the default, and the reason there is no second model bill), OpenAI, DeepSeek,
or anything else speaking the OpenAI chat-completions shape all arrive here
and leave as the same string.
"""

import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path

import httpx

from openvz_leads import paths
from openvz_leads.config import ModelConfig, load_env
from openvz_leads.state import StateManager

logger = logging.getLogger("openvz_leads.brain")

# Resolved lazily through paths.workspace() so a frozen build reads the
# user's editable copies rather than the read-only ones inside the bundle.

# Subprocess safety limits
DEFAULT_TIMEOUT_SECONDS = 300  # a single Claude call should never hang forever
DEFAULT_MAX_RETRIES = 2  # retries on transient failures (non-zero exit, timeout)
RETRY_BASE_DELAY = 5.0  # seconds; doubles per attempt

# Placeholders that are supposed to survive templating.
#
# These are per-recipient merge variables: the Writer is told to put them in
# the copy, and whatever sends the email substitutes them for each prospect.
# They look identical to a templating mistake, so without this the "unfilled
# variables" check below fired on every single campaign — which trains you to
# ignore the warning, and buries the case it exists to catch (a real typo like
# {{prodcut_name}}, which would otherwise reach Claude verbatim).
MERGE_VARIABLES = frozenset({"first_name", "company", "title"})

# Where each remote provider lives, and what it answers to when the config
# does not name a model. Both are overridable — `model.name` wins over the
# default, and `openai_compatible` supplies its own base URL — so a provider
# shipping a newer model does not require a release here.
PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
}
DEFAULT_MODELS = {
    "openai": "gpt-4.1",
    "deepseek": "deepseek-chat",
}

# HTTP statuses worth trying again: rate limits and the provider's own faults.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# stderr fragments that indicate retrying is pointless
_NON_RETRYABLE_PATTERNS = (
    "not logged in",
    "please run `claude login`",
    "invalid api key",
    "unauthorized",
)


class Brain:
    def __init__(
        self,
        state: StateManager,
        model: ModelConfig | None = None,
        env=None,
    ):
        """`model` and `env` are optional so existing callers keep working.

        Omitting `model` means the Claude Code CLI, which is what every
        install did before providers existed and what most should keep doing.
        """
        self.state = state
        self.model = model or ModelConfig()
        self._env = env

    # ── Provider plumbing ──

    @property
    def env(self):
        """Lazily read .env — only providers that bill you separately need it."""
        if self._env is None:
            self._env = load_env()
        return self._env

    @property
    def provider(self) -> str:
        return self.model.provider

    def model_name(self) -> str:
        """The model id to send, falling back to the provider's default."""
        return self.model.name.strip() or DEFAULT_MODELS.get(self.provider, "")

    def _base_url(self) -> str:
        if self.provider == "openai_compatible":
            return self.model.base_url.strip().rstrip("/")
        return PROVIDER_BASE_URLS.get(self.provider, "").rstrip("/")

    def describe(self) -> str:
        """One line naming what is doing the thinking. For logs and the UI."""
        if self.provider == "claude_cli":
            return "Claude Code CLI (local, no extra model cost)"
        name = self.model_name() or "(model not set)"
        return f"{self.provider}: {name}"

    def readiness(self) -> tuple[bool, str]:
        """Whether this Brain can make a call, and why not when it cannot.

        Checked before the pipeline starts so a missing key is a sentence on
        the setup screen rather than a failed cycle an hour later.
        """
        if self.provider == "claude_cli":
            return True, ""
        if not self._api_key():
            var = {
                "openai": "OPENAI_API_KEY",
                "deepseek": "DEEPSEEK_API_KEY",
            }.get(self.provider, "MODEL_API_KEY")
            return False, (
                f"model.provider is '{self.provider}' but {var} is not set. "
                f"Add it to .env, or set model.provider back to 'claude_cli'."
            )
        if not self._base_url():
            return False, (
                "model.provider is 'openai_compatible' but model.base_url is "
                "empty. Point it at the endpoint, e.g. http://localhost:11434/v1."
            )
        if not self.model_name():
            return False, (
                f"No model name for provider '{self.provider}'. "
                "Set model.name in openvz-leads.yaml."
            )
        return True, ""

    def _api_key(self) -> str:
        try:
            return self.env.key_for_provider(self.provider)
        except Exception:
            # A malformed .env must not take the whole agent down; the call
            # that follows will fail with a 401 and say so.
            return os.getenv("MODEL_API_KEY", "").strip()

    # ── The one call every agent makes ──

    async def think(
        self,
        prompt: str,
        session_id: str | None = None,
        expect_json: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> str:
        """Send a prompt to the configured model and return its answer.

        Returns "" on unrecoverable failure — every caller already treats an
        empty response as "this stage produced nothing", so a dead provider
        degrades the cycle instead of crashing the heartbeat.
        """
        if self.provider == "claude_cli":
            return await self._think_claude_cli(
                prompt, session_id=session_id, timeout=timeout, max_retries=max_retries
            )

        ok, why = self.readiness()
        if not ok:
            logger.error(f"Brain not usable — {why}")
            return ""
        return await self._think_http(
            prompt, timeout=timeout, max_retries=max_retries
        )

    async def _think_http(
        self,
        prompt: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> str:
        """OpenAI-shaped chat completion. Covers OpenAI, DeepSeek and clones.

        All three speak `POST /chat/completions` with a bearer token and
        return the text at `choices[0].message.content`, so one backend serves
        them; only the base URL and the model id differ.
        """
        url = f"{self._base_url()}/chat/completions"
        payload = {
            "model": self.model_name(),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.model.temperature,
            "max_tokens": self.model.max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }

        last_error = ""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.info(
                    f"Retrying {self.provider} call in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{max_retries + 1})..."
                )
                await asyncio.sleep(delay)

            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                logger.warning(f"{self.provider} call failed to reach {url}: {e}")
                last_error = str(e)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Brain error ({self.provider}): {e}")
                return ""

            if resp.status_code in (401, 403):
                # A wrong key stays wrong. Say which variable to fix rather
                # than retrying into the same wall three times.
                logger.error(
                    f"{self.provider} rejected the API key ({resp.status_code}). "
                    "Check the key in .env, or set model.provider to 'claude_cli'."
                )
                return ""
            if resp.status_code == 404:
                logger.error(
                    f"{self.provider} has no model '{self.model_name()}'. "
                    "Set model.name in openvz-leads.yaml to one your account can use."
                )
                return ""
            if resp.status_code in _RETRYABLE_STATUS:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                logger.warning(f"{self.provider} returned {resp.status_code}; retrying.")
                continue
            if resp.status_code >= 400:
                logger.error(
                    f"{self.provider} returned {resp.status_code}: {resp.text[:300]}"
                )
                return ""

            text = self._extract_completion(resp)
            if not text:
                last_error = "empty completion"
                logger.warning(f"{self.provider} returned no content; retrying.")
                continue

            try:
                await self.state.increment_usage()
            except Exception as e:
                logger.warning(f"Failed to record usage: {e}")
            logger.debug(f"Brain response: {text[:200]}...")
            return text

        logger.error(
            f"{self.provider} call failed after {max_retries + 1} attempts. "
            f"Last error: {str(last_error)[:200]}"
        )
        return ""

    @staticmethod
    def _extract_completion(resp: "httpx.Response") -> str:
        """Pull the message text out, tolerating the shapes clones return.

        Reasoning models put an empty string in `content` and the answer in a
        list of parts; a few gateways return `text` instead. None of that is
        worth a crash, so anything unrecognised comes back as "".
        """
        try:
            data = resp.json()
        except Exception:
            return ""
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            return ""
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in ("text", "output_text")
            ]
            return "".join(parts).strip()
        return str(choices[0].get("text", "")).strip()

    async def _think_claude_cli(
        self,
        prompt: str,
        session_id: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> str:
        """Claude Code headless mode — the default, and the one with no bill.

        Retries transient failures with exponential backoff and enforces a
        hard timeout so a hung CLI call can never stall the heartbeat.
        """
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "text",
            "--dangerously-skip-permissions",
        ]
        if session_id:
            # The CLI only accepts UUIDs for --session-id; callers pass
            # friendly names ("leads-scout") purely as a debug label, and
            # one-shot -p calls get no continuity from a session id anyway.
            try:
                uuid.UUID(session_id)
                cmd.extend(["--session-id", session_id])
            except ValueError:
                pass

        logger.debug(f"Brain call (session={session_id}): {prompt[:100]}...")

        last_error = ""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.info(
                    f"Retrying brain call in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{max_retries + 1})..."
                )
                await asyncio.sleep(delay)

            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    # DEVNULL: the CLI reads inherited stdin as prompt input,
                    # stealing the terminal (and any piped answers) from OpenVZ Leads.
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    logger.error(f"Claude call timed out after {timeout:.0f}s. Killing process.")
                    try:
                        process.kill()
                        await process.wait()
                    except ProcessLookupError:
                        pass
                    last_error = "timeout"
                    continue  # retry

                if process.returncode != 0:
                    error = stderr.decode(errors="replace").strip()
                    logger.error(
                        f"Claude exited with code {process.returncode}: {error[:300]}"
                    )
                    last_error = error
                    if any(p in error.lower() for p in _NON_RETRYABLE_PATTERNS):
                        logger.error(
                            "Non-retryable Claude error (auth). "
                            "Run 'claude login' and restart OpenVZ Leads."
                        )
                        return ""
                    continue  # retry transient failures

                response = stdout.decode(errors="replace").strip()
                try:
                    await self.state.increment_usage()
                except Exception as e:
                    # Usage accounting must never break the response path
                    logger.warning(f"Failed to record usage: {e}")
                logger.debug(f"Brain response: {response[:200]}...")
                return response

            except FileNotFoundError:
                logger.error(
                    "Claude CLI not found. Install it: https://claude.com/download"
                )
                return ""  # not retryable
            except asyncio.CancelledError:
                # Shutting down — kill the child so it doesn't orphan
                if process is not None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                raise
            except Exception as e:
                logger.error(f"Brain error: {e}")
                last_error = str(e)
                continue

        logger.error(
            f"Brain call failed after {max_retries + 1} attempts. "
            f"Last error: {str(last_error)[:200]}"
        )
        return ""

    async def think_json(
        self, prompt: str, session_id: str | None = None
    ) -> dict | list | None:
        """Send a prompt and parse the response as JSON."""
        full_prompt = (
            prompt
            + "\n\nRespond ONLY with valid JSON. No markdown, no explanation."
        )
        response = await self.think(full_prompt, session_id=session_id)
        if not response:
            return None
        parsed = self._extract_json(response)
        if parsed is None:
            logger.error(f"Failed to parse JSON from brain: {response[:200]}")
        return parsed

    @staticmethod
    def _extract_json(text: str) -> dict | list | None:
        """Best-effort JSON extraction: strips code fences, then falls back to
        locating the outermost JSON object/array in surrounding prose."""
        cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        # Fall back: model wrapped the JSON in explanation text
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            start = cleaned.find(open_ch)
            end = cleaned.rfind(close_ch)
            if start != -1 and end > start:
                try:
                    return json.loads(cleaned[start : end + 1])
                except json.JSONDecodeError:
                    continue
        return None

    async def check_usage(self) -> float:
        """Check current Claude daily usage (our own call count as proxy).

        Returns the number of calls made today. Errors are treated as
        "over budget is unknown" and return a safe 0.0 so a broken DB read
        doesn't crash the heartbeat.
        """
        try:
            return await self.state.get_usage_today()
        except Exception as e:
            logger.warning(f"Could not read usage from state: {e}")
            return 0.0

    async def is_within_budget(self, max_daily_calls: int | None = None) -> bool:
        """Check if we're under the daily usage limit.

        max_daily_claude_percent from config is a percentage of the model's
        daily_call_budget (200 by default, so 80% = 160 calls). Passing an
        explicit count overrides both — the heartbeat does exactly that.
        """
        if max_daily_calls is None:
            max_daily_calls = self.model.daily_call_budget
        calls = await self.check_usage()
        return calls < max_daily_calls

    def load_prompt(self, prompt_name: str, **kwargs) -> str:
        """Load a prompt template from the prompts/ directory and fill in variables."""
        prompt_file = paths.prompts_dir() / f"{prompt_name}.md"
        try:
            template = prompt_file.read_text()
        except FileNotFoundError:
            logger.warning(f"Prompt file not found: {prompt_file}")
            return ""
        except OSError as e:
            logger.error(f"Could not read prompt file {prompt_file}: {e}")
            return ""
        for key, value in kwargs.items():
            template = template.replace(f"{{{{{key}}}}}", str(value))
        # Surface templating mistakes early instead of sending {{foo}} to Claude.
        # Merge variables are excluded — they are meant to still be there.
        leftover = set(re.findall(r"\{\{(\w+)\}\}", template)) - MERGE_VARIABLES
        if leftover:
            logger.warning(
                f"Prompt '{prompt_name}' has unfilled variables: {sorted(leftover)}"
            )
        return template

    def load_skill(self, skill_name: str) -> str:
        """Load a skill knowledge file from the skills/ directory."""
        skill_file = paths.skills_dir() / f"{skill_name}.md"
        try:
            return skill_file.read_text()
        except FileNotFoundError:
            logger.warning(f"Skill file not found: {skill_file}")
            return ""
        except OSError as e:
            logger.error(f"Could not read skill file {skill_file}: {e}")
            return ""

    def load_skills_for_agent(self, agent_name: str) -> str:
        """Load all relevant skills for a specific sub-agent.

        Returns concatenated skill content that should be injected
        into the agent's prompts as foundational knowledge.
        """
        # product_knowledge and competitive_intel are auto-generated by the trainer
        # and loaded by every agent that needs product context
        skill_map = {
            "scout": ["prospecting_tactics", "lead_qualification", "account_navigation", "product_knowledge"],
            "profiler": ["lead_qualification", "account_navigation", "product_knowledge", "competitive_intel"],
            "writer": ["email_frameworks", "sales_methodology", "offer_strategy", "product_knowledge", "competitive_intel"],
            "handler": ["objection_handling", "sales_methodology", "offer_strategy", "product_knowledge", "competitive_intel"],
            "sender": ["email_frameworks", "product_knowledge"],
            "linkedin": ["linkedin_outreach", "prospecting_tactics", "product_knowledge"],
        }

        skill_names = skill_map.get(agent_name, [])
        if not skill_names:
            return ""

        sections = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                sections.append(content)

        if not sections:
            return ""

        return (
            "\n\n---\n## FOUNDATIONAL KNOWLEDGE\n"
            "Use the following frameworks and best practices to guide your work:\n\n"
            + "\n\n---\n\n".join(sections)
        )
