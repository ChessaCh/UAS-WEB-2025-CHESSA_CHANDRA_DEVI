from django.shortcuts import render, redirect
from django.http import HttpResponseBadRequest, JsonResponse
from django.conf import settings
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from .amadeus import get_access_token
from .models import FlightSearchLog, FlightTemp, Booking, Passenger, Payment
from decimal import Decimal


def search_page(request):
    return render(request, "flight/search.html", {})


def flight_results(request):
    origin = request.GET.get("origin")
    destination = request.GET.get("destination")
    departure_date = request.GET.get("departure_date")
    return_date = request.GET.get("return_date")
    adults = request.GET.get("adults", "1")
    origin_label = request.GET.get("origin_label")
    destination_label = request.GET.get("destination_label")

    context = {
        "results": None,
        "error": None,
        "origin": origin or "",
        "destination": destination or "",
        "origin_label": origin_label or "",
        "destination_label": destination_label or "",
        "departure_date": departure_date or "",
        "return_date": return_date or "",
        "adults": adults,
    }

    if not (origin and destination and departure_date):
        return redirect("search_flights")

    token = getattr(request, "amadeus_token", None) or settings.AMADEUS_ACCESS_TOKEN or get_access_token()
    if not token:
        token = get_access_token(force_refresh=True)
    if not token:
        context["error"] = "Token akses Amadeus tidak tersedia. Set AMADEUS_ACCESS_TOKEN atau AMADEUS_CLIENT_ID/SECRET."
        return render(request, "flight/results.html", context)

    params = {
        "originLocationCode": origin.upper(),
        "destinationLocationCode": destination.upper(),
        "departureDate": departure_date,
        "adults": adults,
        "max": 20,
    }
    if return_date:
        params["returnDate"] = return_date

    query = urlencode(params)
    url = f"https://test.api.amadeus.com/v2/shopping/flight-offers?{query}"
    req = Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            context["results"] = data
            request.session["search_results"] = data
            try:
                sl = FlightSearchLog.objects.create(
                    origin=origin.upper(),
                    destination=destination.upper(),
                    departure_date=departure_date,
                    return_date=return_date if return_date else None,
                    is_round_trip=bool(return_date),
                )
                request.session["search_log_id"] = sl.id
                request.session["origin_label"] = origin_label
                request.session["destination_label"] = destination_label
            except Exception:
                pass
    except HTTPError as e:
        if e.code == 401:
            token = get_access_token(force_refresh=True)
            if token:
                req = Request(url)
                req.add_header("Authorization", f"Bearer {token}")
                req.add_header("Content-Type", "application/json")
                try:
                    with urlopen(req, timeout=20) as resp:
                        raw = resp.read().decode("utf-8")
                        data = json.loads(raw)
                        context["results"] = data
                        request.session["search_results"] = data
                except HTTPError as e2:
                    try:
                        err_body = e2.read().decode("utf-8")
                    except Exception:
                        err_body = str(e2)
                    context["error"] = f"HTTP {e2.code}: {err_body}"
                except URLError as e2:
                    context["error"] = f"Network error: {e2.reason}"
            else:
                try:
                    err_body = e.read().decode("utf-8")
                except Exception:
                    err_body = str(e)
                context["error"] = f"HTTP {e.code}: {err_body}"
        else:
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                err_body = str(e)
            context["error"] = f"HTTP {e.code}: {err_body}"
    except URLError as e:
        context["error"] = f"Network error: {e.reason}"
    except Exception as e:
        context["error"] = str(e)

    return render(request, "flight/results.html", context)


