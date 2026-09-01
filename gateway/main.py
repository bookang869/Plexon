from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from prometheus_client import make_asgi_app

from gateway.admin.routes import router as admin_router
from gateway.config.loader import get_config, start_config_watcher
from gateway.db import close_pool, init_pool
from gateway.observability import tracing
from gateway.redis_client import close_redis, init_redis
from gateway.resilience.health_check import start_health_check_loop
from gateway.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    tracing.configure_tracing()
    await init_pool()
    await init_redis()
    start_config_watcher()
    start_health_check_loop(get_config())
    yield
    await close_redis()
    await close_pool()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def _root_span_middleware(request: Request, call_next):
    # Request-scoped root span (TRD §8's first named stage) -- opened here,
    # before FastAPI resolves dependencies like get_current_team, so it's
    # already current by the time the auth/rate_limit_check/... child spans
    # open further down the pipeline (gateway/auth/team_auth.py, gateway/routes.py).
    with tracing.get_tracer().start_as_current_span("request.receipt"):
        return await call_next(request)


app.include_router(router)
app.include_router(admin_router)
app.mount("/metrics", make_asgi_app())


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
