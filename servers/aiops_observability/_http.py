"""Shared pooled HTTP session for observability backends."""

from http.cookiejar import DefaultCookiePolicy

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


def new_session():
    """Reuse connections like a Session, but keep bare requests.* semantics:
    no cookies are carried between calls."""
    session = requests.Session()
    session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    return session


SESSION = new_session() if requests is not None else None
