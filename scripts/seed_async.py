import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import asyncio, uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
import app.auth.models, app.movie.models
from app.auth.models import User
from app.movie.models import Cinema, Screen, ScreenRow, Seat, Movie, Showtime
from app.core.config import settings

pwd = CryptContext(schemes=['bcrypt'], deprecated='auto')
CINEMAS = [
    {'name': 'Rakki Ambattur', 'screens': 2},
    {'name': 'Rakki OMR',      'screens': 2},
]
CITY = 'Chennai'
ROWS = [('A',20,19000),('B',20,19000),('C',20,19000),('D',22,29000),('E',22,29000),('F',22,29000),('G',26,39000),('H',26,39000),('I',28,39000),('J',28,39000)]
MOVIES = [
    {'title':'I Am Game','duration_min':162,'language':'Malayalam','certificate':'UA','release_year':2025,'genre':'Action, Thriller','poster_url':'https://i.pinimg.com/1200x/c7/a8/58/c7a858e124a8da21b34624689fae49b2.jpg','times':[time(15,30),time(19,0)]},
    {'title':'The Final Whistle','duration_min':120,'language':'English','certificate':'UA','release_year':2025,'genre':'Drama, Sport','poster_url':'https://i.pinimg.com/736x/b2/a3/18/b2a31878a8498a21aa582d78094f775c.jpg','times':[time(14,0),time(17,30)]},
    {'title':'Kingdom','duration_min':148,'language':'Malayalam','certificate':'UA','release_year':2025,'genre':'Action, Drama','poster_url':'https://i.pinimg.com/736x/33/43/3d/33433d63732e0e1222ae3534bf6495f9.jpg','times':[time(10,30),time(14,30)]},
    {'title':'Avengers: Endgame Encore','duration_min':182,'language':'English','certificate':'UA','release_year':2025,'genre':'Action, Sci-Fi, Adventure','poster_url':'https://i.pinimg.com/736x/93/54/5f/93545f7e45707c04baf5a972efbcbc02.jpg','times':[time(10,0),time(14,0)]},
]
AVAIL = ['I Am Game','The Final Whistle','Kingdom','Avengers: Endgame Encore']

async def main():
    engine = create_async_engine(settings.DATABASE_URL)
    S = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with S() as s:
        print('user', flush=True)
        u = (await s.execute(select(User).where(User.email=='demo@rakki.local'))).scalar_one_or_none()
        if not u:
            u = User(id=uuid.uuid4(), email='demo@rakki.local', password_hash=pwd.hash('demo1234'), role='PARTNER')
            s.add(u); await s.flush()
        print('movies', flush=True)
        m_by_t = {}
        for spec in MOVIES:
            m = (await s.execute(select(Movie).where(Movie.title==spec['title']))).scalar_one_or_none()
            if not m:
                m = Movie(id=uuid.uuid4(), title=spec['title'], duration_min=spec['duration_min'], language=spec['language'], certificate=spec['certificate'], release_year=spec['release_year'], genre=spec['genre'], poster_url=spec['poster_url'])
                s.add(m); await s.flush()
            m_by_t[spec['title']] = m
        avail = [mm for mm in MOVIES if mm['title'] in AVAIL]
        tz = ZoneInfo('Asia/Kolkata'); today = datetime.now(tz).date()
        for cspec in CINEMAS:
            print('cinema', cspec['name'], flush=True)
            c = Cinema(id=uuid.uuid4(), name=cspec['name'], city=CITY, timezone='Asia/Kolkata')
            s.add(c); await s.flush()
            for si in range(cspec['screens']):
                scr = Screen(id=uuid.uuid4(), cinema_id=c.id, name=f'Screen {si+1}')
                s.add(scr); await s.flush()
                for lbl, sc, pc in ROWS:
                    row = ScreenRow(id=uuid.uuid4(), screen_id=scr.id, label=lbl, seat_count=sc, price_cents=pc)
                    s.add(row); await s.flush()
                    for n in range(1, sc+1):
                        s.add(Seat(id=uuid.uuid4(), row_id=row.id, number=n, code=f'{lbl}{n:02d}'))
                    await s.flush()
                assigned = [avail[(si+i)%len(avail)] for i in range(min(3,len(avail)))]
                for do in range(8):
                    d = today + timedelta(days=do)
                    for spec in assigned:
                        mo = m_by_t[spec['title']]
                        for t in spec['times'][:2]:
                            dt = datetime.combine(d, t, tzinfo=tz).astimezone(ZoneInfo('UTC'))
                            s.add(Showtime(id=uuid.uuid4(), screen_id=scr.id, movie_id=mo.id, starts_at=dt))
                    await s.flush()
        await s.commit()
        print('SEEDED', flush=True)
    await engine.dispose()

asyncio.run(main())
