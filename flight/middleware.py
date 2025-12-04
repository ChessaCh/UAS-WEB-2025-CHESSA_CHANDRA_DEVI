from django.conf import settings
from .amadeus import get_access_token


class AmadeusTokenMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        override = request.GET.get("token")
        if override:
            request.session["amadeus_token"] = override
        token = request.session.get("amadeus_token") or settings.AMADEUS_ACCESS_TOKEN or get_access_token()
        if not token:
            token = get_access_token(force_refresh=True)
        if token:
            request.amadeus_token = token
        response = self.get_response(request)
        return response