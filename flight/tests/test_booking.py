from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User


class BookingFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="p")
        self.client.login(username="u", password="p")

    def test_booking_route_resolves(self):
        url = reverse("booking_page")
        self.assertEqual(url, "/booking")

    def test_booking_redirects_without_results(self):
        r = self.client.get(reverse("booking_page"), {"idx": 0})
        self.assertEqual(r.status_code, 400)

    def test_booking_renders_with_offer(self):
        offer = {
            "id": "O1",
            "price": {"total": "100", "currency": "USD"},
            "itineraries": [
                {"segments": [
                    {
                        "carrierCode": "MH",
                        "number": "710",
                        "departure": {"iataCode": "CGK", "at": "2025-12-12T11:00:00"},
                        "arrival": {"iataCode": "KUL", "at": "2025-12-12T14:00:00"},
                    }
                ]}
            ],
        }
        s = self.client.session
        s["search_results"] = {"data": [offer]}
        s.save()
        r = self.client.get(reverse("booking_page"), {"idx": 0})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Flight Booking", r.content)

    def test_booking_missing_idx(self):
        r = self.client.get(reverse("booking_page"))
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"Parameter idx", r.content)

    def test_booking_invalid_idx(self):
        s = self.client.session
        s["search_results"] = {"data": []}
        s.save()
        r = self.client.get(reverse("booking_page"), {"idx": "abc"})
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"tidak valid", r.content)

    def test_booking_out_of_range(self):
        s = self.client.session
        s["search_results"] = {"data": [{"id": "only"}]}
        s.save()
        r = self.client.get(reverse("booking_page"), {"idx": 5})
        self.assertEqual(r.status_code, 404)
        self.assertIn(b"Penawaran tidak ditemukan", r.content)