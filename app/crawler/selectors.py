"""
本文件集中定义闲鱼页面选择器和 URL 片段。

它属于 crawler 模块，供页面客户端复用，不包含业务判断或页面操作。
"""

SEARCH_API_FRAGMENT = "mtop.taobao.idlemtopsearch.pc.search"
NEXT_PAGE_BUTTON = (
    "button[class*='search-pagination-arrow-container']:has("
    "[class*='search-pagination-arrow-right'])"
)
LOGIN_URL_FRAGMENTS = ("passport.goofish.com", "mini_login", "/login")
RISK_TEXT_SIGNALS = (
    "验证码",
    "安全验证",
    "访问频繁",
    "账号异常",
    "操作受限",
    "操作频繁",
    "请登录",
    "非法访问",
    "请使用正常浏览器",
)
EXPLICIT_UNAVAILABLE_TEXT_SIGNALS = (
    "宝贝已下架",
    "商品已下架",
    "宝贝已售出",
    "商品已售出",
    "宝贝已被卖掉",
    "商品已被删除",
    "宝贝不存在",
)
DETAIL_PRICE_SELECTORS = (
    "[data-testid='item-price']",
    "[itemprop='price']",
    "main [class*='ItemPrice']",
    "main [class*='item-price']",
    "main [class*='price--']",
)

# 商品详情页图片选择器仅用于读取公开图库，不匹配头像、按钮图标或聊天附件。
# 当前真实详情页不包含 main，公开主图和缩略图使用 CSS Modules 的 fadeInImg 类。
DETAIL_IMAGE_SELECTORS = (
    "img[class*='fadeInImg']",
    "main [class*='gallery'] img",
    "main [class*='Gallery'] img",
    "main [class*='image-list'] img",
    "main [class*='ImageList'] img",
    "main [class*='detail-image'] img",
    "main [class*='DetailImage'] img",
)
