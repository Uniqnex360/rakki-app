

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth.routes import auth_router
from app.movie.routes import movie_router

app = FastAPI(title="RAKKI Demo API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "*", 
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.head("/v1/health")
@app.get("/v1/health")
async def health():
    return {"status": "ok"}


app.include_router(auth_router, prefix="/v1")
app.include_router(movie_router, prefix="/v1")