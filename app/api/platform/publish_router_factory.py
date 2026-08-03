from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.base import get_publish_service, http_error
from app.errors import PublishError
from app.schemas import (
    PublishArticleRequest,
    PublishArticleResponse,
    PublishFieldOptionResponse,
    PublishFieldRequirementResponse,
    PublishRequirementsResponse,
)
from app.services import PublishService


def create_publish_router(platform: str) -> APIRouter:
    router = APIRouter(prefix=f"/{platform}", tags=[platform])

    @router.post("/articles/publish", response_model=PublishArticleResponse)
    async def publish_article(
        request: PublishArticleRequest,
        service: PublishService = Depends(get_publish_service),
    ) -> PublishArticleResponse:
        try:
            actual_platform, result = await service.publish(request, platform)
        except PublishError as error:
            raise http_error(error) from error
        return PublishArticleResponse(
            account_id=request.account_id,
            platform=actual_platform,
            success=result.success,
            http_status=result.http_status,
            article_url=result.article_url,
            message=result.message,
        )

    @router.get("/articles/publish/requirements", response_model=PublishRequirementsResponse)
    async def get_publish_requirements(
        account_id: int = Query(gt=0),
        category: str = Query(min_length=1, max_length=128),
        service: PublishService = Depends(get_publish_service),
    ) -> PublishRequirementsResponse:
        category = category.strip()
        if not category:
            raise HTTPException(status_code=422, detail={"code": "invalid_request", "message": "category must not be blank"})
        try:
            actual_platform, requirements = await service.get_requirements(
                account_id, category, platform
            )
        except PublishError as error:
            raise http_error(error) from error
        return PublishRequirementsResponse(
            account_id=account_id,
            platform=actual_platform,
            category=category,
            publishable=requirements.publishable,
            captcha_required=requirements.captcha_required,
            fields=[
                PublishFieldRequirementResponse(
                    key=field.key,
                    label=field.label,
                    control_type=field.control_type,
                    required=field.required,
                    multiple=field.multiple,
                    default=field.default,
                    options=[
                        PublishFieldOptionResponse(value=option.value, label=option.label)
                        for option in field.options
                    ],
                    validation=field.validation,
                    validation_message=field.validation_message,
                )
                for field in requirements.fields
            ],
        )

    return router
