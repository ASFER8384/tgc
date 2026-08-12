"""Weather at the origin, as a warning and never as a number.

Open-Meteo, because it needs no key, no account and no attribution beyond
crediting them — which matters for something that must keep working on a
laptop, in CI and on a free deployment tier without anybody managing a secret.

The deliberate limitation: this returns a sentence, not days. Converting "45mm
of rain in Guangzhou on Thursday" into "your silk is four days late" requires a
model fitted to delay data this business does not have and cannot cheaply buy.
Such a number would look precise and be invented. A buyer told there is a storm
at the origin can ring the supplier, which is the action the information
actually supports.

Failure is silent to the estimate and explicit to the reader: no forecast means
no warning shown, never a claim that the weather is fine.
"""

import httpx

from sca.analytics.geo import origin

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# What is worth interrupting a buyer for. Ordinary weather is not news; these
# are the conditions that stop a lorry loading or shut a port.
HEAVY_RAIN_MM = 40.0
HIGH_WIND_KMH = 60.0


async def advisory(country: str | None, *, timeout: float = 4.0) -> str | None:
    """One sentence about the next week at the origin, or nothing.

    Nothing is returned both when the weather is unremarkable and when the
    service could not be reached, because in neither case is there anything
    truthful to tell a buyer. The distinction matters to an operator and not to
    the person reading a purchase order.
    """
    place = origin(country)
    if place is None:
        return None
    name, lat, lon = place

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                FORECAST_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "precipitation_sum,wind_speed_10m_max",
                    "forecast_days": 7,
                    "timezone": "UTC",
                },
            )
            response.raise_for_status()
            daily = response.json().get("daily") or {}
    except (httpx.HTTPError, ValueError, KeyError):
        return None

    days = daily.get("time") or []
    rain = daily.get("precipitation_sum") or []
    wind = daily.get("wind_speed_10m_max") or []

    notes: list[str] = []
    for index, day in enumerate(days):
        fall = rain[index] if index < len(rain) else None
        gust = wind[index] if index < len(wind) else None
        if fall is not None and fall >= HEAVY_RAIN_MM:
            notes.append(f"{fall:.0f}mm of rain on {day}")
        elif gust is not None and gust >= HIGH_WIND_KMH:
            notes.append(f"{gust:.0f}km/h winds on {day}")

    if not notes:
        return None
    return (
        f"Heavy weather forecast at {name}: {notes[0]}"
        + (f" and {len(notes) - 1} other day(s)" if len(notes) > 1 else "")
        + ". Loading and local transport may be disrupted — the estimate above "
        "does not account for it."
    )
