import os
from UnleashClient import UnleashClient
from flask import request, session

_unleash = None

def init_unleash():
    """
    Call once at app startup (or it will lazy-init on first flag() call).
    Requires:
      UNLEASH_URL (e.g. http://unleash:4242/api)
      UNLEASH_API_TOKEN (server-side token, optional if your Unleash allows)
      UNLEASH_APP_NAME (optional)
      UNLEASH_INSTANCE_ID (optional)
    """
    global _unleash
    if _unleash is not None:
        return _unleash

    url = os.getenv("UNLEASH_URL")
    if not url:
        raise RuntimeError("UNLEASH_URL is required")

    headers = {}
    token = os.getenv("UNLEASH_API_TOKEN")
    if token:
        headers["Authorization"] = token

    _unleash = UnleashClient(
        url=url,
        app_name=os.getenv("UNLEASH_APP_NAME", "car-marketplace"),
        instance_id=os.getenv("UNLEASH_INSTANCE_ID", "car-marketplace-1"),
        custom_headers=headers or None,
    )
    _unleash.initialize_client()
    return _unleash

def _context():
    return {
        "userId": str(session.get("user_id", "anonymous")),
        "sessionId": str(session.get("_sid", "anon-session")),
        "remoteAddress": request.headers.get("X-Forwarded-For", request.remote_addr),
    }

def flag(name: str, default: bool = False) -> bool:
    client = init_unleash()
    return bool(client.is_enabled(name, context=_context(), default_value=default))
