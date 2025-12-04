from django.contrib import admin
from .models import FlightSearchLog, FlightTemp, Booking, Passenger, Payment

@admin.register(FlightSearchLog)
class FlightSearchLogAdmin(admin.ModelAdmin):
    list_display = ("id", "origin", "destination", "departure_date", "return_date", "is_round_trip", "created_at")
    search_fields = ("origin", "destination")

@admin.register(FlightTemp)
class FlightTempAdmin(admin.ModelAdmin):
    list_display = ("id", "search", "airline_code", "flight_number", "origin", "destination", "departure_time", "arrival_time", "price_total", "currency", "amadeus_offer_id", "created_at")
    search_fields = ("amadeus_offer_id", "airline_code", "flight_number")

@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "flight", "booking_reference", "status", "created_at", "updated_at")
    list_filter = ("status",)

@admin.register(Passenger)
class PassengerAdmin(admin.ModelAdmin):
    list_display = ("id", "booking", "full_name", "passport_number", "nationality", "birth_date")

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "booking", "amount", "currency", "status", "created_at")
