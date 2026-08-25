from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.config.loader import start_config_watcher
from gateway.db import close_pool, init_pool
from gateway.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    start_config_watcher()
    yield
    await close_pool()


app = FastAPI(lifespan=lifespan)
app.include_router(router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
