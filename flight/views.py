from django.shortcuts import render, redirect
from django.http import HttpResponseBadRequest, JsonResponse
from django.conf import settings
import json
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from .amadeus import get_access_token
from .models import FlightSearchLog, FlightTemp, Booking, Passenger, Payment
from decimal import Decimal
from django.contrib.auth import authenticate, login, logout
import logging
from django.contrib.auth.decorators import login_required
import os
from django.db import transaction, connection
from django.contrib.auth.models import User

_FX_CACHE = {}
logger = logging.getLogger(__name__)

def _get_rate_to_idr(currency):
    c = (currency or "").upper()
    if not c or c == "IDR":
        return Decimal("1")
    now = int(time.time())
    e = _FX_CACHE.get(c)
    if e and now - e[1] < 21600:
        return e[0]
    try:
        url = f"https://api.exchangerate.host/latest?base={c}&symbols=IDR"
        req = Request(url)
        with urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            rate = Decimal(str((data.get("rates") or {}).get("IDR")))
            if rate:
                _FX_CACHE[c] = (rate, now)
                return rate
    except Exception:
        pass
    fallback = {
        "USD": Decimal("16000"),
        "EUR": Decimal("17000"),
        "SGD": Decimal("12000"),
        "MYR": Decimal("3400"),
        "THB": Decimal("450"),
        "AUD": Decimal("10500"),
        "JPY": Decimal("110"),
    }
    rate = fallback.get(c)
    if rate:
        _FX_CACHE[c] = (rate, now)
        return rate
    return Decimal("1")

def _convert_to_idr(amount, currency):
    try:
        amt = Decimal(str(amount))
    except Exception:
        return None, None
    rate = _get_rate_to_idr(currency)
    idr = (amt * rate).quantize(Decimal("1"))
    s = f"{idr:,.0f}".replace(",", ".")
    return idr, s

def _norm_country_code(val):
    x = (val or "").strip().upper()
    if not x:
        return "ID"
    if len(x) == 2 and x.isalpha():
        return x
    m = {
        "INDONESIA": "ID",
        "IDN": "ID",
        "FRANCE": "FR",
        "FRA": "FR",
        "GERMANY": "DE",
        "DEU": "DE",
        "MALAYSIA": "MY",
        "MYS": "MY",
        "SINGAPORE": "SG",
        "SGP": "SG",
        "THAILAND": "TH",
        "THA": "TH",
        "JAPAN": "JP",
        "JPN": "JP",
        "AUSTRALIA": "AU",
        "AUS": "AU",
        "UNITED STATES": "US",
        "USA": "US",
        "UNITED KINGDOM": "GB",
        "UK": "GB",
        "GREAT BRITAIN": "GB",
        "CHINA": "CN",
        "CHN": "CN",
    }
    return m.get(x) or (x[:2] if len(x) >= 2 else "ID")

def _norm_phone(val):
    s = (val or "").strip()
    s = s.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    s = s.replace("+62", "").lstrip("0")
    return s

def _analyze_offer(offer):
    try:
        seats = offer.get("numberOfBookableSeats")
        try:
            seats = int(seats) if seats is not None else None
        except Exception:
            seats = None
        validating = set((offer.get("validatingAirlineCodes") or []))
        carriers = set()
        for itin in (offer.get("itineraries") or []):
            for seg in (itin.get("segments") or []):
                cc = seg.get("carrierCode")
                if cc:
                    carriers.add(cc)
        mismatch = bool(validating) and len(validating.intersection(carriers)) == 0
        return {"bookableSeats": seats, "validatingMismatch": mismatch}
    except Exception:
        return {"bookableSeats": None, "validatingMismatch": False}

def _ensure_token(request):
    try:
        t = request.session.get("amadeus_token")
        ts = int(request.session.get("amadeus_token_ts") or 0)
    except Exception:
        t = None
        ts = 0
    now = int(time.time())
    if (not t) or (now - ts > 1500):
        nt = get_access_token(force_refresh=True) or get_access_token()
        if nt:
            t = nt
            try:
                request.session["amadeus_token"] = t
                request.session["amadeus_token_ts"] = now
            except Exception:
                pass
    return t

