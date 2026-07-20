"""
本文件使用本地登录态对单个闲鱼商品执行一次实时详情访问。

它属于 crawler 模块，只负责页面访问、风险识别和当前价格提取，不查询数据库或比较商城价格。
"""

import asyncio
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.async_api import BrowserContext, Page, Response, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.core.config import Settings
from app.crawler.risk_control import RiskControlBlocked, detect_risk
from app.crawler.selectors import DETAIL_PRICE_SELECTORS, EXPLICIT_UNAVAILABLE_TEXT_SIGNALS
from app.services.item_verification import (
    LiveVerificationResult,
    LiveVerificationStatus,
    VerificationTarget,
)
from app.services.xianyu_account_guard import (
    AccountAccessGuard,
    AccountGuardInput,
    normalize_account_guard,
)

PRICE_TEXT_PATTERN = re.compile(
    r"(?<![\d.\-])(?:CNY\s*)?[¥￥]?\s*(\d+(?:\.\d{1,2})?)(?![\d.])"
)


def parse_single_price_text(value: str) -> Decimal | None:
    """
    从只含一个金额的公开文本中提取非负两位小数价格。

    输入可见价格文本；无法唯一确认时返回 None；不抛出解析异常且无副作用。
    """

    normalized = value.replace(",", "")
    candidates = set(PRICE_TEXT_PATTERN.findall(normalized))
    if len(candidates) != 1:
        return None
    try:
        price = Decimal(next(iter(candidates)))
    except InvalidOperation:
        return None
    exponent = price.as_tuple().exponent
    if not isinstance(exponent, int) or not price.is_finite() or price < 0 or exponent < -2:
        return None
    return price


def page_matches_target(url: str, item_id: str) -> bool:
    """
    确认详情页最终 URL 仍指向请求的闲鱼商品。

    输入页面 URL 与商品 ID；仅在 goofish 商品页 ID 一致时返回 True；无异常和副作用。
    """

    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    page_ids = parse_qs(parsed.query).get("id", [])
    return host in {"goofish.com", "www.goofish.com"} and parsed.path.rstrip("/") == "/item" and (
        page_ids == [item_id]
    )


def find_explicit_unavailable_signal(visible_text: str) -> str | None:
    """
    仅在页面出现明确售出、下架、删除或不存在文案时返回对应信号。

    输入页面可见文本；没有明确证据时返回 None；不访问网络且无副作用。
    """

    return next(
        (signal for signal in EXPLICIT_UNAVAILABLE_TEXT_SIGNALS if signal in visible_text),
        None,
    )


