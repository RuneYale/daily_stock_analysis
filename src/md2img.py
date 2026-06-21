# -*- coding: utf-8 -*-
"""
===================================
Markdown 转图片工具模块
===================================

将 Markdown 转为 PNG 图片（用于不支持 Markdown 的通知渠道）。
支持 wkhtmltoimage (imgkit) 与 markdown-to-file (m2f)，后者对 emoji 支持更好 (Issue #455)。

Security note: imgkit passes HTML to wkhtmltoimage via stdin, not argv, so
command injection from content is not applicable. Output is rasterized to PNG
(no script execution). Input is from system-generated reports, not raw user
input. Risk is considered low for the current use case.
"""

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from src.formatters import markdown_to_html_document

logger = logging.getLogger(__name__)


# 常见 Chrome/Chromium/Edge 可执行文件路径（跨平台，puppeteer-core 不自带 Chromium）
_CHROME_CANDIDATES = [
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    # Linux（GitHub Actions ubuntu 已预装 Chrome，见 actions/setup-python 镜像）
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
]


def _detect_chrome_executable() -> Optional[str]:
    """返回首个存在的 Chrome/Edge 可执行文件路径，找不到则 None。

    m2f 依赖 puppeteer-core，它不会自动下载 Chromium，必须显式指定一个浏览器。
    GitHub Actions ubuntu-latest 预装了 google-chrome；本地通常装了 Chrome 或 Edge。
    """
    for path in _CHROME_CANDIDATES:
        if os.path.isfile(path):
            return path
    # 退回到 PATH 查找（Linux 常见）
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _markdown_to_image_m2f(markdown_text: str) -> Optional[bytes]:
    """Convert Markdown to PNG via markdown-to-file (m2f) CLI. Better emoji support (Issue #455)."""
    m2f_bin = shutil.which("m2f")
    if m2f_bin is None:
        logger.warning(
            "m2f (markdown-to-file) not found in PATH. "
            "Install with: npm i -g markdown-to-file. Fallback to text."
        )
        return None

    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        md_path = os.path.join(temp_dir, "report.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(markdown_text)

        # m2f 依赖 puppeteer-core，需显式指定浏览器可执行文件
        # m2f_bin 来自 shutil.which，Windows 下解析为 m2f.cmd 完整路径，
        # subprocess 带扩展名即可直接执行（无需 shell=True，避免空格路径转义坑）
        cmd = [m2f_bin, md_path, "png", f"outputDirectory={temp_dir}"]
        chrome_path = _detect_chrome_executable()
        if chrome_path:
            cmd.append(f"executablePath={chrome_path}")
        else:
            logger.warning(
                "m2f: 未检测到 Chrome/Chromium/Edge，puppeteer 可能无法启动。"
                "请安装 Chrome 或设置 executablePath。"
            )

        # 中文字体样式表：默认字体栈无中文字形，中文会显示成方框
        # 用跨平台字体栈(微软雅黑/苹方/Noto/文泉驿)覆盖 html/body/标题/表格
        font_css = os.path.join(os.path.dirname(__file__), "md2img_font.css")
        if os.path.isfile(font_css):
            cmd.append(f"styles={font_css}")
        else:
            logger.warning("m2f: 未找到中文字体样式表 %s，中文可能显示为方框", font_css)

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=60,
            check=False,
        )
        png_path = os.path.join(temp_dir, "report.png")
        if result.returncode != 0 or not os.path.isfile(png_path):
            logger.warning(
                "m2f conversion failed: returncode=%s, stderr=%s",
                result.returncode,
                (result.stderr or b"").decode("utf-8", errors="replace")[:200],
            )
            return None

        with open(png_path, "rb") as f:
            return f.read()
    except subprocess.TimeoutExpired:
        logger.warning("m2f conversion timed out (60s)")
        return None
    except Exception as e:
        logger.warning("markdown_to_image (m2f) failed: %s", e)
        return None
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except OSError as e:
                logger.debug("Failed to remove temp dir %s: %s", temp_dir, e)


def _markdown_to_image_wkhtml(markdown_text: str) -> Optional[bytes]:
    """Convert Markdown to PNG via imgkit/wkhtmltoimage."""
    try:
        import imgkit
    except ImportError:
        logger.debug("imgkit not installed, markdown_to_image unavailable")
        return None

    html = markdown_to_html_document(markdown_text)
    try:
        options = {
            "format": "png",
            "encoding": "UTF-8",
            "quiet": "",
        }
        out = imgkit.from_string(html, False, options=options)
        if out and isinstance(out, bytes) and len(out) > 0:
            return out
        logger.warning("imgkit.from_string returned empty or invalid result")
        return None
    except OSError as e:
        if "wkhtmltoimage" in str(e).lower() or "wkhtmltopdf" in str(e).lower():
            logger.debug("wkhtmltopdf/wkhtmltoimage not found: %s", e)
        else:
            logger.warning("imgkit/wkhtmltoimage error: %s", e)
        return None
    except Exception as e:
        logger.warning("markdown_to_image conversion failed: %s", e)
        return None


def markdown_to_image(markdown_text: str, max_chars: int = 15000) -> Optional[bytes]:
    """
    Convert Markdown to PNG image bytes.

    Engine is read from config.md2img_engine: wkhtmltoimage (default) or
    markdown-to-file (better emoji support, Issue #455).

    When conversion fails or dependencies unavailable, returns None so caller
    can fall back to text sending.

    Args:
        markdown_text: Raw Markdown content.
        max_chars: Skip conversion and return None if content exceeds this length
            (avoids huge images). Default 15000.

    Returns:
        PNG bytes, or None if conversion fails or dependencies unavailable.
    """
    if len(markdown_text) > max_chars:
        logger.warning(
            "Markdown content (%d chars) exceeds max_chars (%d), skipping image conversion",
            len(markdown_text),
            max_chars,
        )
        return None

    try:
        from src.config import get_config

        engine = getattr(get_config(), "md2img_engine", "wkhtmltoimage")
    except Exception:
        engine = "wkhtmltoimage"

    if engine == "markdown-to-file":
        return _markdown_to_image_m2f(markdown_text)
    return _markdown_to_image_wkhtml(markdown_text)
