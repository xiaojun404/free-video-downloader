"""
快手视频解析与下载模块
参考抖音解析逻辑：短链接重定向 → 提取 photo_id → GraphQL API / 页面数据 → 无水印播放地址
"""

import json
import os
import re
import subprocess
import tempfile
import time
import logging
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests

logger = logging.getLogger("kuaishou")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Referer": "https://www.kuaishou.com/",
}

GRAPHQL_HEADERS = {
    **DEFAULT_HEADERS,
    "Content-Type": "application/json",
    "Origin": "https://www.kuaishou.com",
}

MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
}

_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def is_kuaishou_url(url: str) -> bool:
    """判断是否为快手链接"""
    kuaishou_domains = [
        "kuaishou.com", "v.kuaishou.com",
        "www.kuaishou.com", "live.kuaishou.com",
    ]
    try:
        host = urlparse(url).netloc.lower()
        return any(d in host for d in kuaishou_domains)
    except Exception:
        return False


def _generate_did() -> str:
    """生成快手 PC Web 所需的 did 标识"""
    return "web_" + secrets.token_hex(16)


class KuaishouParser:
    """快手视频解析器，优先 GraphQL API，失败则解析页面数据"""

    GRAPHQL_URL = "https://www.kuaishou.com/graphql"

    VISION_VIDEO_DETAIL_QUERY = """
query visionVideoDetail($photoId: String, $type: String) {
    visionVideoDetail(photoId: $photoId, type: $type) {
        status
        author { id, name, headerUrl }
        photo {
            id, duration, caption, likeCount
            viewCount, coverUrl, photoUrl
            manifest {
                adaptationSet {
                    representation {
                        url, qualityLabel, height, width
                    }
                }
            }
        }
    }
}
"""

    def __init__(self, download_dir: str = "downloads"):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.timeout = (10, 30)
        self.max_retries = 3
        self._did = _generate_did()
        self._set_base_cookies()

    def _set_base_cookies(self):
        """设置基础 Cookie（模拟浏览器首次访问）"""
        self.session.cookies.set("kpf", "PC_WEB", domain=".kuaishou.com")
        self.session.cookies.set("clientid", "3", domain=".kuaishou.com")
        self.session.cookies.set("did", self._did, domain=".kuaishou.com")
        self.session.cookies.set("kpn", "KUAISHOU_VISION", domain=".kuaishou.com")

        # 从环境变量加载完整 Cookie（可选，用于需要登录的场景）
        cookie_str = os.getenv("KUAISHOU_COOKIE", "")
        if cookie_str:
            for item in cookie_str.split(";"):
                item = item.strip()
                if "=" in item:
                    name, _, value = item.partition("=")
                    self.session.cookies.set(name.strip(), value.strip(), domain=".kuaishou.com")

    def parse(self, url: str) -> dict:
        """解析快手视频信息，返回统一格式"""
        share_url = self._extract_url(url)
        video_id = self._extract_video_id(share_url)
        needs_redirect = len(video_id) < 10  # 短码需要重定向解析

        item_info = self._fetch_item_info(share_url, video_id, needs_redirect)
        return self._build_result(item_info, video_id)

    def download(self, url: str, format_id: str = "") -> dict:
        """下载快手视频，返回文件路径。format_id: kuaishou_1080p/kuaishou_720p/.../kuaishou_mp3"""
        share_url = self._extract_url(url)
        video_id = self._extract_video_id(share_url)
        needs_redirect = len(video_id) < 10

        item_info = self._fetch_item_info(share_url, video_id, needs_redirect)

        title = item_info.get("caption") or item_info.get("desc") or f"kuaishou_{video_id}"
        safe_title = re.sub(r'[\\/*?:"<>|\n\r\t#@]', "_", title).strip("_. ")[:60]
        safe_title = re.sub(r'_+', '_', safe_title)
        if not safe_title:
            safe_title = f"kuaishou_{video_id}"

        is_audio = format_id == "kuaishou_mp3"

        # 优先使用 photoUrl（直链 MP4），manifest 里的是 m3u8
        video_url = item_info.get("photoUrl", "")
        if not video_url:
            representations = self._get_representations(item_info)
            video_url = self._get_best_video_url(representations)

        if not video_url:
            raise ValueError("未找到视频下载地址")

        if is_audio:
            fd, temp_video = tempfile.mkstemp(suffix=".mp4", prefix="kuaishou_video_")
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

    # ── URL 处理 ─────────────────────────────────────────────

    def _extract_url(self, text: str) -> str:
        """从文本中提取快手链接"""
        match = _URL_PATTERN.search(text)
        if not match:
            raise ValueError("未找到有效的快手链接")
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
        """从 URL 中提取视频/图片 ID"""
        parsed = urlparse(url)

        # /short-video/VIDEO_ID
        m = re.search(r"/short-video/([a-zA-Z0-9]+)", parsed.path)
        if m:
            return m.group(1)

        # /f/VIDEO_ID
        m = re.search(r"/f/([a-zA-Z0-9]+)", parsed.path)
        if m:
            return m.group(1)

        # /photo/VIDEO_ID
        m = re.search(r"/photo/([a-zA-Z0-9]+)", parsed.path)
        if m:
            return m.group(1)

        # 查询参数
        query = parse_qs(parsed.query)
        for key in ("photoId", "videoId", "id"):
            if key in query:
                return query[key][0]

        # 路径最后一段作为 fallback
        path_segments = parsed.path.strip("/").split("/")
        if path_segments and path_segments[-1]:
            return path_segments[-1]

        raise ValueError("无法从链接中提取视频ID")

    # ── 数据获取（GraphQL API 优先，页面解析兜底）──────────────

    def _fetch_item_info(self, share_url: str, video_id: str, needs_redirect: bool) -> dict:
        """获取视频元数据。
        策略1: 移动端 m.gifshow.com/fw/photo/ 页面（最稳定，无需 cookie）
        策略2: GraphQL API
        策略3: PC/Mobile 页面解析
        """
        # 策略1: 移动端 photo 页面（最稳定，反爬最弱）
        try:
            return self._fetch_via_mobile_photo(video_id)
        except Exception as e:
            logger.warning("移动端 photo 页面获取失败(%s)，尝试 GraphQL API", e)

        # 策略2: GraphQL API
        try:
            return self._fetch_via_graphql(video_id)
        except Exception as e:
            logger.warning("GraphQL API 获取失败(%s)，尝试页面解析", e)

        # 策略3: 页面解析
        resolved_url = self._resolve_redirect(share_url) if needs_redirect else share_url
        if needs_redirect:
            video_id = self._extract_video_id(resolved_url)
        return self._fetch_via_page(video_id, resolved_url)

    def _fetch_via_mobile_photo(self, photo_id: str) -> dict:
        """通过 m.gifshow.com/fw/photo/ 页面获取视频数据（无需 cookie，反爬最弱）"""
        url = f"https://m.gifshow.com/fw/photo/{photo_id}"
        resp = self.session.get(url, headers=MOBILE_HEADERS, timeout=self.timeout, allow_redirects=True)
        resp.raise_for_status()
        html = resp.text or ""

        if "INIT_STATE" in html:
            try:
                init_data = self._extract_init_state(html)
                item = self._parse_init_item(init_data, photo_id)
                if item:
                    return item
            except Exception:
                pass

        raise ValueError("移动端 photo 页面未找到视频数据")

    def _fetch_via_graphql(self, photo_id: str) -> dict:
        """通过 GraphQL API 获取视频详情（使用独立 session，避免页面 cookie 触发反爬）"""
        payload = {
            "query": self.VISION_VIDEO_DETAIL_QUERY,
            "variables": {"photoId": photo_id, "type": "video"},
        }

        # 使用独立 session，只带基础 cookie，避免页面访问产生的 cookie 触发反爬
        gql_session = requests.Session()
        gql_session.cookies.set("kpf", "PC_WEB", domain=".kuaishou.com")
        gql_session.cookies.set("clientid", "3", domain=".kuaishou.com")
        gql_session.cookies.set("did", _generate_did(), domain=".kuaishou.com")
        gql_session.cookies.set("kpn", "KUAISHOU_VISION", domain=".kuaishou.com")

        # 复制环境变量中的 KUAISHOU_COOKIE（如果有的话）
        cookie_str = os.getenv("KUAISHOU_COOKIE", "")
        if cookie_str:
            for item in cookie_str.split(";"):
                item = item.strip()
                if "=" in item:
                    name, _, value = item.partition("=")
                    gql_session.cookies.set(name.strip(), value.strip(), domain=".kuaishou.com")

        for attempt in range(self.max_retries):
            try:
                resp = gql_session.post(
                    self.GRAPHQL_URL, json=payload,
                    headers=GRAPHQL_HEADERS, timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()

                if "errors" in data:
                    raise ValueError(self._format_graphql_error(data["errors"]))

                vision_data = data.get("data", {}).get("visionVideoDetail", {})
                if not vision_data:
                    raise ValueError("API 返回空数据")
                if vision_data.get("status") != 1:
                    raise ValueError("视频需要登录或不存在")
                if not vision_data.get("photo"):
                    raise ValueError("API 未返回视频信息")

                return self._normalize_vision_detail(vision_data)
            except ValueError:
                raise
            except Exception as e:
                if attempt == self.max_retries - 1:
                    raise ValueError(f"GraphQL API 请求失败: {e}")
                time.sleep(1 * (2 ** attempt))
        raise ValueError("GraphQL API 请求失败")

    @staticmethod
    def _format_graphql_error(errors: list) -> str:
        """格式化 GraphQL 错误信息"""
        msgs = []
        for err in errors:
            msg = err.get("message", str(err))
            code = err.get("extensions", {}).get("code", "")
            if code:
                msg = f"{msg} [{code}]"
            msgs.append(msg)
        return "; ".join(msgs)

    def _fetch_via_page(self, video_id: str, resolved_url: str) -> dict:
        """从页面 HTML 中解析视频数据（PC Web 优先，Mobile Web 兜底）"""
        # 策略1: PC Web → __APOLLO_STATE__
        try:
            html = self._get_html(resolved_url)
            if "__APOLLO_STATE__" in html:
                apollo_data = self._extract_apollo_state(html)
                item = self._parse_apollo_item(apollo_data, video_id)
                if item:
                    return item
        except Exception as e:
            logger.warning("PC 页面解析失败: %s", e)

        # 策略2: Mobile Web → INIT_STATE
        try:
            mobile_html = self._get_html(resolved_url, headers=MOBILE_HEADERS)
            if "INIT_STATE" in mobile_html:
                init_data = self._extract_init_state(mobile_html)
                item = self._parse_init_item(init_data, video_id)
                if item:
                    return item
        except Exception as e:
            logger.warning("Mobile 页面解析失败: %s", e)

        raise ValueError("无法从页面提取视频数据，请确认链接有效或设置 KUAISHOU_COOKIE 环境变量")

    def _get_html(self, url: str, headers: dict = None) -> str:
        """请求页面 HTML"""
        if headers is None:
            headers = DEFAULT_HEADERS
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(
                    url, timeout=self.timeout,
                    allow_redirects=True, headers=headers,
                )
                resp.raise_for_status()
                return resp.text or ""
            except requests.RequestException as e:
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(1 * (2 ** attempt))
        raise ValueError("页面请求失败")

    # ── __APOLLO_STATE__ 解析 ─────────────────────────────────

    @staticmethod
    def _extract_apollo_state(html: str) -> dict:
        """从 HTML 中提取 window.__APOLLO_STATE__ JSON 数据"""
        marker = "__APOLLO_STATE__"
        pos = html.find(marker)
        if pos < 0:
            raise ValueError("未找到 __APOLLO_STATE__")

        idx = pos + len(marker)
        while idx < len(html) and html[idx] != "{":
            idx += 1
        if idx >= len(html):
            raise ValueError("__APOLLO_STATE__ 格式异常")

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
                    json_str = html[idx: cursor + 1]
                    json_str = json_str.replace("undefined", "null")
                    try:
                        data = json.loads(json_str)
                    except json.JSONDecodeError:
                        raise ValueError("__APOLLO_STATE__ JSON 解析失败")
                    return data.get("defaultClient", data)

        raise ValueError("__APOLLO_STATE__ JSON 截断")

    @staticmethod
    def _resolve_apollo_value(data: dict, value):
        """解析 Apollo cache 中的引用和 JSON 内联值"""
        if not isinstance(value, dict):
            return value
        if value.get("type") == "json" and "json" in value:
            return value["json"]
        if value.get("type") == "id" and value.get("id"):
            ref_key = value["id"]
            if ref_key in data:
                return data[ref_key]
        return value

    @staticmethod
    def _parse_apollo_item(apollo_data: dict, video_id: str) -> Optional[dict]:
        """从 __APOLLO_STATE__ 数据中提取视频信息，并解析 Apollo cache 引用"""
        # 1. 直接查找 VisionVideoDetailPhoto:ID（Apollo 归一化缓存格式）
        photo_key = f"VisionVideoDetailPhoto:{video_id}"
        if photo_key in apollo_data and apollo_data[photo_key]:
            return KuaishouParser._build_apollo_item_info(apollo_data, apollo_data[photo_key])

        # 2. 从 visionVideoDetail query result 中查找
        for key, val in apollo_data.items():
            if "visionVideoDetail" not in key or not isinstance(val, dict):
                continue
            if val.get("status") != 1 or not val.get("photo"):
                continue
            # 解析 photo 引用
            photo = KuaishouParser._resolve_apollo_value(apollo_data, val["photo"])
            if isinstance(photo, dict) and photo.get("id"):
                return KuaishouParser._build_apollo_item_info(apollo_data, photo)

        # 3. 查找其他 VisionVideoDetailPhoto: 键
        for key in apollo_data:
            if key.startswith("VisionVideoDetailPhoto:") and apollo_data[key] and not key.startswith("$"):
                photo = apollo_data[key]
                if isinstance(photo, dict) and photo.get("id"):
                    return KuaishouParser._build_apollo_item_info(apollo_data, photo)

        return None

    @staticmethod
    def _build_apollo_item_info(apollo_data: dict, photo: dict) -> dict:
        """从 Apollo 缓存中的 photo 实体构建 item_info，解析 manifest 引用链"""
        resolve = KuaishouParser._resolve_apollo_value

        # 解析 manifest: photo.manifest → $VisionVideoDetailPhoto:ID.manifest → adaptationSet
        manifest = photo.get("manifest", {})
        manifest = resolve(apollo_data, manifest)
        if isinstance(manifest, dict) and "adaptationSet" in manifest:
            adaptation_set = []
            for adapt in manifest.get("adaptationSet", []):
                adapt = resolve(apollo_data, adapt)
                if isinstance(adapt, dict) and "representation" in adapt:
                    reps = []
                    for rep in adapt.get("representation", []):
                        rep = resolve(apollo_data, rep)
                        if isinstance(rep, dict):
                            reps.append(rep)
                    adapt = {**adapt, "representation": reps}
                adaptation_set.append(adapt)
            manifest = {**manifest, "adaptationSet": adaptation_set}

        # 如果 manifest 没有 adaptationSet，尝试从 manifestH265 获取
        if isinstance(manifest, dict) and not manifest.get("adaptationSet"):
            manifest_h265 = resolve(apollo_data, photo.get("manifestH265", {}))
            if isinstance(manifest_h265, dict) and manifest_h265.get("adaptationSet"):
                manifest = manifest_h265

        # 如果还是没有，尝试从 videoResource 获取
        if isinstance(manifest, dict) and not manifest.get("adaptationSet"):
            video_res = resolve(apollo_data, photo.get("videoResource", {}))
            if isinstance(video_res, dict):
                # videoResource: {h264: {adaptationSet: [...]}, h265: {...}}
                for codec in ("h264", "h265"):
                    codec_data = resolve(apollo_data, video_res.get(codec, {}))
                    if isinstance(codec_data, dict) and codec_data.get("adaptationSet"):
                        manifest = codec_data
                        break

        author = resolve(apollo_data, photo.get("author", {}))

        return {
            "id": photo.get("id", ""),
            "caption": photo.get("caption", ""),
            "duration": photo.get("duration", 0),
            "coverUrl": photo.get("coverUrl", ""),
            "photoUrl": photo.get("photoUrl", ""),
            "likeCount": photo.get("likeCount", "") or photo.get("realLikeCount", ""),
            "viewCount": photo.get("viewCount", ""),
            "width": photo.get("width", 0),
            "height": photo.get("height", 0),
            "author": {
                "id": author.get("id", "") if isinstance(author, dict) else "",
                "name": author.get("name", "") if isinstance(author, dict) else "",
                "headerUrl": author.get("headerUrl", "") if isinstance(author, dict) else "",
            },
            "manifest": manifest if isinstance(manifest, dict) else {},
        }

    @staticmethod
    def _normalize_vision_detail(vd: dict) -> dict:
        """将 visionVideoDetail GraphQL 响应归一化"""
        photo = vd.get("photo", {})
        author = vd.get("author") or {}
        return {
            "id": photo.get("id", ""),
            "caption": photo.get("caption", ""),
            "duration": photo.get("duration", 0),
            "coverUrl": photo.get("coverUrl", ""),
            "photoUrl": photo.get("photoUrl", ""),
            "likeCount": photo.get("likeCount", ""),
            "viewCount": photo.get("viewCount", ""),
            "width": photo.get("width", 0),
            "height": photo.get("height", 0),
            "author": {
                "id": author.get("id", ""),
                "name": author.get("name", ""),
                "headerUrl": author.get("headerUrl", ""),
            },
            "manifest": photo.get("manifest", {}),
        }

    # ── INIT_STATE 解析 ──────────────────────────────────────

    @staticmethod
    def _extract_init_state(html: str) -> dict:
        """从 Mobile Web 页面提取 INIT_STATE 并解码 ROT-1 GraphQL 响应"""
        marker = "INIT_STATE"
        pos = html.find(marker)
        if pos < 0:
            raise ValueError("未找到 INIT_STATE")

        idx = pos + len(marker)
        while idx < len(html) and html[idx] != "{":
            idx += 1
        if idx >= len(html):
            raise ValueError("INIT_STATE 格式异常")

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
                    json_str = html[idx: cursor + 1]
                    break
        else:
            raise ValueError("INIT_STATE JSON 截断")

        data = json.loads(json_str)
        return data

    @staticmethod
    def _parse_init_item(init_data: dict, video_id: str) -> Optional[dict]:
        """从 INIT_STATE 数据中提取视频信息"""
        for key, value in init_data.items():
            # 新格式：photo 直接在 dict value 中
            if isinstance(value, dict) and "photo" in value:
                photo = value["photo"]
                if isinstance(photo, dict) and (photo.get("photoUrl") or photo.get("mainMvUrls")):
                    return KuaishouParser._normalize_photo_data(value)

            if not isinstance(value, str):
                continue
            try:
                response = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue

            if not isinstance(response, dict):
                continue

            vd = response.get("visionVideoDetail") or response
            if isinstance(vd, dict) and vd.get("photo"):
                return KuaishouParser._normalize_vision_detail(vd)
            if "photo" in response and isinstance(response.get("photo"), dict):
                return KuaishouParser._normalize_vision_detail(response)

        return None

    @staticmethod
    def _normalize_photo_data(data: dict) -> dict:
        """从新格式的 INIT_STATE photo 数据归一化（适配 m.gifshow.com 页面结构）"""
        photo = data.get("photo", {})
        counts = data.get("counts", {}) or {}

        # mainMvUrls: [{cdn, url}, ...] → 取第一个 url
        mv_urls = photo.get("mainMvUrls", [])
        photo_url = mv_urls[0]["url"] if mv_urls else ""

        # coverUrls: [{cdn, url}, ...]
        cover_urls = photo.get("coverUrls", [])
        cover_url = cover_urls[0]["url"] if cover_urls else ""

        # manifest: adaptationSet
        manifest = photo.get("manifest", {})

        return {
            "id": photo.get("photoId", ""),
            "caption": photo.get("caption", ""),
            "duration": photo.get("duration", 0),
            "coverUrl": cover_url,
            "photoUrl": photo_url,
            "likeCount": photo.get("likeCount", ""),
            "viewCount": photo.get("viewCount", ""),
            "width": photo.get("width", 0),
            "height": photo.get("height", 0),
            "author": {
                "id": photo.get("userId", ""),
                "name": photo.get("userName", ""),
                "headerUrl": photo.get("headUrl", ""),
            },
            "manifest": manifest,
        }

    # ── 构建结果 ─────────────────────────────────────────────

    @staticmethod
    def _get_representations(item_info: dict) -> dict:
        """提取所有视频清晰度版本，返回 {height: {url, width, height, label}}"""
        representations = {}

        # 从 manifest 提取
        manifest = item_info.get("manifest", {})
        adaptation_set = manifest.get("adaptationSet", [])
        for adaptation in adaptation_set:
            for rep in adaptation.get("representation", []):
                url = rep.get("url", "")
                if not url:
                    continue
                h = rep.get("height", 0)
                representations[h] = {
                    "url": url,
                    "width": rep.get("width", 0),
                    "height": h,
                    "label": rep.get("qualityLabel", ""),
                }

        return representations

    @staticmethod
    def _get_best_video_url(representations: dict) -> str:
        """获取最高质量的视频 URL"""
        if not representations:
            raise ValueError("未找到视频播放地址")
        best = max(representations.keys())
        return representations[best]["url"]

    @staticmethod
    def _get_video_url_by_quality(representations: dict, target_height: int = 0) -> str:
        """根据目标高度获取视频 URL，target_height=0 表示最高质量"""
        if not representations:
            raise ValueError("未找到视频播放地址")

        if target_height == 0:
            best = max(representations.keys())
            return representations[best]["url"]

        available = [h for h in representations if h <= target_height]
        if available:
            best = max(available)
        else:
            best = min(representations.keys())
        return representations[best]["url"]

    def _build_result(self, item_info: dict, video_id: str) -> dict:
        """构建与 yt-dlp 解析结果兼容的统一格式"""
        caption = item_info.get("caption") or ""
        title = caption or f"快手视频_{video_id}"

        author_info = item_info.get("author", {}) or {}
        duration_ms = item_info.get("duration", 0)
        duration_sec = duration_ms // 1000 if duration_ms > 1000 else duration_ms

        cover_url = item_info.get("coverUrl", "") or item_info.get("posterUrl", "")

        representations = self._get_representations(item_info)
        heights = sorted(representations.keys(), reverse=True)

        author_name = author_info.get("name", "") or author_info.get("nickname", "快手用户")
        author_id = author_info.get("id", "") or author_info.get("userId", "")
        author_avatar = author_info.get("headerUrl", "") or author_info.get("avatarUrl", "")

        like_count = item_info.get("likeCount", "") or item_info.get("realLikeCount", "")
        view_count = item_info.get("viewCount", "") or item_info.get("playCount", "")

        if heights:
            best_rep = representations[heights[0]]
            width = best_rep.get("width", 0)
            height = best_rep.get("height", 0)
        else:
            width = item_info.get("width", 0)
            height = item_info.get("height", 0)

        formats = []
        photo_url = item_info.get("photoUrl", "")
        for h in heights:
            rep = representations[h]
            label_suffix = f" ({rep.get('label', '')})" if rep.get("label") else ""
            formats.append({
                "format_id": f"kuaishou_{h}p",
                "ext": "mp4",
                "resolution": f"{rep.get('width', 0)}x{h}" if rep.get("width") else "auto",
                "height": h,
                "filesize": None,
                "filesize_approx": None,
                "vcodec": "h264",
                "acodec": "aac",
                "has_audio": True,
                "label": f"无水印 {h}P{label_suffix}",
                "_direct_url": photo_url or rep["url"],
            })

        if photo_url or heights:
            formats.append({
                "format_id": "kuaishou_mp3",
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
            "thumbnail": cover_url,
            "duration": duration_sec,
            "duration_string": self._fmt_duration(duration_sec),
            "uploader": author_name,
            "uploader_id": author_id,
            "uploader_avatar": author_avatar,
            "platform": "快手",
            "view_count": view_count,
            "like_count": like_count,
            "upload_date": "",
            "description": title,
            "formats": formats,
            "subtitles": {},
            "automatic_captions": {},
        }

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
