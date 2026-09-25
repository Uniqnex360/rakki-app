"""
SQLAlchemy repository for Movie, Showtime, Booking, and Holds write operations.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.movie.interfaces import (
    BookingAlreadyCancelledError,
    BookingDTO,
    BookingNotFoundError,
    BookingOwnershipError,
    BookingSeatDTO,
    HoldAlreadyCommittedError,
    HoldDTO,
    HoldExpiredError,
    HoldNotFoundError,
    HoldSeatDTO,
    HoldStatus,
    InvalidSeatSelectionError,
    RowProjectionDTO,
    SeatAlreadyBookedError,
    SeatMapDTO,
    SeatProjectionDTO,
    SeatStatus,
    SeatUnavailableError,
    ShowtimeNotFoundError,
    ShowtimeSummaryDTO,
)
from app.movie.models import (
    Booking,
    BookingSeat,
    BookingStatus,
    Cinema,
    Hold,
    HoldSeat,
    Movie,
    Screen,
    ScreenRow,
    Seat,
    Showtime,
)

logger = logging.getLogger(__name__)


class MovieRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_showtimes_by_date(
        self, target_date: date
    ) -> list[ShowtimeSummaryDTO]:
        stmt = (
            select(Showtime, Screen, Cinema, Movie)
            .join(Screen, Showtime.screen_id == Screen.id)
            .join(Cinema, Screen.cinema_id == Cinema.id)
            .join(Movie, Showtime.movie_id == Movie.id)
            .order_by(Showtime.starts_at.asc())
        )
        result = await self._session.execute(stmt)
        rows = result.all()

        now = datetime.now(timezone.utc)
        showtimes: list[ShowtimeSummaryDTO] = []
        for st, screen, cinema, movie in rows:
            cinema_tz = ZoneInfo(cinema.timezone)
            st_date_local = st.starts_at.astimezone(cinema_tz).date()

            if st.starts_at > now:
                showtimes.append(
                    ShowtimeSummaryDTO(
                        id=st.id,
                        screen_name=screen.name,
                        cinema_name=cinema.name,
                        movie_title=movie.title,
                        language=movie.language,
                        certificate=movie.certificate,
                        duration_min=movie.duration_min,
                        starts_at=st.starts_at,
                        poster_url=movie.poster_url,
                        city=cinema.city,
                        genre=movie.genre, 
                    )
                )

        return showtimes

    async def get_seat_map(self, showtime_id: UUID) -> SeatMapDTO | None:
        st_stmt = (
            select(Showtime, Screen, Cinema, Movie)
            .join(Screen, Showtime.screen_id == Screen.id)
            .join(Cinema, Screen.cinema_id == Cinema.id)
            .join(Movie, Showtime.movie_id == Movie.id)
            .where(Showtime.id == showtime_id)
        )
        st_res = await self._session.execute(st_stmt)
        st_row = st_res.first()
        if not st_row:
            return None

        showtime, screen, cinema, movie = st_row

        row_stmt = (
            select(ScreenRow)
            .where(ScreenRow.screen_id == screen.id)
            .order_by(ScreenRow.label.asc())
        )
        rows_res = await self._session.execute(row_stmt)
        screen_rows = rows_res.scalars().all()

        row_ids = [r.id for r in screen_rows]
        seat_stmt = (
            select(Seat)
            .where(Seat.row_id.in_(row_ids))
            .order_by(Seat.number.asc())
        )
        seats_res = await self._session.execute(seat_stmt)
        all_seats = seats_res.scalars().all()

        seats_by_row: dict[UUID, list[Seat]] = {r.id: [] for r in screen_rows}
        for s in all_seats:
            if s.row_id in seats_by_row:
                seats_by_row[s.row_id].append(s)

        # 1. Booked seats
        booked_stmt = (
            select(BookingSeat.seat_id)
            .join(Booking, BookingSeat.booking_id == Booking.id)
            .where(
                BookingSeat.showtime_id == showtime_id,
                Booking.status != BookingStatus.CANCELLED.value,
            )
        )
        booked_res = await self._session.execute(booked_stmt)
        booked_seat_ids = set(booked_res.scalars().all())

        # 2. Dynamically active held seats (expires_at > now)
        now_utc = datetime.now(timezone.utc)
        held_stmt = (
            select(HoldSeat.seat_id)
            .join(Hold, HoldSeat.hold_id == Hold.id)
            .where(
                HoldSeat.showtime_id == showtime_id,
                Hold.status == HoldStatus.ACTIVE.value,
                Hold.expires_at > now_utc,
            )
        )
        held_res = await self._session.execute(held_stmt)
        held_seat_ids = set(held_res.scalars().all())

        unavailable_ids = booked_seat_ids | held_seat_ids

        row_dtos: list[RowProjectionDTO] = []
        for r in screen_rows:
            seat_dtos: list[SeatProjectionDTO] = []
            for s in seats_by_row.get(r.id, []):
                status = (
                    SeatStatus.BOOKED
                    if s.id in unavailable_ids
                    else SeatStatus.AVAILABLE
                )
                seat_dtos.append(
                    SeatProjectionDTO(
                        id=s.id,
                        row_label=r.label,
                        number=s.number,
                        code=s.code,
                        price_cents=r.price_cents,
                        status=status,
                    )
                )

            row_dtos.append(
                RowProjectionDTO(
                    label=r.label,
                    price_cents=r.price_cents,
                    seats=seat_dtos,
                )
            )

        return SeatMapDTO(
            showtime_id=showtime.id,
            movie_title=movie.title,
            screen_name=screen.name,
            cinema_name=cinema.name,
            starts_at=showtime.starts_at,
            rows=row_dtos,
        )

    # -----------------------------------------------------------------------
    # Booking Lookups & Write Operations
    # -----------------------------------------------------------------------

    def _build_booking_dto(
        self,
        booking: Booking,
        showtime: Showtime,
        screen: Screen,
        cinema: Cinema,
        movie: Movie,
        seats_data: list[tuple[BookingSeat, Seat]],
    ) -> BookingDTO:
        seat_dtos = [
            BookingSeatDTO(
                seat_id=s.id,
                code=s.code,
                price_cents=bs.price_cents,
            )
            for bs, s in seats_data
        ]
        total_price = sum(s.price_cents for s in seat_dtos)

        return BookingDTO(
            id=booking.id,
            user_id=booking.user_id,
            showtime_id=showtime.id,
            movie_title=movie.title,
            screen_name=screen.name,
            cinema_name=cinema.name,
            starts_at=showtime.starts_at,
            ref_code=booking.ref_code,
            status=booking.status,
            created_at=booking.created_at,
            seats=seat_dtos,
            total_price_cents=total_price,
            barcode=f"BARCODE-{booking.ref_code}",
        )

    async def get_booking_by_id(self, booking_id: UUID) -> BookingDTO | None:
        stmt = (
            select(Booking, Showtime, Screen, Cinema, Movie)
            .join(Showtime, Booking.showtime_id == Showtime.id)
            .join(Screen, Showtime.screen_id == Screen.id)
            .join(Cinema, Screen.cinema_id == Cinema.id)
            .join(Movie, Showtime.movie_id == Movie.id)
            .where(Booking.id == booking_id)
        )
        res = await self._session.execute(stmt)
        row = res.first()
        if not row:
            return None

        booking, showtime, screen, cinema, movie = row

        bs_stmt = (
            select(BookingSeat, Seat)
            .join(Seat, BookingSeat.seat_id == Seat.id)
            .where(BookingSeat.booking_id == booking.id)
        )
        bs_res = await self._session.execute(bs_stmt)
        seats_data = bs_res.all()

        return self._build_booking_dto(
            booking, showtime, screen, cinema, movie, seats_data
        )

    async def get_booking_by_ref(self, ref_code: str) -> BookingDTO | None:
        stmt = (
            select(Booking, Showtime, Screen, Cinema, Movie)
            .join(Showtime, Booking.showtime_id == Showtime.id)
            .join(Screen, Showtime.screen_id == Screen.id)
            .join(Cinema, Screen.cinema_id == Cinema.id)
            .join(Movie, Showtime.movie_id == Movie.id)
            .where(Booking.ref_code == ref_code.strip().upper())
        )
        res = await self._session.execute(stmt)
        row = res.first()
        if not row:
            return None

        booking, showtime, screen, cinema, movie = row

        bs_stmt = (
            select(BookingSeat, Seat)
            .join(Seat, BookingSeat.seat_id == Seat.id)
            .where(BookingSeat.booking_id == booking.id)
        )
        bs_res = await self._session.execute(bs_stmt)
        seats_data = bs_res.all()

        return self._build_booking_dto(
            booking, showtime, screen, cinema, movie, seats_data
        )

    async def get_booking_by_idempotency(
        self, user_id: UUID, idempotency_key: str
    ) -> BookingDTO | None:
        stmt = select(Booking.id).where(
            Booking.user_id == user_id,
            Booking.idempotency_key == idempotency_key,
        )
        res = await self._session.execute(stmt)
        b_id = res.scalar_one_or_none()
        if not b_id:
            return None
        return await self.get_booking_by_id(b_id)

    async def create_booking(
        self,
        *,
        user_id: UUID,
        showtime_id: UUID,
        seat_ids: list[UUID],
        idempotency_key: str | None = None,
    ) -> BookingDTO:
        if idempotency_key:
            existing = await self.get_booking_by_idempotency(
                user_id, idempotency_key
            )
            if existing:
                return existing

        st_stmt = (
            select(Showtime, Screen, Cinema, Movie)
            .join(Screen, Showtime.screen_id == Screen.id)
            .join(Cinema, Screen.cinema_id == Cinema.id)
            .join(Movie, Showtime.movie_id == Movie.id)
            .where(Showtime.id == showtime_id)
        )
        st_res = await self._session.execute(st_stmt)
        st_row = st_res.first()
        if not st_row:
            raise ShowtimeNotFoundError(
                f"Showtime '{showtime_id}' does not exist"
            )
        showtime, screen, cinema, movie = st_row

        seat_stmt = (
            select(Seat, ScreenRow)
            .join(ScreenRow, Seat.row_id == ScreenRow.id)
            .where(
                Seat.id.in_(seat_ids),
                ScreenRow.screen_id == screen.id,
            )
        )
        seat_res = await self._session.execute(seat_stmt)
        seats_with_rows = seat_res.all()

        if len(seats_with_rows) != len(set(seat_ids)):
            raise InvalidSeatSelectionError(
                "One or more selected seats do not exist on this screen"
            )

        ref_code = f"AGS-{uuid.uuid4().hex[:8].upper()}"
        booking = Booking(
            id=uuid.uuid4(),
            user_id=user_id,
            showtime_id=showtime_id,
            ref_code=ref_code,
            status=BookingStatus.CONFIRMED.value,
            idempotency_key=idempotency_key,
        )

        booking_seats_list: list[BookingSeat] = []
        try:
            self._session.add(booking)
            await self._session.flush()

            for seat, row in seats_with_rows:
                bs = BookingSeat(
                    booking_id=booking.id,
                    seat_id=seat.id,
                    showtime_id=showtime_id,
                    price_cents=row.price_cents,
                )
                self._session.add(bs)
                booking_seats_list.append(bs)

            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if idempotency_key:
                existing = await self.get_booking_by_idempotency(
                    user_id, idempotency_key
                )
                if existing:
                    return existing

            raise SeatAlreadyBookedError(
                "One or more selected seats are already booked"
            ) from exc

        seats_data = [
            (bs, seat)
            for bs, (seat, _) in zip(booking_seats_list, seats_with_rows)
        ]
        return self._build_booking_dto(
            booking, showtime, screen, cinema, movie, seats_data
        )

    async def get_user_bookings(self, user_id: UUID) -> list[BookingDTO]:
        stmt = (
            select(Booking.id)
            .where(Booking.user_id == user_id)
            .order_by(Booking.created_at.desc())
        )
        res = await self._session.execute(stmt)
        booking_ids = res.scalars().all()

        results: list[BookingDTO] = []
        for b_id in booking_ids:
            b_dto = await self.get_booking_by_id(b_id)
            if b_dto:
                results.append(b_dto)
        return results

    async def cancel_booking(
        self, *, user_id: UUID, booking_id: UUID
    ) -> BookingDTO:
        stmt = select(Booking).where(Booking.id == booking_id)
        res = await self._session.execute(stmt)
        booking = res.scalar_one_or_none()

        if not booking:
            raise BookingNotFoundError(f"Booking '{booking_id}' not found")

        if booking.user_id != user_id:
            raise BookingOwnershipError("Not authorized to cancel this booking")

        # Reject cancel if the showtime has already started
        if booking.showtime_id:
            from datetime import datetime, timezone
            st = (await self._session.execute(
                select(Showtime).where(Showtime.id == booking.showtime_id)
            )).scalar_one_or_none()
            if st and st.starts_at <= datetime.now(timezone.utc):
                raise BookingAlreadyCancelledError(
                    "Cannot cancel a booking for a showtime that has already started"
                )

        if booking.status == BookingStatus.CANCELLED.value:
            raise BookingAlreadyCancelledError("Booking is already cancelled")

        booking.status = BookingStatus.CANCELLED.value

        del_stmt = delete(BookingSeat).where(
            BookingSeat.booking_id == booking.id
        )
        await self._session.execute(del_stmt)
        await self._session.commit()

        b_dto = await self.get_booking_by_id(booking.id)
        if not b_dto:
            raise BookingNotFoundError("Booking not found after cancellation")
        return b_dto

    # -----------------------------------------------------------------------
    # Holds Operations
    # -----------------------------------------------------------------------

    def _build_hold_dto(
        self,
        hold: Hold,
        seats_data: list[tuple[HoldSeat, Seat]],
    ) -> HoldDTO:
        seat_dtos = [
            HoldSeatDTO(
                seat_id=s.id,
                code=s.code,
                price_cents=hs.price_cents,
            )
            for hs, s in seats_data
        ]

        return HoldDTO(
            id=hold.id,
            showtime_id=hold.showtime_id,
            partner_id=hold.partner_id,
            end_user_ref=hold.end_user_ref,
            status=hold.status,
            idempotency_key=hold.idempotency_key,
            quote_total=hold.quote_total,
            currency=hold.currency,
            expires_at=hold.expires_at,
            created_at=hold.created_at,
            updated_at=hold.updated_at,
            seats=seat_dtos,
        )

    async def get_hold_by_id(self, hold_id: UUID) -> HoldDTO | None:
        stmt = select(Hold).where(Hold.id == hold_id)
        res = await self._session.execute(stmt)
        hold = res.scalar_one_or_none()
        if not hold:
            return None

        hs_stmt = (
            select(HoldSeat, Seat)
            .join(Seat, HoldSeat.seat_id == Seat.id)
            .where(HoldSeat.hold_id == hold.id)
        )
        hs_res = await self._session.execute(hs_stmt)
        seats_data = hs_res.all()

        return self._build_hold_dto(hold, seats_data)

    async def get_hold_by_idempotency(
        self, partner_id: UUID, end_user_ref: str | None, idempotency_key: str
    ) -> HoldDTO | None:
        stmt = select(Hold.id).where(
            Hold.partner_id == partner_id,
            Hold.end_user_ref == end_user_ref,
            Hold.idempotency_key == idempotency_key,
        )
        res = await self._session.execute(stmt)
        h_id = res.scalar_one_or_none()
        if not h_id:
            return None
        return await self.get_hold_by_id(h_id)

    async def close_hold(self, hold_id: UUID, outcome: str) -> None:
        """
        Unified method to end a hold.
        Purges rows from hold_seats keyed on hold_id in a single idempotent statement,
        freeing the unique index constraint ux_hold_showtime_seat immediately.
        """
        await self._session.execute(
            delete(HoldSeat).where(HoldSeat.hold_id == hold_id)
        )

    async def create_hold(
        self,
        *,
        partner_id: UUID,
        showtime_id: UUID,
        seat_ids: list[UUID],
        idempotency_key: str,
        end_user_ref: str | None = None,
        ttl_seconds: int = 600,
    ) -> HoldDTO:
        existing = await self.get_hold_by_idempotency(
            partner_id, end_user_ref, idempotency_key
        )
        if existing:
            return existing

        st_stmt = (
            select(Showtime, Screen)
            .join(Screen, Showtime.screen_id == Screen.id)
            .where(Showtime.id == showtime_id)
        )
        st_res = await self._session.execute(st_stmt)
        st_row = st_res.first()
        if not st_row:
            raise ShowtimeNotFoundError(
                f"Showtime '{showtime_id}' does not exist"
            )
        showtime, screen = st_row

        seat_stmt = (
            select(Seat, ScreenRow)
            .join(ScreenRow, Seat.row_id == ScreenRow.id)
            .where(
                Seat.id.in_(seat_ids),
                ScreenRow.screen_id == screen.id,
            )
            .with_for_update()
        )
        seat_res = await self._session.execute(seat_stmt)
        seats_with_rows = seat_res.all()

        unique_seat_ids = list(dict.fromkeys(seat_ids))
        if len(seats_with_rows) != len(unique_seat_ids) or len(unique_seat_ids) != len(seat_ids):
            raise InvalidSeatSelectionError(
                "One or more selected seats do not exist on this screen or contain duplicates"
            )

        now_utc = datetime.now(timezone.utc)
        expires_at = now_utc + timedelta(seconds=ttl_seconds)

        # 1. Check if any seat is already booked
        booked_stmt = (
            select(BookingSeat.seat_id)
            .join(Booking, BookingSeat.booking_id == Booking.id)
            .where(
                BookingSeat.showtime_id == showtime_id,
                BookingSeat.seat_id.in_(seat_ids),
                Booking.status != BookingStatus.CANCELLED.value,
            )
        )
        booked_res = await self._session.execute(booked_stmt)
        booked_seats = set(booked_res.scalars().all())

        held_stmt = (
            select(HoldSeat.seat_id)
            .join(Hold, HoldSeat.hold_id == Hold.id)
            .where(
                HoldSeat.showtime_id == showtime_id,
                HoldSeat.seat_id.in_(seat_ids),
                Hold.status == HoldStatus.ACTIVE.value,
                Hold.expires_at > now_utc,
            )
        )
        held_res = await self._session.execute(held_stmt)
        held_seats = set(held_res.scalars().all())

        conflict_seats = list(booked_seats | held_seats)
        if conflict_seats:
            raise SeatUnavailableError(conflict_seats)

        quote_total = sum(row.price_cents for _, row in seats_with_rows)
        hold = Hold(
            id=uuid.uuid4(),
            showtime_id=showtime_id,
            partner_id=partner_id,
            end_user_ref=end_user_ref,
            status=HoldStatus.ACTIVE.value,
            idempotency_key=idempotency_key,
            quote_total=quote_total,
            currency="INR",
            expires_at=expires_at,
            created_at=now_utc,
            updated_at=now_utc,
        )

        hold_seats_list: list[HoldSeat] = []
        try:
            self._session.add(hold)
            await self._session.flush()

            for seat, row in seats_with_rows:
                hs = HoldSeat(
                    hold_id=hold.id,
                    seat_id=seat.id,
                    showtime_id=showtime_id,
                    price_cents=row.price_cents,
                )
                self._session.add(hs)
                hold_seats_list.append(hs)

            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            existing = await self.get_hold_by_idempotency(
                partner_id, end_user_ref, idempotency_key
            )
            if existing:
                return existing
            raise SeatUnavailableError(seat_ids)

        seats_data = [
            (hs, seat)
            for hs, (seat, _) in zip(hold_seats_list, seats_with_rows)
        ]
        return self._build_hold_dto(hold, seats_data)

    async def commit_hold(
        self, *, hold_id: UUID, payment_ref: str | None = None
    ) -> BookingDTO:
        stmt = select(Hold).where(Hold.id == hold_id)
        res = await self._session.execute(stmt)
        hold = res.scalar_one_or_none()

        if not hold:
            raise HoldNotFoundError(f"Hold '{hold_id}' not found")

        # If already committed, return the existing booking
        if hold.status == HoldStatus.COMMITTED.value:
            b_stmt = select(Booking).where(
                Booking.showtime_id == hold.showtime_id,
                Booking.idempotency_key == f"hold:{hold.id}",
            )
            b_res = await self._session.execute(b_stmt)
            booking = b_res.scalar_one_or_none()
            if booking:
                b_dto = await self.get_booking_by_id(booking.id)
                if b_dto:
                    raise HoldAlreadyCommittedError(b_dto)

        now_utc = datetime.now(timezone.utc)
        hold_exp = (
            hold.expires_at
            if hold.expires_at.tzinfo
            else hold.expires_at.replace(tzinfo=timezone.utc)
        )
        if hold.status != HoldStatus.ACTIVE.value or hold_exp <= now_utc:
            if hold.status == HoldStatus.ACTIVE.value:
                hold.status = HoldStatus.EXPIRED.value
                hold.updated_at = now_utc
                await self.close_hold(hold.id, "EXPIRED")
                await self._session.commit()
            raise HoldExpiredError("Hold is expired")

        # Fetch held seats snapshot
        hs_stmt = (
            select(HoldSeat, Seat)
            .join(Seat, HoldSeat.seat_id == Seat.id)
            .where(HoldSeat.hold_id == hold.id)
        )
        hs_res = await self._session.execute(hs_stmt)
        held_seats = hs_res.all()

        if not held_seats:
            raise HoldExpiredError("Hold has no seats associated")

        ref_code = f"AGS-{uuid.uuid4().hex[:8].upper()}"
        booking = Booking(
            id=uuid.uuid4(),
            user_id=hold.partner_id,
            showtime_id=hold.showtime_id,
            ref_code=ref_code,
            status=BookingStatus.CONFIRMED.value,
            idempotency_key=f"hold:{hold.id}",
        )

        try:
            self._session.add(booking)
            await self._session.flush()

            # Insert BookingSeats using snapshot price from hold_seats
            for hs, _ in held_seats:
                bs = BookingSeat(
                    booking_id=booking.id,
                    seat_id=hs.seat_id,
                    showtime_id=hold.showtime_id,
                    price_cents=hs.price_cents,
                )
                self._session.add(bs)

            hold.status = HoldStatus.COMMITTED.value
            hold.updated_at = now_utc

            # Remove hold_seats now that seats are committed to booking_seats
            await self.close_hold(hold.id, "COMMITTED")

            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            b_stmt = select(Booking).where(
                Booking.showtime_id == hold.showtime_id,
                Booking.idempotency_key == f"hold:{hold.id}",
            )
            b_res = await self._session.execute(b_stmt)
            booking = b_res.scalar_one_or_none()
            if booking:
                b_dto = await self.get_booking_by_id(booking.id)
                if b_dto:
                    raise HoldAlreadyCommittedError(b_dto)
            raise SeatAlreadyBookedError("Failed to commit hold into booking") from exc

        booking_dto = await self.get_booking_by_id(booking.id)
        if not booking_dto:
            raise BookingNotFoundError("Failed to retrieve created booking")
        return booking_dto

    async def release_hold(self, hold_id: UUID) -> None:
        stmt = select(Hold).where(Hold.id == hold_id)
        res = await self._session.execute(stmt)
        hold = res.scalar_one_or_none()
        if not hold:
            return  # Idempotent 204

        if hold.status == HoldStatus.ACTIVE.value:
            hold.status = HoldStatus.RELEASED.value
            hold.updated_at = datetime.now(timezone.utc)
            await self.close_hold(hold.id, "RELEASED")
            await self._session.commit()

    async def release_expired_holds(self) -> list[UUID]:
        now_utc = datetime.now(timezone.utc)
        
        # Single atomic update-then-return statement:
        stmt = (
            update(Hold)
            .where(
                Hold.status == HoldStatus.ACTIVE.value,
                Hold.expires_at < now_utc,
            )
            .values(status=HoldStatus.EXPIRED.value, updated_at=now_utc)
            .returning(Hold.id)
        )
        res = await self._session.execute(stmt)
        expired_ids = list(res.scalars().all())

        if expired_ids:
            await self._session.execute(
                delete(HoldSeat).where(HoldSeat.hold_id.in_(expired_ids))
            )
            await self._session.commit()

        return expired_ids