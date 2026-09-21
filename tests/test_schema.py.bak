
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
    User,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent






async def _make_full_schema(session: AsyncSession) -> dict:
    user = User(
        id=uuid.uuid4(),
        email=f"t-{uuid.uuid4().hex[:8]}@test.local",
        password_hash="hash",
    )
    cinema = Cinema(
        id=uuid.uuid4(), name="Test Cinema", city="Testville"
    )
    screen = Screen(
        id=uuid.uuid4(), cinema_id=cinema.id, name="Screen 1"
    )
    row_a = ScreenRow(
        id=uuid.uuid4(),
        screen_id=screen.id,
        label="A",
        seat_count=5,
        price_cents=10_000,
    )
    session.add_all([user, cinema, screen, row_a])
    await session.flush()

    seats: list[Seat] = []
    for n in range(1, 6):
        seats.append(
            Seat(
                id=uuid.uuid4(),
                row_id=row_a.id,
                number=n,
                code=f"A{n:02d}",
            )
        )
    session.add_all(seats)
    await session.flush()

    movie = Movie(
        id=uuid.uuid4(),
        title="Test Movie",
        duration_min=120,
        language="English",
        certificate="U",
        release_year=2025,
    )
    session.add(movie)
    await session.flush()

    now = datetime.now(timezone.utc)
    st1 = Showtime(
        id=uuid.uuid4(),
        screen_id=screen.id,
        movie_id=movie.id,
        starts_at=now,
    )
    st2 = Showtime(
        id=uuid.uuid4(),
        screen_id=screen.id,
        movie_id=movie.id,
        starts_at=now,
    )
    session.add_all([st1, st2])
    await session.flush()

    return {
        "user": user,
        "cinema": cinema,
        "screen": screen,
        "row_a": row_a,
        "seats": seats,
        "movie": movie,
        "st1": st1,
        "st2": st2,
    }






def test_t1_migration_and_seed(tmp_path):
    db_file = tmp_path / "t1.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option(
        "script_location", str(BACKEND_DIR / "alembic")
    )
    command.upgrade(cfg, "head")

    from scripts.seed import seed

    seed(db_url)  

    from sqlalchemy import create_engine

    sync_url = db_url.replace("+aiosqlite", "")
    eng = create_engine(sync_url)
    with eng.connect() as conn:
        assert (
            conn.execute(text("SELECT COUNT(*) FROM seats")).scalar()
            == 468
        )
        assert (
            conn.execute(
                text("SELECT COUNT(*) FROM showtimes")
            ).scalar()
            == 6
        )
    eng.dispose()






@pytest.mark.asyncio
async def test_t2_occupancy_independent(session: AsyncSession):
    d = await _make_full_schema(session)

    
    b1 = Booking(
        id=uuid.uuid4(),
        user_id=d["user"].id,
        showtime_id=d["st1"].id,
        ref_code="REF-T2-A",
        status=BookingStatus.CONFIRMED.value,
    )
    session.add(b1)
    await session.flush()

    for seat in d["seats"][:2]:
        session.add(
            BookingSeat(
                booking_id=b1.id,
                seat_id=seat.id,
                showtime_id=d["st1"].id,
                price_cents=10_000,
            )
        )
    await session.commit()

    
    st2_count = (
        await session.execute(
            select(func.count())
            .select_from(BookingSeat)
            .where(BookingSeat.showtime_id == d["st2"].id)
        )
    ).scalar()
    assert st2_count == 0

    
    b2 = Booking(
        id=uuid.uuid4(),
        user_id=d["user"].id,
        showtime_id=d["st2"].id,
        ref_code="REF-T2-B",
        status=BookingStatus.CONFIRMED.value,
    )
    session.add(b2)
    await session.flush()

    for seat in d["seats"][:2]:
        session.add(
            BookingSeat(
                booking_id=b2.id,
                seat_id=seat.id,
                showtime_id=d["st2"].id,
                price_cents=10_000,
            )
        )
    await session.commit()  






@pytest.mark.asyncio
async def test_t3_duplicate_seat_code(session: AsyncSession):
    d = await _make_full_schema(session)

    dup = Seat(
        id=uuid.uuid4(),
        row_id=d["row_a"].id,
        number=99,
        code="A01",  
    )
    session.add(dup)
    with pytest.raises(IntegrityError):
        await session.commit()






