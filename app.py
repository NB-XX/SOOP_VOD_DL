"""SOOP 直播录像下载器 - Flask后端"""
import os
import json
import threading
import time
import shutil
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request, send_file, Response

# 在 Windows 上使用系统证书存储解决 SSL 验证问题
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from api import SoopAPI, Cache
from chzzk_api import ChzzkAPI
from downloader import Downloader, get_output_path

app = Flask(__name__)
soop = SoopAPI()
chzzk = ChzzkAPI()
dl = Downloader(max_workers=3)  # 最多3个并行下载

CONFIG_FILE = "config.json"
DOWNLOADED_FILE = "downloaded.json"  # 已下载记录
HISTORY_FILE = "history.json"  # 下载历史

config = {
    "cookie": "",
    "chzzk_cookie": "",
    "chzzk_device_id": "",
    "download_dir": os.path.expanduser("~/Downloads/SOOP"),
    "m3u8dl_path": "",
    "aria2c_path": "",
    "ffmpeg_path": "",
    "yt_dlp_path": "",
    "proxy": "",
    "all_proxy": "",
    "status_proxy": "",
    "image_proxy": "",
    "download_proxy": "",
    "max_file_size_gb": 16,
    "split_enabled": True,
    "split_threshold_hours": 3.5,
    "split_segment_hours": 2.5
}

# 自动下载配置
auto_download_config = {
    "enabled": False,
    "start_hour": 22,  # 开始检测时间
    "end_hour": 4,     # 结束检测时间
    "interval_minutes": 30,  # 检测间隔
    "earliest_date": "",  # 最早日期，格式 YYYY-MM-DD
    "streamers": []  # 要自动下载的主播ID列表
}

chzzk_auto_download_config = {
    "enabled": False,
    "start_hour": 22,
    "end_hour": 4,
    "interval_minutes": 30,
    "earliest_date": "",
    "streamers": []
}

# 已下载记录 {title_no: {"date": "...", "title": "...", "user_nick": "..."}}
downloaded_records = {}

# 下载历史记录（最近50条）
download_history = []
MAX_HISTORY = 50

# 多任务下载状态管理
download_tasks = {}
download_lock = threading.Lock()

# 自动下载线程
auto_download_thread = None
auto_download_running = False
chzzk_auto_download_thread = None
chzzk_auto_download_running = False

# 重试配置
MAX_RETRY = 3

def detect_tool_paths():
    return {
        "m3u8dl_path": shutil.which("N_m3u8DL-RE") or shutil.which("N_m3u8DL-RE.exe") or "",
        "aria2c_path": shutil.which("aria2c") or shutil.which("aria2c.exe") or "",
        "ffmpeg_path": shutil.which("ffmpeg") or shutil.which("ffmpeg.exe") or "",
        "yt_dlp_path": shutil.which("yt-dlp") or shutil.which("yt-dlp.exe") or ""
    }

def load_config():
    global config, auto_download_config, chzzk_auto_download_config, downloaded_records, download_history
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                config.update({k: v for k, v in data.items() if k in config})
                if "auto_download" in data:
                    auto_download_config.update(data["auto_download"])
                if "chzzk_auto_download" in data:
                    chzzk_auto_download_config.update(data["chzzk_auto_download"])
        except: pass

    # 兼容旧配置：旧版 proxy 作为“全部应用代理”
    if config.get("proxy") and not config.get("all_proxy"):
        config["all_proxy"] = config["proxy"]

    detected_tools = detect_tool_paths()
    for key, detected_path in detected_tools.items():
        if detected_path and not config.get(key):
            config[key] = detected_path

    apply_runtime_config()
    
    # 加载已下载记录
    if os.path.exists(DOWNLOADED_FILE):
        try:
            with open(DOWNLOADED_FILE, 'r', encoding='utf-8') as f:
                downloaded_records.update(json.load(f))
        except: pass
    
    # 加载下载历史
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                download_history.extend(json.load(f))
        except: pass

def save_config():
    data = {
        **config,
        "auto_download": auto_download_config,
        "chzzk_auto_download": chzzk_auto_download_config
    }
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_effective_proxy(proxy_key: str) -> str:
    return config.get(proxy_key, '').strip() or config.get('all_proxy', '').strip() or config.get('proxy', '').strip()

def apply_runtime_config():
    soop.set_cookie(config.get("cookie", ""))
    soop.set_proxy(get_effective_proxy("status_proxy"))
    soop.set_image_proxy(get_effective_proxy("image_proxy"))
    chzzk.set_cookie(config.get("chzzk_cookie", ""))
    chzzk.set_device_id(config.get("chzzk_device_id", ""))
    chzzk.set_proxy(get_effective_proxy("status_proxy"))
    chzzk.set_image_proxy(get_effective_proxy("image_proxy"))
    chzzk.set_yt_dlp_path(config.get("yt_dlp_path", ""))
    dl.set_cookie(config.get("cookie", ""))
    dl.set_proxy(get_effective_proxy("download_proxy"))
    dl.set_tool_paths(
        config.get("m3u8dl_path", ""),
        config.get("aria2c_path", ""),
        config.get("ffmpeg_path", "")
    )
    dl.set_split_config(
        enabled=config.get("split_enabled", True),
        threshold_hours=config.get("split_threshold_hours", 3.5),
        segment_hours=config.get("split_segment_hours", 2.5)
    )

def get_download_record_key(platform: str, video_id) -> str:
    return f"{platform}:{video_id}"

def get_download_task_key(vod: dict):
    platform = (vod or {}).get("platform", "soop")
    if platform == "chzzk":
        video_no = (vod or {}).get("video_no") or (vod or {}).get("title_no")
        return get_download_record_key("chzzk", video_no)
    return (vod or {}).get("title_no")

def build_chzzk_vod_payload(video: dict, download_info: dict | None = None) -> dict:
    download_info = download_info or chzzk.resolve_video_download(video) or {}
    duration_ms = (video.get("duration", 0) or 0) * 1000
    file_entry = {
        "idx": 1,
        "duration": duration_ms,
        "file_order": 1,
        "m3u8_url": download_info.get("url", ""),
        "download_type": download_info.get("download_type", "m3u8"),
        "file_start": video.get("publishDate", ""),
        "quality_info": download_info.get("quality_id") or download_info.get("vod_status", ""),
        "file_title": video.get("videoTitle", "")
    }
    return {
        "platform": "chzzk",
        "task_id": get_download_record_key("chzzk", video["videoNo"]),
        "title_no": video["videoNo"],
        "video_no": video["videoNo"],
        "title": video.get("videoTitle", ""),
        "user_nick": video.get("channel", {}).get("channelName", ""),
        "user_id": video.get("channel", {}).get("channelId", ""),
        "reg_date": video.get("publishDate", ""),
        "duration": duration_ms,
        "thumb": video.get("thumbnailImageUrl", ""),
        "chapter_titles": [],
        "files": [file_entry] if download_info.get("url") else []
    }

