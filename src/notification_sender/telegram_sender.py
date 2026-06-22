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
        """Send a single Telegram message with exponential backoff retry (Fixes #287)

        优先用 sendRichMessage（真表格 <table>），失败回退到 sendMessage HTML（<pre> 模拟表格）。
        """
        # 1) 优先尝试 sendRichMessage：表格用真 <table>，其余用 Rich Markdown（支持 # 标题、**bold**）
        if self._send_telegram_rich(api_url, chat_id, text, message_thread_id, timeout_seconds=timeout_seconds):
            return True

        # 2) 回退：sendMessage + HTML（表格转 <pre>，标题 <b>）
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

    def _send_telegram_rich(
        self,
        api_url: str,
        chat_id: str,
        text: str,
        message_thread_id: Optional[str] = None,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """用 sendRichMessage 发送，表格渲染为真 <table>，其余用 Rich Markdown。

        Rich Markdown 支持 # 标题、**bold**、*italic*、> 引用、- 列表、[text](url)、
        以及嵌入 HTML <table bordered striped> 真表格。
        失败时返回 False，由调用方回退到 sendMessage HTML。
        """
        # 从 sendMessage URL 构造 sendRichMessage URL
        # api_url 形如 https://api.telegram.org/bot<token>/sendMessage
        if "/sendMessage" not in api_url:
            return False
        rich_url = api_url.replace("/sendMessage", "/sendRichMessage")

        # 转换：去 emoji + Markdown 表格转 <table>（其余 Rich Markdown 语法原样保留）
        rich_markdown = self._convert_to_rich_markdown(text)
        if not rich_markdown.strip():
            return False

        payload = {
            "chat_id": chat_id,
            "rich_message": {"markdown": rich_markdown},
        }
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id

        try:
            response = requests.post(rich_url, json=payload, timeout=timeout_seconds or 15)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logger.warning(f"sendRichMessage 请求失败，将回退 HTML: {e}")
            return False

        if response.status_code == 200:
            result = response.json()
            if result.get("ok"):
                logger.info("Telegram 消息发送成功（Rich 真表格）")
                return True
            logger.warning(f"sendRichMessage 返回错误，将回退 HTML: {result.get('description', '')}")
            return False
        logger.warning(f"sendRichMessage HTTP {response.status_code}，将回退 HTML: {response.text[:150]}")
        return False

    def _convert_to_rich_markdown(self, text: str) -> str:
        """将报告 Markdown 转成 Rich Markdown：表格转 <table>，其余原样保留。

        Rich Markdown 支持 # 标题、**bold**、> 引用、- 列表、[link](url)，
        并可在 markdown 字段里直接嵌入 HTML <table> 真表格。
        只需做：去 emoji + Markdown 表格转 <table bordered striped>。
        """
        # 1) 去掉 emoji
        result = self._strip_emoji(text)

        # 2) Markdown 表格 |...| -> <table bordered striped><tr><th>...</th></tr>...</table>
        lines = result.split("\n")
        out_lines: list = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            # 检测表格起始：行内含 | 且下一行是分隔行 |---|---|
            if "|" in line and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]) and "-" in lines[i + 1]:
                table_block = [line, lines[i + 1]]
                j = i + 2
                while j < n and "|" in lines[j] and lines[j].strip():
                    table_block.append(lines[j])
                    j += 1
                # 渲染成 <table> HTML
                out_lines.append(self._render_table_as_html(table_block))
                i = j
                continue
            out_lines.append(line)
            i += 1

        return "\n".join(out_lines)

    def _render_table_as_html(self, table_lines: list) -> str:
        """把 Markdown 表格行渲染成 Rich HTML <table bordered striped>。"""
        # 解析每行单元格
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

        header = rows[0]
        body = rows[2:] if len(rows) > 2 else []  # 跳过分隔行 rows[1]

        def _cell_to_html(cell: str, is_header: bool) -> str:
            # 清理 cell 内的 **/__ 标记（表格 cell 只支持行内格式，** 在 HTML 表格里不渲染）
            cell = re.sub(r"\*\*(.+?)\*\*", r"\1", cell)
            cell = re.sub(r"__(.+?)__", r"\1", cell)
            tag = "th" if is_header else "td"
            return f"<{tag}>{cell}</{tag}>"

        html_parts = ['<table bordered striped>']
        # 表头
        html_parts.append("<tr>" + "".join(_cell_to_html(c, True) for c in header) + "</tr>")
        # 表体
        for r in body:
            html_parts.append("<tr>" + "".join(_cell_to_html(c, False) for c in r) + "</tr>")
        html_parts.append("</table>")
        return "".join(html_parts)

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

    def _convert_to_telegram_plain(self, text: str) -> str:
        """
        将 Markdown 转成纯文本（不使用 parse_mode）。

        参考 Morning Brief 风格：Telegram 原样显示，零转义乱码。
        - 去掉 # 标题标记（保留标题文字 + emoji 做层次）
        - 去掉 ** 和 __ 行内标记
        - 去掉 > 引用标记（保留内容）
        - 表格保留 | 竖线（纯文本竖线在 Telegram 显示整齐）
        - 保留 emoji、【】、分隔线 ---
        """
        result = text

        # 去掉行首 # 标题标记（1-6 个 #），保留标题文字
        result = re.sub(r'^#{1,6}\s+', '', result, flags=re.MULTILINE)

        # 去掉行内粗体/斜体标记：**bold** -> bold, __bold__ -> bold, *italic* -> italic
        result = re.sub(r'\*\*(.+?)\*\*', r'\1', result)
        result = re.sub(r'__(.+?)__', r'\1', result)
        # 单星号斜体 *italic* -> italic（谨慎：避免误删列表 *，只处理成对的）
        result = re.sub(r'(?<!\w)\*([^*\n]+)\*(?!\w)', r'\1', result)

        # 去掉行内 code 标记 `code` -> code
        result = re.sub(r'`([^`\n]+)`', r'\1', result)

        # 去掉 ``` 代码块围栏（保留内容）
        result = re.sub(r'^```\w*\s*$', '', result, flags=re.MULTILINE)
        result = re.sub(r'^```$', '', result, flags=re.MULTILINE)

        # 去掉行首 > 引用标记（保留内容）
        result = re.sub(r'^>\s?', '', result, flags=re.MULTILINE)

        # 链接 [text](url) -> text（纯文本不支持链接，保留显示文字）
        result = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1', result)

        return result

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

        # 先去掉所有 emoji（用户要求，精确匹配 emoji 区段避免误伤中文）
        text = self._strip_emoji(text)

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

    def _render_table_as_mdcode(self, table_lines: list) -> str:
        """把 Markdown 表格行渲染成 MarkdownV2 代码块(```...```)，保留竖线对齐。

        代码块内部按官方规则只需转义 ` 和 \\，其它字符原样。
        清理 cell 内的 **/__/` 标记（代码块内不渲染格式，留着会显示成字面字符）。
        """
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

        header = rows[0]
        body = rows[2:] if len(rows) > 2 else []

        def _disp_width(s: str) -> int:
            return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)

        def _pad(s: str, width: int) -> str:
            return s + " " * (width - _disp_width(s))

        def _clean_cell(s: str) -> str:
            # 去掉行内格式标记（代码块内不渲染），再转义 ` 和 \
            s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
            s = re.sub(r"__(.+?)__", r"\1", s)
            s = s.replace("`", "").replace("\\", "\\\\")
            return s

        num_cols = max(len(r) for r in [header] + body)
        widths = [0] * num_cols
        for r in [header] + body:
            for ci, cell in enumerate(r):
                widths[ci] = max(widths[ci], _disp_width(_clean_cell(cell)))

        def _fmt_row(r):
            return "| " + " | ".join(_pad(_clean_cell(r[ci]), widths[ci]) if ci < len(r) else " " * widths[ci] for ci in range(num_cols)) + " |"

        rendered = [_fmt_row(header), "|" + "|".join("-" * (widths[ci] + 2) for ci in range(num_cols)) + "|"]
        for r in body:
            rendered.append(_fmt_row(r))

        inner = "\n".join(rendered)
        return f"```\n{inner}\n```"

    def _convert_to_telegram_markdownv2(self, text: str) -> str:
        """
        将标准 Markdown 转换为 Telegram MarkdownV2 格式（严格按官方规范）。

        官方规范（core.telegram.org/bots/api#markdownv2-style）：
        - 语法：*bold* _italic_ `code` ```codeblock``` [text](url) > 引用
        - 实体外的这些字符必须转义(前加 \\)：_ * [ ] ( ) ~ ` > # + - = | { } . !
        - pre/code 实体内部：只需转义 ` 和 \\，其它字符原样
        - 链接 (...) 内部：只需转义 ) 和 \\

        转换策略：
        - 去掉所有 emoji（用户要求）
        - Markdown 表格 -> ``` 代码块（内部原样，仅转义 ` 和 \\）
        - # 标题 -> *bold* 加粗
        - **bold** -> *bold*
        - [text](url) 链接保留
        - 实体外特殊字符转义
        """
        # 0) 去掉所有 emoji（覆盖常见 emoji 区段）
        result = self._strip_emoji(text)

        lines = result.split("\n")
        out_lines: list = []
        i = 0
        n = len(lines)
        code_blocks: list = []

        # 1) 抽表格 -> ``` 代码块占位符（代码块内部按官方规则只转义 ` 和 \）
        while i < n:
            line = lines[i]
            if "|" in line and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]) and "-" in lines[i + 1]:
                table_block = [line, lines[i + 1]]
                j = i + 2
                while j < n and "|" in lines[j] and lines[j].strip():
                    table_block.append(lines[j])
                    j += 1
                code_html = self._render_table_as_mdcode(table_block)
                code_blocks.append(code_html)
                out_lines.append(f"\x00CODE{len(code_blocks) - 1}\x00")
                i = j
                continue
            out_lines.append(line)
            i += 1

        result = "\n".join(out_lines)

        # 2) 保护已存在的 ``` 代码块（报告里可能有，按 pre 规则处理）
        existing_codes: list = []

        def _save_existing_code(m):
            # 代码块内部只转义 ` 和 \，其它原样
            inner = m.group(1)
            inner = inner.replace("\\", "\\\\").replace("`", "\\`")
            existing_codes.append(f"```\n{inner}\n```")
            return f"\x00EXIST{len(existing_codes) - 1}\x00"

        result = re.sub(r"```(?:\w*)\n?(.*?)```", _save_existing_code, result, flags=re.DOTALL)

        # 3) 保护行内 `code`（code 实体内部只转义 ` 和 \）
        inline_codes: list = []

        def _save_inline(m):
            inner = m.group(1)
            inner = inner.replace("\\", "\\\\").replace("`", "\\`")
            inline_codes.append(f"`{inner}`")
            return f"\x00INLINE{len(inline_codes) - 1}\x00"

        result = re.sub(r"`([^`\n]+)`", _save_inline, result)

        # 4) 保护 Markdown 链接 [text](url)
        links: list = []

        def _save_link(m):
            links.append((m.group(1), m.group(2)))
            return f"\x00LINK{len(links) - 1}\x00"

        result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _save_link, result)

        # 5) 转换 **bold** -> *bold*（MarkdownV2 粗体是单星号）
        result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)

        # 5.5) # 标题 -> *bold* 加粗（MarkdownV2 不支持 #）
        result = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", result, flags=re.MULTILINE)

        # 6) 保护 *bold* 和 _italic_ 边界（内部内容仍需转义，官方:实体内特殊字符也要转义）
        _BSTAR = "\x00B\x00"
        _ESTAR = "\x00E\x00"
        _BUND = "\x00U\x00"
        _EUND = "\x00V\x00"

        def _mask_bold(m):
            return _BSTAR + m.group(1) + _ESTAR
        result = re.sub(r"\*([^*\n]+)\*", _mask_bold, result)

        def _mask_italic(m):
            return _BUND + m.group(1) + _EUND
        result = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", _mask_italic, result)

        # 7) 转义实体外的特殊字符（官方完整列表：_ * [ ] ( ) ~ ` > # + - = | { } . ! \）
        def _escape_mdv2(s: str) -> str:
            out = []
            for ch in s:
                if ch in '_*[]()~`>#+-=|{}.!\\':
                    out.append("\\")
                    out.append(ch)
                else:
                    out.append(ch)
            return "".join(out)

        result = _escape_mdv2(result)

        # 8) 还原格式标记边界
        result = result.replace(_BSTAR, "*").replace(_ESTAR, "*")
        result = result.replace(_BUND, "_").replace(_EUND, "_")

        # 9) 还原行内 code、代码块、表格代码块（这些已经在保护时处理过内部转义）
        for i_, c in enumerate(inline_codes):
            result = result.replace(f"\x00INLINE{i_}\x00", c)
        for i_, c in enumerate(existing_codes):
            result = result.replace(f"\x00EXIST{i_}\x00", c)
        for i_, cb in enumerate(code_blocks):
            result = result.replace(f"\x00CODE{i_}\x00", cb)

        # 10) 还原链接（text 部分已被转义流程处理，url 内部只需转义 ) 和 \）
        def _restore_link(m):
            idx = int(m.group(1))
            t, url = links[idx]
            url = url.replace("\\", "\\\\").replace(")", "\\)")
            return f"[{t}]({url})"

        result = re.sub(r"\x00LINK(\d+)\x00", _restore_link, result)

        return result

    @staticmethod
    def _strip_emoji(text: str) -> str:
        """去掉所有 emoji（只匹配真正的 emoji 区段，避免误伤中文）。"""
        emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"  # 表情符号 😀-🙏
            "\U0001F300-\U0001F5FF"  # 符号和象形文字 🌀-🗿
            "\U0001F680-\U0001F6FF"  # 交通和地图符号 🚀-🛿
            "\U0001F700-\U0001F77F"  # 代数符号
            "\U0001F780-\U0001F7FF"  # 几何图形扩展
            "\U0001F800-\U0001F8FF"  # 补充箭头-C
            "\U0001F900-\U0001F9FF"  # 补充符号和象形文字 🤀-🧿
            "\U0001FA00-\U0001FA6F"  # 扩展符号A
            "\U0001FA70-\U0001FAFF"  # 扩展符号B
            "\U0001F1E0-\U0001F1FF"  # 旗帜 🇦-🇿
            "\u2600-\u26FF"          # 杂项符号（☀ ☂ ☎ 等，不含中文）
            "\u2700-\u27BF"          # 装饰符号 ✁-➿
            "\uFE0F"                 # 变体选择符-16（emoji 显示修饰）
            "\u200D"                 # 零宽连字（emoji 组合用）
            "]+",
            flags=re.UNICODE,
        )
        result = emoji_pattern.sub("", text)
        # 去掉删除 emoji 后留下的行首多余空格和多余空行
        result = re.sub(r"^[ \t]+", "", result, flags=re.MULTILINE)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result

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
