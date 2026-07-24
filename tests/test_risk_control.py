"""
本文件测试认证和风控文本的立即停止分类。

它只使用固定字符串，不访问页面、网络或本地登录态。
"""

from app.crawler.risk_control import detect_risk, detect_risk_response


def test_detects_illegal_access() -> None:
    """
    验证“非法访问”页面被归类为风控阻塞。

    无输入；断言失败抛出 AssertionError；无副作用。
    """

    reason = detect_risk(
        "https://www.goofish.com/search?q=x",
        "非法访问 为了保障您的体验，请使用正常浏览器访问闲鱼",
    )
    assert reason is not None
    assert "风险提示" in reason


def test_detects_http_200_tmd_punish_response() -> None:
    """
    验证闲鱼以 HTTP 200 返回 TMD 风控流程时仍立即阻塞。

    无输入；断言失败抛出 AssertionError；仅分类固定 URL，不访问真实页面。
    """

    reason = detect_risk_response(
        "https://h5api.m.goofish.com/h5/example/1.0/_____tmd_____/punishTextFetch",
        200,
    )

    assert reason == "页面请求进入平台风控流程"
