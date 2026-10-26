"""Host names are private: what leaves the machine (an export file, a printed report) shows the endpoint's NAME and the model
(they are the information) but not the address of the server. `<host>` stands in for it; the port and the path stay."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

PLACEHOLDER = "<host>"
# addresses of the local network and loopback written out in a text (a log line, an error message, a note)
_PRIVATE_IP = re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def mask_url(url: str) -> str:
    """'http://my-server.lan:11434/v1' -> 'http://<host>:11434/v1' (anything that is not a URL with a host stays as it is).
    A URL that cannot be parsed (a port that is no number, broken brackets) is masked by its text, never passed on: a
    damaged value in an imported run must not carry the address into an export or a report."""
    try:
        u = urlsplit(url)
        port = f":{u.port}" if u.port else ""
    except ValueError:
        return _mask_unparsable(url)
    if not u.hostname:
        return url
    return urlunsplit((u.scheme, PLACEHOLDER + port, u.path, u.query, u.fragment))


def _mask_unparsable(url: str) -> str:
    """The part between '://' and the next '/', '?' or '#' becomes the placeholder (the port is dropped); without a scheme
    the whole value is replaced."""
    m = re.match(r"([a-z][a-z0-9+.-]*://)[^/?#]*", url, flags=re.I)
    return m.group(1) + PLACEHOLDER + url[m.end():] if m else PLACEHOLDER


def mask_base_url(value: str) -> str:
    """The `base_url` field of a run: a URL is masked as such; any other non-empty value (an imported run can hold
    anything) is replaced altogether."""
    masked = mask_url(value)
    return PLACEHOLDER if value and masked == value else masked


def mask_text(text: str, hosts: set[str]) -> str:
    """The hosts (and addresses of the local network) written in a text are replaced; the longest host is replaced first."""
    for h in sorted((h for h in hosts if h), key=len, reverse=True):
        text = re.sub(re.escape(h), PLACEHOLDER, text, flags=re.I)
    return _PRIVATE_IP.sub(PLACEHOLDER, text)


def mask_data(obj, hosts: set[str]):
    """The same for every string of a (nested) JSON-like value; `base_url` fields are masked as URLs."""
    if isinstance(obj, str):
        return mask_text(obj, hosts)
    if isinstance(obj, list):
        return [mask_data(x, hosts) for x in obj]
    if isinstance(obj, dict):
        return {k: (mask_base_url(v) if k == "base_url" and isinstance(v, str) else mask_data(v, hosts)) for k, v in obj.items()}
    return obj
