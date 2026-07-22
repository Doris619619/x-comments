"""
本文件负责规范化闲鱼商品详情页中提取出的公开图片地址。

它属于 crawler 模块，供页面访问器和离线测试复用；它不访问网络、页面或数据库，
也不决定任务状态。
"""

from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

MAX_DETAIL_IMAGE_COUNT = 9


def normalize_detail_image_urls(values: Iterable[object]) -> list[str]:
    """
    规范化、去重并限制详情页公开图片 URL。

    参数：
        values: 页面属性或响应中提取出的原始 URL 值。

    返回：
        保留原顺序、最多九条的 HTTPS/HTTP 图片 URL。

    异常：
        无；无效、非网页协议或重复 URL 会被安全忽略。

    副作用：
        无。
    """

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        raw = str(value or "").strip().replace("\\/", "/")
        if raw.startswith("//"):
            raw = f"https:{raw}"
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        candidate = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        if candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
        if len(normalized) == MAX_DETAIL_IMAGE_COUNT:
            break
    return normalized