class XianyuItemVerifier:
    """
    使用 Playwright 对一个商品详情执行单次、失败关闭的实时核验。

    输入应用配置；每次 verify 只导航一次，不重试；登录或风控信号会返回 blocked。
    """

    def __init__(self, settings: Settings, account_lock: AccountGuardInput | None = None) -> None:
        """
        保存已校验的登录态路径、浏览器模式和核验时限。

        输入 Settings 与可选账号级锁；无返回和异常；副作用仅为保存引用和创建缺省锁。
        """

        self.settings = settings
        self.account_lock = account_lock or asyncio.Lock()
        self.account_guard: AccountAccessGuard = normalize_account_guard(self.account_lock)

    async def verify(self, target: VerificationTarget) -> LiveVerificationResult:
        """
        对目标详情执行一次访问并返回安全分类结果。

        输入闲鱼商品 ID；返回实时结果；内部异常会转为 blocked 或 unknown，不自动重试。
        """

        state_path = Path(self.settings.xianyu_storage_state_path)
        if not state_path.is_file():
            return LiveVerificationResult(
                status=LiveVerificationStatus.BLOCKED,
                current_price=None,
                reason_code="login_state_missing",
            )

        try:
            return await asyncio.wait_for(
                self._verify_serialized(target, state_path),
                timeout=self.settings.xianyu_verify_timeout_seconds,
            )
        except RiskControlBlocked:
            return LiveVerificationResult(
                status=LiveVerificationStatus.BLOCKED,
                current_price=None,
                reason_code="risk_control_blocked",
            )
        except (TimeoutError, PlaywrightTimeoutError):
            return LiveVerificationResult(
                status=LiveVerificationStatus.UNKNOWN,
                current_price=None,
                reason_code="verification_timeout",
            )
        except Exception:
            return LiveVerificationResult(
                status=LiveVerificationStatus.UNKNOWN,
                current_price=None,
                reason_code="verification_page_error",
            )

    async def _verify_serialized(
        self, target: VerificationTarget, state_path: Path
    ) -> LiveVerificationResult:
        """
        在账号级共享锁内执行一次详情核验。

        输入目标与登录态路径；返回核验结果；等待锁和页面异常向上抛出；副作用为串行页面访问。
        """

        async with self.account_guard.hold():
            return await self._verify_once(target, state_path)

    async def _verify_once(
        self, target: VerificationTarget, state_path: Path
    ) -> LiveVerificationResult:
        """
        创建一个临时浏览器上下文并只调用一次详情页导航。

        输入目标与本地登录态路径；返回页面核验结果；页面和浏览器异常向上抛出。
        """

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=self.settings.xianyu_headless)
            try:
                context = await browser.new_context(storage_state=str(state_path))
                return await self._verify_in_context(context, target)
            finally:
                await browser.close()

    async def _verify_in_context(
        self, context: BrowserContext, target: VerificationTarget
    ) -> LiveVerificationResult:
        """
        在既有认证上下文中读取一次目标详情的状态和当前价格。

        输入浏览器上下文和商品 ID；返回分类结果；风控时抛出 RiskControlBlocked。
        """

        page = await context.new_page()
        blocked_status: int | None = None

        def observe_status(response: Response) -> None:
            """记录本次页面访问中首次出现的 403 或 429，不读取响应内容。"""

            nonlocal blocked_status
            status = int(getattr(response, "status", 0))
            if status in {403, 429} and blocked_status is None:
                blocked_status = status

        page.on("response", observe_status)
        try:
            item_url = f"https://www.goofish.com/item?{urlencode({'id': target.item_id})}"
            navigation = await page.goto(
                item_url,
                wait_until="domcontentloaded",
                timeout=self.settings.xianyu_verify_timeout_seconds * 1000,
            )
            visible_text = await page.locator("body").inner_text(timeout=5_000)
            risk_reason = detect_risk(page.url, visible_text, blocked_status)
            if risk_reason:
                raise RiskControlBlocked(risk_reason)

            if not page_matches_target(page.url, target.item_id):
                return LiveVerificationResult(
                    status=LiveVerificationStatus.UNKNOWN,
                    current_price=None,
                    reason_code="listing_identity_not_confirmed",
                )

            unavailable_signal = find_explicit_unavailable_signal(visible_text)
            if unavailable_signal:
                return LiveVerificationResult(
                    status=LiveVerificationStatus.UNAVAILABLE,
                    current_price=None,
                    reason_code="listing_explicitly_unavailable",
                )

            navigation_status = int(navigation.status) if navigation is not None else 0
            if navigation_status >= 400:
                return LiveVerificationResult(
                    status=LiveVerificationStatus.UNKNOWN,
                    current_price=None,
                    reason_code=f"listing_http_{navigation_status}",
                )

            current_price = await self._read_current_price(page)
            if current_price is None:
                return LiveVerificationResult(
                    status=LiveVerificationStatus.UNKNOWN,
                    current_price=None,
                    reason_code="listing_price_not_found",
                )
            return LiveVerificationResult(
                status=LiveVerificationStatus.AVAILABLE,
                current_price=current_price,
                reason_code="listing_available",
            )
        finally:
            await page.close()

    async def _read_current_price(self, page: Page) -> Decimal | None:
        """
        从详情页价格元数据或有限的主商品价格选择器读取唯一金额。

        输入已加载页面；返回唯一可确认价格或 None；选择器变化不会导致额外页面访问。
        """

        metadata = page.locator("meta[property='product:price:amount'], meta[itemprop='price']")
        metadata_prices: set[Decimal] = set()
        for index in range(min(await metadata.count(), 5)):
            content = await metadata.nth(index).get_attribute("content")
            if content and (price := parse_single_price_text(content)) is not None:
                metadata_prices.add(price)
        if len(metadata_prices) == 1:
            return next(iter(metadata_prices))

        for selector in DETAIL_PRICE_SELECTORS:
            locator = page.locator(selector)
            prices: set[Decimal] = set()
            for index in range(min(await locator.count(), 5)):
                node = locator.nth(index)
                if not await node.is_visible():
                    continue
                text = await node.inner_text(timeout=2_000)
                if (price := parse_single_price_text(text)) is not None:
                    prices.add(price)
            if len(prices) == 1:
                return next(iter(prices))
        return None
