"""SOOP API 封装模块"""
import os
import json
import hashlib
import time
import requests
from typing import Optional, Tuple
from dataclasses import dataclass

BASE_URL = "https://myapi.sooplive.co.kr"
CHAPI_URL = "https://chapi.sooplive.co.kr"
MAPI_URL = "https://api.m.sooplive.co.kr"
LOGIN_URL = "https://login.sooplive.co.kr"
STBBS_URL = "https://stbbs.sooplive.co.kr"

# 缓存目录
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
IMAGE_CACHE_DIR = os.path.join(CACHE_DIR, "images")
DATA_CACHE_DIR = os.path.join(CACHE_DIR, "data")

# 确保缓存目录存在
os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)
os.makedirs(DATA_CACHE_DIR, exist_ok=True)

@dataclass
class VODFile:
    idx: int
    duration: int
    file_order: int
    m3u8_url: str
    quality_info: list
    file_start: str
    file_title: str = ""

@dataclass
class VODInfo:
    title_no: int
    title: str
    user_nick: str
    user_id: str
    reg_date: str
    duration: int
    thumb: str
    files: list[VODFile]


class Cache:
    """本地缓存管理"""
    
    # 缓存过期时间（秒）
    IMAGE_TTL = 7 * 24 * 3600  # 图片缓存7天
    VOD_LIST_TTL = 5 * 60      # VOD列表缓存5分钟
    VOD_DETAIL_TTL = 24 * 3600 # VOD详情缓存24小时
    FAVORITES_TTL = 60         # 关注列表缓存1分钟
    
    @staticmethod
    def _get_cache_path(cache_type: str, key: str) -> str:
        """获取缓存文件路径"""
        safe_key = hashlib.md5(key.encode()).hexdigest()
        if cache_type == "image":
            # 保留原始扩展名
            ext = key.split(".")[-1].split("?")[0][:4] if "." in key else "jpg"
            return os.path.join(IMAGE_CACHE_DIR, f"{safe_key}.{ext}")
        else:
            return os.path.join(DATA_CACHE_DIR, f"{cache_type}_{safe_key}.json")
    
    @staticmethod
    def get_image(url: str) -> Optional[str]:
        """获取缓存的图片路径，如果存在且未过期"""
        if not url:
            return None
        cache_path = Cache._get_cache_path("image", url)
        if os.path.exists(cache_path):
            # 检查是否过期
            mtime = os.path.getmtime(cache_path)
            if time.time() - mtime < Cache.IMAGE_TTL:
                return cache_path
        return None
    
    @staticmethod
    def save_image(url: str, data: bytes) -> str:
        """保存图片到缓存"""
        cache_path = Cache._get_cache_path("image", url)
        with open(cache_path, "wb") as f:
            f.write(data)
        return cache_path
    
    @staticmethod
    def get_data(cache_type: str, key: str, ttl: int) -> Optional[dict]:
        """获取缓存的数据"""
        cache_path = Cache._get_cache_path(cache_type, key)
        if os.path.exists(cache_path):
            try:
                mtime = os.path.getmtime(cache_path)
                if time.time() - mtime < ttl:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        return json.load(f)
            except:
                pass
        return None
    
    @staticmethod
    def save_data(cache_type: str, key: str, data: dict):
        """保存数据到缓存"""
        cache_path = Cache._get_cache_path(cache_type, key)
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except:
            pass
    
    @staticmethod
    def clear_expired():
        """清理过期缓存"""
        now = time.time()
        cleared = 0
        
        # 清理图片缓存
        for f in os.listdir(IMAGE_CACHE_DIR):
            path = os.path.join(IMAGE_CACHE_DIR, f)
            if os.path.isfile(path) and now - os.path.getmtime(path) > Cache.IMAGE_TTL:
                try:
                    os.remove(path)
                    cleared += 1
                except:
                    pass
        
        # 清理数据缓存
        for f in os.listdir(DATA_CACHE_DIR):
            path = os.path.join(DATA_CACHE_DIR, f)
            if os.path.isfile(path) and now - os.path.getmtime(path) > Cache.VOD_DETAIL_TTL:
                try:
                    os.remove(path)
                    cleared += 1
                except:
                    pass
        
        return cleared
    
    @staticmethod
    def get_cache_size() -> dict:
        """获取缓存大小统计"""
        image_size = sum(os.path.getsize(os.path.join(IMAGE_CACHE_DIR, f)) 
                        for f in os.listdir(IMAGE_CACHE_DIR) if os.path.isfile(os.path.join(IMAGE_CACHE_DIR, f)))
        data_size = sum(os.path.getsize(os.path.join(DATA_CACHE_DIR, f)) 
                       for f in os.listdir(DATA_CACHE_DIR) if os.path.isfile(os.path.join(DATA_CACHE_DIR, f)))
        return {
            "image_count": len(os.listdir(IMAGE_CACHE_DIR)),
            "image_size_mb": round(image_size / 1024 / 1024, 2),
            "data_count": len(os.listdir(DATA_CACHE_DIR)),
            "data_size_mb": round(data_size / 1024 / 1024, 2),
            "total_mb": round((image_size + data_size) / 1024 / 1024, 2)
        }

