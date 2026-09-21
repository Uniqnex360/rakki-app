
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.models import User
from app.core.database import get_session
from app.main import app
from app.movie.models import (
    Booking,
    BookingSeat,
    BookingStatus,
    Cinema,
    Movie,
    Screen,
    ScreenRow,
    Seat,
    Showtime,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _seed_test_data(session):
    user = User(
        id=uuid.uuid4(),
        email="testuser@rakki.local",
        password_hash="hash",
    )
    cinema = Cinema(
        id=uuid.uuid4(),
        name="RAKKI Lulu Mall",
        city="Kochi",
        timezone="Asia/Kolkata",
    )
    screen = Screen(
        id=uuid.uuid4(), cinema_id=cinema.id, name="Screen 1"
    )
    movie = Movie(
        id=uuid.uuid4(),
        title="I am Game",
        duration_min=162,
        language="Malayalam",
        certificate="UA",
        release_year=2025,
    )
    session.add_all([user, cinema, screen, movie])
    await session.flush()

    row_configs = [
        ("A", 20, 19_000),
        ("B", 20, 19_000),
        ("C", 20, 19_000),
        ("D", 22, 29_000),
        ("E", 22, 29_000),
        ("F", 22, 29_000),
        ("G", 26, 39_000),
        ("H", 26, 39_000),
        ("I", 28, 39_000),
        ("J", 28, 39_000),
    ]

    all_seats = []
    for label, count, price in row_configs:
        r = ScreenRow(
            id=uuid.uuid4(),
            screen_id=screen.id,
            label=label,
            seat_count=count,
            price_cents=price,
        )
        session.add(r)
        await session.flush()

        for num in range(1, count + 1):
            s = Seat(
                id=uuid.uuid4(),
                row_id=r.id,
                number=num,
                code=f"{label}{num:02d}",
            )
            session.add(s)
            all_seats.append(s)

    await session.flush()

    # 3 showtimes for today in cinema timezone (IST)
    tz = ZoneInfo("Asia/Kolkata")
    today = datetime.now(tz).date()
    times = [(15, 30), (19, 0), (22, 15)]
    showtimes = []

    for h, m in times:
        dt_local = datetime.combine(today, datetime.min.time()).replace(
            hour=h, minute=m, tzinfo=tz
        )
        st = Showtime(
            id=uuid.uuid4(),
            screen_id=screen.id,
            movie_id=movie.id,
            starts_at=dt_local.astimezone(ZoneInfo("UTC")),
        )
        session.add(st)
        showtimes.append(st)

    await session.commit()
    return {
        "user": user,
        "cinema": cinema,
        "screen": screen,
        "movie": movie,
        "showtimes": showtimes,
        "seats": all_seats,
    }


@pytest.mark.asyncio
async def test_t12_showtimes_today_and_tomorrow(session):
    app.dependency_overrides[get_session] = lambda: session
    data = await _seed_test_data(session)

    tz = ZoneInfo("Asia/Kolkata")
    today_str = datetime.now(tz).date().isoformat()
    tomorrow_str = (datetime.now(tz).date() + timedelta(days=1)).isoformat()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Default (today)
        resp_today_default = await client.get("/v1/showtimes")
        assert resp_today_default.status_code == 200
        slots_today = resp_today_default.json()
        assert len(slots_today) == 3
        assert slots_today[0]["movie_title"] == "I am Game"

        # Explicit date = today
        resp_today = await client.get(f"/v1/showtimes?date={today_str}")
        assert resp_today.status_code == 200
        assert len(resp_today.json()) == 3

        # Tomorrow -> empty
        resp_tomorrow = await client.get(f"/v1/showtimes?date={tomorrow_str}")
        assert resp_tomorrow.status_code == 200
        assert len(resp_tomorrow.json()) == 0

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_t13_seat_map_all_available_initially(session):
  
    app.dependency_overrides[get_session] = lambda: session
    data = await _seed_test_data(session)
    st = data["showtimes"][0]

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/v1/showtimes/{st.id}/seats")
        assert resp.status_code == 200
        seat_map = resp.json()

        assert seat_map["showtime_id"] == str(st.id)
        assert seat_map["movie_title"] == "I am Game"
        assert len(seat_map["rows"]) == 10

        total_seats = 0
        for row in seat_map["rows"]:
            assert row["price_cents"] in (19_000, 29_000, 39_000)
            for seat in row["seats"]:
                total_seats += 1
                assert seat["status"] == "AVAILABLE"
                assert "id" in seat
                assert "code" in seat
                assert seat["price_cents"] == row["price_cents"]

        assert total_seats == 234

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_t14_seat_map_with_bookings_marks_booked(session):
    app.dependency_overrides[get_session] = lambda: session
    data = await _seed_test_data(session)
    st1 = data["showtimes"][0]
    st2 = data["showtimes"][1]
    seats = data["seats"]

    # Book 2 seats in st1: A01 and A02
    booking = Booking(
        id=uuid.uuid4(),
        user_id=data["user"].id,
        showtime_id=st1.id,
        ref_code="REF-T14",
        status=BookingStatus.CONFIRMED.value,
    )
    session.add(booking)
    await session.flush()

    booked_seat_ids = [seats[0].id, seats[1].id]
    for s_id in booked_seat_ids:
        session.add(
            BookingSeat(
                booking_id=booking.id,
                seat_id=s_id,
                showtime_id=st1.id,
                price_cents=19_000,
            )
        )
    await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Check st1: exactly 2 seats are BOOKED
        resp1 = await client.get(f"/v1/showtimes/{st1.id}/seats")
        assert resp1.status_code == 200
        sm1 = resp1.json()

        booked_codes = []
        for row in sm1["rows"]:
            for s in row["seats"]:
                if s["status"] == "BOOKED":
                    booked_codes.append(s["code"])

        assert sorted(booked_codes) == ["A01", "A02"]

        # Check st2: 0 seats are BOOKED (independent occupancy)
        resp2 = await client.get(f"/v1/showtimes/{st2.id}/seats")
        assert resp2.status_code == 200
        sm2 = resp2.json()

        st2_booked_count = sum(
            1
            for r in sm2["rows"]
            for s in r["seats"]
            if s["status"] == "BOOKED"
        )
        assert st2_booked_count == 0

    app.dependency_overrides.clear()