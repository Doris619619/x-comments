"""
本文件负责离线解析闲鱼搜索响应 JSON。

它属于 crawler 模块，不访问网络或数据库，可由脱敏 fixture 独立测试。
"""

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlparse

from app.crawler.detail_images import normalize_detail_image_urls
from app.schemas.item import ParsedItem


class ItemParseError(ValueError):
    """
    表示单条商品字段缺失或互相矛盾。

    输入错误原因；由调用方捕获并计数，没有额外副作用。
    """


def _nested(data: object, *keys: str) -> object | None:
    """
    安全读取嵌套字典。

    输入任意对象和键路径，返回目标值或 None；无异常和副作用。
    """

    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _parse_price(parts: object) -> Decimal:
    """
    将闲鱼价格片段转换为 Decimal。

    输入字符串或片段列表；格式无效时抛出 ItemParseError；无副作用。
    """

    if isinstance(parts, list):
        text = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
    else:
        text = str(parts or "")
    match = re.search(r"(\d+(?:\.\d+)?)", text.replace(",", ""))
    if not match:
        raise ItemParseError("价格缺失或格式异常")
    try:
        return Decimal(match.group(1))
    except InvalidOperation as exc:
        raise ItemParseError("价格无法转换") from exc


def _normalize_url(raw_url: object) -> str:
    """
    将页面协议链接规范化为 HTTPS 闲鱼链接。

    输入原始 URL，返回规范链接；缺失或非闲鱼链接时抛出 ItemParseError。
    """

    url = str(raw_url or "").strip().replace("fleamarket://", "https://www.goofish.com/")
    if url.startswith("//"):
        url = f"https:{url}"
    if not url.startswith("https://www.goofish.com/"):
        raise ItemParseError("商品链接缺失或来源异常")
    return url


def _item_id_from_url(item_url: str) -> str | None:
    """
    从规范商品链接提取 `id` 查询参数。

    输入 URL 并返回数字 ID 或 None；无副作用。
    """

    candidate = parse_qs(urlparse(item_url).query).get("id", [None])[0]
    return candidate if candidate and candidate.isdigit() else None


def parse_search_response(payload: dict[str, object]) -> tuple[list[ParsedItem], list[str]]:
    """
    解析完整搜索响应并返回有效商品与逐条错误。

    输入脱敏响应字典；结构不存在时返回空列表和错误；不访问网络或数据库。
    """

    result_list = _nested(payload, "data", "resultList")
    if not isinstance(result_list, list):
        return [], ["响应缺少 data.resultList"]
    parsed: list[ParsedItem] = []
    errors: list[str] = []
    for index, raw in enumerate(result_list):
        try:
            main = _nested(raw, "data", "item", "main")
            content = _nested(main, "exContent")
            if not isinstance(content, dict):
                raise ItemParseError("缺少 exContent")
            item_url = _normalize_url(_nested(main, "targetUrl"))
            response_id = str(content.get("itemId") or "").strip()
            url_id = _item_id_from_url(item_url)
            if not response_id.isdigit():
                response_id = url_id or ""
            if not response_id or (url_id and url_id != response_id):
                raise ItemParseError("商品 ID 缺失或与链接不一致")
            image_urls = normalize_detail_image_urls([content.get("picUrl")])
            image = image_urls[0] if image_urls else None
            parsed.append(
                ParsedItem(
                    item_id=response_id,
                    title=str(content.get("title") or ""),
                    price=_parse_price(content.get("price")),
                    image_url=image,
                    image_urls=image_urls,
                    item_url=item_url,
                    location=str(content.get("area") or "").strip() or None,
                )
            )
        except (ItemParseError, ValueError) as exc:
            errors.append(f"resultList[{index}]: {exc}")
    return parsed, errors
