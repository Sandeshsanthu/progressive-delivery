import os

from UnleashClient import UnleashClient
from flask import request, session, has_request_context

_unleash = None


def _normalize_unleash_url(url: str) -> str:
    """
    Try to make UNLEASH_URL robust across common inputs.
    Expected base includes the '/api' path (with or without trailing slash).
    """
    u = (url or "").strip()
    if not u:
        return u
    u = u.rstrip("/")
    if not u.endswith("/api"):
        u = u + "/api"
    return u


def init_unleash():
    """
    Call once at app startup (or it will lazy-init on first flag() call).

    Requires:
      UNLEASH_URL (Unleash server API base including '/api')
      UNLEASH_API_TOKEN (Client API token; required if server enforces auth)
      UNLEASH_APP_NAME (optional)
      UNLEASH_INSTANCE_ID (optional)

    Optional:
      UNLEASH_AUTH_PREFIX (e.g., 'Bearer'; if set, header becomes 'Bearer <token>')
    """
    global _unleash
    if _unleash is not None:
        return _unleash

    raw_url = os.getenv("UNLEASH_URL")
    if not raw_url:
        raise RuntimeError("UNLEASH_URL is required")

    url = _normalize_unleash_url(raw_url)

    headers = {}
    token = (os.getenv("UNLEASH_API_TOKEN") or "").strip()
    if token:
        prefix = (os.getenv("UNLEASH_AUTH_PREFIX") or "").strip()
        headers["Authorization"] = f"{prefix} {token}".strip() if prefix else token

    _unleash = UnleashClient(
        url=url,
        app_name=os.getenv("UNLEASH_APP_NAME", "car-marketplace"),
        instance_id=os.getenv("UNLEASH_INSTANCE_ID", "car-marketplace-1"),
        custom_headers=headers or None,
    )

    # Don’t let startup kill the app if Unleash is misconfigured/unreachable.
    # Flags will fall back to defaults in flag().
    try:
        _unleash.initialize_client()
    except Exception:
        pass

    return _unleash


def _context():
    # Only build context when a request exists
    if not has_request_context():
        return {}

    # Ensure a stable session id exists
    if "_sid" not in session:
        session["_sid"] = os.urandom(8).hex()

    return {
        "userId": str(session.get("user_id", "anonymous")),
        "sessionId": str(session.get("_sid", "anon-session")),
        "remoteAddress": request.headers.get("X-Forwarded-For", request.remote_addr),
    }


def flag(name: str, default: bool = False) -> bool:
    """
    Version-tolerant Unleash 'is_enabled' call:
    - some clients use default_value=
    - some use default=
    - some support no default parameter
    Also fails open to provided default if Unleash is unreachable/unauthorized (403).
    """
    try:
        client = init_unleash()
    except Exception:
        return bool(default)

    ctx = _context()

    try:
        return bool(client.is_enabled(name, context=ctx, default_value=default))
    except TypeError:
        try:
            return bool(client.is_enabled(name, context=ctx, default=default))
        except TypeError:
            try:
                return bool(client.is_enabled(name, context=ctx))
            except Exception:
                return bool(default)
    except Exception:
        return bool(default)