def _amadeus_request_json(request, url, method="GET", body=None, timeout=20):
    attempts = 3
    last_err = None
    for i in range(attempts):
        token = None
        if i == 0:
            token = _ensure_token(request) or getattr(request, "amadeus_token", None) or settings.AMADEUS_ACCESS_TOKEN or os.environ.get("ACCESS_TOKEN") or get_access_token()
        else:
            try:
                request.session.pop("amadeus_token", None)
                request.session.pop("amadeus_token_ts", None)
            except Exception:
                pass
            token = get_access_token(force_refresh=True) or os.environ.get("ACCESS_TOKEN") or get_access_token()
            if token:
                try:
                    request.session["amadeus_token"] = token
                    request.session["amadeus_token_ts"] = int(time.time())
                except Exception:
                    pass
        req = Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except HTTPError as e:
            last_err = e
            if e.code == 401:
                try:
                    logger.warning("API authentication failed (401). Retrying with refreshed token.")
                except Exception:
                    pass
                continue
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                err_body = str(e)
            raise HTTPError(e.url, e.code, err_body, e.hdrs, e.fp)
        except URLError as e:
            raise e
        except Exception as e:
            last_err = e
            break
    if isinstance(last_err, HTTPError):
        try:
            err_body = last_err.read().decode("utf-8")
        except Exception:
            err_body = str(last_err)
        raise HTTPError(last_err.url, last_err.code, err_body, last_err.hdrs, last_err.fp)
    raise last_err or Exception("Unknown error")


@login_required(login_url='login')
def search_page(request):
    bg = request.GET.get("bg")
    if bg:
        try:
            request.session["hero_bg_url"] = bg
        except Exception:
            pass
    hero_bg_url = request.session.get("hero_bg_url") or "https://images.unsplash.com/photo-1519003721541-1d368bb40a63?auto=format&fit=crop&w=1600&q=60"
    return render(request, "flight/search.html", {"hero_bg_url": hero_bg_url})


@login_required(login_url='login')
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
    if origin and destination and origin.upper() == destination.upper():
        context["error"] = "Kota asal dan tujuan tidak boleh sama."
        return render(request, "flight/results.html", context)

    token = _ensure_token(request) or getattr(request, "amadeus_token", None) or settings.AMADEUS_ACCESS_TOKEN or get_access_token()
    if not token:
        token = get_access_token(force_refresh=True)
        if token:
            try:
                request.session["amadeus_token"] = token
            except Exception:
                pass
    if not token:
        try:
            logger.warning("Amadeus access token not available. Ensure AMADEUS_ACCESS_TOKEN or AMADEUS_CLIENT_ID/SECRET are set.")
        except Exception:
            pass
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
    try:
        data = _amadeus_request_json(request, url, method="GET", body=None, timeout=20)
        context["results"] = data
        request.session["search_results"] = data
        try:
            for offer in (data.get("data") or []):
                price = offer.get("price") or {}
                total = price.get("total")
                curr = price.get("currency")
                _, s = _convert_to_idr(total, curr)
                if s:
                    price["total_idr_str"] = s
                    offer["price"] = price
                offer["meta"] = _analyze_offer(offer)
            filtered = []
            for offer in (data.get("data") or []):
                flags = offer.get("meta") or {}
                seats = flags.get("bookableSeats")
                mismatch = flags.get("validatingMismatch")
                if seats is not None and seats <= 0:
                    continue
                if mismatch:
                    continue
                filtered.append(offer)
            data["data"] = filtered
            if not filtered:
                context["error"] = "Tidak ada kursi tersedia atau penawaran valid untuk ditampilkan."
        except Exception:
            pass
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
        body_str = None
        try:
            body_str = str(e)
        except Exception:
            body_str = None
        fallback_needed = False
        try:
            if body_str:
                j = json.loads(body_str)
                errs = j.get("errors") or []
                for err in errs:
                    t = str(err.get("title") or "").upper()
                    c = str(err.get("code") or "")
                    s = str(err.get("status") or "")
                    if ("SYSTEM ERROR HAS OCCURRED" in t) or (c == "141") or (s == "500"):
                        fallback_needed = True
                        break
        except Exception:
            pass
        if fallback_needed:
            try:
                params_v1 = {
                    "origin": origin.upper(),
                    "destination": destination.upper(),
                    "departureDate": departure_date,
                    "adults": adults,
                    "max": "20",
                    "nonStop": "false",
                }
                if return_date:
                    params_v1["returnDate"] = return_date
                url_v1 = f"https://test.api.amadeus.com/v1/shopping/flight-offers?{urlencode(params_v1)}"
                try:
                    logger.warning("Primary search failed with system error; attempting v1 fallback url=%s", url_v1.split("?")[0])
                except Exception:
                    pass
                data = _amadeus_request_json(request, url_v1, method="GET", body=None, timeout=20)
                context["results"] = data
                request.session["search_results"] = data
                try:
                    for offer in (data.get("data") or []):
                        price = offer.get("price") or {}
                        total = price.get("total")
                        curr = price.get("currency")
                        _, s_idr = _convert_to_idr(total, curr)
                        if s_idr:
                            price["total_idr_str"] = s_idr
                            offer["price"] = price
                        offer["meta"] = _analyze_offer(offer)
                except Exception:
                    pass
            except Exception as e2:
                context["error"] = f"HTTP {e.code}: {str(e)}"
        else:
            context["error"] = f"HTTP {e.code}: {str(e)}"
    except URLError as e:
        context["error"] = f"Network error: {e.reason}"
    except Exception as e:
        context["error"] = str(e)

    # Friendly error for common cases
    try:
        if context.get("error") and context["error"].startswith("HTTP "):
            j = json.loads(context["error"].split(": ", 1)[1])
            errs = j.get("errors") or []
            codes = set(str(x.get("code")) for x in errs)
            if "38192" in codes:
                context["friendly_error"] = "Sesi token kadaluarsa. Silakan coba lagi."
            else:
                for x in errs:
                    t = str(x.get("title") or "").lower()
                    d = str(x.get("detail") or "").lower()
                    if ("invalid access token" in t) or ("invalid access token" in d):
                        context["friendly_error"] = "Token akses tidak valid. Sistem memperbarui token secara otomatis. Silakan coba lagi."
                        try:
                            logger.warning("Invalid access token detected. Token refresh initiated.")
                        except Exception:
                            pass
                        break
    except Exception:
        pass
    return render(request, "flight/results.html", context)


