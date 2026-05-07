"""
抖音视频解析与下载模块
基于公开 API，无需 Cookie 和登录
原理：短链接重定向 → 提取 video_id → 公开 API 获取元数据 → 无水印播放地址
"""

import base64
import json
import hashlib
import os
import re
import subprocess
import tempfile
import time
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests

logger = logging.getLogger("douyin")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Referer": "https://www.douyin.com/",
}

MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.douyin.com/",
}

_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def is_douyin_url(url: str) -> bool:
    """判断是否为抖音链接"""
    douyin_domains = [
        "douyin.com", "iesdouyin.com", "v.douyin.com",
        "www.douyin.com", "m.douyin.com",
    ]
    try:
        host = urlparse(url).netloc.lower()
        return any(d in host for d in douyin_domains)
    except Exception:
        return False


class DouyinParser:
    """抖音视频解析器，无需 Cookie"""

    API_URL = "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/"

    def __init__(self, download_dir: str = "downloads"):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.timeout = (10, 30)
        self.max_retries = 3

    def parse(self, url: str) -> dict:
        """解析抖音视频信息，返回统一格式"""
        share_url = self._extract_url(url)
        resolved_url = self._resolve_redirect(share_url)
        video_id = self._extract_video_id(resolved_url)

        item_info = self._fetch_item_info(video_id, resolved_url)
        return self._build_result(item_info, video_id)

    def download(self, url: str, format_id: str = "") -> dict:
        """下载抖音视频，返回文件路径。format_id: douyin_1080p/douyin_720p/.../douyin_mp3"""
        share_url = self._extract_url(url)
        resolved_url = self._resolve_redirect(share_url)
        video_id = self._extract_video_id(resolved_url)

        item_info = self._fetch_item_info(video_id, resolved_url)
        title = item_info.get("desc") or f"douyin_{video_id}"
        safe_title = re.sub(r'[\\/*?:"<>|\n\r\t#@]', "_", title).strip("_. ")[:60]
        safe_title = re.sub(r'_+', '_', safe_title)
        if not safe_title:
            safe_title = f"douyin_{video_id}"

        is_audio = format_id == "douyin_mp3"

        if is_audio:
            video_url = self._get_media_url(item_info, "video")
            # 按指定的 ratio 构造下载 URL
            fd, temp_video = tempfile.mkstemp(suffix=".mp4", prefix="douyin_video_")
            os.close(fd)
            try:
                self._download_file(video_url, Path(temp_video))
                ext = ".mp3"
                filename = f"{safe_title}{ext}"
                filepath = self.download_dir / filename
                subprocess.run([
                    "ffmpeg", "-y", "-i", temp_video,
                    "-vn", "-acodec", "libmp3lame", "-q:a", "2",
                    str(filepath)
                ], check=True, capture_output=True)
            finally:
                try:
                    os.unlink(temp_video)
                except OSError:
                    pass
        else:
            # 根据 format_id 解析 ratio，构造对应清晰度的 URL
            video_url = self._get_media_url(item_info, "video")
            if format_id and format_id.startswith("douyin_"):
                ratio = format_id.replace("douyin_", "")
                video_url = re.sub(r'ratio=\w+', f'ratio={ratio}', video_url)
            ext = ".mp4"
            filename = f"{safe_title}{ext}"
            filepath = self.download_dir / filename
            self._download_file(video_url, filepath)

        return {
            "filepath": str(filepath),
            "filename": filename,
            "title": title,
            "ext": ext.lstrip("."),
        }

    def _extract_url(self, text: str) -> str:
        match = _URL_PATTERN.search(text)
        if not match:
            raise ValueError("未找到有效的抖音链接")
        candidate = match.group(0).strip().strip('"').strip("'")
        return candidate.rstrip(").,;!?")

    def _resolve_redirect(self, share_url: str) -> str:
        """解析短链接重定向"""
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(
                    share_url, timeout=self.timeout,
                    allow_redirects=True, headers=DEFAULT_HEADERS,
                )
                resp.raise_for_status()
                return resp.url
            except requests.RequestException as e:
                if attempt == self.max_retries - 1:
                    raise ValueError(f"链接解析失败: {e}")
                time.sleep(1 * (2 ** attempt))
        raise ValueError("链接解析失败")

    def _extract_video_id(self, url: str) -> str:
        """从 URL 中提取视频 ID"""
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        for key in ("modal_id", "item_ids", "group_id", "aweme_id"):
            values = query.get(key)
            if values:
                match = re.search(r"(\d{8,24})", values[0])
                if match:
                    return match.group(1)

        for pattern in (r"/video/(\d{8,24})", r"/note/(\d{8,24})", r"/(\d{8,24})(?:/|$)"):
            match = re.search(pattern, parsed.path)
            if match:
                return match.group(1)

        fallback = re.search(r"(\d{15,24})", url)
        if fallback:
            return fallback.group(1)

        raise ValueError("无法从链接中提取视频ID")

    def _fetch_item_info(self, video_id: str, resolved_url: str) -> dict:
        """获取视频元数据，优先公开 API，失败则解析分享页"""
        try:
            return self._fetch_via_api(video_id)
        except Exception as e:
            logger.warning("公开API获取失败(%s)，尝试分享页解析", e)
            return self._fetch_via_share_page(video_id, resolved_url)

    def _fetch_via_api(self, video_id: str) -> dict:
        params = {"item_ids": video_id}
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(
                    self.API_URL, params=params, timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get("item_list") or []
                if items:
                    return items[0]
                raise ValueError("API 返回空数据")
            except Exception as e:
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(1 * (2 ** attempt))
        raise ValueError("API 请求失败")

    def _fetch_via_share_page(self, video_id: str, resolved_url: str) -> dict:
        """从分享页面 HTML 中解析视频信息"""
        parsed = urlparse(resolved_url)
        if "iesdouyin.com" in (parsed.netloc or ""):
            share_url = resolved_url
        else:
            share_url = f"https://www.iesdouyin.com/share/video/{video_id}/"

        resp = self.session.get(share_url, headers=MOBILE_HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        html = resp.text or ""

        if "Please wait..." in html and "wci=" in html and "cs=" in html:
            html = self._solve_waf_and_retry(html, share_url)

        router_data = self._extract_router_data(html)
        if not router_data:
            raise ValueError("无法从分享页提取数据")

        loader_data = router_data.get("loaderData", {})
        for node in loader_data.values():
            if not isinstance(node, dict):
                continue
            video_info_res = node.get("videoInfoRes", {})
            if not isinstance(video_info_res, dict):
                continue
            item_list = video_info_res.get("item_list", [])
            if item_list and isinstance(item_list[0], dict):
                return item_list[0]

        raise ValueError("分享页中未找到视频信息")

    def _solve_waf_and_retry(self, html: str, page_url: str) -> str:
        """解决抖音 WAF 反爬验证"""
        match = re.search(r'wci="([^"]+)"\s*,\s*cs="([^"]+)"', html)
        if not match:
            return html

        cookie_name, challenge_blob = match.groups()
        try:
            decoded = self._decode_b64(challenge_blob).decode("utf-8")
            challenge_data = json.loads(decoded)
            prefix = self._decode_b64(challenge_data["v"]["a"])
            expected = self._decode_b64(challenge_data["v"]["c"]).hex()
        except (KeyError, ValueError):
            return html

        for candidate in range(1_000_001):
            digest = hashlib.sha256(prefix + str(candidate).encode()).hexdigest()
            if digest == expected:
                challenge_data["d"] = base64.b64encode(
                    str(candidate).encode()
                ).decode()
                cookie_val = base64.b64encode(
                    json.dumps(challenge_data, separators=(",", ":")).encode()
                ).decode()
                domain = urlparse(page_url).hostname or "www.iesdouyin.com"
                self.session.cookies.set(cookie_name, cookie_val, domain=domain, path="/")
                resp = self.session.get(page_url, headers=MOBILE_HEADERS, timeout=self.timeout)
                return resp.text or ""

        return html

    @staticmethod
    def _decode_b64(value: str) -> bytes:
        normalized = value.replace("-", "+").replace("_", "/")
        normalized += "=" * (-len(normalized) % 4)
        return base64.b64decode(normalized)

    def _extract_router_data(self, html: str) -> dict:
        marker = "window._ROUTER_DATA = "
        start = html.find(marker)
        if start < 0:
            return {}

        idx = start + len(marker)
        while idx < len(html) and html[idx].isspace():
            idx += 1
        if idx >= len(html) or html[idx] != "{":
            return {}

        depth = 0
        in_str = False
        escaped = False
        for cursor in range(idx, len(html)):
            ch = html[cursor]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[idx: cursor + 1])
                    except ValueError:
                        return {}
        return {}

    def _get_media_url(self, item_info: dict, mode: str = "video") -> str:
        """提取无水印播放地址"""
        if mode == "video":
            play_urls = (
                item_info.get("video", {})
                .get("play_addr", {})
                .get("url_list", [])
            )
            if not play_urls:
                raise ValueError("未找到视频播放地址")
            return play_urls[0].replace("playwm", "play")

        if mode == "audio":
            # 抖音分享页 API 不提供音频直链，返回视频 URL，由 download() 中用 ffmpeg 提取音频
            play_urls = (
                item_info.get("video", {})
                .get("play_addr", {})
                .get("url_list", [])
            )
            if not play_urls:
                raise ValueError("未找到视频播放地址")
            return play_urls[0].replace("playwm", "play")

        raise ValueError(f"不支持的模式: {mode}")

    def _build_result(self, item_info: dict, video_id: str) -> dict:
        """构建与 yt-dlp 解析结果兼容的统一格式"""
        title = item_info.get("desc") or f"抖音视频_{video_id}"
        author = item_info.get("author", {})
        stats = item_info.get("statistics", {})

        video_info = item_info.get("video", {})
        play_urls = video_info.get("play_addr", {}).get("url_list", [])
        cover_urls = video_info.get("cover", {}).get("url_list", [])
        duration = video_info.get("duration", 0)
        duration_sec = duration // 1000 if duration > 1000 else duration
        width = video_info.get("width", 0)
        height = video_info.get("height", 0)

        # 从接口返回的 URL 中提取实际 ratio，以此为上限
        default_ratio = "720p"
        if play_urls:
            ratio_match = re.search(r'ratio=(\d+)p?', play_urls[0])
            if ratio_match:
                default_ratio = f"{ratio_match.group(1)}p"

        # 所有可选清晰度，按从高到低排列
        all_ratios = [
            ("1080p", 1080, "1080P 高清"),
            ("720p", 720, "720P 标清"),
            ("540p", 540, "540P 流畅"),
            ("480p", 480, "480P 省流"),
        ]
        # 只保留不高于接口返回 ratio 的选项
        default_ratio_h = int(default_ratio.rstrip("p"))
        available_ratios = [(r, h, l) for r, h, l in all_ratios if h <= default_ratio_h]

        formats = []
        if play_urls:
            base_url = play_urls[0].replace("playwm", "play")
            for ratio, target_h, label_suffix in available_ratios:
                variant_url = re.sub(r'ratio=\w+', f'ratio={ratio}', base_url)
                if variant_url == base_url:
                    variant_url = base_url + (f'&ratio={ratio}' if '?' in base_url else f'?ratio={ratio}')
                # 按比例计算对应清晰度的实际尺寸
                if width and height:
                    scale = target_h / height
                    fmt_width = round(width * scale)
                    fmt_height = target_h
                else:
                    fmt_width = 0
                    fmt_height = target_h
                formats.append({
                    "format_id": f"douyin_{ratio}",
                    "ext": "mp4",
                    "resolution": f"{fmt_width}x{fmt_height}" if fmt_width else f"auto",
                    "height": fmt_height,
                    "filesize": None,
                    "filesize_approx": None,
                    "vcodec": "h264",
                    "acodec": "aac",
                    "has_audio": True,
                    "label": f"无水印 {label_suffix}",
                    "_direct_url": variant_url,
                })

        # MP3 音频（从视频中提取，不需要独立音频链接）
        if play_urls:
            formats.append({
                "format_id": "douyin_mp3",
                "ext": "mp3",
                "resolution": "audio only",
                "height": 0,
                "filesize": None,
                "filesize_approx": None,
                "vcodec": "none",
                "acodec": "mp3",
                "has_audio": True,
                "has_video": False,
                "label": "MP3 音频",
                "_direct_url": "",
            })

        return {
            "id": video_id,
            "title": title,
            "thumbnail": cover_urls[0] if cover_urls else "",
            "duration": duration_sec,
            "duration_string": self._fmt_duration(duration_sec),
            "uploader": author.get("nickname", "抖音用户"),
            "platform": "抖音",
            "view_count": stats.get("play_count") or stats.get("digg_count"),
            "upload_date": "",
            "description": title,
            "formats": formats,
            "subtitles": self._extract_subtitles(item_info),
            "automatic_captions": self._extract_auto_subtitles(item_info),
        }

    def _extract_subtitles(self, item_info: dict) -> dict:
        """从 item_info 中提取人工字幕，返回 yt-dlp 兼容格式 {lang: [{url, ext}]}"""
        result = {}
        video = item_info.get("video", {}) or {}
        subtitle_data = video.get("subtitle") or video.get("subtitles") or {}
        if isinstance(subtitle_data, list):
            subtitle_data = {}
        for lang, tracks in subtitle_data.items():
            if isinstance(tracks, list) and tracks:
                result[lang] = [
                    {"url": t.get("url", ""), "ext": t.get("format", "vtt")}
                    for t in tracks if t.get("url")
                ]
        return result

    def _extract_auto_subtitles(self, item_info: dict) -> dict:
        """从 item_info 中提取 AI 自动字幕，返回 yt-dlp 兼容格式"""
        result = {}
        video = item_info.get("video", {}) or {}
        auto_data = video.get("auto_subtitle") or video.get("ai_subtitle") or {}
        if isinstance(auto_data, list):
            auto_data = {}
        for lang, tracks in auto_data.items():
            if isinstance(tracks, list) and tracks:
                result[lang] = [
                    {"url": t.get("url", ""), "ext": t.get("format", "vtt")}
                    for t in tracks if t.get("url")
                ]
        return result

    @staticmethod
    def _fmt_duration(seconds: Optional[int]) -> str:
        if not seconds:
            return "00:00"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _download_file(self, url: str, filepath: Path, chunk_size: int = 64 * 1024):
        """下载文件到本地"""
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(
                    url, stream=True, timeout=self.timeout, allow_redirects=True,
                )
                resp.raise_for_status()

                temp_path = filepath.with_suffix(filepath.suffix + ".part")
                with temp_path.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)
                temp_path.replace(filepath)
                return
            except Exception as e:
                if attempt == self.max_retries - 1:
                    raise ValueError(f"文件下载失败: {e}")
                time.sleep(1 * (2 ** attempt))
