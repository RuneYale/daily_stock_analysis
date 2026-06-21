# -*- coding: utf-8 -*-
"""
Telegram 发送提醒服务

职责：
1. 通过 Telegram Bot API 发送 文本消息
2. 通过 Telegram Bot API 发送 图片消息
"""
import logging
from typing import Optional
import requests
import time
import re

from src.config import Config


logger = logging.getLogger(__name__)


class TelegramSender:

    def __init__(self, config: Config):
        """
        初始化 Telegram 配置

        Args:
            config: 配置对象
        """
        self._telegram_config = {
            'bot_token': getattr(config, 'telegram_bot_token', None),
            'chat_id': getattr(config, 'telegram_chat_id', None),
            'message_thread_id': getattr(config, 'telegram_message_thread_id', None),
        }

    def _is_telegram_configured(self) -> bool:
        """检查 Telegram 配置是否完整"""
        return bool(self._telegram_config['bot_token'] and self._telegram_config['chat_id'])

    def send_to_telegram(
        self,
        content: str,
        *,
        chat_id: Optional[str] = None,
        message_thread_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """
        推送消息到 Telegram 机器人

        Telegram Bot API 格式：
        POST https://api.telegram.org/bot<token>/sendMessage
        {
            "chat_id": "xxx",
            "text": "消息内容",
            "parse_mode": "Markdown"
        }

        Args:
            content: 消息内容（Markdown 格式）

        Returns:
            是否发送成功
        """
        target_chat_id = chat_id if chat_id is not None else self._telegram_config.get("chat_id")
        target_message_thread_id = (
            message_thread_id
            if message_thread_id is not None
            else self._telegram_config.get("message_thread_id")
        )

        if not (self._telegram_config["bot_token"] and target_chat_id):
            logger.warning("Telegram 配置不完整，跳过推送")
            return False

        bot_token = self._telegram_config['bot_token']
        chat_id = target_chat_id
        message_thread_id = target_message_thread_id

        try:
            # Telegram API 端点
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

            # Telegram 消息最大长度 4096 字符
            max_length = 4096

            if len(content) <= max_length:
                # 单条消息发送
                return self._send_telegram_message(api_url, chat_id, content, message_thread_id, timeout_seconds=timeout_seconds)
            else:
                # 分段发送长消息
                return self._send_telegram_chunked(api_url, chat_id, content, max_length, message_thread_id, timeout_seconds=timeout_seconds)

        except Exception as e:
            logger.error(f"发送 Telegram 消息失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return False

    def _send_telegram_message(
        self,
        api_url: str,
        chat_id: str,
        text: str,
        message_thread_id: Optional[str] = None,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """Send a single Telegram message with exponential backoff retry (Fixes #287)"""
        # Convert Markdown to Telegram HTML (tables -> <pre>, headings -> <b>, etc.)
        telegram_text = self._convert_to_telegram_html(text)

        payload = {
            "chat_id": chat_id,
            "text": telegram_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }

        if message_thread_id:
            payload['message_thread_id'] = message_thread_id

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(api_url, json=payload, timeout=timeout_seconds or 10)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt < max_retries:
                    delay = 2 ** attempt  # 2s, 4s
                    logger.warning(f"Telegram request failed (attempt {attempt}/{max_retries}): {e}, "
                                   f"retrying in {delay}s...")
                    time.sleep(delay)
                    continue
                else:
                    logger.error(f"Telegram request failed after {max_retries} attempts: {e}")
                    return False

            if response.status_code == 200:
                result = response.json()
                if result.get('ok'):
                    logger.info("Telegram 消息发送成功")
                    return True
                else:
                    error_desc = result.get('description', '未知错误')
                    logger.error(f"Telegram 返回错误: {error_desc}")

                    # If Markdown parsing failed, fall back to plain text
                    if self._should_fallback_to_plain_text(error_desc=error_desc):
                        if self._send_plain_text_fallback(api_url, payload, text, timeout_seconds=timeout_seconds):
                            return True

                    return False
            elif response.status_code == 429:
                # Rate limited — respect Retry-After header
                retry_after = int(response.headers.get('Retry-After', 2 ** attempt))
                if attempt < max_retries:
                    logger.warning(f"Telegram rate limited, retrying in {retry_after}s "
                                   f"(attempt {attempt}/{max_retries})...")
                    time.sleep(retry_after)
                    continue
                else:
                    logger.error(f"Telegram rate limited after {max_retries} attempts")
                    return False
            else:
                if attempt < max_retries and response.status_code >= 500:
                    delay = 2 ** attempt
                    logger.warning(f"Telegram server error HTTP {response.status_code} "
                                   f"(attempt {attempt}/{max_retries}), retrying in {delay}s...")
                    time.sleep(delay)
                    continue
                if self._should_fallback_to_plain_text(response_text=response.text):
                    if self._send_plain_text_fallback(api_url, payload, text, timeout_seconds=timeout_seconds):
                        return True
                logger.error(f"Telegram 请求失败: HTTP {response.status_code}")
                logger.error(f"响应内容: {response.text}")
                return False

        return False

    @staticmethod
    def _should_fallback_to_plain_text(error_desc: str = "", response_text: str = "") -> bool:
        """Detect Telegram Markdown parsing failures that should retry as plain text."""
        haystack = f"{error_desc}\n{response_text}".lower()
        markers = (
            "can't parse entities",
            "can't parse entity",
            "can't find end of the entity",
            "parse entities",
            "parse_mode",
            "markdown",
        )
        return any(marker in haystack for marker in markers)

    def _send_plain_text_fallback(
        self,
        api_url: str,
        payload: dict,
        text: str,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """Retry Telegram send without parse_mode when Markdown parsing fails."""
        logger.info("Telegram Markdown 解析失败，尝试使用纯文本格式重新发送...")
        plain_payload = dict(payload)
        plain_payload.pop('parse_mode', None)
        plain_payload['text'] = text

        try:
            response = requests.post(api_url, json=plain_payload, timeout=timeout_seconds or 10)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logger.error(f"Telegram plain-text fallback failed: {e}")
            return False

        if response.status_code == 200:
            try:
                result = response.json()
            except ValueError:
                logger.error("Telegram 纯文本回退失败: 响应不是有效 JSON")
                logger.error(f"响应内容: {response.text}")
                return False

            if result.get('ok'):
                logger.info("Telegram 消息发送成功（纯文本）")
                return True

            logger.error("Telegram 纯文本回退失败: Telegram API 返回 ok=false")
            logger.error(f"响应内容: {response.text}")
            return False

        logger.error(f"Telegram 纯文本回退失败: HTTP {response.status_code}")
        logger.error(f"响应内容: {response.text}")
        return False

    def _send_telegram_chunked(
        self,
        api_url: str,
        chat_id: str,
        content: str,
        max_length: int,
        message_thread_id: Optional[str] = None,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """分段发送长 Telegram 消息"""
        # 按段落分割
        sections = content.split("\n---\n")

        current_chunk = []
        current_length = 0
        all_success = True
        chunk_index = 1

        for section in sections:
            section_length = len(section) + 5  # +5 for "\n---\n"

            if current_length + section_length > max_length:
                # 发送当前块
                if current_chunk:
                    chunk_content = "\n---\n".join(current_chunk)
                    logger.info(f"发送 Telegram 消息块 {chunk_index}...")
                    if not self._send_telegram_message(api_url, chat_id, chunk_content, message_thread_id, timeout_seconds=timeout_seconds):
                        all_success = False
                    chunk_index += 1

                # 重置
                current_chunk = [section]
                current_length = section_length
            else:
                current_chunk.append(section)
                current_length += section_length

        # 发送最后一块
        if current_chunk:
            chunk_content = "\n---\n".join(current_chunk)
            logger.info(f"发送 Telegram 消息块 {chunk_index}...")
            if not self._send_telegram_message(api_url, chat_id, chunk_content, message_thread_id, timeout_seconds=timeout_seconds):
                all_success = False

        return all_success

    def _send_telegram_photo(self, image_bytes: bytes) -> bool:
        """Send image via Telegram sendPhoto API (Issue #289)."""
        if not self._is_telegram_configured():
            return False
        bot_token = self._telegram_config['bot_token']
        chat_id = self._telegram_config['chat_id']
        message_thread_id = self._telegram_config.get('message_thread_id')
        api_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        try:
            data = {"chat_id": chat_id}
            if message_thread_id:
                data['message_thread_id'] = message_thread_id
            files = {"photo": ("report.png", image_bytes, "image/png")}
            response = requests.post(api_url, data=data, files=files, timeout=30)
            if response.status_code == 200 and response.json().get('ok'):
                logger.info("Telegram 图片发送成功")
                return True
            logger.error("Telegram 图片发送失败: %s", response.text[:200])
            return False
        except Exception as e:
            logger.error("Telegram 图片发送异常: %s", e)
            return False

    def _convert_to_telegram_html(self, text: str) -> str:
        """
        将标准 Markdown 转换为 Telegram HTML 格式。

        Telegram HTML 支持的标签：<b> <i> <u> <s> <code> <pre> <a>
        不支持 <h1-6>、<table>、<blockquote>、<ul> 等。

        转换策略：
        - Markdown 表格 -> <pre> 块（等宽字体，保留竖线对齐，整齐可读）
        - # 标题 -> <b> 加粗 + 换行
        - **bold** -> <b>bold</b>，*italic*/_italic_ -> <i>
        - > 引用 -> › 前缀
        - [text](url) -> <a href="url">text</a>
        - 其余 HTML 特殊字符 < > & 转义
        """
        import html as _html

        lines = text.split("\n")
        out_lines: list = []
        i = 0
        n = len(lines)
        # 表格 <pre> 块用占位符保护，避免后续 _html.escape 把 < > 转义破坏标签
        pre_blocks: list = []

        # 先把表格段抽出来单独处理（连续的 | ... | 行 + 紧跟的分隔行 |---|---|）
        while i < n:
            line = lines[i]
            # 检测表格起始：行内含 | 且下一行是分隔行 |---|---|
            if "|" in line and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]) and "-" in lines[i + 1]:
                # 收集整个表格块
                table_block = [line, lines[i + 1]]
                j = i + 2
                while j < n and "|" in lines[j] and lines[j].strip():
                    table_block.append(lines[j])
                    j += 1
                # 渲染成 <pre> 对齐文本，用占位符保护（内部已转义）
                pre_html = self._render_table_as_pre(table_block)
                pre_blocks.append(pre_html)
                out_lines.append(f"\x00PRE{len(pre_blocks) - 1}\x00")
                i = j
                continue
            out_lines.append(line)
            i += 1

        result = "\n".join(out_lines)

        # 1) 先保护 Markdown 链接 [text](url)，避免被后续转义破坏
        links: list = []

        def _save_link(m):
            links.append((m.group(1), m.group(2)))
            return f"\x00LINK{len(links) - 1}\x00"

        result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _save_link, result)

        # 2) 转义 HTML 特殊字符
        result = _html.escape(result, quote=False)

        # 3) 行内格式：**bold** -> <b>，*italic* / _italic_ -> <i>，`code` -> <code>
        #    注意转义后 ** 仍是 **，* 仍是 *，` 仍是 `
        result = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", result)
        result = re.sub(r"(?<!\w)[*_]([^*_\n]+?)[*_](?!\w)", r"<i>\1</i>", result)
        result = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", result)

        # 4) 还原表格 <pre> 块（占位符 -> 已转义的 pre HTML）
        result = re.sub(r"\x00PRE(\d+)\x00", lambda m: pre_blocks[int(m.group(1))], result)

        # 4) 标题 # -> <b> 加粗（行首 1-6 个 #）
        result = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", result, flags=re.MULTILINE)

        # 5) 引用 > -> › 前缀（< 已转义为 &gt;，这里匹配转义后的）
        result = re.sub(r"^&gt;\s?", "› ", result, flags=re.MULTILINE)

        # 6) 还原链接为 <a href>
        def _restore_link(m):
            idx = int(m.group(1))
            text_, url = links[idx]
            return f'<a href="{_html.escape(url, quote=True)}">{text_}</a>'

        result = re.sub(r"\x00LINK(\d+)\x00", _restore_link, result)

        return result

    def _render_table_as_pre(self, table_lines: list) -> str:
        """把 Markdown 表格行渲染成 <pre> 对齐文本块。"""
        # 解析每行为单元格（去掉首尾 |）
        rows = []
        for ln in table_lines:
            ln = ln.strip()
            if not ln.startswith("|"):
                ln = "|" + ln
            if not ln.endswith("|"):
                ln = ln + "|"
            cells = [c.strip() for c in ln.strip("|").split("|")]
            rows.append(cells)

        if len(rows) < 2:
            return "\n".join(table_lines)

        # 第二行是分隔行 |---|---|，丢弃（不显示）
        header = rows[0]
        body = rows[2:] if len(rows) > 2 else []

        # 计算每列最大宽度（按显示宽度，CJK 字符算 2）
        def _disp_width(s: str) -> int:
            w = 0
            for ch in s:
                w += 2 if ord(ch) > 0x2E80 else 1
            return w

        def _pad(s: str, width: int) -> str:
            return s + " " * (width - _disp_width(s))

        num_cols = max(len(r) for r in [header] + body)
        widths = [0] * num_cols
        for r in [header] + body:
            for ci, cell in enumerate(r):
                widths[ci] = max(widths[ci], _disp_width(cell))

        def _fmt_row(r):
            return "| " + " | ".join(_pad(r[ci], widths[ci]) if ci < len(r) else " " * widths[ci] for ci in range(num_cols)) + " |"

        rendered = [_fmt_row(header), "|" + "|".join("-" * (widths[ci] + 2) for ci in range(num_cols)) + "|"]
        for r in body:
            rendered.append(_fmt_row(r))

        # <pre> 里不需要 HTML 标签，但要转义 < > &（表格内容里可能有）
        import html as _html
        inner = "\n".join(rendered)
        return f"<pre>{_html.escape(inner, quote=False)}</pre>"

    def _convert_to_telegram_markdown(self, text: str) -> str:
        """已弃用：旧版 Telegram Markdown 转换。保留以兼容外部调用，内部改用 HTML。"""
        result = text
        result = re.sub(r'^#{1,6}\s+', '', result, flags=re.MULTILINE)
        result = re.sub(r'\*\*(.+?)\*\*', r'*\1*', result)
        import uuid as _uuid
        _link_placeholder = f"__LINK_{_uuid.uuid4().hex[:8]}__"
        _links = []
        def _save_link(m):
            _links.append(m.group(0))
            return f"{_link_placeholder}{len(_links) - 1}"
        result = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _save_link, result)
        for char in ['[', ']', '(', ')']:
            result = result.replace(char, f'\\{char}')
        for i, link in enumerate(_links):
            result = result.replace(f"{_link_placeholder}{i}", link)
        return result
