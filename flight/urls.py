from django.urls import path
from .views import search_page, flight_results, booking_page, confirm_booking, price_offer

urlpatterns = [
    path('', search_page, name='search_flights'),
    path('results', flight_results, name='flight_results'),
    path('booking', booking_page, name='flight_booking'),
    path('confirm', confirm_booking, name='confirm_booking'),
    path('price', price_offer, name='price_offer'),
]