@login_required(login_url='login')
def price_offer(request):
    if request.method != "POST":
        return HttpResponseBadRequest("Metode harus POST")

    token = _ensure_token(request) or getattr(request, "amadeus_token", None) or settings.AMADEUS_ACCESS_TOKEN or get_access_token()
    if not token:
        token = get_access_token(force_refresh=True)
        if token:
            try:
                request.session["amadeus_token"] = token
            except Exception:
                pass
    if not token:
        try:
            logger.warning("Amadeus access token not available for price_offer.")
        except Exception:
            pass
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
    context = {"priced": None, "error": None}
    try:
        data = _amadeus_request_json(request, url, method="POST", body=body, timeout=20)
        context["priced"] = data
    except HTTPError as e:
        context["error"] = f"HTTP {e.code}: {str(e)}"
    except URLError as e:
        context["error"] = f"Network error: {e.reason}"
    except Exception as e:
        context["error"] = str(e)

    return render(request, "flight/priced.html", context)


@login_required(login_url='login')
def booking_page(request):
    if request.method == "GET":
        idx_str = request.GET.get("idx")
        logger.debug("booking_page GET idx=%s", idx_str)
        status_code = 200
        error_msg = None

        if idx_str is None:
            error_msg = "Parameter idx wajib disertakan."
            return render(request, "flight/bookings.html", {
                "error": error_msg,
                "offer": None,
                "idx": None,
                "selection_id": None,
            }, status=400)

        try:
            idx = int(idx_str)
        except ValueError:
            error_msg = "Parameter idx tidak valid."
            return render(request, "flight/bookings.html", {
                "error": error_msg,
                "offer": None,
                "idx": None,
                "selection_id": None,
            }, status=400)

        results = request.session.get("search_results")
        if not results or not results.get("data"):
            logger.warning("booking_page missing search_results in session")
            error_msg = "Tidak ada hasil pencarian aktif. Silakan cari penerbangan terlebih dahulu."
            return render(request, "flight/bookings.html", {
                "error": error_msg,
                "offer": None,
                "idx": idx,
                "selection_id": None,
            }, status=400)

        data = results.get("data", [])
        if idx < 0 or idx >= len(data):
            logger.warning("booking_page idx out of range idx=%s len=%s", idx, len(data))
            error_msg = "Penawaran tidak ditemukan."
            return render(request, "flight/bookings.html", {
                "error": error_msg,
                "offer": None,
                "idx": idx,
                "selection_id": None,
            }, status=404)

        offer = data[idx]
        temp_id = None
        offer_idr_str = None
        try:
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
            _, offer_idr_str = _convert_to_idr(price_total, currency)
            sl_id = request.session.get("search_log_id")
            if not sl_id:
                logger.warning("booking_page missing search_log_id; skipping FlightTemp creation")
            else:
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
            logger.exception("booking_page failed to create FlightTemp")
            error_msg = "Terjadi masalah saat menyiapkan data booking. Anda tetap dapat melanjutkan pengisian."

        return render(request, "flight/bookings.html", {
            "offer": offer,
            "idx": idx,
            "selection_id": temp_id,
            "origin_label": request.session.get("origin_label"),
            "destination_label": request.session.get("destination_label"),
            "offer_idr_str": offer_idr_str,
            "error": error_msg,
        }, status=status_code)

    if request.method == "POST":
        return redirect("confirm_booking")
    return redirect("search_flights")


