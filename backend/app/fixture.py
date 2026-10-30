"""A new ASGI fixture per agent; this runner has no network transport."""
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse


def create_fixture(version: str) -> FastAPI:
    app = FastAPI()
    orders = {
        "101": {"id": "101", "owner": "A", "item": "notebook", "total": 12},
        "202": {"id": "202", "owner": "B", "item": "keyboard", "total": 35},
    }

    @app.get("/orders/{order_id}")
    async def order(order_id: str, authorization: str | None = Header(default=None)):
        account = {"Bearer fixture-a": "A", "Bearer fixture-b": "B"}.get(authorization)
        if account is None:
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        data = orders.get(order_id)
        if data is None:
            return JSONResponse({"detail": "not found"}, status_code=404)
        if version == "fixed" and data["owner"] != account:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return data.copy()

    return app
