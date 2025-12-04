from django.db import models
from django.conf import settings


class FlightSearchLog(models.Model):
    origin = models.CharField(max_length=10)
    destination = models.CharField(max_length=10)
    departure_date = models.DateField()
    return_date = models.DateField(null=True, blank=True)
    is_round_trip = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class FlightTemp(models.Model):
    search = models.ForeignKey(FlightSearchLog, on_delete=models.CASCADE, related_name="flights")
    airline_code = models.CharField(max_length=10)
    flight_number = models.CharField(max_length=20)
    origin = models.CharField(max_length=10)
    destination = models.CharField(max_length=10)
    departure_time = models.DateTimeField()
    arrival_time = models.DateTimeField()
    duration = models.CharField(max_length=20)
    price_total = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=10)
    amadeus_offer_id = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)


class Booking(models.Model):
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("confirmed", "Confirmed"),
        ("cancelled", "Cancelled"),
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    flight = models.ForeignKey(FlightTemp, on_delete=models.CASCADE)
    booking_reference = models.CharField(max_length=50, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Passenger(models.Model):
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="passengers")
    full_name = models.CharField(max_length=100)
    passport_number = models.CharField(max_length=50)
    nationality = models.CharField(max_length=50)
    birth_date = models.DateField()


class Payment(models.Model):
    booking = models.OneToOneField(Booking, on_delete=models.CASCADE, related_name="payment")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=10)
    status = models.CharField(max_length=20, choices=(
        ("pending", "Pending"),
        ("paid", "Paid"),
        ("failed", "Failed"),
    ), default="pending")
    created_at = models.DateTimeField(auto_now_add=True)