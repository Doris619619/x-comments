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
