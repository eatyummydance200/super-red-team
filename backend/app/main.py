import os
import ipaddress
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from .contracts import ExecutionRequest
from .manager import Manager
from .providers import registry
from .report import report
from .store import Store
from .targets import load_targets
from .catalog import scenarios as scenario_catalog, execution_payload


def create_app(data_dir=None, provider=None, evaluator=None, call_timeout=None):
    call_timeout = call_timeout if call_timeout is not None else float(os.getenv("MODEL_CALL_TIMEOUT", "15"))
    if not 0 < call_timeout <= 120:
        raise ValueError("MODEL_CALL_TIMEOUT must be between 0 and 120 seconds")
    token = os.getenv("BACKEND_API_TOKEN", "")
    origins = [v.strip() for v in os.getenv("CORS_ALLOWED_ORIGINS", "https://super-red-team-frontend.vercel.app,http://localhost:5173,http://127.0.0.1:5173").split(",") if v.strip()]
    if "*" in origins:
        raise ValueError("Explicit CORS origins are required")
    if os.getenv("REQUIRE_API_AUTH", "false").lower() == "true" and not token:
        raise ValueError("BACKEND_API_TOKEN is required")

    async def authorize(request: Request):
        if request.url.path == "/health":
            return
        if token:
            if not secrets.compare_digest(request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()):
                raise HTTPException(401, "Backend authentication required", headers={"WWW-Authenticate": "Bearer"})
        else:
            try:
                local = ipaddress.ip_address(request.client.host).is_loopback
            except (ValueError, AttributeError):
                local = False
            if not local:
                raise HTTPException(403, "Configure BACKEND_API_TOKEN for remote access")
    @asynccontextmanager
    async def lifespan(app):
        store = Store(Path(data_dir or os.getenv("DATA_DIR", "data")))
        models = registry()
        kwargs = {"evaluator": evaluator} if evaluator else {}
        manager = Manager(store, models, provider=provider, call_timeout=call_timeout, targets=load_targets(), **kwargs)
        app.state.manager, app.state.store, app.state.models = manager, store, models
        manager.recover()
        try:
            yield
        finally:
            await manager.close()
            store.close()

    app = FastAPI(title="Adaptive Red Team Orchestrator", version="0.2.0", lifespan=lifespan, dependencies=[Depends(authorize)])
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
                       allow_headers=["Authorization", "Content-Type"], allow_credentials=False)

    def get(execution_id):
        execution = app.state.store.get(execution_id)
        if execution is None:
            raise HTTPException(404, "Execution not found")
        return execution

    @app.get("/health")
    async def health():
        return {"status": "ok", "single_process": True, "max_concurrent_executions": 1}

    @app.get("/models")
    async def models():
        return [{"id": s.id, "mode": s.mode,
                 "name": s.name or s.id, "provider": s.provider,
                 "input_per_mtok": s.input_rate, "output_per_mtok": s.output_rate,
                 "configured": s.mode == "mock" or bool(os.getenv(s.key_env)),
                 "cost_cap_supported": s.reservation_cost(1, 1) is not None}
                for s in app.state.models.values()]

    @app.get("/scenarios")
    async def scenarios():
        return scenario_catalog(app.state.manager.targets)

    @app.post("/executions", status_code=202)
    async def create(config: ExecutionRequest):
        try:
            execution = app.state.manager.create(config)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error
        return {"execution_id": execution.id, "status": execution.status, "is_mock": config.mode == "mock"}

    @app.get("/executions")
    async def executions(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        items = app.state.store.list(limit + 1, offset)
        return {"executions": [execution_payload(e) for e in items[:limit]],
                "next_offset": offset + limit if len(items) > limit else None}

    @app.get("/executions/{execution_id}")
    async def execution(execution_id: str):
        return execution_payload(get(execution_id))

    @app.get("/executions/{execution_id}/logs")
    async def logs(execution_id: str, after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
        events = get(execution_id).events
        items = [event for event in events if event["sequence"] > after][:limit]
        items = [{"message": event["type"].replace("_", " "), "level": "info", **event} for event in items]
        return {"events": items, "next_after": items[-1]["sequence"] if items else after,
                "is_mock": get(execution_id).config.mode == "mock"}

    @app.post("/executions/{execution_id}/cancel")
    async def cancel(execution_id: str):
        get(execution_id)
        return execution_payload(await app.state.manager.cancel(execution_id))

    @app.get("/executions/{execution_id}/report")
    async def execution_report(execution_id: str):
        return report(get(execution_id))

    return app


app = create_app()
