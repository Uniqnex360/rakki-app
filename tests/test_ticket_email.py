

from __future__ import annotations

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
from app.movie.dependencies import get_email_service
from app.movie.email_service import ConsoleEmailService
from app.movie.models import Cinema, Movie, Screen, ScreenRow, Seat, Showtime


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _seed_data(session):
    cinema = Cinema(
        id=uuid.uuid4(), name="RAKKI Lulu Mall", city="Kochi", timezone="Asia/Kolkata"
    )
    screen = Screen(id=uuid.uuid4(), cinema_id=cinema.id, name="Screen 1")
    movie = Movie(
        id=uuid.uuid4(),
        title="I am Game",
        duration_min=162,
        language="Malayalam",
        certificate="UA",
        release_year=2025,
    )
    row = ScreenRow(
        id=uuid.uuid4(), screen_id=screen.id, label="A", seat_count=2, price_cents=19_000
    )
    seat1 = Seat(id=uuid.uuid4(), row_id=row.id, number=1, code="A01")
    seat2 = Seat(id=uuid.uuid4(), row_id=row.id, number=2, code="A02")

    tz = ZoneInfo("Asia/Kolkata")
    st = Showtime(
        id=uuid.uuid4(),
        screen_id=screen.id,
        movie_id=movie.id,
        starts_at=datetime.now(tz).astimezone(ZoneInfo("UTC")),
    )
    session.add_all([cinema, screen, movie, row, seat1, seat2, st])
    await session.commit()
    return {"showtime": st, "seats": [seat1, seat2]}


def _token_for(user_id: uuid.UUID) -> str:
    from app.auth.repository import UserRepository
    svc = AuthService(UserRepository(None), jwt_secret=settings.JWT_SECRET)
    return svc._create_token(user_id)


# ---------------------------------------------------------------------------
# T19 — Email logged with working ticket link + public GET /v1/tickets/{ref}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_t19_email_sent_and_public_ticket_link(session, session_factory):
    test_email_service = ConsoleEmailService(web_base_url="http://rakki.demo")

    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    app.dependency_overrides[get_email_service] = lambda: test_email_service

    data = await _seed_data(session)
    user = User(id=uuid.uuid4(), email="ticket_fan@rakki.local", password_hash="h")
    session.add(user)
    await session.commit()

    token = _token_for(user.id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. Book seat
        resp = await client.post(
            "/v1/bookings",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][0].id)],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
        booking = resp.json()
        ref_code = booking["ref_code"]

        # 2. Assert email was recorded
        assert len(test_email_service.sent_emails) == 1
        sent = test_email_service.sent_emails[0]
        assert sent["to"] == "ticket_fan@rakki.local"
        assert sent["ref_code"] == ref_code
        assert f"?ref={ref_code}" in sent["ticket_url"]

        # 3. Access public ticket route without token
        ticket_resp = await client.get(f"/v1/tickets/{ref_code}")
        assert ticket_resp.status_code == 200
        ticket_body = ticket_resp.json()
        assert ticket_body["ref_code"] == ref_code
        assert ticket_body["movie_title"] == "I am Game"
        assert ticket_body["seats"][0]["code"] == "A01"
        assert ticket_body["status"] == "CONFIRMED"

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# T20 — GET /v1/tickets/{nonexistent} -> 404 domain error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_t20_ticket_not_found(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/tickets/NON-EXISTENT-REF")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    app.dependency_overrides.clear()