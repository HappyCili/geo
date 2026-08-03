from app.api.base import get_publish_service
from app.api.site.cnblogs.publish import router as cnblogs_router
from app.api.site.hepan.publish import router as hepan_router
from app.api.site.lieju.publish import router as lieju_router
from fastapi import APIRouter

router = APIRouter()
router.include_router(cnblogs_router)
router.include_router(hepan_router)
router.include_router(lieju_router)

__all__ = ["get_publish_service", "router"]