def save_downloaded():
    with open(DOWNLOADED_FILE, 'w', encoding='utf-8') as f:
        json.dump(downloaded_records, f, ensure_ascii=False, indent=2)

def save_history():
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(download_history[-MAX_HISTORY:], f, ensure_ascii=False, indent=2)

def add_to_history(title_no, title, user_nick, status, files=None):
    """添加下载记录到历史"""
    download_history.append({
        "title_no": title_no,
        "title": title,
        "user_nick": user_nick,
        "status": status,
        "files": files or [],
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    # 保持最大数量
    while len(download_history) > MAX_HISTORY:
        download_history.pop(0)
    save_history()

def get_file_value(file_obj, key, default=None):
    """同时兼容 dict 和 dataclass 对象的字段读取"""
    if isinstance(file_obj, dict):
        return file_obj.get(key, default)
    return getattr(file_obj, key, default)

def build_chapter_entries(files, chapter_titles=None):
    """为下载任务构建统一的章节信息，并补齐章节标题"""
    chapter_titles = sorted(chapter_titles or [], key=lambda x: x.get("time_sec", 0))
    chapters = []
    accumulated_time = 0

    for file_obj in files:
        duration = get_file_value(file_obj, "duration", 0) or 0
        fallback_title = get_file_value(file_obj, "file_title", "") or ""
        file_start_sec = accumulated_time
        accumulated_time += duration / 1000

        matched_title = ""
        for title_info in reversed(chapter_titles):
            if title_info.get("time_sec", 0) <= file_start_sec:
                matched_title = title_info.get("title", "") or ""
                break

        if not matched_title and chapter_titles:
            matched_title = chapter_titles[0].get("title", "") or ""

        chapters.append({
            "m3u8_url": get_file_value(file_obj, "m3u8_url", ""),
            "file_order": get_file_value(file_obj, "file_order", 1),
            "duration": duration,
            "file_title": matched_title or fallback_title
        })

    return chapters

def send_notification(title, message):
    """发送系统通知"""
    print(f"[通知] 尝试发送通知: {title} - {message}")
    try:
        import platform
        system = platform.system()
        print(f"[通知] 系统类型: {system}")
        
        if system == 'Windows':
            # Windows 10/11 Toast 通知
            try:
                from win10toast import ToastNotifier
                print(f"[通知] 使用 win10toast 库")
                toaster = ToastNotifier()
                # 清理消息中的换行符，避免显示问题
                clean_message = message.replace('\n', ' ')
                toaster.show_toast(title, clean_message, duration=5, threaded=True)
                print(f"[通知] ✓ win10toast 发送成功")
                return
            except ImportError:
                print(f"[通知] win10toast 未安装，使用 PowerShell 后备方案")
            except Exception as e:
                print(f"[通知] win10toast 发送失败: {e}，使用 PowerShell 后备方案")
            
            # 后备方案：使用 PowerShell
            try:
                import subprocess
                # 转义特殊字符，避免 PowerShell 脚本错误
                safe_title = title.replace('"', '""').replace("'", "''")
                safe_message = message.replace('"', '""').replace("'", "''").replace('\n', ' ')
                
                ps_script = f'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$textNodes = $template.GetElementsByTagName("text")
$textNodes.Item(0).AppendChild($template.CreateTextNode("{safe_title}")) | Out-Null
$textNodes.Item(1).AppendChild($template.CreateTextNode("{safe_message}")) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("SOOP下载器").Show($toast)
'''
                print(f"[通知] 执行 PowerShell 脚本...")
                result = subprocess.run(
                    ["powershell", "-Command", ps_script], 
                    capture_output=True, 
                    creationflags=0x08000000,
                    encoding='utf-8',
                    errors='ignore',
                    timeout=5
                )
                if result.returncode == 0:
                    print(f"[通知] ✓ PowerShell 发送成功")
                else:
                    print(f"[通知] ✗ PowerShell 执行失败 (返回码: {result.returncode})")
                    if result.stderr:
                        print(f"[通知] 错误信息: {result.stderr[:200]}")
            except Exception as e:
                print(f"[通知] ✗ PowerShell 执行异常: {e}")
                
        elif system == 'Darwin':
            # macOS
            import subprocess
            subprocess.run(['osascript', '-e', f'display notification "{message}" with title "{title}"'])
            print(f"[通知] ✓ macOS 通知发送成功")
        else:
            # Linux
            import subprocess
            subprocess.run(['notify-send', title, message])
            print(f"[通知] ✓ Linux 通知发送成功")
    except Exception as e:
        print(f"[通知] ✗ 发送失败: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

def check_disk_space(path, required_gb=5):
    """检查磁盘空间是否足够"""
    try:
        # 确保目录存在
        os.makedirs(path, exist_ok=True)
        total, used, free = shutil.disk_usage(path)
        free_gb = free / (1024 ** 3)
        return free_gb >= required_gb, free_gb
    except Exception as e:
        print(f"[磁盘检查] 错误: {e}")
        return True, 0  # 出错时默认允许下载

def cleanup_old_tasks():
    """清理已完成的旧任务，保留最近20条"""
    with download_lock:
        # 获取所有已完成的任务
        completed = [(k, v) for k, v in download_tasks.items() if not v.get("running")]
        # 按时间排序（如果有的话）
        if len(completed) > 20:
            # 删除最旧的
            to_remove = completed[:-20]
            for title_no, _ in to_remove:
                del download_tasks[title_no]
            print(f"[清理] 已清理 {len(to_remove)} 条旧任务记录")

load_config()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'POST':
        data = request.json
        for key in ['download_dir', 'm3u8dl_path', 'aria2c_path', 'ffmpeg_path', 'yt_dlp_path', 'proxy', 'all_proxy', 'status_proxy', 'image_proxy', 'download_proxy', 'max_file_size_gb', 'split_enabled', 'split_threshold_hours', 'split_segment_hours']:
            if key in data:
                config[key] = data[key]
        for key in ['cookie', 'chzzk_cookie', 'chzzk_device_id']:
            if key in data and data[key].strip():
                config[key] = data[key]
        if config.get('proxy') and not config.get('all_proxy'):
            config['all_proxy'] = config['proxy']
        apply_runtime_config()
        save_config()
        return jsonify({"success": True})
    return jsonify({
        "cookie": config.get('cookie', ''),
        "chzzk_cookie": config.get('chzzk_cookie', ''),
        "chzzk_device_id": config.get('chzzk_device_id', ''),
        "download_dir": config.get('download_dir', ''),
        "m3u8dl_path": config.get('m3u8dl_path', ''),
        "aria2c_path": config.get('aria2c_path', ''),
        "ffmpeg_path": config.get('ffmpeg_path', ''),
        "yt_dlp_path": config.get('yt_dlp_path', ''),
        "proxy": config.get('proxy', ''),
        "all_proxy": config.get('all_proxy', ''),
        "status_proxy": config.get('status_proxy', ''),
        "image_proxy": config.get('image_proxy', ''),
        "download_proxy": config.get('download_proxy', ''),
        "max_file_size_gb": config.get('max_file_size_gb', 16),
        "split_enabled": config.get('split_enabled', True),
        "split_threshold_hours": config.get('split_threshold_hours', 3.5),
        "split_segment_hours": config.get('split_segment_hours', 2.5)
    })

@app.route('/api/tool_paths/detect')
def detect_tools_api():
    detected = detect_tool_paths()
    for key, value in detected.items():
        if value:
            config[key] = value
    apply_runtime_config()
    save_config()
    return jsonify(detected)

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    ok, result = soop.login(data.get('username', ''), data.get('password', ''))
    if ok:
        config['cookie'] = result
        dl.set_cookie(result)
        save_config()
    return jsonify({"success": ok, "error": None if ok else result})

@app.route('/api/favorites')
def get_favorites():
    return jsonify(soop.get_favorites())

@app.route('/api/chzzk/followings')
def get_chzzk_followings():
    return jsonify(chzzk.get_followings())

@app.route('/api/vods/<user_id>')
def get_vods(user_id):
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    return jsonify(soop.get_streamer_vods(user_id, page, per_page))

@app.route('/api/chzzk/channel/<channel_id>/videos')
def get_chzzk_videos(channel_id):
    page = request.args.get('page', 0, type=int)
    size = request.args.get('size', 18, type=int)
    return jsonify(chzzk.get_channel_videos(channel_id, page, size))

@app.route('/api/chzzk/video/<int:video_no>')
def get_chzzk_video_detail(video_no):
    video = chzzk.get_video_detail(video_no)
    if not video:
        return jsonify({"error": "获取失败"}), 404

    download_info = chzzk.resolve_video_download(video)
    return jsonify({
        **video,
        "download_info": download_info,
        "vod_payload": build_chzzk_vod_payload(video, download_info) if download_info else None
    })

@app.route('/api/vod/<int:title_no>')
def get_vod_detail(title_no):
    vod = soop.get_vod_detail(title_no)
    if not vod:
        return jsonify({"error": "获取失败"}), 404
    
    # 获取章节标题
    chapter_titles = soop.get_chapter_titles(title_no)
    print(f"[DEBUG] 录像 {title_no} 章节标题: {chapter_titles}")
    
    return jsonify({
        "platform": "soop",
        "task_id": title_no,
        "title_no": vod.title_no, 
        "title": vod.title, 
        "user_nick": vod.user_nick,
        "user_id": vod.user_id, 
        "reg_date": vod.reg_date, 
        "duration": vod.duration,
        "thumb": vod.thumb,
        "chapter_titles": chapter_titles,
        "files": [{
            "idx": f.idx, 
            "duration": f.duration, 
            "file_order": f.file_order,
            "m3u8_url": f.m3u8_url, 
            "file_start": f.file_start,
            "quality_info": f.quality_info,
            "file_title": f.file_title
        } for f in vod.files]
    })

@app.route('/api/download', methods=['POST'])
def start_download():
    data = request.json
    vod, chapters = data.get('vod'), data.get('chapters', [])
    if not vod or not chapters:
        return jsonify({"error": "参数错误"}), 400
    
    # 调试：打印接收到的章节信息
    print(f"[DEBUG] 接收到 {len(chapters)} 个章节:")
    for ch in chapters:
        print(f"  章节 {ch.get('file_order')}: file_title='{ch.get('file_title', '')}', chapter_title='{ch.get('chapter_title', '')}'")
    
    task_key = get_download_task_key(vod)
    title_no = vod.get('title_no', 0)
    platform = vod.get('platform', 'soop')
    if not task_key:
        return jsonify({"error": "缺少任务标识"}), 400
    
    # 检查该录像是否已在下载
    with download_lock:
        if task_key in download_tasks and download_tasks[task_key].get("running"):
            return jsonify({"error": "该录像正在下载中"}), 400
    
    # 检查磁盘空间（预估需要的空间：每小时约2GB）
    total_duration_ms = sum(ch.get('duration', 0) for ch in chapters)
    estimated_gb = max(5, (total_duration_ms / 1000 / 3600) * 2)  # 至少5GB
    has_space, free_gb = check_disk_space(config['download_dir'], estimated_gb)
    if not has_space:
        return jsonify({"error": f"磁盘空间不足，需要约{estimated_gb:.1f}GB，当前剩余{free_gb:.1f}GB"}), 400
    
    total_files = len(vod.get('files', chapters))
    tasks = [{
        "m3u8_url": ch['m3u8_url'],
        "download_type": ch.get("download_type", "m3u8"),
        "output_path": get_output_path(
            config['download_dir'], 
            vod['user_nick'], 
            vod['reg_date'], 
            vod['title'], 
            ch['file_order'], 
            total_files,
            chapter_title=ch.get('chapter_title') or ch.get('file_title', '')
        ),
        "chapter": ch['file_order'],
        "duration": ch.get('duration', 0),
        "status": "pending",
        "progress": "",
        "progress_percent": 0,
        "result": "",
        "retry_count": 0  # 重试计数
    } for ch in chapters]
    
    task_status = {
        "running": True, 
        "tasks": tasks, 
        "current": 0, 
        "total": len(tasks), 
        "message": "开始下载",
        "current_file": "",
        "vod_title": vod['title'],
        "user_nick": vod['user_nick'],
        "title_no": title_no,
        "task_id": task_key,
        "platform": platform,
        "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    
    with download_lock:
        download_tasks[task_key] = task_status
    
    # 清理旧任务
    cleanup_old_tasks()
    
    # 发送开始下载通知
    send_notification(
        "开始下载",
        f"{vod['user_nick']} - {vod['title'][:30]}{'...' if len(vod['title']) > 30 else ''} (共{len(tasks)}章节)"
    )
    
    # 启动下载线程
    threading.Thread(target=run_downloads, args=(task_key,), daemon=True).start()
    return jsonify({"success": True, "title_no": title_no, "task_id": task_key})

def run_downloads(task_key):
    with download_lock:
        if task_key not in download_tasks:
            return
        status = download_tasks[task_key]
    
    print(f"\n{'='*50}")
    print(f"[下载任务启动] 录像 {task_key} 共 {len(status['tasks'])} 个章节")
    print(f"{'='*50}")
    
    # 准备任务列表
    download_task_list = []
    for task in status["tasks"]:
        download_task_list.append({
            "m3u8_url": task["m3u8_url"],
            "download_type": task.get("download_type", "m3u8"),
            "output_path": task["output_path"],
            "chapter": task["chapter"],
            "duration": task.get("duration", 0)  # 传递时长用于切分判断
        })
        task["status"] = "pending"
    
    # 进度回调
    def progress_callback(chapter, msg, progress_percent=0):
        with download_lock:
            if task_key not in download_tasks:
                return
            current_status = download_tasks[task_key]
            for task in current_status["tasks"]:
                if task["chapter"] == chapter:
                    task["progress"] = msg
                    # progress_percent 传入的是百分比*100（如21.88%传入2188），转换为0-100
                    task["progress_percent"] = progress_percent / 100
                    if "完成" in msg:
                        task["status"] = "completed"
                        task["progress_percent"] = 100
                    elif "%" in msg or "MiB" in msg or "GiB" in msg or "MB" in msg:
                        task["status"] = "downloading"
                    break
            current_status["message"] = f"章节 {chapter}: {msg}"
    
    # 并行下载
    results = dl.download_chapters_parallel(download_task_list, progress_callback)
    
    # 处理结果，包括重试失败的任务
    failed_tasks = []
    completed_files = []
    
    with download_lock:
        if task_key not in download_tasks:
            return
        status = download_tasks[task_key]
        
        for result in results:
            for task in status["tasks"]:
                if task["chapter"] == result["chapter"]:
                    if result["success"]:
                        task["status"] = "completed"
                        task["result"] = result["result"]
                        task["progress"] = "完成"
                        task["progress_percent"] = 100
                        completed_files.append(result["result"])
                    else:
                        task["retry_count"] = task.get("retry_count", 0) + 1
                        if task["retry_count"] < MAX_RETRY:
                            # 加入重试队列
                            failed_tasks.append(task)
                            task["status"] = "retrying"
                            task["progress"] = f"失败，准备重试 ({task['retry_count']}/{MAX_RETRY})"
                        else:
                            task["status"] = "failed"
                            task["result"] = result["result"]
                            task["progress"] = f"失败: {result['result']}"
                    break
    
    # 重试失败的任务
    while failed_tasks:
        print(f"[重试] 有 {len(failed_tasks)} 个任务需要重试...")
        time.sleep(5)  # 等待5秒后重试
        
        retry_list = [{
            "m3u8_url": t["m3u8_url"],
            "download_type": t.get("download_type", "m3u8"),
            "output_path": t["output_path"],
            "chapter": t["chapter"],
            "duration": t.get("duration", 0)
        } for t in failed_tasks]
        
        retry_results = dl.download_chapters_parallel(retry_list, progress_callback)
        
        new_failed = []
        with download_lock:
            if task_key not in download_tasks:
                break
            status = download_tasks[task_key]
            
            for result in retry_results:
                for task in status["tasks"]:
                    if task["chapter"] == result["chapter"]:
                        if result["success"]:
                            task["status"] = "completed"
                            task["result"] = result["result"]
                            task["progress"] = "完成"
                            task["progress_percent"] = 100
                            completed_files.append(result["result"])
                        else:
                            task["retry_count"] = task.get("retry_count", 0) + 1
                            if task["retry_count"] < MAX_RETRY:
                                new_failed.append(task)
                                task["progress"] = f"重试失败 ({task['retry_count']}/{MAX_RETRY})"
                            else:
                                task["status"] = "failed"
                                task["result"] = result["result"]
                                task["progress"] = f"失败: {result['result']}"
                        break
        
        failed_tasks = new_failed
    
    # 更新最终状态
    with download_lock:
        if task_key not in download_tasks:
            return
        status = download_tasks[task_key]
        status["running"] = False
        completed = sum(1 for t in status["tasks"] if t["status"] == "completed")
        failed = sum(1 for t in status["tasks"] if t["status"] == "failed")
        status["message"] = f"完成 {completed}/{status['total']}" + (f", 失败 {failed}" if failed else "")
        status["end_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 添加到历史记录
    add_to_history(
        status.get("task_id") or status.get("title_no"),
        status.get("vod_title", ""),
        status.get("user_nick", ""),
        "completed" if failed == 0 else "partial",
        completed_files
    )
    
    # 发送通知
    if failed == 0:
        send_notification(
            "下载完成",
            f"{status.get('user_nick', '')} - {status.get('vod_title', '')[:30]}"
        )
    else:
        send_notification(
            "下载部分完成",
            f"{status.get('user_nick', '')} - 成功{completed}/{status['total']}"
        )
    
    print(f"\n{'='*50}")
    print(f"[下载完成] 录像 {task_key} 成功: {completed}, 失败: {failed}")
    print(f"{'='*50}\n")

@app.route('/api/download/status')
def get_status():
    """获取指定录像或所有下载任务的状态"""
    task_id = request.args.get('task_id', '').strip()
    title_no = request.args.get('title_no', type=int)
    
    with download_lock:
        lookup_key = task_id or title_no
        if isinstance(lookup_key, str) and lookup_key.isdigit():
            lookup_key = int(lookup_key)

        if lookup_key:
            # 返回指定录像的状态
            if lookup_key in download_tasks:
                return jsonify(download_tasks[lookup_key])
            else:
                return jsonify({"running": False, "tasks": [], "title_no": title_no, "task_id": task_id or title_no})
        else:
            # 返回所有任务状态
            return jsonify({
                "all_tasks": list(download_tasks.values()),
                "active_count": sum(1 for t in download_tasks.values() if t.get("running"))
            })

@app.route('/api/download/cancel', methods=['POST'])
def cancel():
    data = request.json or {}
    task_id = str(data.get('task_id', '')).strip()
    title_no = data.get('title_no')
    
    with download_lock:
        lookup_key = task_id or title_no
        if isinstance(lookup_key, str) and lookup_key.isdigit():
            lookup_key = int(lookup_key)

        if lookup_key and lookup_key in download_tasks:
            download_tasks[lookup_key]["running"] = False
            download_tasks[lookup_key]["message"] = "已取消"
        else:
            # 取消所有任务
            for task in download_tasks.values():
                task["running"] = False
                task["message"] = "已取消"
    
    dl.cancel()
    return jsonify({"success": True})

@app.route('/api/open_folder', methods=['POST'])
def open_folder():
    """打开下载目录"""
    import subprocess
    import platform
    folder = config.get('download_dir', '')
    if os.path.exists(folder):
        if platform.system() == 'Windows':
            os.startfile(folder)
        elif platform.system() == 'Darwin':
            subprocess.run(['open', folder])
        else:
            subprocess.run(['xdg-open', folder])
        return jsonify({"success": True})
    return jsonify({"error": "目录不存在"}), 404

@app.route('/api/processes')
def get_processes():
    """获取所有下载进程"""
    return jsonify(dl.get_all_processes())

@app.route('/api/processes/<int:pid>/logs')
def get_process_logs(pid):
    """获取进程日志"""
    return jsonify(dl.get_process_logs(pid))

@app.route('/api/processes/<int:pid>/kill', methods=['POST'])
def kill_process(pid):
    """强制终止进程"""
    success = dl.kill_process(pid)
    return jsonify({"success": success})

@app.route('/api/processes/cleanup', methods=['POST'])
def cleanup_processes():
    """清理已完成的进程记录"""
    count = dl.cleanup_finished()
    return jsonify({"cleaned": count})

@app.route('/api/image')
def proxy_image():
    """图片代理，支持本地缓存"""
    url = request.args.get('url', '')
    if not url:
        return '', 404
    
    # 添加协议前缀
    if url.startswith("//"):
        url = "https:" + url
    
    # 获取缓存的图片
    cached_path = soop.get_cached_image(url)
    if cached_path and os.path.exists(cached_path):
        # 根据扩展名确定 MIME 类型
        ext = cached_path.split(".")[-1].lower()
        mime_types = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
        mime = mime_types.get(ext, "image/jpeg")
        return send_file(cached_path, mimetype=mime)
    
    # 缓存失败，直接代理请求
    try:
        import requests as req
        proxy = get_effective_proxy("image_proxy")
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = req.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}, proxies=proxies)
        if resp.status_code == 200:
            return Response(resp.content, mimetype=resp.headers.get('Content-Type', 'image/jpeg'))
    except:
        pass
    return '', 404

@app.route('/api/cache/stats')
def cache_stats():
    """获取缓存统计"""
    return jsonify(Cache.get_cache_size())

@app.route('/api/cache/clear', methods=['POST'])
def clear_cache():
    """清理过期缓存"""
    cleared = Cache.clear_expired()
    return jsonify({"cleared": cleared})

@app.route('/api/cache/clear_selective', methods=['POST'])
def clear_cache_selective():
    """选择性清理缓存"""
    data = request.json or {}
    clear_images = data.get('images', False)
    clear_data = data.get('data', False)
    clear_fragments = data.get('fragments', False)
    
    result = {
        "images": 0,
        "data": 0,
        "fragments": 0
    }
    
    try:
        # 清理图片缓存
        if clear_images:
            image_dir = os.path.join(os.path.dirname(__file__), "cache", "images")
            if os.path.exists(image_dir):
                count = 0
                for f in os.listdir(image_dir):
                    try:
                        os.remove(os.path.join(image_dir, f))
                        count += 1
                    except:
                        pass
                result["images"] = count
        
        # 清理数据缓存（章节信息、录像列表等）
        if clear_data:
            data_dir = os.path.join(os.path.dirname(__file__), "cache", "data")
            if os.path.exists(data_dir):
                count = 0
                for f in os.listdir(data_dir):
                    try:
                        os.remove(os.path.join(data_dir, f))
                        count += 1
                    except:
                        pass
                result["data"] = count
        
        # 清理下载片段缓存（N_m3u8DL-RE 的临时文件）
        if clear_fragments:
            download_dir = config.get('download_dir', '')
            if download_dir and os.path.exists(download_dir):
                count = 0
                # 查找所有主播文件夹
                for streamer_folder in os.listdir(download_dir):
                    streamer_path = os.path.join(download_dir, streamer_folder)
                    if not os.path.isdir(streamer_path):
                        continue
                    
                    # 在每个主播文件夹中查找临时文件
                    for item in os.listdir(streamer_path):
                        item_path = os.path.join(streamer_path, item)
                        # 删除 N_m3u8DL-RE 的临时文件夹和文件
                        if os.path.isdir(item_path) and item.startswith('0____'):
                            try:
                                import shutil
                                shutil.rmtree(item_path)
                                count += 1
                            except:
                                pass
                        # 删除 .m3u8 文件
                        elif item.endswith('.m3u8'):
                            try:
                                os.remove(item_path)
                                count += 1
                            except:
                                pass
                        # 删除 meta.json 等临时文件
                        elif item in ['meta.json', 'meta_selected.json', 'raw.m3u8']:
                            try:
                                os.remove(item_path)
                                count += 1
                            except:
                                pass
                
                result["fragments"] = count
        
        return jsonify({"success": True, "result": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/parse_url', methods=['POST'])
def parse_url():
    """解析 SOOP / CHZZK 链接，支持录像链接、主页和手动 MPD/M3U8 链接"""
    import re
    data = request.json
    url = data.get('url', '').strip()
    manual_title = data.get('manual_title', '').strip()  # 手动输入 MPD/M3U8 时的可选标题

    if not url:
        return jsonify({"error": "请输入链接"}), 400

    # 匹配手动输入的 MPD / M3U8 链接，以及 Naver neonplayer 回放 API
    is_mpd_m3u8 = re.search(r'\.(mpd|m3u8?)(\?|$)', url, re.IGNORECASE)
    is_neonplayer = bool(re.search(r'apis\.naver\.com/neonplayer/vodplay', url))
    if is_mpd_m3u8 or is_neonplayer:
        download_type = "mpd" if is_neonplayer else "m3u8"
        # 使用文件名或用户输入的标题作为录像标题
        if manual_title:
            title = manual_title
        elif is_neonplayer:
            # 从 Naver neonplayer URL 提取视频 ID 作为标题前缀
            neon_match = re.search(r'playback/([A-F0-9]+)', url)
            vid = neon_match.group(1)[:16] if neon_match else ""
            title = f"CHZZK_VOD_{vid}" if vid else "CHZZK VOD"
        else:
            # 尝试从 URL 中提取文件名
            path_part = url.split('?')[0].split('/')[-1]
            title = path_part or "手动链接"

        return jsonify({
            "type": "vod",
            "data": {
                "platform": "naver" if is_neonplayer else "manual",
                "task_id": abs(hash(url)) % (10 ** 9),  # 生成一个数字 ID
                "title_no": abs(hash(url)) % (10 ** 9),
                "video_no": abs(hash(url)) % (10 ** 9),
                "title": title,
                "user_nick": "CHZZK_Naver" if is_neonplayer else "手动输入",
                "user_id": "naver" if is_neonplayer else "manual",
                "reg_date": datetime.now().strftime("%Y-%m-%d"),
                "duration": 0,
                "thumb": "",
                "chapter_titles": [],
                "files": [{
                    "idx": 1,
                    "duration": 0,
                    "file_order": 1,
                    "m3u8_url": url,
                    "download_type": download_type,
                    "file_start": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "quality_info": "NEONPLAYER_MPD" if is_neonplayer else "MANUAL",
                    "file_title": title
                }]
            }
        })

    # 匹配录像链接:
    # https://vod.sooplive.co.kr/player/183329333
    # https://vod.sooplive.com/player/193664251
    # 以及 /player/<id>/catch
    vod_match = re.search(r'vod\.sooplive\.(?:co\.kr|com)/player/(\d+)', url)
    if vod_match:
        title_no = int(vod_match.group(1))
        vod = soop.get_vod_detail(title_no)
        if not vod:
            return jsonify({"error": "获取录像详情失败，请检查链接是否正确"}), 404
        
        chapter_titles = soop.get_chapter_titles(title_no)
        return jsonify({
            "type": "vod",
            "data": {
                "platform": "soop",
                "task_id": title_no,
                "title_no": vod.title_no,
                "title": vod.title,
                "user_nick": vod.user_nick,
                "user_id": vod.user_id,
                "reg_date": vod.reg_date,
                "duration": vod.duration,
                "thumb": vod.thumb,
                "chapter_titles": chapter_titles,
                "files": [{
                    "idx": f.idx,
                    "duration": f.duration,
                    "file_order": f.file_order,
                    "m3u8_url": f.m3u8_url,
                    "file_start": f.file_start,
                    "quality_info": f.quality_info,
                    "file_title": f.file_title
                } for f in vod.files]
            }
        })
    
    # 匹配个人主页: https://www.sooplive.co.kr/station/khm11903 或 sooplive.co.kr/khm11903
    station_match = re.search(r'sooplive\.(?:co\.kr|com)/(?:station/)?([a-zA-Z0-9_]+)(?:/|$|\?)', url)
    if station_match:
        user_id = station_match.group(1)
        # 排除一些非用户ID的路径
        if user_id in ['player', 'vod', 'live', 'api', 'login', 'search']:
            return jsonify({"error": "无法识别的链接格式"}), 400
        
        # 获取该用户的录像列表
        vods_data = soop.get_streamer_vods(user_id, page=1, per_page=20)
        vods = vods_data.get('data', [])
        
        if not vods:
            return jsonify({"error": f"未找到用户 {user_id} 的录像，请检查ID是否正确"}), 404
        
        # 获取用户昵称
        user_nick = vods[0].get('user_nick', user_id) if vods else user_id
        
        return jsonify({
            "type": "streamer",
            "data": {
                "platform": "soop",
                "user_id": user_id,
                "user_nick": user_nick,
                "vods": vods,
                "meta": vods_data.get('meta', {})
            }
        })

    chzzk_vod_match = re.search(r'chzzk\.naver\.com/video/(\d+)', url)
    if chzzk_vod_match:
        video_no = int(chzzk_vod_match.group(1))
        video = chzzk.get_video_detail(video_no)
        if not video:
            return jsonify({"error": "获取 CHZZK 录像详情失败，请检查链接是否正确"}), 404
        return jsonify({
            "type": "vod",
            "data": build_chzzk_vod_payload(video)
        })

    chzzk_channel_match = re.search(r'chzzk\.naver\.com/([a-f0-9]{32})(?:/videos)?(?:/|$|\?)', url)
    if chzzk_channel_match:
        channel_id = chzzk_channel_match.group(1)
        vods_data = chzzk.get_channel_videos(channel_id, page=0, size=20)
        vods = vods_data.get('data', [])
        if not vods:
            return jsonify({"error": "未找到该 CHZZK 频道的录像"}), 404

        channel_name = vods[0].get('channel', {}).get('channelName', channel_id)
        return jsonify({
            "type": "streamer",
            "data": {
                "platform": "chzzk",
                "channel_id": channel_id,
                "user_id": channel_id,
                "user_nick": channel_name,
                "vods": vods,
                "meta": {
                    "current_page": 1,
                    "last_page": vods_data.get('totalPages', 1)
                }
            }
        })

    return jsonify({"error": "无法识别的链接格式，请输入 SOOP / CHZZK 的录像或主页链接"}), 400

@app.route('/api/history')
def get_history():
    """获取下载历史"""
    return jsonify(download_history[-MAX_HISTORY:][::-1])  # 最新的在前

@app.route('/api/history/clear', methods=['POST'])
def clear_history():
    """清空下载历史"""
    global download_history
    download_history = []
    save_history()
    return jsonify({"success": True})

@app.route('/api/disk_space')
def get_disk_space():
    """获取磁盘空间信息"""
    try:
        path = config.get('download_dir', '')
        if not path or not os.path.exists(path):
            path = os.path.expanduser("~")
        total, used, free = shutil.disk_usage(path)
        return jsonify({
            "total_gb": round(total / (1024**3), 2),
            "used_gb": round(used / (1024**3), 2),
            "free_gb": round(free / (1024**3), 2),
            "percent_used": round(used / total * 100, 1)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== 自动下载功能 ====================

@app.route('/api/auto_download/config', methods=['GET', 'POST'])
def handle_auto_download_config():
    """获取或设置自动下载配置"""
    global auto_download_config
    if request.method == 'POST':
        data = request.json
        for key in ['enabled', 'start_hour', 'end_hour', 'interval_minutes', 'earliest_date', 'streamers']:
            if key in data:
                auto_download_config[key] = data[key]
        save_config()
        
        # 根据配置启动或停止自动下载
        if auto_download_config['enabled']:
            start_auto_download()
        else:
            stop_auto_download()
        
        return jsonify({"success": True})
    
    return jsonify({
        **auto_download_config,
        "running": auto_download_running
    })

@app.route('/api/auto_download/status')
def auto_download_status():
    """获取自动下载状态"""
    return jsonify({
        "enabled": auto_download_config['enabled'],
        "running": auto_download_running,
        "config": auto_download_config
    })

@app.route('/api/auto_download/downloaded')
def get_downloaded():
    """获取已下载记录"""
    return jsonify(downloaded_records)

@app.route('/api/auto_download/downloaded/<int:title_no>', methods=['DELETE'])
def remove_downloaded(title_no):
    """从已下载记录中移除（允许重新自动下载）"""
    if str(title_no) in downloaded_records:
        del downloaded_records[str(title_no)]
        save_downloaded()
    return jsonify({"success": True})

@app.route('/api/auto_download/trigger', methods=['POST'])
def trigger_auto_download():
    """手动触发一次自动下载检测"""
    threading.Thread(target=check_and_download_new_vods, daemon=True).start()
    return jsonify({"success": True, "message": "已触发检测"})

def is_in_download_time(schedule_config):
    """检查当前是否在自动下载时间范围内"""
    now = datetime.now()
    hour = now.hour
    start = schedule_config['start_hour']
    end = schedule_config['end_hour']
    
    if start <= end:
        # 同一天内，如 9-17
        return start <= hour < end
    else:
        # 跨天，如 22-4
        return hour >= start or hour < end

def is_in_auto_download_time():
    return is_in_download_time(auto_download_config)

def check_and_download_new_vods():
    """检测并下载新录像"""
    global downloaded_records
    
    if not auto_download_config['enabled']:
        return
    
    streamers = auto_download_config.get('streamers', [])
    if not streamers:
        print("[自动下载] 未配置要监控的主播")
        return
    
    earliest_date = auto_download_config.get('earliest_date', '')
    
    print(f"\n[自动下载] 开始检测 {len(streamers)} 个主播的新录像...")
    
    # 统计信息
    total_checked = 0
    skipped_downloaded = 0
    skipped_old = 0
    skipped_downloading = 0
    new_downloads = 0
    
    for streamer_id in streamers:
        try:
            # 获取主播最新录像
            vods_data = soop.get_streamer_vods(streamer_id, page=1, per_page=10)
            vods = vods_data.get('data', [])
            
            for vod in vods:
                title_no = vod.get('title_no')
                if not title_no:
                    continue
                
                total_checked += 1
                
                # 检查是否已下载
                if str(title_no) in downloaded_records:
                    skipped_downloaded += 1
                    continue
                
                # 检查是否正在下载
                with download_lock:
                    if title_no in download_tasks and download_tasks[title_no].get('running'):
                        skipped_downloading += 1
                        continue
                
                # 检查日期
                reg_date = vod.get('reg_date', '')
                if earliest_date and reg_date:
                    vod_date = reg_date.split(' ')[0]
                    if vod_date < earliest_date:
                        skipped_old += 1
                        continue
                
                # 获取详细信息并开始下载
                print(f"[自动下载] ✓ 发现新录像: {vod.get('user_nick', '')} - {vod.get('title_name', '')} (ID: {title_no})")
                new_downloads += 1
                
                # 发送自动下载开始通知
                send_notification(
                    "自动下载开始",
                    f"{vod.get('user_nick', '')} - {vod.get('title_name', '')[:30]}"
                )
                
                vod_detail = soop.get_vod_detail(title_no)
                if not vod_detail:
                    print(f"[自动下载] ✗ 获取详情失败: {title_no}")
                    continue

                chapter_titles = soop.get_chapter_titles(title_no)
                 
                # 构建下载任务
                chapters = build_chapter_entries(vod_detail.files, chapter_titles)
                 
                # 启动下载
                start_auto_download_task(vod_detail, chapters)
                
                # 记录已下载
                downloaded_records[str(title_no)] = {
                    "date": reg_date,
                    "title": vod_detail.title,
                    "user_nick": vod_detail.user_nick,
                    "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                save_downloaded()
                
        except Exception as e:
            print(f"[自动下载] ✗ 检测主播 {streamer_id} 出错: {e}")
    
    # 输出汇总信息
    print(f"[自动下载] 检测完成 - 共检查 {total_checked} 个录像, "
          f"新下载 {new_downloads} 个, "
          f"跳过 {skipped_downloaded + skipped_old + skipped_downloading} 个 "
          f"(已下载:{skipped_downloaded}, 过旧:{skipped_old}, 下载中:{skipped_downloading})\n")

def start_auto_download_task(vod, chapters):
    """启动自动下载任务"""
    title_no = vod.title_no
    
    total_files = len(vod.files)
    tasks = [{
        "m3u8_url": ch['m3u8_url'],
        "output_path": get_output_path(
            config['download_dir'], 
            vod.user_nick, 
            vod.reg_date, 
            vod.title, 
            ch['file_order'], 
            total_files,
            chapter_title=ch.get('file_title', '')
        ),
        "chapter": ch['file_order'],
        "duration": ch.get('duration', 0),
        "status": "pending",
        "progress": "",
        "progress_percent": 0,
        "result": ""
    } for ch in chapters]
    
    task_status = {
        "running": True, 
        "tasks": tasks, 
        "current": 0, 
        "total": len(tasks), 
        "message": "自动下载开始",
        "current_file": "",
        "vod_title": vod.title,
        "user_nick": vod.user_nick,
        "title_no": title_no,
        "auto": True  # 标记为自动下载
    }
    
    with download_lock:
        download_tasks[title_no] = task_status
    
    threading.Thread(target=run_downloads, args=(title_no,), daemon=True).start()

def auto_download_loop():
    """自动下载主循环"""
    global auto_download_running
    auto_download_running = True
    
    print("[自动下载] 服务已启动")
    
    while auto_download_running and auto_download_config['enabled']:
        try:
            if is_in_auto_download_time():
                check_and_download_new_vods()
            else:
                print(f"[自动下载] 当前不在检测时间范围内 ({auto_download_config['start_hour']}:00 - {auto_download_config['end_hour']}:00)")
        except Exception as e:
            print(f"[自动下载] 循环出错: {e}")
        
        # 等待下一次检测
        interval = auto_download_config.get('interval_minutes', 30) * 60
        for _ in range(interval):
            if not auto_download_running or not auto_download_config['enabled']:
                break
            time.sleep(1)
    
    auto_download_running = False
    print("[自动下载] 服务已停止")

def start_auto_download():
    """启动自动下载服务"""
    global auto_download_thread, auto_download_running
    
    if auto_download_running:
        return
    
    auto_download_thread = threading.Thread(target=auto_download_loop, daemon=True)
    auto_download_thread.start()

def stop_auto_download():
    """停止自动下载服务"""
    global auto_download_running
    auto_download_running = False

# ==================== CHZZK 自动下载功能 ====================

@app.route('/api/chzzk/auto_download/config', methods=['GET', 'POST'])
def handle_chzzk_auto_download_config():
    """获取或设置 CHZZK 自动下载配置"""
    global chzzk_auto_download_config
    if request.method == 'POST':
        data = request.json
        for key in ['enabled', 'start_hour', 'end_hour', 'interval_minutes', 'earliest_date', 'streamers']:
            if key in data:
                chzzk_auto_download_config[key] = data[key]
        save_config()

        if chzzk_auto_download_config['enabled']:
            start_chzzk_auto_download()
        else:
            stop_chzzk_auto_download()

        return jsonify({"success": True})

    return jsonify({
        **chzzk_auto_download_config,
        "running": chzzk_auto_download_running
    })

@app.route('/api/chzzk/auto_download/status')
def chzzk_auto_download_status():
    return jsonify({
        "enabled": chzzk_auto_download_config['enabled'],
        "running": chzzk_auto_download_running,
        "config": chzzk_auto_download_config
    })

@app.route('/api/chzzk/auto_download/downloaded')
def get_chzzk_downloaded():
    result = {
        key: value for key, value in downloaded_records.items()
        if str(key).startswith("chzzk:")
    }
    return jsonify(result)

@app.route('/api/chzzk/auto_download/downloaded/<int:video_no>', methods=['DELETE'])
def remove_chzzk_downloaded(video_no):
    record_key = get_download_record_key("chzzk", video_no)
    if record_key in downloaded_records:
        del downloaded_records[record_key]
        save_downloaded()
    return jsonify({"success": True})

@app.route('/api/chzzk/auto_download/trigger', methods=['POST'])
def trigger_chzzk_auto_download():
    threading.Thread(target=check_and_download_new_chzzk_vods, daemon=True).start()
    return jsonify({"success": True, "message": "已触发 CHZZK 检测"})

def start_chzzk_auto_download_task(video_meta: dict, download_info: dict):
    """启动 CHZZK 自动下载任务"""
    video_no = video_meta["videoNo"]
    task_key = get_download_record_key("chzzk", video_no)
    output_path = get_output_path(
        config['download_dir'],
        video_meta['channel']['channelName'],
        video_meta['publishDate'],
        video_meta['videoTitle'],
        1,
        1
    )

    tasks = [{
        "m3u8_url": download_info["url"],
        "download_type": download_info["download_type"],
        "output_path": output_path,
        "chapter": 1,
        "duration": (video_meta.get("duration", 0) or 0) * 1000,
        "status": "pending",
        "progress": "",
        "progress_percent": 0,
        "result": ""
    }]

    task_status = {
        "running": True,
        "tasks": tasks,
        "current": 0,
        "total": 1,
        "message": "CHZZK 自动下载开始",
        "current_file": "",
        "vod_title": video_meta['videoTitle'],
        "user_nick": video_meta['channel']['channelName'],
        "title_no": video_no,
        "platform": "chzzk",
        "auto": True
    }

    with download_lock:
        download_tasks[task_key] = task_status

    threading.Thread(target=run_downloads, args=(task_key,), daemon=True).start()

def check_and_download_new_chzzk_vods():
    """检测并下载 CHZZK 新录像"""
    global downloaded_records

    if not chzzk_auto_download_config['enabled']:
        return

    streamers = chzzk_auto_download_config.get('streamers', [])
    if not streamers:
        print("[CHZZK 自动下载] 未配置要监控的主播")
        return

    earliest_date = chzzk_auto_download_config.get('earliest_date', '')

    print(f"\n[CHZZK 自动下载] 开始检测 {len(streamers)} 个主播的新录像...")

    total_checked = 0
    skipped_downloaded = 0
    skipped_old = 0
    skipped_downloading = 0
    new_downloads = 0

    for channel_id in streamers:
        try:
            videos_data = chzzk.get_channel_videos(channel_id, page=0, size=10)
            videos = videos_data.get('data', [])

            for video in videos:
                video_no = video.get('videoNo')
                if not video_no:
                    continue

                total_checked += 1
                record_key = get_download_record_key("chzzk", video_no)
                task_key = record_key

                if record_key in downloaded_records:
                    skipped_downloaded += 1
                    continue

                with download_lock:
                    if task_key in download_tasks and download_tasks[task_key].get('running'):
                        skipped_downloading += 1
                        continue

                publish_date = video.get('publishDate', '')
                if earliest_date and publish_date:
                    video_date = publish_date.split(' ')[0]
                    if video_date < earliest_date:
                        skipped_old += 1
                        continue

                print(f"[CHZZK 自动下载] ✓ 发现新录像: {video['channel']['channelName']} - {video.get('videoTitle', '')} (ID: {video_no})")
                new_downloads += 1

                send_notification(
                    "CHZZK 自动下载开始",
                    f"{video['channel']['channelName']} - {video.get('videoTitle', '')[:30]}"
                )

                video_detail = chzzk.get_video_detail(video_no)
                if not video_detail:
                    print(f"[CHZZK 自动下载] ✗ 获取详情失败: {video_no}")
                    continue

                download_info = chzzk.resolve_video_download(video_detail)
                if not download_info or not download_info.get("url"):
                    print(f"[CHZZK 自动下载] ✗ 获取下载链接失败: {video_no}")
                    continue

                start_chzzk_auto_download_task(video_detail, download_info)

                downloaded_records[record_key] = {
                    "platform": "chzzk",
                    "date": publish_date,
                    "title": video_detail.get("videoTitle", ""),
                    "user_nick": video_detail.get("channel", {}).get("channelName", ""),
                    "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "download_type": download_info.get("download_type", "")
                }
                save_downloaded()

        except Exception as e:
            print(f"[CHZZK 自动下载] ✗ 检测主播 {channel_id} 出错: {e}")

    print(f"[CHZZK 自动下载] 检测完成 - 共检查 {total_checked} 个录像, "
          f"新下载 {new_downloads} 个, "
          f"跳过 {skipped_downloaded + skipped_old + skipped_downloading} 个 "
          f"(已下载:{skipped_downloaded}, 过旧:{skipped_old}, 下载中:{skipped_downloading})\n")

def chzzk_auto_download_loop():
    """CHZZK 自动下载主循环"""
    global chzzk_auto_download_running
    chzzk_auto_download_running = True

    print("[CHZZK 自动下载] 服务已启动")

    while chzzk_auto_download_running and chzzk_auto_download_config['enabled']:
        try:
            if is_in_download_time(chzzk_auto_download_config):
                check_and_download_new_chzzk_vods()
            else:
                print(f"[CHZZK 自动下载] 当前不在检测时间范围内 ({chzzk_auto_download_config['start_hour']}:00 - {chzzk_auto_download_config['end_hour']}:00)")
        except Exception as e:
            print(f"[CHZZK 自动下载] 循环出错: {e}")

        interval = chzzk_auto_download_config.get('interval_minutes', 30) * 60
        for _ in range(interval):
            if not chzzk_auto_download_running or not chzzk_auto_download_config['enabled']:
                break
            time.sleep(1)

    chzzk_auto_download_running = False
    print("[CHZZK 自动下载] 服务已停止")

def start_chzzk_auto_download():
    """启动 CHZZK 自动下载服务"""
    global chzzk_auto_download_thread, chzzk_auto_download_running

    if chzzk_auto_download_running:
        return

    chzzk_auto_download_thread = threading.Thread(target=chzzk_auto_download_loop, daemon=True)
    chzzk_auto_download_thread.start()

def stop_chzzk_auto_download():
    """停止 CHZZK 自动下载服务"""
    global chzzk_auto_download_running
    chzzk_auto_download_running = False

if __name__ == '__main__':
    # 如果配置了自动下载，启动服务
    if auto_download_config.get('enabled'):
        start_auto_download()
    if chzzk_auto_download_config.get('enabled'):
        start_chzzk_auto_download()
    
    app.run(host='127.0.0.1', port=5000, debug=True, use_reloader=False)
