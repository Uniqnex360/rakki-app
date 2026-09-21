from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.auth.models import User
from app.auth.services import AuthService
from app.core.config import settings
from app.core.database import get_session
from app.main import app
from app.movie.models import (
    Booking,
    BookingSeat,
    Cinema,
    Hold,
    HoldSeat,
    Movie,
    Screen,
    ScreenRow,
    Seat,
    Showtime,
)
from app.movie.repository import MovieRepository


def _token_for(user_id: uuid.UUID) -> str:
    from app.auth.repository import UserRepository

    svc = AuthService(UserRepository(None), jwt_secret=settings.JWT_SECRET)
    return svc._create_token(user_id)


async def _seed_test_hold_data(session):
    cinema = Cinema(
        id=uuid.uuid4(), name="RAKKI Test", city="Kochi", timezone="Asia/Kolkata"
    )
    screen = Screen(id=uuid.uuid4(), cinema_id=cinema.id, name="Screen 1")
    movie = Movie(
        id=uuid.uuid4(),
        title="Inception",
        duration_min=148,
        language="English",
        certificate="UA",
        release_year=2010,
    )
    row = ScreenRow(
        id=uuid.uuid4(),
        screen_id=screen.id,
        label="A",
        seat_count=5,
        price_cents=20_000,
    )
    seats = [
        Seat(id=uuid.uuid4(), row_id=row.id, number=i, code=f"A{i:02d}")
        for i in range(1, 6)
    ]

    tz = ZoneInfo("Asia/Kolkata")
    st = Showtime(
        id=uuid.uuid4(),
        screen_id=screen.id,
        movie_id=movie.id,
        starts_at=datetime.now(tz).astimezone(ZoneInfo("UTC")),
    )
    user = User(
        id=uuid.uuid4(),
        email=f"partner_{uuid.uuid4().hex[:6]}@rakki.local",
        password_hash="pwd",
    )
    session.add_all([cinema, screen, movie, row, *seats, st, user])
    await session.commit()
    return {"showtime": st, "seats": seats, "user": user, "row": row}


