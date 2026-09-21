import logging
from datetime import timezone
from zoneinfo import ZoneInfo
import httpx

from app.core.config import settings
from app.movie.interfaces import BookingDTO, IEmailService

logger = logging.getLogger(__name__)


class ResendEmailService(IEmailService):
    def __init__(
        self,
        api_key: str | None = settings.RESEND_API_KEY,
        from_email: str | None = settings.RESEND_FROM_EMAIL or settings.SMTP_USER,
        from_name: str = settings.SMTP_FROM_NAME,
        frontend_url: str = settings.FRONTEND_URL,
    ) -> None:
        self.api_key = api_key
        sender = from_email if from_email else "tickets@datavioai.com"
        self.from_sender = f"{from_name} <{sender}>"
        self.frontend_url = frontend_url
        self.api_url = "https://api.resend.com/emails"

    def _build_html_email(self, booking: BookingDTO, ticket_url: str) -> str:
        dt = booking.starts_at if booking.starts_at.tzinfo else booking.starts_at.replace(tzinfo=timezone.utc)
        starts_at_ist = dt.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%A, %d %B %Y at %I:%M %p")
        seat_codes = ", ".join(s.code for s in booking.seats)
        amount_formatted = f"₹{booking.total_price_cents / 100:.2f}"

        return f"""\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0a0a0a; color: #f5f5f5; margin: 0; padding: 20px; }}
    .ticket-card {{ max-width: 520px; margin: 0 auto; background: #171717; border-radius: 16px; border: 1px solid #262626; overflow: hidden; }}
    .ticket-header {{ background: linear-gradient(135deg, #f59e0b, #d97706); color: #000; padding: 24px; }}
    .ticket-header h1 {{ margin: 0; font-size: 24px; font-weight: 900; letter-spacing: 1px; }}
    .ticket-ref {{ font-family: monospace; font-size: 14px; font-weight: bold; opacity: 0.9; margin-top: 4px; }}
    .ticket-body {{ padding: 24px; }}
    .movie-title {{ font-size: 22px; font-weight: bold; color: #ffffff; margin: 0 0 4px 0; }}
    .cinema-name {{ color: #a3a3a3; font-size: 14px; margin-bottom: 20px; }}
    .info-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; background: #262626; padding: 16px; border-radius: 12px; margin-bottom: 24px; }}
    .info-label {{ font-size: 11px; text-transform: uppercase; color: #737373; font-weight: bold; }}
    .info-val {{ font-size: 14px; color: #f5f5f5; font-weight: bold; margin-top: 2px; }}
    .btn {{ display: block; text-align: center; background: #f59e0b; color: #000; text-decoration: none; font-weight: bold; padding: 14px; border-radius: 10px; font-size: 14px; }}
    .footer {{ text-align: center; color: #525252; font-size: 12px; margin-top: 20px; }}
  </style>
</head>
<body>
  <div class="ticket-card">
    <div class="ticket-header">
      <h1>RAKKI CINEMAS</h1>
      <div class="ticket-ref">BOOKING CONFIRMED: {booking.ref_code}</div>
    </div>
    <div class="ticket-body">
      <div class="movie-title">{booking.movie_title}</div>
      <div class="cinema-name">{booking.cinema_name} • {booking.screen_name}</div>
      
      <div class="info-grid">
        <div>
          <div class="info-label">Showtime</div>
          <div class="info-val">{starts_at_ist}</div>
        </div>
        <div>
          <div class="info-label">Seats ({len(booking.seats)})</div>
          <div class="info-val" style="color: #f59e0b;">{seat_codes}</div>
        </div>
        <div>
          <div class="info-label">Total Amount</div>
          <div class="info-val">{amount_formatted}</div>
        </div>
        <div>
          <div class="info-label">Status</div>
          <div class="info-val" style="color: #4ade80;">{booking.status}</div>
        </div>
      </div>

      <a href="{ticket_url}" class="btn">View & Download Ticket</a>
    </div>
  </div>
  <div class="footer">
    Please present this digital ticket at the cinema gate. Enjoy your movie!
  </div>
</body>
</html>
"""

    async def send_booking_confirmation(self, to_email: str, booking: BookingDTO) -> None:
        if not self.api_key:
            logger.warning("RESEND_API_KEY missing. Email confirmation skipped.")
            return

        ticket_url = f"{self.frontend_url}/ticket?ref={booking.ref_code}"
        payload = {
            "from": self.from_sender,
            "to": [to_email],
            "subject": f"Your Ticket: {booking.movie_title} ({booking.ref_code})",
            "html": self._build_html_email(booking, ticket_url),
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self.api_url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
                if response.status_code == 200:
                    logger.info("Confirmation email sent via Resend to %s", to_email)
                else:
                    logger.warning(
                        "Resend API returned status %s for %s: %s",
                        response.status_code,
                        to_email,
                        response.text,
                    )
        except Exception as e:
            logger.error("Failed to connect to Resend API: %s", e)


class ConsoleEmailService(IEmailService):
    def __init__(self, web_base_url: str = "http://localhost:5173") -> None:
        self.web_base_url = web_base_url
        self.sent_emails: list[dict] = []

    async def send_booking_confirmation(
        self, to_email: str, booking: BookingDTO
    ) -> None:
        ticket_url = f"{self.web_base_url}/ticket?ref={booking.ref_code}"
        self.sent_emails.append(
            {
                "to": to_email,
                "ref": booking.ref_code,
                "ref_code": booking.ref_code,
                "ticket_url": ticket_url,
            }
        )
        print(f"\n--- [CONSOLE EMAIL] To: {to_email} | Ticket: {ticket_url} ---\n")