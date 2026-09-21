# RAKKI Demo

Single-screen cinema booking app. One theatre, one screen, one movie,
a real seat map, atomic booking with 409 on conflict.

Built to be **mergeable** into the Vybh platform — same layering, same
conventions, same module name (`movie/`).

## Quick start

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
python scripts/seed.py
uvicorn app.main:app --reload# PVR_Backend
docker run --env-file .env -p 8000:8000 rakki-backend
