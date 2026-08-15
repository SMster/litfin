"""robots.txt fetching, caching, and enforcement.

Two behaviors here are deliberate and slightly unusual:

1. We honor robots for the `*` group AND for our own declared token. A site
   that names us specifically gets obeyed specifically.

2. When a site does NOT disallow `*` but DOES name AI crawlers (ClaudeBot,
   CCBot, Google-Extended, Bytespider, Amazonbot) with Disallow: /, we log a
   warning against that source rather than silently proceeding. Omni does
   exactly this. The `*` group permits us and we are not those crawlers, but
   the operator has expressed a machine-readable preference and a human should
   see that it exists before we lean on the source.
"""

from __future__ import annotations

import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

# Tokens whose presence signals an operator opinion about automated/AI access.
AI_CRAWLER_TOKENS = (
    "claudebot",
    "ccbot",
    "google-extended",
    "bytespider",
    "amazonbot",
    "gptbot",
    "anthropic-ai",
)


@dataclass(slots=True)
class RobotsVerdict:
    allowed: bool
    crawl_delay: float | None
    ai_signal: str | None
    fetched: bool
    reason: str = ""


class RobotsCache:
    """Per-origin robots.txt cache with a TTL."""

    def __init__(self, fetch_text, *, ttl_seconds: int = 86_400) -> None:
        """`fetch_text(url) -> tuple[int, str]` returns (status, body).

        Injected rather than importing the client, to avoid a circular
        dependency and to make this trivially testable.
        """
        self._fetch_text = fetch_text
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, urllib.robotparser.RobotFileParser, str | None]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    def _load(
        self, origin: str, *, unavailable_allows: bool = False
    ) -> tuple[urllib.robotparser.RobotFileParser, str | None]:
        robots_url = origin.rstrip("/") + "/robots.txt"
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        ai_signal: str | None = None

        try:
            status, body = self._fetch_text(robots_url)
        except Exception:
            # Network trouble fetching robots. Fail OPEN for the `*` rules but
            # record nothing -- the circuit breaker and rate limiter still
            # apply. This matches every mainstream crawler's behavior.
            parser.parse([])
            return parser, None

        if status == 200 and body:
            lines = body.splitlines()
            parser.parse(lines)
            ai_signal = _extract_ai_signal(lines)
        elif status in (401, 403):
            # A refusal to serve robots.txt. DEFAULT: treat as disallow-all --
            # a site that will not tell an identified client its rules is not
            # inviting that client in (this is what blocks Kroll, which 403s
            # both robots.txt and its content).
            #
            # `unavailable_allows` overrides that, and ONLY when a human has
            # recorded a per-source determination in the compliance registry
            # (SourcePolicy.robots_unavailable = "allow"). The case it exists
            # for: an API host that serves no robots.txt but serves its
            # content to an identified client under a separately published
            # written access policy -- e.g. efts.sec.gov under SEC's
            # fair-access policy.
            if not unavailable_allows:
                parser.disallow_all = True
            else:
                parser.parse([])
        else:
            # 404 and friends: no robots.txt means no restrictions.
            parser.parse([])

        return parser, ai_signal

    def check(
        self, url: str, user_agent: str, *, unavailable_allows: bool = False
    ) -> RobotsVerdict:
        origin = self._origin(url)
        now = time.time()

        with self._lock:
            cached = self._entries.get(origin)
            fresh = cached is not None and (now - cached[0]) < self._ttl

        if not fresh:
            parser, ai_signal = self._load(
                origin, unavailable_allows=unavailable_allows
            )
            with self._lock:
                self._entries[origin] = (now, parser, ai_signal)
        else:
            assert cached is not None
            _, parser, ai_signal = cached

        allowed = parser.can_fetch(user_agent, url)
        if not allowed:
            reason = f"robots.txt at {origin} disallows this path for our agent"
        else:
            reason = ""

        delay = None
        try:
            raw_delay = parser.crawl_delay(user_agent)
            if raw_delay is not None:
                delay = float(raw_delay)
        except Exception:
            delay = None

        return RobotsVerdict(
            allowed=allowed,
            crawl_delay=delay,
            ai_signal=ai_signal,
            fetched=not fresh,
            reason=reason,
        )


def _extract_ai_signal(lines: list[str]) -> str | None:
    """Detect AI-crawler-specific directives and Content-Signal headers.

    Returns a human-readable summary, or None if the file says nothing about
    automated/AI access.
    """
    signal_parts: list[str] = []
    current_agents: list[str] = []
    blocked_ai: set[str] = set()

    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()

        if field == "content-signal":
            signal_parts.append(f"Content-Signal: {value}")
        elif field == "user-agent":
            current_agents.append(value.lower())
        elif field == "disallow":
            if value == "/":
                for agent in current_agents:
                    if any(tok in agent for tok in AI_CRAWLER_TOKENS):
                        blocked_ai.add(agent)
            current_agents = current_agents if value else current_agents
        elif field == "allow":
            pass
        else:
            current_agents = []

    if blocked_ai:
        signal_parts.append("blocks AI crawlers: " + ", ".join(sorted(blocked_ai)))

    return "; ".join(signal_parts) if signal_parts else None
