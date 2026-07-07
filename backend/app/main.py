import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import init_db
from .routers import auth, campaigns, internal, sessions
from .services import search

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    search.create_index()
    search.rebuild_if_empty()
    yield


app = FastAPI(title="Tablecast", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(campaigns.router)
app.include_router(sessions.router)
app.include_router(internal.router)

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.get("/healthz")
def healthz():
    return {"ok": True}
