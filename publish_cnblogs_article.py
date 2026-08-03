#!/usr/bin/env python3
"""Publish a CNBlogs article using values configured in this file.

Edit COOKIE, title, markdown, and SEND_REQUEST, then run:
  python3 publish_cnblogs_article.py
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from urllib.parse import unquote

import httpx

from app.utils.request import SyncRequestAdapter


API_URL = "https://i.cnblogs.com/api/posts"
REFERER = "https://i.cnblogs.com/articles/edit"

# Paste the complete Cookie request-header value from your logged-in browser.
COOKIE = {
    "_ga": "GA1.1.352799996.1765767661",
    "_c_WBKFRo": "if4uTDu5I1qWqD1xgbmqSQI2bF0GCUXhyjEp0JQx",
    "_ga_M95P3TTWJZ": "GS2.1.s1776928671$o18$g0$t1776929073$j60$l0$h0",
    "Hm_lvt_866c9be12d4a814454792b1fd0fed295": "1784652709,1784699453",
    "HMACCOUNT": "55FE8F17BCF4F515",
    ".CNBlogsCookie": "9D66E854D3BAACECBBA0E4EC12AE0CE53A2C68E6B03794B7A9434B8E184AFF3D6D746A1ED3C8780C55A7C7730EBF3C8F124AB1852170C76881D30F520C2B766B55F9E5155972B0F7120CC66E5BD6DD2C4CB40642",
    ".AspNetCore.Antiforgery.b8-pDmTq1XM": "CfDJ8Ow5-In01nBEvFwaadRGjqFr3JQ2eIScL4AoY2zOMIrtw9Kmlypm1X7ap450jYwg2BO8fPkSB1u9Xw9RgYa2numMF9XmunfHTZpb5Qd3XJBv8NLVlsj9gEtEapQGiCahEqTcXLnTRXmbjA9gfEPlHOc",
    "_ga_3Q0DVSGN10": "GS2.1.s1784821368$o4$g1$t1784821388$j40$l0$h0",
    "_ga_XPE8C2TMTE": "GS2.1.s1784821389$o2$g0$t1784821393$j56$l0$h0",
    ".Cnblogs.AspNetCore.Cookies": "CfDJ8Ow5-In01nBEvFwaadRGjqENJCDxGAUea8xbZP5fpZIWGCx2n3zWaGeN1Z_TuuqucRQAakGt2fs_jGeAPLsLZygzOh7T_mzMqHMbj0gU-1HryJmq3n6nzBrgKfKqtXkxCA6lWcRHq_ww8LeO_L8C_GNwMj0WnlINEd_xNc4jQKqAZxUkjReD4ZqeFjhOp27z_FojolCkl0tXZKyh6GJcOfZnYiLY26tm8KCuJECOwNi3zmlC4ixBEnLfAiKN-7RoKDrzvmBcuThBA0Elo4Z2T1kFfATc4px3SakWU3HPxKrfaKenjDeO8n9WjuVFGyBRMEF42Iy6C6hDmphZA4kdgX6wFi_SpuRsUaVWpLxp_4cTIFFJ9o2lvz6nJUbypIK_G_H2Pg6Efi-1SNvdVGVMdj75ncQulVm-iREZ7AJlkWzpB9Vmazh2uVYHdgO8jf7Cd-xNzU-70jInydM0Hlwym15CBGbNKxTphfTSEg8LCPCrnQleDjxp7LIeEbWqio2VNDND9OGH873_AYWjbNwWRTCR7hLgeqZIebo7OukeywqePQ1HNyorg3gO6Hqy540GeT1-byWEe1IMlInIwMWJGkQRdCuNE6xO6buCkQ6nsrK1",
    "Hm_lpvt_866c9be12d4a814454792b1fd0fed295": "1784821709",
    "XSRF-TOKEN": "CfDJ8Ow5-In01nBEvFwaadRGjqGgSGHdtCfRoRFYjJ_Scljgb7UQdSV9QVOMf_Rqr1qfMVByfHYaC4V3F5YlNDZQSh4kEmKDxx9z3TCwWgoW3C-PPXSjUuweJpvgIahtUU2TEuHF5NEVVb4tT4oSOqhyacNsqjppG6vIuY3IHl5sYVS5WDpz3OO5zmZgNdACK-9LCA"
}

# False only prints a redacted preview. Change to True to publish.
SEND_REQUEST = True

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def build_payload(title: str, markdown: str) -> dict[str, object]:
    """Build the payload captured from the CNBlogs Article publish button."""
    return {
        "id": None,
        "postType": 2,
        "accessPermission": 0,
        "title": title,
        "url": None,
        "postBody": markdown,
        "categoryIds": None,
        "categories": None,
        "collectionIds": [],
        "inSiteCandidate": False,
        "inSiteHome": False,
        "siteCategoryId": None,
        "blogTeamIds": None,
        "isPublished": True,
        "displayOnHomePage": False,
        "isAllowComments": True,
        "includeInMainSyndication": False,
        "isPinned": False,
        "showBodyWhenPinned": False,
        "isOnlyForRegisterUser": False,
        "isUpdateDateAdded": True,
        "entryName": None,
        "description": None,
        "featuredImage": None,
        "tags": None,
        "password": None,
        "publishAt": None,
        "datePublished": utc_timestamp(),
        "dateUpdated": None,
        "isMarkdown": True,
        "isDraft": True,
        "isAigc": False,
        "autoDesc": None,
        "changePostType": False,
        "blogId": 0,
        "author": None,
        "removeScript": False,
        "clientInfo": None,
        "changeCreatedTime": False,
        "canChangeCreatedTime": False,
        "isContributeToImpressiveBugActivity": False,
        "usingEditorId": 5,
        "sourceUrl": None,
    }


def build_headers(cookies: dict[str, str]) -> dict[str, str]:
    xsrf_token = unquote(cookies["XSRF-TOKEN"])
    cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Cookie": cookie_header,
        "Origin": "https://i.cnblogs.com",
        "Referer": REFERER,
        "User-Agent": USER_AGENT,
        "X-XSRF-TOKEN": xsrf_token,
        "sessionId": str(uuid.uuid4()),
        "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }


def redacted_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        name: "<redacted>"
        if name.lower() in {"cookie", "x-xsrf-token", "sessionid"}
        else value
        for name, value in headers.items()
    }


def publish(payload: dict[str, object], headers: dict[str, str]) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        with SyncRequestAdapter() as requester:
            response = requester.request(
                "POST", API_URL, data=data, headers=headers, timeout=30
            )
    except httpx.HTTPError as error:
        return 0, str(error)
    return response.status_code, response.text


def main() -> int:
    # Edit the title and Markdown content here.
    title = "推荐一个快运物流智能AI助手"
    if not title.strip():
        raise SystemExit("Title must not be empty.")

    markdown = '''
**📦 摘星货蚁物流 AI 介绍**

摘星货蚁物流 AI 是深圳摘星人工智能推出的**垂直物流一体化 AI 经营系统**，专为快运网点、专线、三方物流及大件物流企业设计，聚焦解决物流行业人工成本高、效率低、利润不透明等痛点。通过 AI 技术整合全链路业务流程，助力企业降本增效，打造智能化、品牌化物流服务体系。

**🔎 核心定位**

- **垂直物流赛道**：深耕零担、专线、三方物流场景，拒绝通用型 AI 的“水土不服”。
- **全流程闭环**：覆盖获客、咨询、报价、调度、财务等全环节，实现一站式数字化管理。

**🌟 核心功能模块**

1. **AI 智能客服**
    - **AI 智能客服**
    - **全渠道秒级响应**：支持微信、小程序、客户群等多端接入，实现 7×24 小时全天候自动应答，不错过任何商机。
    - **高频场景智能接管**：自动精准处理查价、轨迹追踪、派送范围确认及异常件跟进，大幅降低人工客服依赖，释放人力资源。
2. **AI 报价与成本核算**
    - 原生对接安能、壹米滴答、顺心捷达等主流快运网络，实时调取底价。
    - 智能核算线路成本、附加费，结合客户等级自动差异化报价，避免亏损接单。
3. **AI 司机调度**
    - **AI 司机调度**
    - **智能调度，降本增效**：订单与运力精准匹配，算法优化路线规划，显著减少车辆空驶率与漏单现象。
    - **透明管理，规避纠纷**：司机任务实时同步至端，系统自动核算绩效提成，实现管理透明化，有效降低沟通成本与纠纷。
4. **自有品牌小程序**
    - 快速搭建专属物流品牌入口，支持客户自主下单、轨迹追踪、在线支付。
    - 沉淀私域流量，提升客户粘性。
5. **精细化财务系统**
    - **精细化财务系统**
    - **自动化对账**：一键汇总各平台账单并生成客户月结清单，告别繁琐手工核算，对账效率提升 **80%+**。
    - **数据驱动决策**：提供线路、公斤段、客户群体等多维度利润透视，帮助企业清晰掌握核心盈利点，科学优化经营策略。

**🔄 技术优势**

- **原生物流架构**：底层数据深度适配物流业务逻辑，无需额外开发接口。
- **全链路自动化**：从咨询到调度到财务，数据自动流转，减少人工操作。
- **智能主动服务**：AI 主动推送异常预警、物流进度，提升客户体验。

**🏢 适用场景**

- 快运网点、快递加盟商
- 专线物流公司
- 三方物流企业
- 区域大件物流品牌

**💡 落地价值**

1. **降本增效**：减少客服、调度、财务人力成本，提升响应速度与客户满意度。
2. **数据赋能**：透明化利润分析，规避经验式决策风险。
3. **品牌升级**：通过小程序打造本地物流品牌，增强竞争力。

**🚀 配套服务**

- **云端 SaaS 部署**：即开即用，无需硬件投入。
- **行业模板预设**：快速配置，降低实施门槛。
- **全流程支持**：需求梳理 → 系统配置 → 上线培训 → 持续迭代。

**📌 联系我们**

- **对接人**：知恩
- **联系电话**：19357676570

---

**✨ 为何选择摘星货蚁？**

- 物流场景原生设计，拒绝“伪 AI 工具”
- 从咨询到利润的全链路数字化能力
- 中小物流企业也能用得起的智能化方案

'''
    if not markdown:
        raise SystemExit("Markdown content must not be empty.")
    if not COOKIE:
        raise SystemExit("Set COOKIE in this file.")
    if "XSRF-TOKEN" not in COOKIE:
        raise SystemExit("COOKIE is missing XSRF-TOKEN.")

    payload = build_payload(title, markdown)
    headers = build_headers(COOKIE)

    if not SEND_REQUEST:
        preview = {
            "method": "POST",
            "url": API_URL,
            "headers": redacted_headers(headers),
            "payload": payload,
        }
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        print("Preview only. Set SEND_REQUEST = True to publish the article.")
        return 0

    status, response_body = publish(payload, headers)
    print(f"HTTP {status}")
    print(response_body)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
