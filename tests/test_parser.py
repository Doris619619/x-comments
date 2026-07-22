"""
本文件测试搜索响应解析器的离线字段映射和异常边界。

它只读取脱敏 fixture，不访问真实闲鱼或数据库。
"""

import json
from decimal import Decimal
from pathlib import Path

from app.crawler.detail_images import normalize_detail_image_urls
from app.crawler.parser import parse_search_response


def test_parse_fixture() -> None:
    """
    验证脱敏响应能解析为标准商品。

    无输入；断言失败抛出 AssertionError；只读取 fixture。
    """

    payload = json.loads(Path("tests/fixtures/search_response.json").read_text(encoding="utf-8"))
    items, errors = parse_search_response(payload)
    assert errors == []
    assert len(items) == 2
    assert items[0].item_id == "10001"
    assert items[0].title == "蝴蝶结 发夹"
    assert items[0].price == Decimal("12.80")
    assert str(items[0].image_url) == "https://example.invalid/item-10001.jpg"
    assert [str(url) for url in items[0].image_urls] == [
        "https://example.invalid/item-10001.jpg"
    ]


def test_normalizes_deduplicates_and_limits_detail_images() -> None:
    """
    验证详情图库会忽略非法地址、去掉片段并保留前九张公开图片。

    无输入；断言失败时抛出 AssertionError；无外部副作用。
    """

    values = [
        "//img.example.invalid/first.jpg#preview",
        "https://img.example.invalid/first.jpg",
        "data:image/png;base64,unsafe",
        "javascript:alert(1)",
        *[f"https://img.example.invalid/{index}.jpg" for index in range(2, 12)],
    ]

    assert normalize_detail_image_urls(values) == [
        "https://img.example.invalid/first.jpg",
        *[f"https://img.example.invalid/{index}.jpg" for index in range(2, 10)],
    ]


def test_rejects_mismatched_id() -> None:
    """
    验证响应 ID 与链接 ID 不一致时拒绝商品。

    无输入；断言失败抛出 AssertionError；无外部副作用。
    """

    payload: dict[str, object] = {
        "data": {
            "resultList": [
                {
                    "data": {
                        "item": {
                            "main": {
                                "targetUrl": "https://www.goofish.com/item?id=2",
                                "exContent": {
                                    "itemId": "1",
                                    "title": "发夹",
                                    "price": [{"text": "¥1"}],
                                },
                            }
                        }
                    }
                }
            ]
        }
    }
    items, errors = parse_search_response(payload)
    assert items == []
    assert "不一致" in errors[0]
