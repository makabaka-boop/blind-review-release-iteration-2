from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from .db import init_pool, init_schema_and_seed
from .routers import admin, auth, reviewer


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    init_schema_and_seed()
    yield


app = FastAPI(title="Peer Review System", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(reviewer.router)


@app.middleware("http")
async def no_store_api(request, call_next):
    # Author identities must never survive in a shared browser/proxy cache.
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    # Log full details server-side, but never leak them (they may contain
    # authors / SQL) to the client.
    import logging

    logging.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


@app.get("/api/health")
def health():
    return {"status": "ok"}


static_dir = os.environ.get("STATIC_DIR", "/app/static")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
