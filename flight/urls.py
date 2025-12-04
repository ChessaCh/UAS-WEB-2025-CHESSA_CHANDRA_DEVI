from django.urls import path
from .views import search_page, flight_results, booking_page, confirm_booking, price_offer, locations_lookup, login_view, register_view, logout_view, my_bookings, confirm_payment, receipt_api, availability_api

urlpatterns = [
    path('', search_page, name='search_flights'),
    path('results', flight_results, name='flight_results'),
    path('booking', booking_page, name='booking_page'),
    path('confirm', confirm_booking, name='confirm_booking'),
    path('price', price_offer, name='price_offer'),
    path('api/locations', locations_lookup, name='locations_lookup'),
    path('login', login_view, name='login'),
    path('register', register_view, name='register'),
    path('logout', logout_view, name='logout'),
    path('bookings', my_bookings, name='my_bookings'),
    path('payments/confirm', confirm_payment, name='confirm_payment'),
    path('api/receipt', receipt_api, name='receipt_api'),
    path('api/availability', availability_api, name='availability_api'),
]