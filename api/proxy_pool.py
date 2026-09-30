from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.support import require_admin
from services.account_service import account_service
from services.proxy_pool_service import proxy_pool


class PoolSettings(BaseModel):
    enabled: bool
    check_interval_seconds: int = Field(default=300, ge=60, le=3600)


class ProxyNode(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str | None = None
    enabled: bool = True
    max_concurrency: int = Field(default=2, ge=1, le=20)


def create_router():
    router = APIRouter(prefix="/api/proxy-pool")

    def view():
        # Allocate existing accounts on explicit enable and account listing; never when disabled.
        if proxy_pool.state["enabled"]:
            proxy_pool.annotate(account_service.list_accounts())
        return proxy_pool.public()

    async def mutate(function, *args, **kwargs):
        try:
            await run_in_threadpool(function, *args, **kwargs)
        except ValueError as exc:
            raise HTTPException(400, detail={"error": str(exc)}) from exc
        return await run_in_threadpool(view)

    @router.get("")
    async def get_pool(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(view)

    @router.post("/settings")
    async def settings(body: PoolSettings, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await mutate(proxy_pool.configure, **body.model_dump())

    @router.post("/nodes")
    async def add(body: ProxyNode, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await mutate(proxy_pool.save_node, **body.model_dump())

    @router.put("/nodes/{node_id}")
    async def update(node_id: str, body: ProxyNode, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await mutate(proxy_pool.save_node, node_id=node_id, **body.model_dump())

    @router.delete("/nodes/{node_id}")
    async def delete(node_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await mutate(proxy_pool.delete_node, node_id)

    @router.post("/nodes/{node_id}/check")
    async def check(node_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await mutate(proxy_pool.check, node_id)

    return router