def price_offer(request):
    if request.method != "POST":
        return HttpResponseBadRequest("Metode harus POST")

    token = getattr(request, "amadeus_token", None) or settings.AMADEUS_ACCESS_TOKEN or get_access_token()
    if not token:
        token = get_access_token(force_refresh=True)
    if not token:
        return HttpResponseBadRequest("Token akses Amadeus tidak tersedia")

    offer_json = request.POST.get("offer_json")
    if not offer_json:
        return HttpResponseBadRequest("Tidak ada data penawaran")

    try:
        offer = json.loads(offer_json)
    except Exception:
        return HttpResponseBadRequest("Format penawaran tidak valid")

    body = json.dumps({
        "data": {
            "type": "flight-offers-pricing",
            "flightOffers": [offer]
        }
    }).encode("utf-8")

    url = "https://test.api.amadeus.com/v1/shopping/flight-offers/pricing"
    req = Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")

    context = {"priced": None, "error": None}
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            context["priced"] = data
    except HTTPError as e:
        if e.code == 401:
            token = get_access_token(force_refresh=True)
            if token:
                req = Request(url, data=body, method="POST")
                req.add_header("Authorization", f"Bearer {token}")
                req.add_header("Content-Type", "application/json")
                try:
                    with urlopen(req, timeout=20) as resp:
                        raw = resp.read().decode("utf-8")
                        data = json.loads(raw)
                        context["priced"] = data
                except HTTPError as e2:
                    try:
                        err_body = e2.read().decode("utf-8")
                    except Exception:
                        err_body = str(e2)
                    context["error"] = f"HTTP {e2.code}: {err_body}"
                except URLError as e2:
                    context["error"] = f"Network error: {e2.reason}"
            else:
                try:
                    err_body = e.read().decode("utf-8")
                except Exception:
                    err_body = str(e)
                context["error"] = f"HTTP {e.code}: {err_body}"
        else:
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                err_body = str(e)
            context["error"] = f"HTTP {e.code}: {err_body}"
    except URLError as e:
        context["error"] = f"Network error: {e.reason}"
    except Exception as e:
        context["error"] = str(e)

    return render(request, "flight/priced.html", context)


def booking_page(request):
    if request.method == "GET":
        idx = request.GET.get("idx")
        results = request.session.get("search_results")
        offer = None
        if results and idx is not None:
            try:
                offer = results.get("data", [])[int(idx)]
            except Exception:
                offer = None
        temp_id = None
        if offer:
            try:
                sl_id = request.session.get("search_log_id")
                first_itin = (offer.get("itineraries") or [{}])[0]
                first_seg = (first_itin.get("segments") or [{}])[0]
                airline_code = first_seg.get("carrierCode", "")
                flight_number = first_seg.get("number", "")
                origin_code = first_seg.get("departure", {}).get("iataCode", "")
                destination_code = first_seg.get("arrival", {}).get("iataCode", "")
                departure_time = first_seg.get("departure", {}).get("at")
                arrival_time = first_seg.get("arrival", {}).get("at")
                duration = first_itin.get("duration", "")
                price_total = offer.get("price", {}).get("total", "0")
                currency = offer.get("price", {}).get("currency", "")
                ft = FlightTemp.objects.create(
                    search_id=sl_id,
                    airline_code=airline_code,
                    flight_number=flight_number,
                    origin=origin_code,
                    destination=destination_code,
                    departure_time=departure_time,
                    arrival_time=arrival_time,
                    duration=duration,
                    price_total=Decimal(str(price_total or "0")),
                    currency=currency,
                    amadeus_offer_id=offer.get("id", ""),
                )
                temp_id = ft.id
                request.session["flight_temp_id"] = temp_id
            except Exception:
                temp_id = None
        return render(request, "flight/booking.html", {
            "offer": offer,
            "idx": idx,
            "selection_id": temp_id,
            "origin_label": request.session.get("origin_label"),
            "destination_label": request.session.get("destination_label"),
        })

    if request.method == "POST":
        return redirect("confirm_booking")
    return redirect("search_flights")


