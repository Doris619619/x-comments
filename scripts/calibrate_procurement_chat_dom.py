"""
本文件对单个白名单闲鱼商品执行只读聊天 DOM 标定，并输出脱敏结构夹具。

它只打开商品页和“我想要/联系卖家”聊天入口，不填写输入框、不点击发送、不读取或输出
聊天正文、Cookie、账号值或卖家身份值。输出只保留控件标签、稳定属性名、脱敏类名和
身份值哈希，供人工确认唯一选择器后更新适配器。
"""

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from playwright.async_api import ElementHandle, Page, async_playwright

from app.core.config import get_settings

SAFE_CONTROL_LABELS = {
    "我想要",
    "联系卖家",
    "聊一聊",
    "发送",
}
SENSITIVE_ATTRIBUTE_NAME = re.compile(
    r"(?:item|seller|user|account|message|chat).*(?:id|key)|(?:id|key).*(?:item|seller|user|account|message|chat)",
    re.I,
)
STABLE_CLASS_TOKEN = re.compile(r"^[A-Za-z_-][A-Za-z0-9_-]{0,79}$")


def _sha256(value: str) -> str:
    """对可能含身份的数据计算不可逆摘要；输入字符串，返回十六进制摘要，无外部副作用。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_class_tokens(value: str | None) -> list[str]:
    """保留不含长数字的短类名；输入 class 字符串，返回最多八项脱敏标记。"""

    if not value:
        return []
    return [
        token
        for token in value.split()
        if STABLE_CLASS_TOKEN.fullmatch(token) and not re.search(r"\d{4,}", token)
    ][:8]


async def _visible(element: ElementHandle) -> bool:
    """读取元素可见性；元素分离时返回 False，不执行页面写入。"""

    try:
        return await element.is_visible()
    except Exception:
        return False


async def _describe_control(element: ElementHandle) -> dict[str, Any]:
    """把一个可交互控件转换成不含自由正文和身份值的结构描述。"""

    payload = await element.evaluate(
        """node => ({
          tag: node.tagName.toLowerCase(),
          role: node.getAttribute('role'),
          type: node.getAttribute('type'),
          ariaLabel: node.getAttribute('aria-label'),
          placeholder: node.getAttribute('placeholder'),
          className: typeof node.className === 'string' ? node.className : '',
          text: (node.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 80),
          attributes: Array.from(node.attributes || []).map(
            attribute => [attribute.name, attribute.value]
          )
        })"""
    )
    text = payload.get("text") or ""
    label = next(
        (allowed for allowed in SAFE_CONTROL_LABELS if allowed in text and len(text) <= 30),
        None,
    )
    safe_attributes: list[dict[str, str]] = []
    for name, value in payload.get("attributes", []):
        if SENSITIVE_ATTRIBUTE_NAME.search(name):
            safe_attributes.append({"name": name, "value_sha256": _sha256(value)})
    return {
        "tag": payload.get("tag"),
        "role": payload.get("role"),
        "type": payload.get("type"),
        "aria_label": payload.get("ariaLabel"),
        "placeholder": payload.get("placeholder"),
        "allowed_label": label,
        "class_tokens": _safe_class_tokens(payload.get("className")),
        "identity_attributes": safe_attributes,
    }


async def _collect_controls(page: Page) -> list[dict[str, Any]]:
    """按 DOM 顺序收集可见按钮、链接和输入控件的脱敏结构。"""

    elements = await page.query_selector_all(
        "button, a, [role='button'], textarea, input, [contenteditable='true']"
    )
    result: list[dict[str, Any]] = []
    for element in elements:
        if await _visible(element):
            result.append(await _describe_control(element))
    return result[:200]


async def _collect_identity_attribute_shapes(page: Page) -> list[dict[str, Any]]:
    """枚举身份相关属性名与值哈希，不输出真实商品、卖家或账号标识。"""

    return await page.evaluate(
        """() => {
          const identityWords = '(?:item|seller|user|account|message|chat)';
          const pattern = new RegExp(
            `${identityWords}.*(?:id|key)|(?:id|key).*${identityWords}`,
            'i'
          );
          const digestPlaceholder = value => value;
          return Array.from(document.querySelectorAll('*')).flatMap(node => {
            const matches = Array.from(node.attributes || [])
              .filter(attribute => pattern.test(attribute.name))
              .map(attribute => ({
                name: attribute.name,
                rawValueForLocalHash: digestPlaceholder(attribute.value),
                tag: node.tagName.toLowerCase(),
                className: typeof node.className === 'string' ? node.className : ''
              }));
            return matches;
          }).slice(0, 300);
        }"""
    )


async def _collect_chat_structure_shapes(page: Page) -> list[dict[str, Any]]:
    """收集聊天相关类名节点的标签和属性名，不读取任何节点正文。"""

    rows = await page.evaluate(
        """() => Array.from(document.querySelectorAll('*')).flatMap(node => {
          const className = typeof node.className === 'string' ? node.className : '';
          if (!/(?:message|chat|msg|conversation|session|dialog|textarea|send)/i.test(className)) {
            return [];
          }
          return [{
            tag: node.tagName.toLowerCase(),
            className,
            attributeNames: Array.from(node.attributes || []).map(attribute => attribute.name),
            childTags: Array.from(node.children || [])
              .slice(0, 8)
              .map(child => child.tagName.toLowerCase())
          }];
        }).slice(0, 300)"""
    )
    sanitized = [
        {
            "tag": row.get("tag"),
            "class_tokens": _safe_class_tokens(row.get("className")),
            "attribute_names": row.get("attributeNames", []),
            "child_tags": row.get("childTags", []),
        }
        for row in rows
    ]
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in sanitized:
        key = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


async def _collect_account_match_shapes(
    page: Page,
    expected_account_id: str,
) -> list[dict[str, Any]]:
    """
    在 DOM 与浏览器存储中定位预期账号 ID 的结构位置。

    输入预期账号 ID；只返回存储区、键名、JSON 路径或 DOM 属性名，不返回命中值。
    该函数用于把账号身份校验收敛到经过标定的稳定路径，不修改页面或登录态。
    """

    if not expected_account_id:
        return []
    return await page.evaluate(
        """expected => {
          const matches = [];
          const recordJsonMatches = (value, source, key, path = '$', depth = 0) => {
            if (depth > 8) return;
            if (typeof value === 'string' || typeof value === 'number') {
              if (String(value) === expected) {
                matches.push({kind: 'storage', source, key, path});
              }
              return;
            }
            if (Array.isArray(value)) {
              value.forEach((child, index) =>
                recordJsonMatches(child, source, key, `${path}[${index}]`, depth + 1)
              );
              return;
            }
            if (value && typeof value === 'object') {
              Object.entries(value).forEach(([name, child]) =>
                recordJsonMatches(child, source, key, `${path}.${name}`, depth + 1)
              );
            }
          };
          const scanStorage = (storage, source) => {
            for (let index = 0; index < storage.length; index += 1) {
              const key = storage.key(index);
              if (!key) continue;
              const raw = storage.getItem(key);
              if (raw === expected) {
                matches.push({kind: 'storage', source, key, path: '$'});
              }
              if (!raw) continue;
              try {
                recordJsonMatches(JSON.parse(raw), source, key);
              } catch {
                // 非 JSON 存储项只做严格全值比较，禁止模糊搜索误认账号。
              }
            }
          };
          scanStorage(window.localStorage, 'localStorage');
          scanStorage(window.sessionStorage, 'sessionStorage');
          for (const node of document.querySelectorAll('*')) {
            for (const attribute of Array.from(node.attributes || [])) {
              let matched = attribute.value === expected;
              if (!matched && /^(?:href|src|action)$/i.test(attribute.name)) {
                try {
                  const parsed = new URL(attribute.value, location.href);
                  matched = Array.from(parsed.searchParams.values()).some(
                    value => value === expected
                  );
                } catch {
                  matched = false;
                }
              }
              if (matched) {
                matches.push({
                  kind: 'dom',
                  tag: node.tagName.toLowerCase(),
                  attribute: attribute.name,
                  className: typeof node.className === 'string' ? node.className : ''
                });
              }
            }
          }
          return matches.slice(0, 100);
        }""",
        expected_account_id,
    )


async def _collect_message_layout_shapes(page: Page) -> dict[str, Any]:
    """
    读取消息列表布局方向与相关 CSS 选择器，不读取聊天正文。

    返回消息列表的 ``flex-direction``、子节点数量和本页可访问样式表中与聊天方向相关的
    选择器。跨域样式表会被浏览器拒绝读取并自动跳过。
    """

    return await page.evaluate(
        """() => {
          const messageList = document.querySelector("div[class*='message-list-reverse--']");
          const selectors = [];
          for (const sheet of Array.from(document.styleSheets || [])) {
            let rules = [];
            try {
              rules = Array.from(sheet.cssRules || []);
            } catch {
              continue;
            }
            for (const rule of rules) {
              const selector = rule.selectorText || '';
              const relevant = new RegExp(
                '(?:message-row|message-content|msg-text-(?:left|right)|'
                  + 'message-list-reverse)',
                'i'
              );
              if (relevant.test(selector)) {
                selectors.push(selector.slice(0, 500));
              }
            }
          }
          return {
            messageListFound: Boolean(messageList),
            flexDirection: messageList ? getComputedStyle(messageList).flexDirection : null,
            childCount: messageList ? messageList.children.length : 0,
            selectors: Array.from(new Set(selectors)).slice(0, 100)
          };
        }"""
    )


async def _inspect_account_panel(
    page: Page,
    expected_account_id: str,
) -> dict[str, Any]:
    """
    只读展开聊天页账号菜单并检查账号标识出现位置。

    仅允许点击唯一的账号按钮；返回脱敏控件和严格账号匹配位置，不选择菜单项、不退出登录，
    也不返回账号名称、头像地址或标识值。
    """

    selector = "button[class*='xianyu-account--']"
    candidates = page.locator(selector)
    visible = [
        candidates.nth(index)
        for index in range(await candidates.count())
        if await candidates.nth(index).is_visible()
    ]
    if len(visible) != 1:
        return {"opened": False, "reason": "account_button_not_unique"}
    await visible[0].click(timeout=5_000)
    await page.wait_for_timeout(500)
    return {
        "opened": True,
        "account_match_shapes": await _collect_account_match_shapes(
            page,
            expected_account_id,
        ),
        "controls": await _collect_controls(page),
    }


def _sanitized_query_shape(url: str) -> list[dict[str, str]]:
    """返回 URL 查询参数名和值哈希，不泄露聊天、卖家或账号标识。"""

    return [
        {"name": name, "value_sha256": _sha256(value)}
        for name, value in parse_qsl(urlparse(url).query, keep_blank_values=True)
    ]


async def _sanitize_identity_shapes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """在 Python 侧哈希浏览器返回的临时属性值，并清除临时字段。"""

    sanitized: list[dict[str, Any]] = []
    for row in rows:
        raw_value = str(row.pop("rawValueForLocalHash", ""))
        sanitized.append(
            {
                "name": row.get("name"),
                "value_sha256": _sha256(raw_value),
                "tag": row.get("tag"),
                "class_tokens": _safe_class_tokens(row.get("className")),
            }
        )
    return sanitized


async def _click_unique_chat_entry(page: Page) -> tuple[str | None, Page]:
    """仅点击唯一安全聊天入口，并返回可能新打开的聊天页；不触碰交易控件。"""

    candidates: list[tuple[ElementHandle, str]] = []
    for element in await page.query_selector_all("button, a, [role='button']"):
        if not await _visible(element):
            continue
        label = " ".join((await element.inner_text()).split())
        if label in {"我想要", "联系卖家", "聊一聊"}:
            candidates.append((element, label))
    if len(candidates) != 1:
        return None, page
    element, label = candidates[0]
    previous_pages = set(page.context.pages)
    await element.click(timeout=5_000)
    await page.wait_for_timeout(1_000)
    new_pages = [candidate for candidate in page.context.pages if candidate not in previous_pages]
    active_page = new_pages[-1] if new_pages else page
    await active_page.wait_for_load_state("domcontentloaded", timeout=10_000)
    await active_page.wait_for_timeout(5_000)
    return label, active_page


async def calibrate(item_id: str, output_path: Path | None) -> dict[str, Any]:
    """打开白名单商品并输出发送前、聊天打开后的脱敏 DOM 结构，不执行消息发送。"""

    if not item_id.isdigit() or len(item_id) > 64:
        raise ValueError("item_id 必须是最多 64 位数字")
    settings = get_settings()
    storage_state = Path(settings.xianyu_storage_state_path)
    if not storage_state.is_file():
        raise FileNotFoundError("闲鱼登录态文件不存在")

    url = f"https://www.goofish.com/item?id={item_id}"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.xianyu_headless)
        try:
            context = await browser.new_context(storage_state=str(storage_state))
            page = await context.new_page()
            blocked_statuses: list[int] = []

            def observe_response(response: Any) -> None:
                """只记录 403/429 状态码，不读取响应正文。"""

                if response.status in {403, 429}:
                    blocked_statuses.append(response.status)

            page.on("response", observe_response)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(8_000)
            page_diagnostics = await page.evaluate(
                """() => {
                  const text = (document.body?.innerText || '').replace(/\\s+/g, ' ');
                  return {
                    title: document.title,
                    elementCount: document.querySelectorAll('*').length,
                    bodyTextLength: text.length,
                    signals: {
                      login: /登录|登入|扫码登录/.test(text),
                      captcha: /验证码|安全验证|人机验证|captcha/i.test(text),
                      accessDenied: /访问受限|403|请求频繁/.test(text)
                    }
                  };
                }"""
            )
            before_controls = await _collect_controls(page)
            before_shapes = await _sanitize_identity_shapes(
                await _collect_identity_attribute_shapes(page)
            )
            opened_by, chat_page = await _click_unique_chat_entry(page)
            after_controls = await _collect_controls(chat_page) if opened_by else []
            after_shapes = (
                await _sanitize_identity_shapes(await _collect_identity_attribute_shapes(chat_page))
                if opened_by
                else []
            )
            after_structure = await _collect_chat_structure_shapes(chat_page) if opened_by else []
            account_matches = (
                await _collect_account_match_shapes(
                    chat_page,
                    settings.xianyu_expected_account_id,
                )
                if opened_by
                else []
            )
            cookie_matches: list[dict[str, str]] = []
            account_cookie_candidates: list[dict[str, str]] = []
            if opened_by and settings.xianyu_expected_account_id:
                # Cookie 仅做严格全值或 URL 解码后的全值比较；结果只保留 Cookie 名。
                for cookie in await context.cookies():
                    raw_value = str(cookie.get("value") or "")
                    if raw_value == settings.xianyu_expected_account_id or (
                        unquote(raw_value) == settings.xianyu_expected_account_id
                    ):
                        cookie_matches.append(
                            {"kind": "cookie", "name": str(cookie.get("name") or "")}
                        )
            if opened_by:
                # 淘宝系 ``unb`` 是稳定用户数字标识；这里只输出摘要用于人工配置核对。
                for cookie in await context.cookies():
                    cookie_name = str(cookie.get("name") or "")
                    if cookie_name.casefold() in {"unb", "tracknick"}:
                        account_cookie_candidates.append(
                            {
                                "name": cookie_name,
                                "value_sha256": _sha256(unquote(str(cookie.get("value") or ""))),
                            }
                        )
            message_layout = await _collect_message_layout_shapes(chat_page) if opened_by else {}
            account_panel = (
                await _inspect_account_panel(
                    chat_page,
                    settings.xianyu_expected_account_id or "",
                )
                if opened_by
                else {}
            )
            if account_panel.get("controls"):
                account_panel["controls"] = [
                    control
                    for control in account_panel["controls"]
                    if control["allowed_label"] or control["identity_attributes"]
                ]
            if account_panel.get("account_match_shapes"):
                account_panel["account_match_shapes"] = [
                    {
                        **match,
                        "class_tokens": _safe_class_tokens(str(match.pop("className", ""))),
                    }
                    for match in account_panel["account_match_shapes"]
                ]
            fixture = {
                "fixture_version": 1,
                "navigation": {
                    "host": page.url.split("/", 3)[2] if page.url.startswith("https://") else "",
                    "status": response.status if response is not None else None,
                    "blocked_statuses": sorted(set(blocked_statuses)),
                    "item_id_sha256": _sha256(item_id),
                },
                "chat_entry_opened_by": opened_by,
                "chat_navigation": {
                    "host": (
                        chat_page.url.split("/", 3)[2]
                        if chat_page.url.startswith("https://")
                        else ""
                    ),
                    "path": (
                        "/" + chat_page.url.split("/", 3)[3].split("?", 1)[0]
                        if chat_page.url.startswith("https://")
                        and len(chat_page.url.split("/", 3)) > 3
                        else ""
                    ),
                    "title": await chat_page.title() if opened_by else None,
                    "query_parameters": _sanitized_query_shape(chat_page.url),
                },
                "page_diagnostics": page_diagnostics,
                "before": {
                    "controls": before_controls,
                    "identity_attribute_shapes": before_shapes,
                },
                "after": {
                    "controls": after_controls,
                    "identity_attribute_shapes": after_shapes,
                    "chat_structure_shapes": after_structure,
                    "account_match_shapes": [
                        {
                            **match,
                            "class_tokens": _safe_class_tokens(str(match.pop("className", ""))),
                        }
                        for match in account_matches
                    ]
                    + cookie_matches,
                    "account_cookie_candidates": account_cookie_candidates,
                    "message_layout": message_layout,
                    "account_panel": account_panel,
                },
            }
            if output_path is not None:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(
                    json.dumps(fixture, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            return fixture
        finally:
            await browser.close()


def main() -> None:
    """解析命令行并执行一次只读标定；失败返回非零状态。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--item-id", required=True)
    parser.add_argument("--output")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(calibrate(args.item_id, Path(args.output) if args.output else None))
    if not args.quiet:
        printable = result
        if args.summary:
            printable = {
                "navigation": result["navigation"],
                "chat_entry_opened_by": result["chat_entry_opened_by"],
                "chat_navigation": result["chat_navigation"],
                "page_diagnostics": result["page_diagnostics"],
                "after_controls": [
                    control
                    for control in result["after"]["controls"]
                    if control["tag"] != "a" or control["allowed_label"]
                ],
                "after_identity_attribute_shapes": result["after"]["identity_attribute_shapes"],
                "after_chat_structure_shapes": result["after"]["chat_structure_shapes"],
                "after_account_match_shapes": result["after"]["account_match_shapes"],
                "after_message_layout": result["after"]["message_layout"],
                "after_account_cookie_candidates": result["after"]["account_cookie_candidates"],
                "after_account_panel": result["after"]["account_panel"],
            }
        print(json.dumps(printable, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
