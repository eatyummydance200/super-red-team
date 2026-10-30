import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query

from .contracts import ExecutionRequest
from .manager import Manager
from .providers import registry
from .report import report
from .store import Store


def create_app(data_dir=None, provider=None, evaluator=None, call_timeout=15):
    @asynccontextmanager
    async def lifespan(app):
        store = Store(Path(data_dir or os.getenv("DATA_DIR", "data")))
        models = registry()
        kwargs = {"evaluator": evaluator} if evaluator else {}
        manager = Manager(store, models, provider=provider, call_timeout=call_timeout, **kwargs)
        app.state.manager, app.state.store, app.state.models = manager, store, models
        manager.recover()
        try:
            yield
        finally:
            await manager.close()
            store.close()

    app = FastAPI(title="Adaptive Red Team Orchestrator", version="0.1.0", lifespan=lifespan)

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
                 "configured": s.mode == "mock" or bool(os.getenv(s.key_env)),
                 "cost_cap_supported": s.reservation_cost(1, 1) is not None}
                for s in app.state.models.values()]

    @app.get("/scenarios")
    async def scenarios():
        return [{"id": "orders-v1", "environments": ["vulnerable", "fixed"],
                 "information": "gray-box", "tools": ["http"], "state_changes": False,
                 "collaboration": "independent", "accounts": ["A", "B"],
                 "supported_checks": ["own_order_A", "own_order_B", "cross_account", "authentication"],
                 "unsupported": ["external_url", "sequential", "debate", "browser", "code_reading",
                                 "scheduling", "writes", "black-box", "white-box", "plan_approval"]}]

    @app.post("/executions", status_code=202)
    async def create(config: ExecutionRequest):
        try:
            execution = app.state.manager.create(config)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error
        return {"execution_id": execution.id, "status": execution.status}

    @app.get("/executions/{execution_id}")
    async def execution(execution_id: str):
        return get(execution_id)

    @app.get("/executions/{execution_id}/logs")
    async def logs(execution_id: str, after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
        events = get(execution_id).events
        items = [event for event in events if event["sequence"] > after][:limit]
        return {"events": items, "next_after": items[-1]["sequence"] if items else after}

    @app.post("/executions/{execution_id}/cancel")
    async def cancel(execution_id: str):
        get(execution_id)
        return await app.state.manager.cancel(execution_id)

    @app.get("/executions/{execution_id}/report")
    async def execution_report(execution_id: str):
        return report(get(execution_id))

    return app


app = create_app()
