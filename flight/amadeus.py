from django.conf import settings
import time
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

_TOKEN = None
_EXPIRES_AT = 0


def get_access_token(force_refresh=False):
    global _TOKEN, _EXPIRES_AT
    now = int(time.time())
    if not force_refresh and settings.AMADEUS_ACCESS_TOKEN:
        return settings.AMADEUS_ACCESS_TOKEN
    if not force_refresh and _TOKEN and now < _EXPIRES_AT:
        return _TOKEN

    cid = getattr(settings, "AMADEUS_CLIENT_ID", None) or getattr(settings, "AMADEUS_API_KEY", None)
    csecret = getattr(settings, "AMADEUS_CLIENT_SECRET", None) or getattr(settings, "AMADEUS_API_SECRET", None)
    if not cid or not csecret:
        return None

    url = "https://test.api.amadeus.com/v1/security/oauth2/token"
    body = urlencode({
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": csecret,
    }).encode("utf-8")
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            token = data.get("access_token")
            expires_in = int(data.get("expires_in", 0))
            if token:
                _TOKEN = token
                _EXPIRES_AT = now + max(expires_in - 60, 0)
                return token
    except (HTTPError, URLError, Exception):
        return None

    return None