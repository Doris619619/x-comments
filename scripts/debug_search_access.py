"""
本文件用于诊断一次闲鱼搜索页面为何未产生预期响应。

它只记录公开 URL 路径、状态码、页面标题和截图，不输出 Cookie、请求头或登录态内容。
"""

import asyncio
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from playwright.async_api import async_playwright

from app.core.config import get_settings


async def inspect(keyword: str) -> dict[str, object]:
    """
    执行一次有限页面访问并返回脱敏诊断信息。

    输入关键词；返回公开页面元数据；导航错误向上抛出；副作用为一次页面访问和截图。
    """

    settings = get_settings()
    observed: list[dict[str, object]] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.xianyu_headless)
        try:
            context = await browser.new_context(storage_state=settings.xianyu_storage_state_path)
            page = await context.new_page()

            def record_response(response: object) -> None:
                """记录包含 search/mtop 的公开响应路径和状态，不读取正文。"""

                url = str(getattr(response, "url", ""))
                if "search" in url.casefold() or "mtop" in url.casefold():
                    parts = urlsplit(url)
                    observed.append(
                        {"host": parts.hostname, "path": parts.path, "status": response.status}
                    )

            page.on("response", record_response)
            await page.goto(
                f"https://www.goofish.com/search?{urlencode({'q': keyword})}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            await page.wait_for_timeout(10_000)
            Path("data/debug").mkdir(parents=True, exist_ok=True)
            await page.screenshot(path="data/debug/search-timeout.png", full_page=False)
            body = await page.locator("body").inner_text(timeout=10_000)
            return {
                "url": page.url,
                "title": await page.title(),
                "signals": [
                    signal
                    for signal in (
                        "登录",
                        "验证码",
                        "安全验证",
                        "访问频繁",
                        "非法访问",
                        "请使用正常浏览器",
                        "加载中...",
                    )
                    if signal in body
                ],
                "observed": observed[-30:],
            }
        finally:
            await browser.close()


def main() -> None:
    """
    运行一次固定关键词诊断并打印脱敏结果。

    无输入输出对象；失败向命令行抛出；副作用为一次有限访问。
    """

    print(asyncio.run(inspect("女生发饰")))


if __name__ == "__main__":
    main()
