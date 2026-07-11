"""
本文件用于 Goal 2 随机抽查数据库商品与真实搜索页卡片的一致性。

它只读取公开商品卡片和数据库公开字段，不读取或输出登录态具体内容。
"""

import asyncio
import json
import random
import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.async_api import Page, async_playwright
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionFactory
from app.crawler.risk_control import RiskControlBlocked, detect_risk
from app.crawler.selectors import NEXT_PAGE_BUTTON
from app.models.item import Item
from app.models.keyword import ItemKeyword, Keyword


@dataclass(frozen=True)
class AuditRow:
    """
    保存一条公开商品人工辅助抽查结果。

    仅包含公开字段和布尔结论，无副作用。
    """

    item_id: str
    title: str
    price: str
    item_url: str
    id_ok: bool
    link_ok: bool
    title_ok: bool
    price_ok: bool
    image_ok: bool

    @property
    def passed(self) -> bool:
        """
        返回五项字段是否全部正确。

        无输入，返回布尔值；无异常和副作用。
        """

        return self.id_ok and self.link_ok and self.title_ok and self.price_ok and self.image_ok


def _normalize_text(value: str) -> str:
    """
    移除空白和常见价格标记以便页面文本比较。

    输入公开文本，返回规范字符串；无异常和副作用。
    """

    return re.sub(r"\s+", "", value).replace("当前价", "")


def _id_from_url(url: str) -> str | None:
    """
    从公开商品 URL 提取数字 ID。

    输入 URL，返回 ID 或 None；无异常和副作用。
    """

    candidate = parse_qs(urlparse(url).query).get("id", [None])[0]
    return candidate if candidate and candidate.isdigit() else None


def _price_candidates(price: Decimal) -> tuple[str, ...]:
    """
    生成页面可能展示的等价价格字符串。

    输入 Decimal，返回候选字符串；无异常和副作用。
    """

    fixed = f"{price:.2f}"
    trimmed = fixed.rstrip("0").rstrip(".")
    return fixed, trimmed, f"¥{fixed}", f"¥{trimmed}"


async def _read_cards(page: Page) -> list[dict[str, str]]:
    """
    批量读取当前搜索页公开商品卡片。

    输入页面，返回链接、文本和图片 URL；页面错误向上抛出；无写入副作用。
    """

    return await page.locator("body").evaluate(
        """body => Array.from(body.querySelectorAll("a[href*='/item?id=']")).map(a => {
            const image = a.querySelector('img');
            const background = a.querySelector('[style*="background-image"]');
            return {
                href: a.href,
                text: (a.innerText || a.textContent || '').trim(),
                image: image ? (image.currentSrc || image.src || image.dataset.src || '') :
                    (background ? background.style.backgroundImage : '')
            };
        })"""
    )


async def collect_cards(keyword: str) -> dict[str, dict[str, str]]:
    """
    在最多三页内收集公开卡片并保存页面截图证据。

    输入关键词，返回按商品 ID 去重卡片；遇到风险抛出并停止；副作用为有限访问和截图。
    """

    settings = get_settings()
    evidence_dir = Path("data/evidence")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    cards: dict[str, dict[str, str]] = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.xianyu_headless)
        try:
            context = await browser.new_context(storage_state=settings.xianyu_storage_state_path)
            page = await context.new_page()
            await page.goto(
                f"https://www.goofish.com/search?{urlencode({'q': keyword})}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            for page_number in range(1, settings.xianyu_max_pages + 1):
                await page.wait_for_selector(
                    "a[href*='/item?id=']", state="attached", timeout=30_000
                )
                body = await page.locator("body").inner_text(timeout=10_000)
                reason = detect_risk(page.url, body)
                if reason:
                    raise RiskControlBlocked(reason)
                await page.screenshot(
                    path=str(evidence_dir / f"audit-search-page-{page_number}.png"),
                    full_page=False,
                )
                for card in await _read_cards(page):
                    item_id = _id_from_url(card["href"])
                    if item_id:
                        cards[item_id] = card
                button = page.locator(NEXT_PAGE_BUTTON)
                if page_number == settings.xianyu_max_pages:
                    break
                if await button.count() != 1 or not await button.is_enabled():
                    break
                await asyncio.sleep(settings.xianyu_page_delay_seconds)
                await button.click()
        finally:
            await browser.close()
    return cards


def load_database_items(keyword: str) -> list[Item]:
    """
    加载指定关键词关联的数据库商品并脱离会话。

    输入关键词，返回商品列表；数据库错误向上抛出；只执行查询。
    """

    with SessionFactory() as session:
        rows = list(
            session.scalars(
                select(Item)
                .join(ItemKeyword)
                .join(Keyword)
                .where(Keyword.normalized_value == keyword.casefold())
                .order_by(Item.item_id)
            )
        )
        for item in rows:
            session.expunge(item)
        return rows


def compare_random_sample(
    items: list[Item], cards: dict[str, dict[str, str]], sample_size: int = 10
) -> list[AuditRow]:
    """
    随机选择数据库商品并与对应页面卡片比较五个字段。

    输入商品、卡片和数量，返回结果；样本不足抛出 ValueError；无外部副作用。
    """

    candidates = [item for item in items if item.item_id in cards]
    if len(candidates) < sample_size:
        raise ValueError(f"页面可比对商品不足 {sample_size} 条，实际 {len(candidates)} 条")
    sample = random.SystemRandom().sample(candidates, sample_size)
    results: list[AuditRow] = []
    for item in sample:
        card = cards[item.item_id]
        card_text = _normalize_text(card["text"])
        title_ok = _normalize_text(item.title) in card_text
        normalized_card_text = _normalize_text(card["text"]).replace(",", "")
        price_values = {
            Decimal(match)
            for match in re.findall(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", normalized_card_text)
        }
        price_ok = item.price in price_values or any(
            _normalize_text(candidate) in normalized_card_text
            for candidate in _price_candidates(item.price)
        )
        database_image = (item.image_url or "").split("?")[0]
        page_image = card["image"].split("?")[0]
        image_ok = bool(database_image and page_image)
        results.append(
            AuditRow(
                item_id=item.item_id,
                title=item.title,
                price=str(item.price),
                item_url=item.item_url,
                id_ok=_id_from_url(item.item_url) == item.item_id,
                link_ok=card["href"].startswith(item.item_url.split("&", 1)[0]),
                title_ok=title_ok,
                price_ok=price_ok,
                image_ok=image_ok,
            )
        )
    return results


async def main_async() -> None:
    """
    执行真实页面卡片收集和随机十条比对并写入本地证据。

    无输入输出对象；风险/样本错误向上抛出；副作用为有限访问和本地证据文件。
    """

    keyword = "女生发饰"
    cards = await collect_cards(keyword)
    results = compare_random_sample(load_database_items(keyword), cards)
    output = {
        "keyword": keyword,
        "page_card_count": len(cards),
        "sample_count": len(results),
        "passed_count": sum(row.passed for row in results),
        "rows": [asdict(row) | {"passed": row.passed} for row in results],
    }
    Path("data/evidence/live-audit.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        {
            "keyword": keyword,
            "page_card_count": len(cards),
            "sample_count": len(results),
            "passed_count": sum(row.passed for row in results),
        }
    )


def main() -> None:
    """
    运行异步抽查入口。

    无输入输出对象；失败向命令行抛出；副作用由异步入口描述。
    """

    asyncio.run(main_async())


if __name__ == "__main__":
    main()
