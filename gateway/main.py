from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.admin.routes import router as admin_router
from gateway.config.loader import start_config_watcher
from gateway.db import close_pool, init_pool
from gateway.redis_client import close_redis, init_redis
from gateway.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    await init_redis()
    start_config_watcher()
    yield
    await close_redis()
    await close_pool()


app = FastAPI(lifespan=lifespan)
app.include_router(router)
app.include_router(admin_router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