# ---------------------------------------------------------------------------
# A1: Hold 2 seats -> 201 and both seats show unavailable on that showtime's map immediately
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a1_hold_seats_unavailable_immediately(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [
                    str(data["seats"][0].id),
                    str(data["seats"][1].id),
                ],
                "idempotency_key": "k-a1",
                "end_user_ref": "user-a1",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
        hold_data = resp.json()
        assert hold_data["total"] == 40_000
        assert len(hold_data["seats"]) == 2

        map_resp = await client.get(
            f"/v1/showtimes/{data['showtime'].id}/seats"
        )
        assert map_resp.status_code == 200
        seats = map_resp.json()["rows"][0]["seats"]
        status_by_id = {s["id"]: s["status"] for s in seats}
        assert status_by_id[str(data["seats"][0].id)] == "BOOKED"
        assert status_by_id[str(data["seats"][1].id)] == "BOOKED"
        assert status_by_id[str(data["seats"][2].id)] == "AVAILABLE"

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# A2: Same seat held by a second end_user_ref -> 409 SEAT_UNAVAILABLE naming exactly that seat
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a2_same_seat_second_user_conflict(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][0].id)],
                "idempotency_key": "k-1",
                "end_user_ref": "cust-1",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        resp2 = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [
                    str(data["seats"][0].id),
                    str(data["seats"][2].id),
                ],
                "idempotency_key": "k-2",
                "end_user_ref": "cust-2",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 409
        err = resp2.json()["detail"]
        assert err["code"] == "SEAT_UNAVAILABLE"
        assert str(data["seats"][0].id) in err["seats"]

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# A3: Same partner+end_user_ref+key twice -> SAME hold_id, COUNT(holds)==1
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a3_idempotency_same_hold(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        payload = {
            "showtime_id": str(data["showtime"].id),
            "seat_ids": [str(data["seats"][0].id)],
            "idempotency_key": "same-key",
            "end_user_ref": "cust-same",
        }
        r1 = await client.post(
            "/v1/holds", json=payload, headers={"Authorization": f"Bearer {token}"}
        )
        r2 = await client.post(
            "/v1/holds", json=payload, headers={"Authorization": f"Bearer {token}"}
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["hold_id"] == r2.json()["hold_id"]

    holds = (await session.execute(select(Hold))).scalars().all()
    assert len(holds) == 1

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# A4: Two users, same key, different end_user_ref -> two independent holds, no collision
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a4_scoped_idempotency_per_customer(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1 = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][0].id)],
                "idempotency_key": "common-key",
                "end_user_ref": "cust-A",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        r2 = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][1].id)],
                "idempotency_key": "common-key",
                "end_user_ref": "cust-B",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["hold_id"] != r2.json()["hold_id"]

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# A5: Commit after expiry -> 410, seats free again, no booking row created
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a5_commit_after_expiry(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][0].id)],
                "idempotency_key": "k-exp",
                "end_user_ref": "cust-exp",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        hold_id = r.json()["hold_id"]

        past = datetime.now(timezone.utc) - timedelta(minutes=10)
        await session.execute(
            update(Hold).where(Hold.id == uuid.UUID(hold_id)).values(expires_at=past)
        )
        await session.commit()

        commit_resp = await client.post(
            f"/v1/holds/{hold_id}/commit",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert commit_resp.status_code == 410
        assert commit_resp.json()["detail"]["code"] == "HOLD_EXPIRED"

        map_resp = await client.get(
            f"/v1/showtimes/{data['showtime'].id}/seats"
        )
        seat_status = map_resp.json()["rows"][0]["seats"][0]["status"]
        assert seat_status == "AVAILABLE"

        bookings = (await session.execute(select(Booking))).scalars().all()
        assert len(bookings) == 0

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# A6: Commit twice -> one booking, second call returns the same booking id
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a6_commit_twice_idempotent(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][0].id)],
                "idempotency_key": "k-comm",
                "end_user_ref": "cust-comm",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        hold_id = r.json()["hold_id"]

        c1 = await client.post(
            f"/v1/holds/{hold_id}/commit",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert c1.status_code == 200
        booking_id = c1.json()["id"]

        c2 = await client.post(
            f"/v1/holds/{hold_id}/commit",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert c2.status_code == 409
        err = c2.json()["detail"]
        assert err["code"] == "HOLD_ALREADY_COMMITTED"
        assert err["booking"]["id"] == booking_id

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# A7: 20 concurrent holds on ONE seat from 20 distinct end_user_refs -> exactly 1 success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a7_concurrent_holds_one_seat(session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    async with session_factory() as s:
        data = await _seed_test_hold_data(s)
    token = _token_for(data["user"].id)

    target_seat_id = str(data["seats"][0].id)
    showtime_id = str(data["showtime"].id)

    async def _place_hold(i: int):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                "/v1/holds",
                json={
                    "showtime_id": showtime_id,
                    "seat_ids": [target_seat_id],
                    "idempotency_key": f"key-race-{i}",
                    "end_user_ref": f"cust-race-{i}",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

    responses = await asyncio.gather(*[_place_hold(i) for i in range(20)])
    status_codes = [r.status_code for r in responses]
    assert status_codes.count(201) == 1
    assert status_codes.count(409) == 19

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# A8: Hold then DELETE -> seat shows available in the very next read, without running the sweep
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a8_hold_then_delete_frees_seat(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][0].id)],
                "idempotency_key": "k-del",
                "end_user_ref": "cust-del",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        hold_id = r.json()["hold_id"]

        del_resp = await client.delete(
            f"/v1/holds/{hold_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert del_resp.status_code == 204

        del_resp2 = await client.delete(
            f"/v1/holds/{hold_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert del_resp2.status_code == 204

        map_resp = await client.get(
            f"/v1/showtimes/{data['showtime'].id}/seats"
        )
        seat_status = map_resp.json()["rows"][0]["seats"][0]["status"]
        assert seat_status == "AVAILABLE"

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# A9: Hold over an already-booked seat -> 409, and that seat stays in its state
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a9_hold_over_booked_seat(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    b = Booking(
        id=uuid.uuid4(),
        user_id=data["user"].id,
        showtime_id=data["showtime"].id,
        ref_code="REF-BOOKED",
        status="CONFIRMED",
    )
    session.add(b)
    await session.flush()
    bs = BookingSeat(
        booking_id=b.id,
        seat_id=data["seats"][0].id,
        showtime_id=data["showtime"].id,
        price_cents=20_000,
    )
    session.add(bs)
    await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][0].id)],
                "idempotency_key": "k-booked",
                "end_user_ref": "cust-booked",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "SEAT_UNAVAILABLE"

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# A10: release_expired() twice concurrently -> no error, second call affects 0
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a10_release_expired_concurrent(session, session_factory):
    data = await _seed_test_hold_data(session)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    hold = Hold(
        id=uuid.uuid4(),
        showtime_id=data["showtime"].id,
        partner_id=data["user"].id,
        end_user_ref="exp-sweep",
        status="ACTIVE",
        idempotency_key="sweep-key",
        quote_total=20_000,
        currency="INR",
        expires_at=past,
    )
    session.add(hold)
    await session.commit()

    hs = HoldSeat(
        hold_id=hold.id,
        seat_id=data["seats"][0].id,
        showtime_id=data["showtime"].id,
        price_cents=20_000,
    )
    session.add(hs)
    await session.commit()

    repo1 = MovieRepository(session)
    async with session_factory() as session2:
        repo2 = MovieRepository(session2)

        res1, res2 = await asyncio.gather(
            repo1.release_expired_holds(),
            repo2.release_expired_holds(),
        )

        total_swept = len(res1) + len(res2)
        assert total_swept == 1


# ---------------------------------------------------------------------------
# A11: Row price changed between hold and commit -> ticket still charged quote_total
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a11_price_freeze_at_hold(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][0].id)],
                "idempotency_key": "k-freeze",
                "end_user_ref": "cust-freeze",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        hold_id = r.json()["hold_id"]
        assert r.json()["total"] == 20_000

        data["row"].price_cents = 99_999
        session.add(data["row"])
        await session.commit()

        commit_resp = await client.post(
            f"/v1/holds/{hold_id}/commit",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert commit_resp.status_code == 200
        booking = commit_resp.json()
        assert booking["total_price_cents"] == 20_000
        assert booking["seats"][0]["price_cents"] == 20_000

    app.dependency_overrides.clear()
# ---------------------------------------------------------------------------
# Phase D - Defects & Regressions (D1 - D7)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_d1_hold_release_rehold_success(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        payload = {
            "showtime_id": str(data["showtime"].id),
            "seat_ids": [str(data["seats"][1].id)],
            "idempotency_key": "k-d1-1",
            "end_user_ref": "cust-d1",
        }
        # First hold
        r1 = await client.post("/v1/holds", json=payload, headers={"Authorization": f"Bearer {token}"})
        assert r1.status_code == 201
        hold_id = r1.json()["hold_id"]

        # Release
        del_resp = await client.delete(f"/v1/holds/{hold_id}", headers={"Authorization": f"Bearer {token}"})
        assert del_resp.status_code == 204

        # Re-hold immediately -> must SUCCEED
        payload["idempotency_key"] = "k-d1-2"
        r2 = await client.post("/v1/holds", json=payload, headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 201

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_d2_hold_expire_sweep_rehold_success(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        payload = {
            "showtime_id": str(data["showtime"].id),
            "seat_ids": [str(data["seats"][1].id)],
            "idempotency_key": "k-d2-1",
            "end_user_ref": "cust-d2",
        }
        r1 = await client.post("/v1/holds", json=payload, headers={"Authorization": f"Bearer {token}"})
        assert r1.status_code == 201
        hold_id = r1.json()["hold_id"]

        # Force expire
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        await session.execute(
            update(Hold).where(Hold.id == uuid.UUID(hold_id)).values(expires_at=past)
        )
        await session.commit()

        # Run Sweep
        repo = MovieRepository(session)
        expired_ids = await repo.release_expired_holds()
        assert len(expired_ids) == 1

        # Re-hold successfully
        payload["idempotency_key"] = "k-d2-2"
        r2 = await client.post("/v1/holds", json=payload, headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 201

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_d3_hold_commit_retains_block_in_booking(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][1].id)],
                "idempotency_key": "k-d3-1",
                "end_user_ref": "cust-d3-1",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        hold_id = r1.json()["hold_id"]

        # Commit
        c = await client.post(f"/v1/holds/{hold_id}/commit", headers={"Authorization": f"Bearer {token}"})
        assert c.status_code == 200

        # Try to hold it again - must fail 409
        r2 = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][1].id)],
                "idempotency_key": "k-d3-2",
                "end_user_ref": "cust-d3-2",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 409

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_d4_commit_then_rehold_b7_conflict(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][1].id)],
                "idempotency_key": "k-d4-1",
                "end_user_ref": "cust-d4-1",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        hold_id = r1.json()["hold_id"]

        # Commit
        await client.post(f"/v1/holds/{hold_id}/commit", headers={"Authorization": f"Bearer {token}"})

        # Re-hold
        r2 = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][1].id)],
                "idempotency_key": "k-d4-2",
                "end_user_ref": "cust-d4-2",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 409

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_d5_loser_retries_after_expiration_succeeds(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Winner holds seat
        r1 = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][1].id)],
                "idempotency_key": "winner-key",
                "end_user_ref": "winner",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        hold_id = r1.json()["hold_id"]

        # Loser attempts hold -> fails
        r2 = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][1].id)],
                "idempotency_key": "loser-key",
                "end_user_ref": "loser",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 409

        # Force expire winner
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        await session.execute(
            update(Hold).where(Hold.id == uuid.UUID(hold_id)).values(expires_at=past)
        )
        await session.commit()

        # Run sweep
        repo = MovieRepository(session)
        await repo.release_expired_holds()

        # Loser retries -> succeeds
        r3 = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][1].id)],
                "idempotency_key": "loser-key",
                "end_user_ref": "loser",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r3.status_code == 201

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_d6_release_twice_idempotent_no_orphan(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.post(
            "/v1/holds",
            json={
                "showtime_id": str(data["showtime"].id),
                "seat_ids": [str(data["seats"][1].id)],
                "idempotency_key": "k-d6",
                "end_user_ref": "cust-d6",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        hold_id = r1.json()["hold_id"]

        # First release
        d1 = await client.delete(f"/v1/holds/{hold_id}", headers={"Authorization": f"Bearer {token}"})
        assert d1.status_code == 204

        # Second release
        d2 = await client.delete(f"/v1/holds/{hold_id}", headers={"Authorization": f"Bearer {token}"})
        assert d2.status_code == 204

        from sqlalchemy import func
        cnt = (await session.execute(
            select(func.count(HoldSeat.hold_id)).where(HoldSeat.hold_id == uuid.UUID(hold_id))
        )).scalar()
        assert cnt == 0

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_d7_expired_and_released_have_zero_hold_seats(session, session_factory):
    async def _get_test_session():
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_test_session
    data = await _seed_test_hold_data(session)
    token = _token_for(data["user"].id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create a hold to release
        r1 = await client.post(
            "/v1/holds",
            json={"showtime_id": str(data["showtime"].id), "seat_ids": [str(data["seats"][1].id)], "idempotency_key": "k-d7-1", "end_user_ref": "c-d7-1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        h1 = r1.json()["hold_id"]
        await client.delete(f"/v1/holds/{h1}", headers={"Authorization": f"Bearer {token}"})

        # Create a hold to expire
        r2 = await client.post(
            "/v1/holds",
            json={"showtime_id": str(data["showtime"].id), "seat_ids": [str(data["seats"][2].id)], "idempotency_key": "k-d7-2", "end_user_ref": "c-d7-2"},
            headers={"Authorization": f"Bearer {token}"},
        )
        h2 = r2.json()["hold_id"]
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        await session.execute(update(Hold).where(Hold.id == uuid.UUID(h2)).values(expires_at=past))
        await session.commit()
        
        repo = MovieRepository(session)
        await repo.release_expired_holds()

        # Query all non-ACTIVE holds and verify they have exactly zero entries in hold_seats
        non_active_hold_ids_subq = select(Hold.id).where(Hold.status != "ACTIVE").scalar_subquery()
        from sqlalchemy import func
        orphan_count = (await session.execute(
            select(func.count(HoldSeat.hold_id)).where(HoldSeat.hold_id.in_(non_active_hold_ids_subq))
        )).scalar()
        assert orphan_count == 0

    app.dependency_overrides.clear()