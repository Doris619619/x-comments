"""
本文件负责通过 Playwright 有限访问闲鱼搜索页面并收集公开商品。

它属于 crawler 模块，调用纯解析器并执行安全停止，不访问数据库或决定任务状态。
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from urllib.parse import urlencode

from playwright.async_api import (
    BrowserContext,
    Page,
    Response,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from app.core.config import Settings
from app.crawler.detail_images import normalize_detail_image_urls
from app.crawler.parser import parse_search_response
from app.crawler.risk_control import RiskControlBlocked, detect_risk
from app.crawler.selectors import DETAIL_IMAGE_SELECTORS, NEXT_PAGE_BUTTON, SEARCH_API_FRAGMENT
from app.schemas.item import ParsedItem
from app.services.xianyu_account_guard import (
    AccountAccessGuard,
    AccountGuardInput,
    normalize_account_guard,
)


@dataclass(frozen=True)
class CrawlResult:
    """
    表示一次有限采集的解析结果和非致命错误。

    由客户端返回；无副作用。
    """

    items: list[ParsedItem]
    errors: list[str]
    pages_visited: int


class XianyuCrawler:
    """
    使用单个 Playwright 上下文执行有限关键词搜索。

    输入配置；网络、页面或结构错误向上抛出；副作用仅为有限页面访问。
    """

    def __init__(self, settings: Settings, account_lock: AccountGuardInput | None = None) -> None:
        """
        保存已校验配置。

        输入 Settings 与可选账号级锁；无返回和异常；不启动浏览器。
        """

        self.settings = settings
        self.account_lock = account_lock or asyncio.Lock()
        self.account_guard: AccountAccessGuard = normalize_account_guard(self.account_lock)

    async def collect(self, keyword: str) -> CrawlResult:
        """
        复用本地登录态采集最多三页或五十条公开商品。

        输入关键词；返回解析结果；认证/风控抛出 RiskControlBlocked，其他错误向上抛出。
        """

        async with self.account_guard.hold():
            state_path = Path(self.settings.xianyu_storage_state_path)
            if not state_path.is_file():
                raise RiskControlBlocked("本地登录态文件不存在，需要人工重新登录")
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=self.settings.xianyu_headless)
                try:
                    context = await browser.new_context(storage_state=str(state_path))
                    return await self._collect_in_context(context, keyword)
                finally:
                    await browser.close()

    async def _collect_in_context(self, context: BrowserContext, keyword: str) -> CrawlResult:
        """
        在已创建的认证上下文中执行搜索和有限翻页。

        输入上下文和关键词；返回结果；风险或响应结构异常会立即抛出并关闭页面。
        """

        page = await context.new_page()
        blocked_status: int | None = None
        search_responses: asyncio.Queue[Response] = asyncio.Queue()

        def observe_status(response: Response) -> None:
            """记录风控状态并把搜索响应加入当前页面的内存队列。"""

            nonlocal blocked_status
            status = int(getattr(response, "status", 0))
            url = str(getattr(response, "url", ""))
            if status in {403, 429} and blocked_status is None:
                blocked_status = status
            if SEARCH_API_FRAGMENT in url:
                search_responses.put_nowait(response)

        page.on("response", observe_status)
        try:
            search_url = f"https://www.goofish.com/search?{urlencode({'q': keyword})}"
            await page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
            first_payload = await self._wait_valid_payload(search_responses, 30)
            all_items: list[ParsedItem] = []
            errors: list[str] = []
            pages_visited = 0
            payload: dict[str, object] | None = first_payload
            while payload is not None and pages_visited < self.settings.xianyu_max_pages:
                await self._assert_safe(page, blocked_status)
                parsed, page_errors = parse_search_response(payload)
                if not parsed:
                    raise RiskControlBlocked("搜索结果为空或结构异常，无法确认采集正常")
                detailed_items, detail_errors = await self._enrich_detail_images(context, parsed)
                all_items.extend(detailed_items)
                errors.extend(page_errors)
                errors.extend(detail_errors)
                pages_visited += 1
                unique_items = {item.item_id: item for item in all_items}
                if len(unique_items) >= self.settings.xianyu_max_items:
                    all_items = list(unique_items.values())[: self.settings.xianyu_max_items]
                    break
                payload = await self._next_page(page, search_responses)
            return CrawlResult(items=all_items, errors=errors, pages_visited=pages_visited)
        finally:
            await page.close()

    async def _enrich_detail_images(
        self, context: BrowserContext, items: list[ParsedItem]
    ) -> tuple[list[ParsedItem], list[str]]:
        """
        顺序读取搜索结果中每件商品的公开详情图库。

        参数：
            context: 与搜索页共用登录态的浏览器上下文。
            items: 已从搜索响应解析出的商品。

        返回：
            带详情图库的商品及每件非风控失败的错误信息。

        异常：
            RiskControlBlocked: 出现登录失效、403/429 或风控时立即向上停止整次任务。

        副作用：
            对每件商品最多打开一次公开详情页。
        """

        enriched: list[ParsedItem] = []
        errors: list[str] = []
        for item in items:
            try:
                enriched.append(await self._read_detail_images(context, item))
            except RiskControlBlocked:
                raise
            except (PlaywrightTimeoutError, ValueError) as exc:
                enriched.append(item)
                errors.append(f"item {item.item_id} detail images: {type(exc).__name__}")
        return enriched, errors

    async def _read_detail_images(self, context: BrowserContext, item: ParsedItem) -> ParsedItem:
        """
        访问一次商品详情页并提取其公开图库。

        参数：
            context: 已认证浏览器上下文。
            item: 搜索页解析的单件商品，包含兼容用首图。

        返回：
            以详情图库首图更新后的解析商品。

        异常：
            RiskControlBlocked: 页面出现登录或风控信号时抛出。
            ValueError: 未获得任何可用公开图片时抛出。
            PlaywrightTimeoutError: 页面访问超时时抛出。

        副作用：
            创建、访问并关闭一个详情页。
        """

        page = await context.new_page()
        try:
            response = await page.goto(
                str(item.item_url),
                wait_until="domcontentloaded",
                timeout=self.settings.xianyu_verify_timeout_seconds * 1_000,
            )
            await self._assert_safe(page, response.status if response is not None else None)
            image_nodes = page.locator(", ".join(DETAIL_IMAGE_SELECTORS))
            raw_urls = await image_nodes.evaluate_all(
                """nodes => nodes.map(node =>
                    node.currentSrc || node.getAttribute('src') ||
                    node.getAttribute('data-src') || ''
                )"""
            )
            if not isinstance(raw_urls, list):
                raise ValueError("详情图库节点格式异常")
            normalized_urls = normalize_detail_image_urls(raw_urls)
            image_urls = normalized_urls[: self.settings.xianyu_max_images_per_item]
            if not image_urls:
                raise ValueError("详情页未返回公开图库")
            return item.model_copy(update={"image_url": image_urls[0], "image_urls": image_urls})
        finally:
            await page.close()

    async def _wait_valid_payload(
        self, search_responses: asyncio.Queue[Response], timeout_seconds: float
    ) -> dict[str, object]:
        """
        等待当前页面产生包含非空商品列表的搜索响应。

        输入响应队列和时限；返回有效 JSON；超时抛出安全阻塞，不访问额外页面。
        """

        deadline = monotonic() + timeout_seconds
        while (remaining := deadline - monotonic()) > 0:
            try:
                response = await asyncio.wait_for(search_responses.get(), timeout=remaining)
            except TimeoutError as exc:
                raise RiskControlBlocked("搜索响应超时，无法确认采集正常") from exc
            try:
                payload = await response.json()
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            data = payload.get("data")
            result_list = data.get("resultList") if isinstance(data, dict) else None
            if isinstance(result_list, list) and result_list:
                return payload
        raise RiskControlBlocked("搜索结果为空或结构异常，无法确认采集正常")

    async def _assert_safe(self, page: Page, blocked_status: int | None) -> None:
        """
        检查当前页面是否出现登录或风控信号。

        输入页面与状态；发现风险抛出 RiskControlBlocked；只读取 URL 和可见文本。
        """

        visible_text = await page.locator("body").inner_text(timeout=10_000)
        reason = detect_risk(page.url, visible_text, blocked_status)
        if reason:
            raise RiskControlBlocked(reason)

    async def _next_page(
        self, page: Page, search_responses: asyncio.Queue[Response]
    ) -> dict[str, object] | None:
        """
        在限速后点击唯一可用下一页并等待响应。

        输入页面；无下一页返回 None；页面错误向上抛出；副作用为最多一次翻页。
        """

        await asyncio.sleep(self.settings.xianyu_page_delay_seconds)
        button = page.locator(NEXT_PAGE_BUTTON)
        if await button.count() != 1 or not await button.is_enabled():
            return None
        try:
            await button.click()
            return await self._wait_valid_payload(search_responses, 20)
        except (PlaywrightTimeoutError, RiskControlBlocked):
            return None