@login_required(login_url='login')
def confirm_booking(request):
    if request.method != "POST":
        return redirect("search_flights")

    idx = request.POST.get("idx")
    name = request.POST.get("name")
    passport = request.POST.get("passport")
    nationality = request.POST.get("nationality")
    birth_date = request.POST.get("birth_date")
    email = request.POST.get("email")
    phone = request.POST.get("phone")
    passport_expiry = request.POST.get("passport_expiry")
    results = request.session.get("search_results")
    offer = None
    if results and idx is not None:
        try:
            offer = results.get("data", [])[int(idx)]
        except Exception:
            offer = None

    token = _ensure_token(request) or getattr(request, "amadeus_token", None) or settings.AMADEUS_ACCESS_TOKEN or get_access_token()
    priced = None
    error = None
    order = None
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
                    try:
                        request.session["amadeus_token"] = token
                    except Exception:
                        pass
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
            _, price_idr_str = _convert_to_idr(price_total, currency)
            offer_flags = _analyze_offer(p or {})
    except Exception:
        pass

    temp_id = request.session.get("flight_temp_id")
    temp = None
    if temp_id:
        temp = FlightTemp.objects.filter(id=temp_id).first()

    created_booking_ref = None
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
            created_booking_ref = ref
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

    try:
        if token and priced and email and phone:
            try:
                parts = (name or "").strip().split()
                first = parts[0] if parts else ""
                last = " ".join(parts[1:]) if len(parts) > 1 else first
                po = priced.get("data", {}).get("flightOffers", [{}])[0]
                travelers = [{
                    "id": "1",
                    "dateOfBirth": birth_date or "1990-01-01",
                    "name": {"firstName": first, "lastName": last},
                    "contact": {
                        "emailAddress": email,
                        "phones": [{"deviceType": "MOBILE", "countryCallingCode": "62", "number": _norm_phone(phone)}]
                    },
                    "documents": [{
                        "documentType": "PASSPORT",
                        "number": passport or "",
                        "issuanceCountry": _norm_country_code(nationality or "ID"),
                        "nationality": _norm_country_code(nationality or "ID"),
                        "expiryDate": passport_expiry or "2030-12-31",
                        "holder": True
                    }]
                }]
                body_order = json.dumps({
                    "data": {
                        "type": "flight-order",
                        "flightOffers": [po],
                        "travelers": travelers,
                        "remarks": {
                            "general": [{"subType": "GENERAL_MISCELLANEOUS", "text": "Customer contact provided"}]
                        },
                        "ticketingAgreement": {"option": "DELAY_TO_CANCEL", "delay": "1D"},
                        "contacts": [{
                            "addresseeName": {"firstName": first, "lastName": last},
                            "purpose": "STANDARD",
                            "phones": [{"deviceType": "MOBILE", "countryCallingCode": "62", "number": _norm_phone(phone)}],
                            "emailAddress": email,
                            "address": {"lines": ["Jakarta"], "postalCode": "10000", "cityName": "Jakarta", "countryCode": _norm_country_code(nationality or "ID")}
                        }]
                    }
                }).encode("utf-8")
                url_o = "https://test.api.amadeus.com/v1/booking/flight-orders"
                req_o = Request(url_o, data=body_order, method="POST")
                req_o.add_header("Authorization", f"Bearer {token}")
                req_o.add_header("Content-Type", "application/json")
                try:
                    with urlopen(req_o, timeout=20) as resp:
                        raw = resp.read().decode("utf-8")
                        order = json.loads(raw)
                except HTTPError as e:
                    if e.code == 401:
                        token = get_access_token(force_refresh=True)
                        if token:
                            try:
                                request.session["amadeus_token"] = token
                            except Exception:
                                pass
                            req_o = Request(url_o, data=body_order, method="POST")
                            req_o.add_header("Authorization", f"Bearer {token}")
                            req_o.add_header("Content-Type", "application/json")
                            try:
                                with urlopen(req_o, timeout=20) as resp:
                                    raw = resp.read().decode("utf-8")
                                    order = json.loads(raw)
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
                        try:
                            j = json.loads(err_body)
                            errs = j.get("errors") or []
                            need_retry = any(str(x.get("code")) == "34651" for x in errs)
                        except Exception:
                            need_retry = False
                        if need_retry:
                            try:
                                body2 = json.dumps({
                                    "data": {
                                        "type": "flight-offers-pricing",
                                        "flightOffers": [offer]
                                    }
                                }).encode("utf-8")
                                req2 = Request("https://test.api.amadeus.com/v1/shopping/flight-offers/pricing", data=body2, method="POST")
                                req2.add_header("Authorization", f"Bearer {token}")
                                req2.add_header("Content-Type", "application/json")
                                with urlopen(req2, timeout=20) as resp2:
                                    raw2 = resp2.read().decode("utf-8")
                                    priced2 = json.loads(raw2)
                                    po2 = (priced2.get("data", {}).get("flightOffers") or [{}])[0]
                                    body_order2 = json.dumps({
                                        "data": {
                                            "type": "flight-order",
                                            "flightOffers": [po2],
                                            "travelers": travelers,
                                            "remarks": {
                                                "general": [{"subType": "GENERAL_MISCELLANEOUS", "text": "Customer contact provided"}]
                                            },
                                            "ticketingAgreement": {"option": "DELAY_TO_CANCEL", "delay": "1D"},
                                            "contacts": [{
                                                "addresseeName": {"firstName": first, "lastName": last},
                                                "purpose": "STANDARD",
                                                "phones": [{"deviceType": "MOBILE", "countryCallingCode": "62", "number": _norm_phone(phone)}],
                                                "emailAddress": email,
                                                "address": {"lines": ["Jakarta"], "postalCode": "10000", "cityName": "Jakarta", "countryCode": _norm_country_code(nationality or "ID")}
                                            }]
                                        }
                                    }).encode("utf-8")
                                    req_o2 = Request(url_o, data=body_order2, method="POST")
                                    req_o2.add_header("Authorization", f"Bearer {token}")
                                    req_o2.add_header("Content-Type", "application/json")
                                    with urlopen(req_o2, timeout=20) as resp3:
                                        raw3 = resp3.read().decode("utf-8")
                                        order = json.loads(raw3)
                                        error = None
                            except Exception:
                                pass
            except Exception as e:
                error = str(e)
        elif priced and priced.get("data", {}).get("bookingRequirements"):
            br = priced.get("data", {}).get("bookingRequirements")
            if br.get("emailAddressRequired") and not email:
                error = (error or "") or "Email diperlukan untuk melanjutkan booking"
            if br.get("mobilePhoneNumberRequired") and not phone:
                error = (error or "") or "Nomor HP diperlukan untuk melanjutkan booking"
    except Exception:
        pass

    # Build friendly error messages when possible
    friendly = None
    try:
        if error and error.startswith("HTTP "):
            j = json.loads(error.split(": ")[-1])
            errs = j.get("errors") or []
            codes = set(str(x.get("code")) for x in errs)
            if "34651" in codes:
                friendly = "Kursi tidak tersedia pada segmen yang dipilih. Silakan pilih penawaran lain atau lakukan pencarian ulang."
            elif "32171" in codes:
                friendly = "Data wajib belum lengkap. Periksa masa berlaku paspor, pemegang dokumen, dan kontak."
            elif "477" in codes:
                friendly = "Format data tidak valid pada permintaan."
            elif "38192" in codes:
                friendly = "Sesi token kadaluarsa. Silakan coba lagi."
    except Exception:
        pass

    context = {"offer": offer, "priced": priced, "name": name, "passport": passport, "nationality": nationality, "email": email, "phone": phone, "error": error, "price_idr_str": price_idr_str, "order": order, "friendly_error": friendly, "offer_flags": offer_flags if 'offer_flags' in locals() else None, "booking_reference": created_booking_ref}
    return render(request, "flight/payment.html", context)


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
        if token:
            try:
                request.session["amadeus_token"] = token
            except Exception:
                pass
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
                try:
                    request.session["amadeus_token"] = token
                except Exception:
                    pass
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


