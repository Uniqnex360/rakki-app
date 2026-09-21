

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.models import User
from app.auth.services import AuthService
from app.core.config import settings
from app.core.database import get_session
from app.main import app
from app.movie.models import (
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


async def _seed_cinema_and_showtime(session):
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
    session.add_all([cinema, screen, movie])
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

    tz = ZoneInfo("Asia/Kolkata")
    now_local = datetime.now(tz)
    st = Showtime(
        id=uuid.uuid4(),
        screen_id=screen.id,
        movie_id=movie.id,
        starts_at=now_local.astimezone(ZoneInfo("UTC")),
    )
    session.add(st)
    await session.commit()

    return {
        "cinema": cinema,
        "screen": screen,
        "movie": movie,
        "showtime": st,
        "seats": all_seats,
    }


def _generate_token_for_user(user_id: uuid.UUID) -> str:
    from app.auth.repository import UserRepository
    dummy_repo = UserRepository(None)  
    svc = AuthService(dummy_repo, jwt_secret=settings.JWT_SECRET)
    return svc._create_token(user_id)






@pytest.mark.asyncio
async def test_t15_happy_path_booking(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_cinema_and_showtime(session)

    user = User(
        id=uuid.uuid4(),
        email="customer1@rakki.local",
        password_hash="hash",
    )
    session.add(user)
    await session.commit()

    token = _generate_token_for_user(user.id)
    seats_to_book = [data["seats"][0].id, data["seats"][1].id, data["seats"][2].id]

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/bookings",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(s) for s in seats_to_book],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
        body = resp.json()

        assert body["status"] == "CONFIRMED"
        assert body["ref_code"].startswith("RAKKI-")
        assert len(body["seats"]) == 3
        assert body["total_price_cents"] == 19_000 * 3

        
        me_resp = await client.get(
            "/v1/bookings/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_resp.status_code == 200
        my_bookings = me_resp.json()
        assert len(my_bookings) == 1
        assert my_bookings[0]["id"] == body["id"]

    app.dependency_overrides.clear()






@pytest.mark.asyncio
async def test_t16_concurrency_race_same_seat(session, session_factory):
    
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_cinema_and_showtime(session)

    user_a = User(id=uuid.uuid4(), email="userA@rakki.local", password_hash="h")
    user_b = User(id=uuid.uuid4(), email="userB@rakki.local", password_hash="h")
    user_c = User(id=uuid.uuid4(), email="userC@rakki.local", password_hash="h")
    session.add_all([user_a, user_b, user_c])
    await session.commit()

    token_a = _generate_token_for_user(user_a.id)
    token_b = _generate_token_for_user(user_b.id)
    token_c = _generate_token_for_user(user_c.id)

    target_seat = data["seats"][10].id  
    other_seat = data["seats"][11].id   

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        
        async def book(tok, s_id):
            return await client.post(
                "/v1/bookings",
                json={
                    "showtime_id": str(data["showtime"].id),
                    "seat_ids": [str(s_id)],
                },
                headers={"Authorization": f"Bearer {tok}"},
            )

        resp_a, resp_b = await asyncio.gather(
            book(token_a, target_seat),
            book(token_b, target_seat),
        )

        status_codes = sorted([resp_a.status_code, resp_b.status_code])
        assert status_codes == [201, 409], (
            f"Expected [201, 409], got {status_codes}"
        )

        
        resp_c = await book(token_c, other_seat)
        assert resp_c.status_code == 201

    app.dependency_overrides.clear()






@pytest.mark.asyncio
async def test_t17_idempotency_key(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_cinema_and_showtime(session)

    user = User(id=uuid.uuid4(), email="idem@rakki.local", password_hash="h")
    session.add(user)
    await session.commit()

    token = _generate_token_for_user(user.id)
    seat = data["seats"][20].id

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        payload = {
            "showtime_id": str(data["showtime"].id),
            "seat_ids": [str(seat)],
            "idempotency_key": "pay-click-12345",
        }

        
        resp1 = await client.post(
            "/v1/bookings",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp1.status_code == 201
        data1 = resp1.json()

        
        resp2 = await client.post(
            "/v1/bookings",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 201
        data2 = resp2.json()

        assert data1["id"] == data2["id"]
        assert data1["ref_code"] == data2["ref_code"]

    app.dependency_overrides.clear()






@pytest.mark.asyncio
async def test_t18_cancel_frees_seats(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_cinema_and_showtime(session)

    user1 = User(id=uuid.uuid4(), email="u1@rakki.local", password_hash="h")
    user2 = User(id=uuid.uuid4(), email="u2@rakki.local", password_hash="h")
    session.add_all([user1, user2])
    await session.commit()

    token1 = _generate_token_for_user(user1.id)
    token2 = _generate_token_for_user(user2.id)
    seat = data["seats"][30].id

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        
        resp1 = await client.post(
            "/v1/bookings",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(seat)],
            },
            headers={"Authorization": f"Bearer {token1}"},
        )
        assert resp1.status_code == 201
        booking_id = resp1.json()["id"]

        
        conflict_resp = await client.post(
            "/v1/bookings",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(seat)],
            },
            headers={"Authorization": f"Bearer {token2}"},
        )
        assert conflict_resp.status_code == 409

        
        cancel_resp = await client.post(
            f"/v1/bookings/{booking_id}/cancel",
            headers={"Authorization": f"Bearer {token1}"},
        )
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "CANCELLED"

        
        sm_resp = await client.get(
            f"/v1/showtimes/{data['showtime'].id}/seats"
        )
        seat_status = None
        for row in sm_resp.json()["rows"]:
            for s in row["seats"]:
                if s["id"] == str(seat):
                    seat_status = s["status"]
        assert seat_status == "AVAILABLE"

        
        resp2 = await client.post(
            "/v1/bookings",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(seat)],
            },
            headers={"Authorization": f"Bearer {token2}"},
        )
        assert resp2.status_code == 201
        assert resp2.json()["status"] == "CONFIRMED"

    app.dependency_overrides.clear()