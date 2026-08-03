#!/usr/bin/env python3
"""Run the live Lieju publication flow for the Zhaixing logistics AI article.

The default mode only reads the current category form and validates that its
required fields still match the payload. Pass --publish to send exactly one
request through the project's LiejuPublisher implementation.

The Lieju account and the Tencent captcha settings are loaded from the normal
project database and environment. Supply real public contact values through
the command line or the LIEJU_ZHAIXING_* environment variables.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from sqlalchemy import text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.credentials import get_credentials
from app.db import SessionLocal
from app.domain import MediumAccount
from app.api.platform.lieju.article_publisher import LiejuPublisher
from app.schemas import ContentType


CATEGORY = "商务服务/网站/软件服务"
CATEGORY_VALUE = "95/111"
TITLE = "摘星货蚁物流AI助手推荐"
CONTENT = """摘星货蚁物流AI助手面向快运网点、专线、第三方物流和大件物流企业，提供物流业务场景下的智能化经营支持。

产品围绕客户咨询、报价、调度和财务等日常环节组织能力，帮助团队集中处理业务信息，提升协同效率。

适用场景包括：
1. 快运网点与区域直营网点
2. 专线物流公司
3. 第三方物流企业
4. 大件物流服务团队

可通过线上服务方式了解产品功能与适用场景。"""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="send the live publish request")
    parser.add_argument("--account-id", type=int, default=3)
    parser.add_argument("--region", default="830", help="Lieju zone_id; 830 is Other")
    parser.add_argument("--service-type", default="5", help="Lieju leibie; 5 is Software development")
    parser.add_argument(
        "--address",
        default=os.environ.get("LIEJU_ZHAIXING_ADDRESS", "线上服务（全国）"),
        help="public service address",
    )
    parser.add_argument(
        "--phone",
        default=os.environ.get("LIEJU_ZHAIXING_PHONE"),
        help="public contact phone number",
    )
    parser.add_argument(
        "--contact",
        default=os.environ.get("LIEJU_ZHAIXING_CONTACT"),
        help="public contact name",
    )
    return parser.parse_args(argv)


async def load_account(account_id: int) -> MediumAccount:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, platform, status, sync_status, `del`, cookie, cookies, session_id
                    FROM tb_medium_account
                    WHERE id = :account_id
                    """
                ),
                {"account_id": account_id},
            )
        ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"Lieju account {account_id} was not found")
    account = MediumAccount(
        id=row["id"],
        platform=row["platform"],
        status=row["status"],
        sync_status=row["sync_status"],
        deleted=row["del"],
        cookie=row["cookie"],
        cookies=row["cookies"],
        session_id=row["session_id"],
    )
    if account.platform != "lieju" or account.status != 1 or account.deleted != 0:
        raise RuntimeError(f"account {account_id} is not an active Lieju account")
    return account


def platform_fields(args: argparse.Namespace) -> dict[str, str]:
    values = {
        "zone_id": args.region,
        "leibie": args.service_type,
        "dizhi": args.address.strip(),
        "mobphone": (args.phone or "").strip(),
        "linkman": (args.contact or "").strip(),
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ValueError(f"missing required platform fields: {', '.join(missing)}")
    return values


async def run(args: argparse.Namespace) -> dict[str, object]:
    account = await load_account(args.account_id)
    credentials = get_credentials(account)
    publisher = LiejuPublisher()
    fields = platform_fields(args)
    requirements = await publisher.get_requirements(
        category=CATEGORY,
        category_value=CATEGORY_VALUE,
        credentials=credentials,
    )
    summary: dict[str, object] = {
        "mode": "publish" if args.publish else "validate",
        "account_id": account.id,
        "category": CATEGORY,
        "category_value": CATEGORY_VALUE,
        "title": TITLE,
        "captcha_required": requirements.captcha_required,
        "platform_fields": sorted(fields),
    }
    if not args.publish:
        summary["publishable"] = requirements.publishable
        summary["required_fields"] = [field.key for field in requirements.fields if field.required]
        return summary

    result = await publisher.publish_article(
        title=TITLE,
        category=CATEGORY,
        category_value=CATEGORY_VALUE,
        content=CONTENT,
        content_type=ContentType.MARKDOWN,
        credentials=credentials,
        platform_fields=fields,
    )
    summary.update(
        success=result.success,
        http_status=result.http_status,
        article_url=result.article_url,
        message=result.message,
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(run(args))
    except Exception as error:
        print(json.dumps({"success": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
