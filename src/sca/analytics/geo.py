"""Where suppliers are, and how far that is.

Distance is real geometry and is computed. Everything downstream of it —
how many days a container takes to cross that distance — is an assumption, and
is labelled as one wherever it appears.

Origins are the main export hub of each sourcing country rather than the
supplier's own address, because a street address is not held anywhere and
guessing one would be inventing precision. Guangzhou stands for Chinese textile
supply, Istanbul for Turkish packaging. The error this introduces is a few
hundred kilometres against journeys of several thousand, which moves the
estimate by well under a day — far less than the transit assumption itself.
"""

from math import asin, cos, radians, sin, sqrt

# Riyadh. Everything is measured to here because this is where the buying is.
HOME = (24.7136, 46.6753)

# ISO 3166-1 alpha-2 → (hub, latitude, longitude). Extend as suppliers appear;
# an unknown country returns no distance rather than a default one, so a missing
# entry shows as "unknown" instead of quietly reporting a journey to nowhere.
ORIGINS: dict[str, tuple[str, float, float]] = {
    "CN": ("Guangzhou", 23.1291, 113.2644),
    "IN": ("Mumbai", 19.0760, 72.8777),
    "TR": ("Istanbul", 41.0082, 28.9784),
    "AE": ("Dubai", 25.2048, 55.2708),
    "SA": ("Riyadh", 24.7136, 46.6753),
    "EG": ("Cairo", 30.0444, 31.2357),
    "PK": ("Karachi", 24.8607, 67.0011),
    "BD": ("Dhaka", 23.8103, 90.4125),
    "VN": ("Ho Chi Minh City", 10.8231, 106.6297),
    "ID": ("Jakarta", -6.2088, 106.8456),
    "KR": ("Seoul", 37.5665, 126.9780),
    "JP": ("Tokyo", 35.6762, 139.6503),
    "IT": ("Milan", 45.4642, 9.1900),
    "FR": ("Paris", 48.8566, 2.3522),
    "DE": ("Frankfurt", 50.1109, 8.6821),
    "ES": ("Madrid", 40.4168, -3.7038),
    "GB": ("London", 51.5074, -0.1278),
    "US": ("New York", 40.7128, -74.0060),
    "JO": ("Amman", 31.9539, 35.9106),
    "KW": ("Kuwait City", 29.3759, 47.9774),
    "BH": ("Manama", 26.2285, 50.5860),
    "QA": ("Doha", 25.2854, 51.5310),
    "OM": ("Muscat", 23.5880, 58.3829),
}

EARTH_RADIUS_KM = 6371.0


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle kilometres between two points.

    Straight-line, which no ship or lorry travels. It is used only as the input
    to a per-mode transit assumption calibrated against real lane times, so the
    detour a route actually takes is absorbed there rather than pretended away
    here.
    """
    lat1, lon1 = radians(a[0]), radians(a[1])
    lat2, lon2 = radians(b[0]), radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(h))


# Timezones that name exactly one sourcing country. Used only to rescue a
# supplier recorded before there was a country field: Asia/Shanghai is China and
# nothing else, so inferring it is reading the data rather than guessing at it.
# Deliberately partial — Europe/Istanbul is Turkey, but Asia/Dubai covers the
# UAE and Oman alike, and an ambiguous zone is left unresolved instead of being
# assigned a plausible country nobody chose.
TIMEZONE_COUNTRY: dict[str, str] = {
    "Asia/Shanghai": "CN",
    "Asia/Kolkata": "IN",
    "Europe/Istanbul": "TR",
    "Asia/Riyadh": "SA",
    "Asia/Karachi": "PK",
    "Asia/Dhaka": "BD",
    "Asia/Ho_Chi_Minh": "VN",
    "Asia/Jakarta": "ID",
    "Asia/Seoul": "KR",
    "Asia/Tokyo": "JP",
    "Europe/London": "GB",
    "Europe/Paris": "FR",
    "Europe/Berlin": "DE",
    "Europe/Rome": "IT",
    "Europe/Madrid": "ES",
    "Africa/Cairo": "EG",
    "Asia/Amman": "JO",
    "Asia/Kuwait": "KW",
    "Asia/Bahrain": "BH",
    "Asia/Qatar": "QA",
    "Asia/Muscat": "OM",
}


def country_of(supplier) -> str | None:
    """The supplier's country, falling back to what their timezone implies."""
    stored = (getattr(supplier, "country", None) or "").strip().upper()
    if stored:
        return stored
    return TIMEZONE_COUNTRY.get(getattr(supplier, "timezone", "") or "")


def origin(country: str | None) -> tuple[str, float, float] | None:
    if not country:
        return None
    return ORIGINS.get(country.strip().upper())


def distance_km(country: str | None) -> float | None:
    place = origin(country)
    if place is None:
        return None
    return round(haversine(HOME, (place[1], place[2])), 0)
