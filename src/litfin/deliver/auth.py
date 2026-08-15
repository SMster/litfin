"""Authentication for a hosted control panel.

`litfin serve` was built loopback-only and unauthenticated, which is the right
model for a tool running on your own laptop. Hosting it changes the threat
model completely: the panel can spend money on an API and it displays case
analysis about named parties in real litigation.

WHAT THIS IS, AND WHAT IT IS NOT. This is a single-operator password gate:
one username, one password, signed session cookies. It is sized for "one or
two people behind a private URL", which is the deployment being asked for. It
is NOT multi-user auth, has no roles, no password reset, and no audit trail
per user. If more than a couple of people need access, put a real identity
proxy in front of it rather than growing this file.

FOUR PROPERTIES THAT MATTER:

1. **Constant-time comparison** on both username and password, so response
   timing cannot be used to recover either one character at a time.

2. **The password is never stored, logged, or echoed** -- only compared. It
   arrives from the environment and stays there.

3. **Sessions are signed, not encrypted, and carry an expiry.** A cookie the
   server did not sign is rejected; a cookie past its expiry is rejected even
   though the signature is still valid. Rotating LITFIN_SESSION_SECRET
   invalidates every session at once, which is the point of having it.

4. **Login attempts are rate-limited per source IP.** Without it, a private
   URL plus a password is a password guessable at network speed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field

SESSION_COOKIE = "litfin_session"
SESSION_TTL_SECONDS = 12 * 3600

# Deliberately strict. A hosted panel that spends money should not be
# reachable by a password somebody typed in a hurry.
MIN_PASSWORD_LENGTH = 16

# Login throttle. Generous enough that a human fat-fingering a password twice
# is unaffected, tight enough that guessing is not a strategy.
MAX_ATTEMPTS = 6
ATTEMPT_WINDOW_SECONDS = 300
LOCKOUT_SECONDS = 900


class AuthMisconfigured(RuntimeError):
    """Raised at startup, before a socket is opened. Never caught."""


@dataclass(frozen=True, slots=True)
class AuthConfig:
    username: str
    password: str
    secret: str

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password and self.secret)


def from_environment() -> AuthConfig:
    return AuthConfig(
        username=os.environ.get("LITFIN_WEB_USER", "").strip(),
        password=os.environ.get("LITFIN_WEB_PASSWORD", ""),
        secret=os.environ.get("LITFIN_SESSION_SECRET", "").strip(),
    )


def require_for_public_bind(cfg: AuthConfig, host: str) -> None:
    """Refuse a non-loopback bind unless auth is fully configured.

    Called before the socket is created. A misconfigured hosted panel must
    fail to start rather than come up open -- an unauthenticated panel that
    boots successfully looks exactly like a working one.
    """
    if host in ("127.0.0.1", "localhost", "::1"):
        return

    missing = [
        name for name, value in (
            ("LITFIN_WEB_USER", cfg.username),
            ("LITFIN_WEB_PASSWORD", cfg.password),
            ("LITFIN_SESSION_SECRET", cfg.secret),
        ) if not value
    ]
    if missing:
        raise AuthMisconfigured(
            f"Refusing to bind {host} without authentication.\n\n"
            f"Missing: {', '.join(missing)}\n\n"
            f"This panel can spend money on the Anthropic API and displays "
            f"case analysis about named parties in real litigation. It is "
            f"loopback-only by default for that reason.\n\n"
            f"Generate the secrets with:\n"
            f'  python -c "import secrets; print(secrets.token_urlsafe(24))"'
        )
    if len(cfg.password) < MIN_PASSWORD_LENGTH:
        raise AuthMisconfigured(
            f"LITFIN_WEB_PASSWORD is {len(cfg.password)} characters; "
            f"{MIN_PASSWORD_LENGTH} is the minimum for a hosted panel. "
            f'Generate one with: python -c "import secrets; '
            f'print(secrets.token_urlsafe(24))"'
        )
    if len(cfg.secret) < 32:
        raise AuthMisconfigured(
            "LITFIN_SESSION_SECRET is too short; use at least 32 characters "
            "so session signatures cannot be brute-forced."
        )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _sign(secret: str, payload: bytes) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    ).decode().rstrip("=")


def issue_session(cfg: AuthConfig, *, now: float | None = None) -> str:
    """A signed, expiring session token. Signed, not encrypted -- it carries
    no secret, only a claim the server can verify it made."""
    body = json.dumps(
        {"u": cfg.username, "exp": int((now or time.time()) + SESSION_TTL_SECONDS)},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    b64 = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"{b64}.{_sign(cfg.secret, body)}"


def verify_session(cfg: AuthConfig, token: str, *, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    b64, _, signature = token.rpartition(".")
    try:
        body = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    except Exception:
        return False
    # Signature FIRST: never parse a payload the server did not sign.
    if not hmac.compare_digest(_sign(cfg.secret, body), signature):
        return False
    try:
        claims = json.loads(body)
    except json.JSONDecodeError:
        return False
    if not hmac.compare_digest(str(claims.get("u", "")), cfg.username):
        return False
    # A valid signature is not a valid session. Expiry is checked separately
    # so an old cookie cannot be replayed forever.
    return float(claims.get("exp", 0)) > (now or time.time())


def check_credentials(cfg: AuthConfig, username: str, password: str) -> bool:
    """Constant-time on BOTH fields.

    Comparing the username with `==` and only the password in constant time
    would leak which usernames exist, one character of timing at a time.
    """
    user_ok = hmac.compare_digest((username or "").strip(), cfg.username)
    pass_ok = hmac.compare_digest(password or "", cfg.password)
    return user_ok and pass_ok


# ---------------------------------------------------------------------------
# Login throttle
# ---------------------------------------------------------------------------

@dataclass
class LoginThrottle:
    """Per-IP attempt limiting. A private URL and a password is not a defence
    against guessing at network speed."""

    _attempts: dict[str, list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def locked_out(self, ip: str, *, now: float | None = None) -> float:
        """Seconds remaining in a lockout, or 0."""
        t = now or time.time()
        with self._lock:
            recent = [a for a in self._attempts.get(ip, []) if t - a < LOCKOUT_SECONDS]
            self._attempts[ip] = recent
            if len(recent) < MAX_ATTEMPTS:
                return 0.0
            return max(0.0, LOCKOUT_SECONDS - (t - recent[-MAX_ATTEMPTS]))

    def record_failure(self, ip: str, *, now: float | None = None) -> None:
        t = now or time.time()
        with self._lock:
            self._attempts.setdefault(ip, []).append(t)

    def clear(self, ip: str) -> None:
        with self._lock:
            self._attempts.pop(ip, None)


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LitFin — sign in</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font: 15px -apple-system, "Segoe UI", Roboto, system-ui, sans-serif;
       display: grid; place-items: center; min-height: 100vh; margin: 0;
       background: Canvas; color: CanvasText; }}
form {{ border: 1px solid rgba(128,128,128,.35); border-radius: 10px;
       padding: 26px 28px; width: min(360px, 92vw); }}
h1 {{ font-size: 17px; margin: 0 0 4px; }}
p.sub {{ margin: 0 0 18px; opacity: .7; font-size: 13px; }}
label {{ display: block; font-size: 12.5px; opacity: .75; margin-bottom: 4px; }}
input {{ width: 100%; padding: 8px 10px; margin-bottom: 14px; font: inherit;
        border: 1px solid rgba(128,128,128,.4); border-radius: 6px;
        background: Canvas; color: CanvasText; box-sizing: border-box; }}
button {{ width: 100%; padding: 9px; font: inherit; cursor: pointer;
         border: 0; border-radius: 6px; background: #1a4f8a; color: #fff; }}
.err {{ color: #b3261e; font-size: 13px; margin-bottom: 12px; }}
</style></head>
<body>
<form method="post" action="/login">
  <h1>LitFin</h1>
  <p class="sub">Litigation-finance sourcing — research project</p>
  {error}
  <label for="u">User</label>
  <input id="u" name="username" autocomplete="username" autofocus>
  <label for="p">Password</label>
  <input id="p" name="password" type="password" autocomplete="current-password">
  <button type="submit">Sign in</button>
</form>
</body></html>
"""


def login_page(error: str = "") -> str:
    return LOGIN_PAGE.format(
        error=f'<div class="err">{error}</div>' if error else ""
    )