class SoopAPI:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://www.sooplive.co.kr",
            "Referer": "https://www.sooplive.co.kr/",
            "Sec-Ch-Ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        })
        self.timeout = 30  # 请求超时时间
        self.max_retries = 3  # 最大重试次数
        self.retry_delay = 2  # 重试间隔（秒）
        self.image_proxy = ""
    
    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """带重试的请求方法，处理SSL错误和网络问题"""
        kwargs.setdefault('timeout', self.timeout)
        last_error = None
        
        for attempt in range(self.max_retries):
            try:
                if method.upper() == 'GET':
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
                    print(f"[API] 请求失败 (尝试 {attempt + 1}/{self.max_retries}): {type(e).__name__}")
                    time.sleep(self.retry_delay * (attempt + 1))  # 递增延迟
                continue
            except requests.exceptions.HTTPError as e:
                # HTTP错误不重试
                raise e
        
        raise last_error
    
    def set_cookie(self, cookie: str):
        self.session.headers["Cookie"] = cookie
    
    def set_proxy(self, proxy: str):
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        else:
            self.session.proxies = {}

    def set_image_proxy(self, proxy: str):
        self.image_proxy = proxy or ""
    
    def login(self, username: str, password: str) -> Tuple[bool, str]:
        """账号密码登录，返回(成功, cookie或错误信息)"""
        try:
            url = f"{LOGIN_URL}/app/LoginAction.php"
            data = {"szWork": "login", "szType": "json", "szUid": username, "szPassword": password}
            resp = self.session.post(url, data=data)
            result = resp.json()
            if result.get("RESULT") == 1:
                cookies = "; ".join([f"{k}={v}" for k, v in resp.cookies.items()])
                self.set_cookie(cookies)
                return True, cookies
            return False, result.get("MSG", "登录失败")
        except Exception as e:
            return False, str(e)
    
    def get_favorites(self) -> list[dict]:
        """获取关注的主播列表"""
        # 检查缓存
        cached = Cache.get_data("favorites", "list", Cache.FAVORITES_TTL)
        if cached:
            return cached
        
        try:
            resp = self._request_with_retry(
                'GET',
                f"{BASE_URL}/api/favorite",
                headers={"priority": "u=1, i"}
            )
            data = resp.json().get("data", [])
            Cache.save_data("favorites", "list", data)
            return data
        except Exception as e:
            print(f"获取关注列表失败: {e}")
            return []
    
    def get_streamer_vods(self, user_id: str, page: int = 1, per_page: int = 60) -> dict:
        """获取主播的录像列表"""
        cache_key = f"{user_id}_{page}_{per_page}"
        cached = Cache.get_data("vod_list", cache_key, Cache.VOD_LIST_TTL)
        if cached:
            return cached
        
        try:
            resp = self._request_with_retry(
                'GET',
                f"{CHAPI_URL}/api/{user_id}/vods/review",
                params={
                    "keyword": "", 
                    "orderby": "reg_date", 
                    "page": page, 
                    "field": "title,contents,user_nick,user_id", 
                    "per_page": per_page, 
                    "start_date": "", 
                    "end_date": ""
                },
                headers={"priority": "u=1, i"}
            )
            data = resp.json()
            Cache.save_data("vod_list", cache_key, data)
            return data
        except Exception as e:
            print(f"获取录像列表失败: {e}")
            return {"data": [], "meta": {}}
    
    def get_vod_detail(self, title_no: int) -> Optional[VODInfo]:
        """获取录像详细信息，包含各章节M3U8"""
        # 检查缓存
        cache_key = str(title_no)
        cached = Cache.get_data("vod_detail", cache_key, Cache.VOD_DETAIL_TTL)
        if cached:
            try:
                files = [VODFile(**f) for f in cached.get("files", [])]
                return VODInfo(
                    title_no=cached["title_no"],
                    title=cached["title"],
                    user_nick=cached["user_nick"],
                    user_id=cached["user_id"],
                    reg_date=cached["reg_date"],
                    duration=cached["duration"],
                    thumb=cached["thumb"],
                    files=files
                )
            except:
                pass
        
        try:
            resp = self._request_with_retry(
                'POST',
                f"{MAPI_URL}/station/video/a/view",
                data={"nTitleNo": str(title_no), "nApiLevel": "11", "nPlaylistIdx": "0"},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://vod.sooplive.co.kr",
                    "Referer": f"https://vod.sooplive.co.kr/player/{title_no}",
                }
            )
            result = resp.json()
            if result.get("result") != 1:
                return None
            
            vod = result["data"]
            files = []
            for f in vod.get("files", []):
                # 获取最高画质M3U8 - 优先original，其次hd8k，再次hd4k
                quality_info = f.get("quality_info", [])
                best_url = f.get("file", "")
                best_bitrate = 0
                
                for q in quality_info:
                    name = q.get("name", "")
                    bitrate_str = q.get("bitrate", "0k").replace("k", "")
                    try:
                        bitrate = int(bitrate_str)
                    except:
                        bitrate = 0
                    
                    # 选择最高码率的非adaptive选项
                    if name != "adaptive" and bitrate > best_bitrate:
                        best_bitrate = bitrate
                        best_url = q.get("file", best_url)
                
                files.append(VODFile(
                    idx=f.get("idx", 0),
                    duration=f.get("duration", 0),
                    file_order=f.get("file_order", 1),
                    m3u8_url=best_url,
                    quality_info=quality_info,
                    file_start=f.get("file_start", ""),
                    file_title=f.get("file_title", "")
                ))
            
            vod_info = VODInfo(
                title_no=vod.get("title_no", title_no),
                title=vod.get("title", ""),
                user_nick=vod.get("writer_nick", ""),
                user_id=vod.get("bj_id", ""),
                reg_date=vod.get("write_tm", "").split(" ~ ")[0] if vod.get("write_tm") else "",
                duration=vod.get("total_file_duration", 0),
                thumb=vod.get("thumb", ""),
                files=files,
            )
            
            # 保存到缓存
            cache_data = {
                "title_no": vod_info.title_no,
                "title": vod_info.title,
                "user_nick": vod_info.user_nick,
                "user_id": vod_info.user_id,
                "reg_date": vod_info.reg_date,
                "duration": vod_info.duration,
                "thumb": vod_info.thumb,
                "files": [{"idx": f.idx, "duration": f.duration, "file_order": f.file_order,
                          "m3u8_url": f.m3u8_url, "quality_info": f.quality_info, "file_start": f.file_start,
                          "file_title": f.file_title}
                         for f in files]
            }
            Cache.save_data("vod_detail", cache_key, cache_data)
            
            return vod_info
        except Exception as e:
            print(f"获取录像详情失败: {e}")
            return None
    
    def get_chapter_titles(self, title_no: int) -> list[dict]:
        """获取录像的章节标题列表"""
        cache_key = f"chapters_{title_no}"
        cached = Cache.get_data("chapters", cache_key, Cache.VOD_DETAIL_TTL)
        if cached:
            return cached
        
        try:
            resp = self._request_with_retry(
                'GET',
                f"{STBBS_URL}/api/chapter/Controllers/ChapterListController.php",
                params={"nTitleNo": str(title_no), "szFileType": "REVIEW"}
            )
            result = resp.json()
            if result.get("result") == 1:
                chapters = result.get("data", [])
                Cache.save_data("chapters", cache_key, chapters)
                return chapters
        except Exception as e:
            print(f"获取章节标题失败: {e}")
        return []
    
    def get_cached_image(self, url: str) -> Optional[str]:
        """获取缓存的图片路径，如果不存在则下载"""
        if not url:
            return None
        
        # 检查缓存
        cached_path = Cache.get_image(url)
        if cached_path:
            return cached_path
        
        # 下载图片
        try:
            # 添加协议前缀
            if url.startswith("//"):
                url = "https:" + url

            proxies = {"http": self.image_proxy, "https": self.image_proxy} if self.image_proxy else None
            resp = self.session.get(url, timeout=10, proxies=proxies)
            if resp.status_code == 200:
                return Cache.save_image(url, resp.content)
        except:
            pass
        return None