@pytest.mark.asyncio
async def test_t4_ux_showtime_seat(session: AsyncSession):
    d = await _make_full_schema(session)

    b1 = Booking(
        id=uuid.uuid4(),
        user_id=d["user"].id,
        showtime_id=d["st1"].id,
        ref_code="REF-T4-A",
    )
    session.add(b1)
    await session.flush()

    seat = d["seats"][0]
    session.add(
        BookingSeat(
            booking_id=b1.id,
            seat_id=seat.id,
            showtime_id=d["st1"].id,
            price_cents=10_000,
        )
    )
    await session.commit()

    
    b2 = Booking(
        id=uuid.uuid4(),
        user_id=d["user"].id,
        showtime_id=d["st1"].id,
        ref_code="REF-T4-B",
    )
    session.add(b2)
    await session.flush()

    session.add(
        BookingSeat(
            booking_id=b2.id,
            seat_id=seat.id,
            showtime_id=d["st1"].id,
            price_cents=10_000,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()






@pytest.mark.asyncio
async def test_t5_price_snapshot(session: AsyncSession):
    d = await _make_full_schema(session)

    booking = Booking(
        id=uuid.uuid4(),
        user_id=d["user"].id,
        showtime_id=d["st1"].id,
        ref_code="REF-T5",
    )
    session.add(booking)
    await session.flush()

    original_price = 10_000
    session.add(
        BookingSeat(
            booking_id=booking.id,
            seat_id=d["seats"][0].id,
            showtime_id=d["st1"].id,
            price_cents=original_price,
        )
    )
    await session.commit()

    
    d["row_a"].price_cents = 99_999
    await session.commit()

    
    bs = (
        await session.execute(
            select(BookingSeat).where(
                BookingSeat.booking_id == booking.id
            )
        )
    ).scalar_one()
    assert bs.price_cents == original_price






def test_t6_seed_idempotent(tmp_path):
    db_file = tmp_path / "t6.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option(
        "script_location", str(BACKEND_DIR / "alembic")
    )
    command.upgrade(cfg, "head")

    from scripts.seed import seed

    seed(db_url)
    seed(db_url)  

    from sqlalchemy import create_engine

    eng = create_engine(db_url.replace("+aiosqlite", ""))
    with eng.connect() as conn:
        assert (
            conn.execute(text("SELECT COUNT(*) FROM seats")).scalar()
            == 468
        )
        assert (
            conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
            == 1
        )
        assert (
            conn.execute(text("SELECT COUNT(*) FROM cinemas")).scalar()
            == 1
        )
    eng.dispose()






@pytest.mark.asyncio
async def test_t7_timestamps_tz_aware(session: AsyncSession):
    d = await _make_full_schema(session)

    
    user = (
        await session.execute(
            select(User).where(User.id == d["user"].id)
        )
    ).scalar_one()
    assert user.created_at.tzinfo is not None, (
        "users.created_at is naive"
    )

    st = (
        await session.execute(
            select(Showtime).where(Showtime.id == d["st1"].id)
        )
    ).scalar_one()
    assert st.starts_at.tzinfo is not None, (
        "showtimes.starts_at is naive"
    )

    booking = Booking(
        id=uuid.uuid4(),
        user_id=d["user"].id,
        showtime_id=d["st1"].id,
        ref_code="REF-T7",
    )
    session.add(booking)
    await session.commit()

    b = (
        await session.execute(
            select(Booking).where(Booking.id == booking.id)
        )
    ).scalar_one()
    assert b.created_at.tzinfo is not None, (
        "bookings.created_at is naive"
    )






def test_t8_autogenerate_no_diff(tmp_path):
   
    from app.core.database import Base

    db_file = tmp_path / "t8.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option(
        "script_location", str(BACKEND_DIR / "alembic")
    )
    command.upgrade(cfg, "head")

    
    from sqlalchemy import create_engine

    sync_url = db_url.replace("+aiosqlite", "")
    eng = create_engine(sync_url)

    with eng.connect() as conn:
        mc = MigrationContext.configure(
            conn, opts={"target_metadata": Base.metadata}
        )
        diff = compare_metadata(mc, Base.metadata)

    eng.dispose()

    
    real_diff = [
        d
        for d in diff
        if not (
            d[0] in ("add_constraint", "remove_constraint")
            and "CHECK" in str(d).upper()
        )
    ]

    assert not real_diff, (
        "autogenerate found differences — migration and models "
        f"disagree:\n{real_diff}"
    )