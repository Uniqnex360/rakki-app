

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from app.movie.interfaces import (
    BookingDTO,
    HoldDTO,
    IEmailService,
    IMovieRepository,
    InvalidSeatSelectionError,
    SeatMapDTO,
    ShowtimeNotFoundError,
    ShowtimeSummaryDTO,
    TicketNotFoundError,
)
import asyncio

from collections import defaultdict

_seat_hold_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class MovieService:
    def __init__(
        self,
        movie_repo: IMovieRepository,
        email_service: IEmailService | None = None,
    ) -> None:
        self._repo = movie_repo
        self._email_service = email_service

    async def get_showtimes(
        self, target_date: date | None = None
    ) -> list[ShowtimeSummaryDTO]:
        if target_date is None:
            target_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()

        return await self._repo.get_showtimes_by_date(target_date)

    async def get_seat_map(self, showtime_id: UUID) -> SeatMapDTO:
        seat_map = await self._repo.get_seat_map(showtime_id)
        if seat_map is None:
            raise ShowtimeNotFoundError(
                f"Showtime '{showtime_id}' does not exist"
            )
        return seat_map

    async def book_seats(
        self,
        *,
        user_id: UUID,
        user_email: str,
        showtime_id: UUID,
        seat_ids: list[UUID],
        idempotency_key: str | None = None,
    ) -> BookingDTO:
        if not seat_ids:
            raise InvalidSeatSelectionError("Must select at least 1 seat")
        if len(seat_ids) > 10:
            raise InvalidSeatSelectionError("Cannot book more than 10 seats")

        booking = await self._repo.create_booking(
            user_id=user_id,
            showtime_id=showtime_id,
            seat_ids=seat_ids,
            idempotency_key=idempotency_key,
        )

        if self._email_service:
            await self._email_service.send_booking_confirmation(
                to_email=user_email, booking=booking
            )

        return booking

    async def get_my_bookings(self, user_id: UUID) -> list[BookingDTO]:
        return await self._repo.get_user_bookings(user_id)

    async def cancel_booking(
        self, user_id: UUID, booking_id: UUID
    ) -> BookingDTO:
        return await self._repo.cancel_booking(
            user_id=user_id, booking_id=booking_id
        )

    async def get_ticket(self, ref_code: str) -> BookingDTO:
        booking = await self._repo.get_booking_by_ref(ref_code)
        if booking is None:
            raise TicketNotFoundError(f"Ticket '{ref_code}' not found")
        return booking

    # -----------------------------------------------------------------------
    # Holds Service Methods
    # -----------------------------------------------------------------------

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
        if not seat_ids:
            raise InvalidSeatSelectionError("Must select at least 1 seat")
        if len(seat_ids) != len(set(seat_ids)):
            raise InvalidSeatSelectionError("Duplicate seats requested")

        lock = _seat_hold_locks[str(showtime_id)]
        async with lock:
            return await self._repo.create_hold(
                partner_id=partner_id,
                showtime_id=showtime_id,
                seat_ids=seat_ids,
                idempotency_key=idempotency_key,
                end_user_ref=end_user_ref,
                ttl_seconds=ttl_seconds,
            )

    async def get_hold(self, hold_id: UUID) -> HoldDTO:
        hold = await self._repo.get_hold_by_id(hold_id)
        if not hold:
            from app.movie.interfaces import HoldNotFoundError
            raise HoldNotFoundError(f"Hold '{hold_id}' not found")
        return hold

    async def commit_hold(
        self, *, hold_id: UUID, payment_ref: str | None = None
    ) -> BookingDTO:
        return await self._repo.commit_hold(
            hold_id=hold_id, payment_ref=payment_ref
        )

    async def release_hold(self, hold_id: UUID) -> None:
        await self._repo.release_hold(hold_id)

    async def release_expired_holds(self) -> list[UUID]:
        return await self._repo.release_expired_holds()