@login_required(login_url='login')
def availability_api(request):
    origin = (request.GET.get("origin") or "").strip().upper()
    destination = (request.GET.get("destination") or "").strip().upper()
    departure_date = (request.GET.get("departure_date") or "").strip()
    return_date = (request.GET.get("return_date") or "").strip()
    adults = (request.GET.get("adults") or "1").strip()
    origin_label = (request.GET.get("origin_label") or "").strip()
    destination_label = (request.GET.get("destination_label") or "").strip()

    if not (origin and destination and departure_date):
        return HttpResponseBadRequest("Parameter origin, destination, dan departure_date wajib disertakan")

    def _city_from_label(lbl, code):
        try:
            if lbl and "(" in lbl:
                return lbl.split("(", 1)[0].strip()
        except Exception:
            pass
        return code

    params = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": departure_date,
        "adults": adults,
        "max": 30,
    }
    if return_date:
        params["returnDate"] = return_date

    def _seats_by_class(offer):
        seats = None
        try:
            seats = int(offer.get("numberOfBookableSeats"))
        except Exception:
            seats = None
        cabin = None
        try:
            tp = (offer.get("travelerPricings") or [])
            if tp:
                fdbs = (tp[0].get("fareDetailsBySegment") or [])
                if fdbs:
                    cabin = (fdbs[0].get("cabin") or "").strip().upper()
        except Exception:
            pass
        out = {"economy": None, "business": None, "first": None}
        if seats and seats > 0:
            if cabin == "ECONOMY" or cabin == "PREMIUM_ECONOMY":
                out["economy"] = seats
            elif cabin == "BUSINESS":
                out["business"] = seats
            elif cabin == "FIRST":
                out["first"] = seats
        return out

    def _first_departure_time(offer):
        try:
            itin = (offer.get("itineraries") or [{}])[0]
            seg = (itin.get("segments") or [{}])[0]
            return seg.get("departure", {}).get("at") or ""
        except Exception:
            return ""

    url_v2 = "https://test.api.amadeus.com/v2/shopping/flight-offers?" + urlencode(params)
    data = None
    try:
        data = _amadeus_request_json(request, url_v2, method="GET", body=None, timeout=20)
    except HTTPError as e:
        fallback_needed = False
        try:
            j = json.loads(str(e))
            errs = j.get("errors") or []
            for err in errs:
                t = str(err.get("title") or "").upper()
                c = str(err.get("code") or "")
                s = str(err.get("status") or "")
                if ("SYSTEM ERROR HAS OCCURRED" in t) or (c == "141") or (s == "500"):
                    fallback_needed = True
                    break
        except Exception:
            pass
        if fallback_needed:
            params_v1 = {
                "origin": origin,
                "destination": destination,
                "departureDate": departure_date,
                "adults": adults,
                "max": "30",
                "nonStop": "false",
            }
            if return_date:
                params_v1["returnDate"] = return_date
            url_v1 = "https://test.api.amadeus.com/v1/shopping/flight-offers?" + urlencode(params_v1)
            try:
                data = _amadeus_request_json(request, url_v1, method="GET", body=None, timeout=20)
            except Exception as e2:
                return JsonResponse({"error": f"HTTP {getattr(e, 'code', 400)}: {str(e)}"}, status=400)
        else:
            return JsonResponse({"error": f"HTTP {getattr(e, 'code', 400)}: {str(e)}"}, status=400)
    except URLError as e:
        return JsonResponse({"error": f"Network error: {e.reason}"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    offers = list(data.get("data") or [])
    cleaned = []
    for offer in offers:
        flags = _analyze_offer(offer) or {}
        seats = flags.get("bookableSeats")
        mismatch = flags.get("validatingMismatch")
        if seats is not None and seats <= 0:
            continue
        if mismatch:
            continue
        cleaned.append(offer)

    cleaned.sort(key=_first_departure_time)

    items = []
    for offer in cleaned:
        try:
            itineraries = (offer.get("itineraries") or [])
            first_itin = itineraries[0] if itineraries else {}
            segments = (first_itin.get("segments") or [])
            dep_seg = segments[0] if segments else {}
            arr_seg = segments[-1] if segments else {}
            origin_code = dep_seg.get("departure", {}).get("iataCode") or origin
            dest_code = arr_seg.get("arrival", {}).get("iataCode") or destination
            origin_city = _city_from_label(origin_label, origin_code)
            dest_city = _city_from_label(destination_label, dest_code)

            seg_details = []
            for seg in segments:
                seg_details.append({
                    "carrier": seg.get("carrierCode"),
                    "flight_number": seg.get("number"),
                    "departure_time": (seg.get("departure") or {}).get("at"),
                    "arrival_time": (seg.get("arrival") or {}).get("at"),
                    "origin": (seg.get("departure") or {}).get("iataCode"),
                    "destination": (seg.get("arrival") or {}).get("iataCode"),
                })

            price = offer.get("price") or {}
            base = price.get("base")
            currency = price.get("currency")
            fees_list = price.get("fees") or []
            fees_total = None
            try:
                fees_total = str(sum(Decimal(str(x.get("amount"))) for x in fees_list)) if fees_list else "0"
            except Exception:
                fees_total = None

            seats_class = _seats_by_class(offer)

            items.append({
                "origin_airport": {"city": origin_city, "code": origin_code},
                "destination_airport": {"city": dest_city, "code": dest_code},
                "travel_dates": {"departure": departure_date, "return": return_date or None},
                "available_seats": seats_class,
                "flight_details": seg_details,
                "pricing": {"base_fare": base, "fees_total": fees_total, "currency": currency},
            })
        except Exception:
            continue

    return JsonResponse({"count": len(items), "items": items})


# Auth views
def register_view(request):
    if request.method == "GET":
        return render(request, "flight/register.html", {"error": None})
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password = (request.POST.get("password") or "").strip()
        if not username or not password:
            return render(request, "flight/register.html", {"error": "Username dan password wajib"})
        if User.objects.filter(username=username).exists():
            return render(request, "flight/register.html", {"error": "Username sudah digunakan"})
        u = User.objects.create_user(username=username, password=password)
        login(request, u)
        return redirect("my_bookings")

def login_view(request):
    if request.method == "GET":
        return render(request, "flight/login.html", {"error": None})
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password = (request.POST.get("password") or "").strip()
        u = authenticate(request, username=username, password=password)
        if not u:
            return render(request, "flight/login.html", {"error": "Login gagal. Periksa username/password."})
        login(request, u)
        return redirect("my_bookings")

def logout_view(request):
    logout(request)
    return redirect("search_flights")

def my_bookings(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return redirect("login")
    qs = (
        Booking.objects.filter(user=request.user, status="confirmed")
        .select_related("payment", "flight")
        .prefetch_related("passengers")
        .order_by("-created_at")
    )
    items = []
    for b in qs:
        f = b.flight
        amt = getattr(b.payment, "amount", None)
        curr = getattr(b.payment, "currency", "")
        _, idr_str = _convert_to_idr(amt, curr)
        ps = []
        try:
            for px in b.passengers.all():
                ps.append(px.full_name)
        except Exception:
            pass
        passenger_summary = ", ".join([p for p in ps if p])
        items.append({
            "ref": b.booking_reference,
            "status": b.status,
            "airline": getattr(f, "airline_code", ""),
            "flight_number": getattr(f, "flight_number", ""),
            "origin": getattr(f, "origin", ""),
            "destination": getattr(f, "destination", ""),
            "departure_time": getattr(f, "departure_time", None),
            "arrival_time": getattr(f, "arrival_time", None),
            "price": amt,
            "currency": curr,
            "price_idr_str": idr_str,
            "passenger_summary": passenger_summary,
        })
    confirmed_ref = (request.GET.get("ref") or "").strip()
    confirmed_flag = (request.GET.get("confirmed") or "").strip().lower() in ("1","true","yes")
    confirmation = None
    if confirmed_ref and confirmed_flag:
        bb = (
            Booking.objects.filter(user=request.user, booking_reference=confirmed_ref, status="confirmed")
            .select_related("payment", "flight")
            .first()
        )
        if bb and getattr(bb, "payment", None) and bb.payment.status == "paid":
            ff = bb.flight
            amt = bb.payment.amount
            curr = bb.payment.currency
            _, idr_str = _convert_to_idr(amt, curr)
            confirmation = {
                "ref": bb.booking_reference,
                "amount": amt,
                "currency": curr,
                "amount_idr": idr_str,
                "airline": getattr(ff, "airline_code", ""),
                "flight_number": getattr(ff, "flight_number", ""),
                "origin": getattr(ff, "origin", ""),
                "destination": getattr(ff, "destination", ""),
                "departure_time": getattr(ff, "departure_time", None),
                "arrival_time": getattr(ff, "arrival_time", None),
            }
    return render(request, "flight/bookings.html", {"items": items, "confirmation": confirmation})

@login_required(login_url='login')
def receipt_api(request):
    ref = (request.GET.get("ref") or "").strip()
    if not ref:
        return HttpResponseBadRequest("Referensi booking wajib disertakan")
    try:
        connection.ensure_connection()
    except Exception:
        return HttpResponseBadRequest("Koneksi database tidak tersedia")
    b = (
        Booking.objects.filter(booking_reference=ref, user=request.user)
        .select_related("payment", "flight")
        .prefetch_related("passengers")
        .first()
    )
    if not b:
        return JsonResponse({"error": "not_found"}, status=404)
    f = b.flight
    p = getattr(b, "payment", None)
    passengers = []
    try:
        for px in b.passengers.all():
            passengers.append({
                "full_name": px.full_name,
                "passport_number": px.passport_number,
                "nationality": px.nationality,
                "birth_date": px.birth_date.isoformat() if px.birth_date else None,
                "email": getattr(px, "email", None),
            })
    except Exception:
        pass
    payload = {
        "booking_reference": b.booking_reference,
        "status": b.status,
        "flight": {
            "airline_code": getattr(f, "airline_code", ""),
            "flight_number": getattr(f, "flight_number", ""),
            "origin": getattr(f, "origin", ""),
            "destination": getattr(f, "destination", ""),
            "departure_time": getattr(f, "departure_time", None),
            "arrival_time": getattr(f, "arrival_time", None),
        },
        "passengers": passengers,
        "payment": {
            "amount": getattr(p, "amount", None),
            "currency": getattr(p, "currency", ""),
            "status": getattr(p, "status", ""),
        },
    }
    return JsonResponse(payload)

@login_required(login_url='login')
def confirm_payment(request):
    if request.method != "POST":
        return HttpResponseBadRequest("Metode harus POST")
    ref = (request.POST.get("booking_reference") or "").strip()
    status_in = (request.POST.get("status") or "").strip().lower()
    method = (request.POST.get("method") or "").strip()
    txid = (request.POST.get("txid") or "").strip()
    amount = request.POST.get("amount")
    try:
        amt = Decimal(str(amount)) if amount is not None else None
    except Exception:
        return HttpResponseBadRequest("Jumlah tidak valid")
    if not ref:
        return HttpResponseBadRequest("Referensi booking wajib disertakan")
    try:
        connection.ensure_connection()
    except Exception:
        return HttpResponseBadRequest("Koneksi database tidak tersedia")
    b = Booking.objects.filter(booking_reference=ref, user=request.user).select_related("payment", "flight").first()
    if not b:
        return HttpResponseBadRequest("Booking tidak ditemukan")
    p = getattr(b, "payment", None)
    if not p:
        return HttpResponseBadRequest("Pembayaran belum dibuat untuk booking ini")
    if p.status == "failed":
        logger.warning("Payment confirm attempted on failed payment ref=%s", ref)
        return HttpResponseBadRequest("Pembayaran sudah gagal")
    if p.status == "paid" and b.status == "confirmed":
        return JsonResponse({"ok": True, "status": "already_confirmed"})
    if status_in != "paid":
        logger.info("Payment not successful, ref=%s status=%s", ref, status_in)
        return HttpResponseBadRequest("Status pembayaran tidak sukses")
    if amt is not None and p.amount != amt:
        logger.warning("Payment amount mismatch for ref=%s expected=%s got=%s", ref, p.amount, amt)
        return HttpResponseBadRequest("Jumlah pembayaran tidak cocok")
    try:
        with transaction.atomic():
            p.status = "paid"
            p.save(update_fields=["status"])
            b.status = "confirmed"
            b.save(update_fields=["status", "updated_at"])
            logger.info("Booking confirmed after payment ref=%s txid=%s method=%s", ref, txid, method)
        return JsonResponse({"ok": True, "booking_reference": ref, "status": "confirmed"})
    except Exception as e:
        logger.error("Error confirming payment ref=%s: %s", ref, str(e))
        return HttpResponseBadRequest("Gagal mengkonfirmasi pembayaran")
