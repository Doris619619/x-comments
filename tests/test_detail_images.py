"""
本文件测试详情页图库读取的正常映射与安全失败边界。

它使用 Playwright 接口的最小替身，不启动浏览器、不读取登录态，也不访问真实闲鱼。
"""

from decimal import Decimal

import pytest

from app.core.config import Settings
from app.crawler.client import XianyuCrawler
from app.crawler.risk_control import RiskControlBlocked
from app.schemas.item import ParsedItem


class _Response:
    """表示详情页导航响应的最小替身；只提供状态码，无副作用。"""

    def __init__(self, status: int) -> None:
        """保存测试用 HTTP 状态码；无返回、异常或副作用。"""

        self.status = status


class _Locator:
    """表示页面节点集合的最小替身；返回预设文本和图片地址。"""

    def __init__(self, text: str, image_urls: list[object]) -> None:
        """保存预设正文和节点属性；无返回、异常或副作用。"""

        self.text = text
        self.image_urls = image_urls

    async def inner_text(self, timeout: int) -> str:
        """返回页面正文；timeout 仅匹配真实接口，未使用且无副作用。"""

        del timeout
        return self.text

    async def evaluate_all(self, expression: str) -> list[object]:
        """返回详情图库属性；expression 仅匹配真实接口，未使用且无副作用。"""

        del expression
        return self.image_urls


class _Page:
    """表示详情页的最小替身；模拟一次导航、节点读取和关闭。"""

    def __init__(self, text: str, image_urls: list[object], status: int = 200) -> None:
        """保存测试页面状态；无返回、异常或外部副作用。"""

        self.text = text
        self.image_urls = image_urls
        self.status = status
        self.url = "https://www.goofish.com/item?id=10001"
        self.closed = False

    async def goto(self, url: str, wait_until: str, timeout: int) -> _Response:
        """返回预设响应；输入仅匹配真实接口，未访问网络。"""

        del url, wait_until, timeout
        return _Response(self.status)

    def locator(self, selector: str) -> _Locator:
        """按 body 或图库选择器返回预设节点；无异常和副作用。"""

        return _Locator(self.text, [] if selector == "body" else self.image_urls)

    async def close(self) -> None:
        """标记页面已关闭；无返回和外部副作用。"""

        self.closed = True


class _Context:
    """表示浏览器上下文的最小替身；每次只返回一张预设详情页。"""

    def __init__(self, page: _Page) -> None:
        """保存详情页替身；无返回、异常或副作用。"""

        self.page = page

    async def new_page(self) -> _Page:
        """返回预设页面；无异常和外部副作用。"""

        return self.page


def _item() -> ParsedItem:
    """创建带搜索首图的最小解析商品；无输入、异常和外部副作用。"""

    return ParsedItem(
        item_id="10001",
        title="测试发饰",
        price=Decimal("12.50"),
        image_url="https://img.example.invalid/cover.jpg",
        item_url="https://www.goofish.com/item?id=10001",
    )


@pytest.mark.asyncio
async def test_detail_images_normalize_deduplicate_and_preserve_cover_contract() -> None:
    """验证详情图库以详情首图覆盖搜索首图、去重并在关闭页面后返回。"""

    page = _Page(
        "正常商品详情",
        [
            "//img.example.invalid/detail-one.jpg",
            "https://img.example.invalid/detail-one.jpg#fragment",
            "https://img.example.invalid/detail-two.jpg",
        ],
    )
    crawler = XianyuCrawler(Settings(xianyu_max_images_per_item=9))

    result = await crawler._read_detail_images(_Context(page), _item())  # type: ignore[arg-type]

    assert [str(url) for url in result.image_urls] == [
        "https://img.example.invalid/detail-one.jpg",
        "https://img.example.invalid/detail-two.jpg",
    ]
    assert str(result.image_url) == "https://img.example.invalid/detail-one.jpg"
    assert page.closed is True


@pytest.mark.asyncio
async def test_detail_images_stop_on_risk_control_signal() -> None:
    """验证详情页出现风控文本时立即中止，而不是使用搜索首图继续发布。"""

    page = _Page("访问频繁，请稍后再试", ["https://img.example.invalid/detail.jpg"])
    crawler = XianyuCrawler(Settings())

    with pytest.raises(RiskControlBlocked):
        await crawler._read_detail_images(_Context(page), _item())  # type: ignore[arg-type]

    assert page.closed is True
