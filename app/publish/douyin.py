"""抖音发布（CDP 附加到真实 Chrome，操作创作者中心上传页）。

为什么走 CDP 而不是新起一个无头浏览器：
抖音对自动化的检测比小红书更硬（设备指纹、行为图谱、短信验证）。
新起的干净浏览器是"伪装成真人"，附加到日常在用的 Chrome 是"本来就是真人"，
后者在这个平台上存活率高得多。另外窗口摆在屏幕上，出问题能一眼看见，
弹验证码时人就在现场可以顺手点掉。

前置条件（缺一不可）：
1. 用专用 profile 带调试端口启动 Chrome：
   chrome.exe --user-data-dir=<项目>/data/chrome-profile --remote-debugging-port=9222
   注意 Chrome 111+ 如果已有实例在跑，--remote-debugging-port 会被忽略，
   所以必须用独立的 user-data-dir 起一个新实例。
2. 在那个窗口里手动登录一次抖音创作者中心。
3. 保持窗口开着（可以最小化，但不要关）。

**选择器状态：未验证。** 没有真实抖音账号可测，页面结构随时可能改版。
所以这里采用「多候选选择器 + 失败截图 + 明确报错」，
第一次真实使用时需要现场校准 DOUYIN_CREATOR_URL 之后的定位逻辑。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .xhs import PublishResult

logger = logging.getLogger(__name__)

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
# 先走 upload 页，抖音会自动跳转到编辑页
UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"

# 候选选择器：按命中概率排序，逐个尝试。抖音是字节系，semi-design 组件库。
TITLE_SELECTORS = [
    'input[placeholder*="填写作品标题"]',
    'input[placeholder*="标题"]',
    ".semi-input",
]
DESC_SELECTORS = [
    ".zone-container",
    'div[contenteditable="true"]',
    'textarea[placeholder*="添加作品简介"]',
]
PUBLISH_BUTTON_TEXTS = ["发布", "立即发布", "发布作品"]

# 登录态判定：出现这些就是掉登录了
LOGIN_MARKERS = ["扫码登录", "验证码登录", "密码登录", "登录后查看"]


@dataclass
class CdpStatus:
    reachable: bool
    logged_in: bool
    message: str

    def as_dict(self) -> dict:
        return {
            "reachable": self.reachable,
            "logged_in": self.logged_in,
            "message": self.message,
        }


class DouyinPublisher:
    def __init__(self, cdp_url: str = "") -> None:
        self.cdp_url = (cdp_url or DEFAULT_CDP_URL).strip().rstrip("/")

    # ---------- 不依赖 playwright 的轻探测 ----------

    def cdp_reachable(self) -> tuple[bool, str]:
        try:
            # 同样要绕开本机代理，否则 9222 的请求会被 502 掉
            with httpx.Client(trust_env=False, timeout=3.0) as client:
                response = client.get(f"{self.cdp_url}/json/version")
            if response.status_code != 200:
                return False, f"调试端口返回 {response.status_code}"
            info = response.json()
            return True, str(info.get("Browser", "Chrome"))
        except (httpx.HTTPError, ValueError) as error:
            return False, f"连不上 {self.cdp_url}（{error.__class__.__name__}）"

    def status(self) -> CdpStatus:
        reachable, detail = self.cdp_reachable()
        if not reachable:
            return CdpStatus(
                reachable=False,
                logged_in=False,
                message=(
                    f"{detail}。请用专用 profile 启动 Chrome："
                    "chrome.exe --user-data-dir=<项目>/data/chrome-profile "
                    "--remote-debugging-port=9222"
                ),
            )
        try:
            logged_in, message = self._probe_login()
        except Exception as error:  # noqa: BLE001 - 探测失败不该影响 WebUI
            logger.warning("抖音登录态探测失败：%s", error)
            return CdpStatus(True, False, f"{detail} 已连接，但登录态探测失败：{error}")
        return CdpStatus(True, logged_in, message)

    def _connect(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError(
                "没装 playwright。CDP 只需要 Python 包，不需要下载浏览器："
                "pip install playwright"
            ) from error
        return sync_playwright()

    def _probe_login(self) -> tuple[bool, str]:
        """开一个标签页看创作者中心，靠页面文字判断登录态。"""
        with self._connect() as playwright:
            browser = playwright.chromium.connect_over_cdp(self.cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                body = page.inner_text("body")[:4000]
                if any(marker in body for marker in LOGIN_MARKERS):
                    return False, "已连接 Chrome，但抖音未登录，请在那个窗口里登录一次"
                return True, "已连接 Chrome，创作者中心可访问"
            finally:
                page.close()

    # ---------- 发布 ----------

    def publish_video(
        self,
        title: str,
        content: str,
        video: Path,
        tags: list[str] | None = None,
        screenshot_dir: Path | None = None,
        stem: str = "douyin",
    ) -> PublishResult:
        reachable, detail = self.cdp_reachable()
        if not reachable:
            return PublishResult(False, f"{detail}（抖音需要先启动带调试端口的 Chrome）")

        tags = tags or []
        shot_path = (screenshot_dir or Path(".")) / f"{stem}_douyin.png"
        try:
            return self._publish(reachable, title, content, video, tags, shot_path)
        except Exception as error:  # noqa: BLE001 - 页面结构变化的各种可能都收敛成一句人话
            logger.exception("抖音发布异常")
            return PublishResult(
                False,
                f"抖音发布失败：{error}。页面截图已存到 {shot_path}"
                if shot_path.exists()
                else f"抖音发布失败：{error}",
            )

    def _publish(self, reachable, title, content, video, tags, shot_path) -> PublishResult:
        with self._connect() as playwright:
            browser = playwright.chromium.connect_over_cdp(self.cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2500)
                body = page.inner_text("body")[:4000]
                if any(marker in body for marker in LOGIN_MARKERS):
                    return PublishResult(
                        False, "抖音未登录，请先在专用 Chrome 窗口里登录创作者中心"
                    )

                # 上传：抖音的接受区就是页面里的 file input，不需要点开弹窗
                file_input = page.locator('input[type="file"]').first
                file_input.set_input_files(str(video))
                # 视频处理需要时间，等编辑页出现标题框
                page.wait_for_timeout(8000)
                if not self._try_fill(page, TITLE_SELECTORS, title):
                    page.screenshot(path=str(shot_path))
                    return PublishResult(
                        False, "上传后没找到标题输入框，页面结构可能改版了（已截图）"
                    )
                self._try_fill(page, DESC_SELECTORS, self._compose_desc(content, tags))

                if not self._click_publish(page):
                    page.screenshot(path=str(shot_path))
                    return PublishResult(False, "没找到发布按钮（已截图）")

                page.wait_for_timeout(4000)
                page.screenshot(path=str(shot_path))
                body = page.inner_text("body")[:4000]
                if "成功" in body or "审核" in body:
                    return PublishResult(
                        True,
                        "已提交抖音发布（抖音侧还有一次审核，期间仅自己可见）",
                        raw=body[:500],
                        uncertain=True,
                    )
                return PublishResult(
                    True,
                    "已点击发布，但没有读到明确结果，请到创作者中心确认",
                    raw=body[:500],
                    uncertain=True,
                )
            finally:
                # 只关自己开的标签页，不关浏览器——那是用户正在用的窗口
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _compose_desc(content: str, tags: list[str]) -> str:
        tag_text = " ".join(f"#{tag}" for tag in tags if tag)
        return f"{content}\n{tag_text}".strip() if tag_text else content

    @staticmethod
    def _try_fill(page, selectors: list[str], value: str) -> bool:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() == 0:
                    continue
                locator.click(timeout=3000)
                # contenteditable 用 type，input 用 fill
                try:
                    locator.fill(value, timeout=3000)
                except Exception:  # noqa: BLE001
                    locator.type(value, delay=30, timeout=8000)
                time.sleep(0.3)
                return True
            except Exception:  # noqa: BLE001 - 继续尝试下一个候选选择器
                continue
        return False

    @staticmethod
    def _click_publish(page) -> bool:
        for text in PUBLISH_BUTTON_TEXTS:
            button = page.get_by_role("button", name=text, exact=False).first
            try:
                if button.count() == 0:
                    continue
                button.click(timeout=5000)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False


def chrome_launch_hint(profile_dir: Path) -> str:
    """给 WebUI 显示的一行命令，省得朋友去翻文档。"""
    return (
        'chrome.exe --user-data-dir="'
        + str(profile_dir)
        + '" --remote-debugging-port=9222'
    )
