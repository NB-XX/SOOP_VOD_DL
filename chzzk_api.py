"""CHZZK API 封装模块"""
import json
import shutil
import subprocess
import time
import requests
from typing import Optional

from api import Cache

CHZZK_API_URL = "https://api.chzzk.naver.com/service"
NEONPLAYER_URL = "https://apis.naver.com/neonplayer/vodplay/v1/playback"


class ChzzkAPI:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,ko-CN;q=0.8,ko-KR;q=0.7,ko;q=0.6,en-US;q=0.5,en;q=0.4",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Origin": "https://chzzk.naver.com",
            "Referer": "https://chzzk.naver.com/",
            "front-client-platform-type": "PC",
            "front-client-product-type": "web",
            "if-modified-since": "Mon, 26 Jul 1997 05:00:00 GMT",
            "priority": "u=1, i",
        })
        self.timeout = 30
        self.max_retries = 3
        self.retry_delay = 2
        self.image_proxy = ""
        self.yt_dlp_path = shutil.which("yt-dlp")

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        last_error = None

        for attempt in range(self.max_retries):
            try:
                if method.upper() == "GET":
                    resp = self.session.get(url, **kwargs)
                else:
                    resp = self.session.post(url, **kwargs)
                resp.raise_for_status()
                return resp
            except (requests.exceptions.SSLError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    print(f"[CHZZK API] 请求失败 (尝试 {attempt + 1}/{self.max_retries}): {type(e).__name__}")
                    time.sleep(self.retry_delay * (attempt + 1))
                continue
            except requests.exceptions.HTTPError as e:
                raise e

        raise last_error

    def set_cookie(self, cookie: str):
        if cookie:
            self.session.headers["Cookie"] = cookie
        else:
            self.session.headers.pop("Cookie", None)

    def set_device_id(self, device_id: str):
        if device_id:
            self.session.headers["deviceid"] = device_id
        else:
            self.session.headers.pop("deviceid", None)

    def set_proxy(self, proxy: str):
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        else:
            self.session.proxies = {}

    def set_image_proxy(self, proxy: str):
        self.image_proxy = proxy or ""

    def set_yt_dlp_path(self, yt_dlp_path: str):
        self.yt_dlp_path = yt_dlp_path or shutil.which("yt-dlp")

    def _iter_playback_urls(self, node):
        if isinstance(node, dict):
            value = node.get("value")
            if isinstance(value, str) and value.startswith("http"):
                yield value

            path = node.get("path")
            if isinstance(path, str) and path.startswith("http"):
                yield path

            source = node.get("source")
            if isinstance(source, str) and source.startswith("http"):
                yield source

            for item in node.values():
                yield from self._iter_playback_urls(item)
        elif isinstance(node, list):
            for item in node:
                yield from self._iter_playback_urls(item)

    def _extract_best_http_from_playback(self, playback: dict, vod_status: str) -> Optional[dict]:
        best = None
        for period in playback.get("period", []):
            for adaptation in period.get("adaptationSet", []):
                for rep in adaptation.get("representation", []):
                    candidate_urls = list(dict.fromkeys(self._iter_playback_urls(rep)))
                    if not candidate_urls:
                        continue

                    url = next((u for u in candidate_urls if ".mp4" in u.lower()), candidate_urls[0])
                    bandwidth = rep.get("bandwidth", 0) or 0
                    quality_id = rep.get("id", "") or rep.get("label", "")
                    if best is None or bandwidth > best.get("bandwidth", 0):
                        best = {
                            "download_type": "http",
                            "url": url,
                            "quality_id": quality_id,
                            "bandwidth": bandwidth,
                            "vod_status": vod_status
                        }
        return best

    def _extract_m3u8_from_playback(self, playback: dict, vod_status: str) -> Optional[dict]:
        urls = list(dict.fromkeys(self._iter_playback_urls(playback)))
        m3u8_url = next((u for u in urls if ".m3u8" in u.lower()), "")
        if not m3u8_url:
            return None
        return {
            "download_type": "m3u8",
            "url": m3u8_url,
            "vod_status": vod_status or "UPLOAD"
        }

    def _resolve_with_yt_dlp(self, video_no: int, vod_status: str) -> Optional[dict]:
        if not self.yt_dlp_path:
            return None

        cmd = [self.yt_dlp_path, "--no-check-certificate", "-g", f"https://chzzk.naver.com/video/{video_no}"]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=self.timeout,
            )
            if result.returncode != 0:
                print(f"yt-dlp 解析 CHZZK 失败: {result.stderr.strip()[:300]}")
                return None

            urls = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("http")]
            if not urls:
                return None

            http_url = next((u for u in urls if ".mp4" in u.lower()), "")
            if http_url:
                return {
                    "download_type": "http",
                    "url": http_url,
                    "quality_id": "yt-dlp",
                    "bandwidth": 0,
                    "vod_status": vod_status
                }

            m3u8_url = next((u for u in urls if ".m3u8" in u.lower()), urls[0])
            return {
                "download_type": "m3u8" if ".m3u8" in m3u8_url.lower() else "http",
                "url": m3u8_url,
                "quality_id": "yt-dlp",
                "bandwidth": 0,
                "vod_status": vod_status
            }
        except Exception as e:
            print(f"yt-dlp 兜底解析 CHZZK 失败: {e}")
            return None

    def get_followings(self, page: int = 0, size: int = 505) -> list[dict]:
        cache_key = f"{page}_{size}"
        cached = Cache.get_data("chzzk_followings", cache_key, Cache.FAVORITES_TTL)
        if cached:
            return cached

        try:
            resp = self._request_with_retry(
                "GET",
                f"{CHZZK_API_URL}/v1/channels/followings",
                params={"page": page, "size": size, "sortType": "FOLLOW"},
                headers={"Referer": "https://chzzk.naver.com/"}
            )
            data = resp.json().get("content", {}).get("followingList", [])
            Cache.save_data("chzzk_followings", cache_key, data)
            return data
        except Exception as e:
            print(f"获取 CHZZK 关注列表失败: {e}")
            return []

    def get_channel_videos(self, channel_id: str, page: int = 0, size: int = 18) -> dict:
        cache_key = f"{channel_id}_{page}_{size}"
        cached = Cache.get_data("chzzk_videos", cache_key, Cache.VOD_LIST_TTL)
        if cached:
            return cached

        try:
            resp = self._request_with_retry(
                "GET",
                f"{CHZZK_API_URL}/v1/channels/{channel_id}/videos",
                params={
                    "sortType": "LATEST",
                    "pagingType": "PAGE",
                    "page": page,
                    "size": size,
                    "publishDateAt": "",
                    "videoType": ""
                },
                headers={"Referer": f"https://chzzk.naver.com/{channel_id}/videos?sortType=LATEST&videoType=&page={page + 1}"}
            )
            data = resp.json().get("content", {})
            Cache.save_data("chzzk_videos", cache_key, data)
            return data
        except Exception as e:
            print(f"获取 CHZZK 录像列表失败: {e}")
            return {"data": [], "totalCount": 0, "totalPages": 0}

    def get_video_detail(self, video_no: int, use_cache: bool = True) -> Optional[dict]:
        cache_key = str(video_no)
        if use_cache:
            cached = Cache.get_data("chzzk_video_detail", cache_key, Cache.VOD_DETAIL_TTL)
            if cached:
                return cached

        try:
            resp = self._request_with_retry(
                "GET",
                f"{CHZZK_API_URL}/v3/videos/{video_no}",
                params={"dt": "6d41e"},
                headers={"Referer": f"https://chzzk.naver.com/video/{video_no}"}
            )
            data = resp.json().get("content")
            if data:
                Cache.save_data("chzzk_video_detail", cache_key, data)
            return data
        except Exception as e:
            print(f"获取 CHZZK 录像详情失败: {e}")
            return None

    def resolve_video_download(self, video_meta: dict) -> Optional[dict]:
        if not video_meta:
            return None

        vod_status = video_meta.get("vodStatus")
        video_no = video_meta.get("videoNo")

        if vod_status == "ABR_HLS":
            # ABR_HLS 视频使用 neonplayer v1 API 获取 MP4 下载链接
            # inKey 有时效性，可能需要刷新
            result = self._try_neonplayer_api(video_meta)
            if result:
                return result

            # 如果失败，尝试重新获取 inKey（绕过缓存）
            # 只有当原数据有 inKey 时才尝试刷新（无 inKey 说明视频受限，刷新也没用）
            if video_no and video_meta.get("inKey"):
                print(f"[CHZZK] neonplayer API 失败，尝试刷新 inKey...")
                refreshed = self.get_video_detail(video_no, use_cache=False)
                if refreshed and refreshed.get("inKey") != video_meta.get("inKey"):
                    result = self._try_neonplayer_api(refreshed)
                    if result:
                        return result

        playback_json = video_meta.get("liveRewindPlaybackJson")
        if playback_json:
            try:
                playback = json.loads(playback_json) if isinstance(playback_json, str) else playback_json
                m3u8 = self._extract_m3u8_from_playback(playback, vod_status)
                if m3u8:
                    return m3u8
            except Exception as e:
                print(f"解析 CHZZK HLS 下载链接失败: {e}")

        return self._resolve_with_yt_dlp(video_meta.get("videoNo"), vod_status)

    def _try_neonplayer_api(self, video_meta: dict) -> Optional[dict]:
        """尝试调用 neonplayer v1 API 获取播放信息"""
        in_key = video_meta.get("inKey")
        video_id = video_meta.get("videoId")
        vod_status = video_meta.get("vodStatus", "")

        if not in_key or not video_id:
            if vod_status == "ABR_HLS":
                blind_type = video_meta.get("blindType", "")
                tv_policy = video_meta.get("tvAppViewingPolicyType", "")
                print(f"[CHZZK] 视频缺少 videoId/inKey (blindType={blind_type}, tvPolicy={tv_policy})，可能受限")
            return None

        try:
            playback = self._request_with_retry(
                "GET",
                f"{NEONPLAYER_URL}/{video_id}",
                params={
                    "key": in_key,
                    "env": "real",
                    "lc": "en_US",
                    "cpl": "en_US",
                }
            ).json()

            best = self._extract_best_http_from_playback(playback, vod_status)
            if best:
                return best

            m3u8 = self._extract_m3u8_from_playback(playback, vod_status)
            if m3u8:
                return m3u8
        except Exception as e:
            print(f"解析 CHZZK MP4DASH 下载链接失败: {e}")

        return None