def confirm_booking(request):
    if request.method != "POST":
        return redirect("search_flights")

    idx = request.POST.get("idx")
    name = request.POST.get("name")
    passport = request.POST.get("passport")
    nationality = request.POST.get("nationality")
    birth_date = request.POST.get("birth_date")
    results = request.session.get("search_results")
    offer = None
    if results and idx is not None:
        try:
            offer = results.get("data", [])[int(idx)]
        except Exception:
            offer = None

    token = getattr(request, "amadeus_token", None) or settings.AMADEUS_ACCESS_TOKEN or get_access_token()
    priced = None
    error = None
    if token and offer:
        body = json.dumps({
            "data": {"type": "flight-offers-pricing", "flightOffers": [offer]}
        }).encode("utf-8")
        url = "https://test.api.amadeus.com/v1/shopping/flight-offers/pricing"
        req = Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
                priced = json.loads(raw)
        except HTTPError as e:
            if e.code == 401:
                token = get_access_token(force_refresh=True)
                if token:
                    req = Request(url, data=body, method="POST")
                    req.add_header("Authorization", f"Bearer {token}")
                    req.add_header("Content-Type", "application/json")
                    try:
                        with urlopen(req, timeout=20) as resp:
                            raw = resp.read().decode("utf-8")
                            priced = json.loads(raw)
                    except HTTPError as e2:
                        try:
                            err_body = e2.read().decode("utf-8")
                        except Exception:
                            err_body = str(e2)
                        error = f"HTTP {e2.code}: {err_body}"
                    except URLError as e2:
                        error = f"Network error: {e2.reason}"
                else:
                    try:
                        err_body = e.read().decode("utf-8")
                    except Exception:
                        err_body = str(e)
                    error = f"HTTP {e.code}: {err_body}"
            else:
                try:
                    err_body = e.read().decode("utf-8")
                except Exception:
                    err_body = str(e)
                error = f"HTTP {e.code}: {err_body}"
        except URLError as e:
            error = f"Network error: {e.reason}"
        except Exception as e:
            error = str(e)

    price_total = None
    currency = None
    try:
        src = priced or offer or {}
        if src:
            if priced and priced.get("data") and isinstance(priced.get("data"), dict):
                p = priced.get("data", {}).get("flightOffers", [{}])[0]
            else:
                p = offer
            price_total = p.get("price", {}).get("total")
            currency = p.get("price", {}).get("currency")
    except Exception:
        pass

    temp_id = request.session.get("flight_temp_id")
    temp = None
    if temp_id:
        temp = FlightTemp.objects.filter(id=temp_id).first()

    try:
        if temp and price_total and currency:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            u = request.user if getattr(request, "user", None) and request.user.is_authenticated else User.objects.filter(username="admin").first()
            ref = f"BK{int(__import__('time').time())}"
            b = Booking.objects.create(
                user=u,
                flight=temp,
                booking_reference=ref,
                status="pending",
            )
            Passenger.objects.create(
                booking=b,
                full_name=name,
                passport_number=passport,
                nationality=nationality or "",
                birth_date=birth_date,
            )
            Payment.objects.create(
                booking=b,
                amount=Decimal(str(price_total)),
                currency=currency,
                status="pending",
            )
    except Exception:
        pass

    context = {"offer": offer, "priced": priced, "name": name, "passport": passport, "nationality": nationality, "error": error}
    return render(request, "flight/confirm.html", context)


def locations_lookup(request):
    q = request.GET.get("q", "").strip()
    defaults = [
        {"code": "CGK", "name": "Jakarta", "label": "Jakarta (CGK)"},
        {"code": "DPS", "name": "Denpasar", "label": "Denpasar (DPS)"},
        {"code": "SUB", "name": "Surabaya", "label": "Surabaya (SUB)"},
        {"code": "JOG", "name": "Yogyakarta", "label": "Yogyakarta (JOG)"},
        {"code": "KNO", "name": "Medan", "label": "Medan (KNO)"},
        {"code": "BDO", "name": "Bandung", "label": "Bandung (BDO)"},
        {"code": "SIN", "name": "Singapore", "label": "Singapore (SIN)"},
        {"code": "KUL", "name": "Kuala Lumpur", "label": "Kuala Lumpur (KUL)"},
        {"code": "BKK", "name": "Bangkok", "label": "Bangkok (BKK)"},
    ]
    if not q:
        return JsonResponse({"items": defaults})
    token = getattr(request, "amadeus_token", None) or settings.AMADEUS_ACCESS_TOKEN or get_access_token()
    if not token:
        token = get_access_token(force_refresh=True)
    if not token:
        return JsonResponse({"items": []})
    params = {
        "subType": "AIRPORT,CITY",
        "keyword": q,
        "page[limit]": 7,
    }
    url = "https://test.api.amadeus.com/v1/reference-data/locations?" + urlencode(params)
    req = Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    items = []
    def _parse(data):
        for entry in data.get("data", []):
            code = entry.get("iataCode")
            name = entry.get("name") or (entry.get("address") or {}).get("cityName") or ""
            label = f"{name} ({code})" if code and name else (code or name or "")
            items.append({"code": code, "name": name, "label": label})
    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            _parse(data)
    except HTTPError as e:
        if e.code == 401:
            token = get_access_token(force_refresh=True)
            if token:
                req = Request(url)
                req.add_header("Authorization", f"Bearer {token}")
                req.add_header("Content-Type", "application/json")
                try:
                    with urlopen(req, timeout=15) as resp:
                        raw = resp.read().decode("utf-8")
                        data = json.loads(raw)
                        _parse(data)
                except Exception:
                    pass
        else:
            pass
    except Exception:
        pass
    if not items:
        items = defaults
    return JsonResponse({"items": items})
