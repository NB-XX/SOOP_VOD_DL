"""下载管理模块"""
import json
import os
import re
import shutil
import subprocess
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable, Optional, Tuple, List, Dict

MAX_FILE_SIZE_GB = 16
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_GB * 1024 * 1024 * 1024

def sanitize_filename(name: str) -> str:
    """清理文件名非法字符"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    return name.replace(' ', '_').strip()[:100]

def format_date(date_str: str) -> str:
    """格式化日期为YYYYMMDD"""
    try:
        return datetime.strptime(date_str.split()[0], "%Y-%m-%d").strftime("%Y%m%d")
    except:
        return datetime.now().strftime("%Y%m%d")

def get_output_path(base_dir: str, user_nick: str, reg_date: str, title: str, chapter: int, total: int, part: int = 0, chapter_title: str = "") -> str:
    """生成输出路径: 主播名/主播_录像日期_章节序号_章节标题.mp4
    
    命名规则：
    - 文件夹：只用主播名（不加日期）
    - 第一章节不加序号：主播_日期_章节标题.mp4
    - 第二章节开始加序号：主播_日期_2_章节标题.mp4
    - 切分后第一部分不加part：主播_日期_章节标题.mp4
    - 切分后第二部分开始加part：主播_日期_章节标题_part2.mp4
    """
    # 文件夹: 只用主播名
    folder = os.path.join(base_dir, sanitize_filename(user_nick))
    os.makedirs(folder, exist_ok=True)
    
    # 文件名
    video_date = format_date(reg_date)
    clean_nick = sanitize_filename(user_nick)
    
    if chapter_title:
        # 使用章节标题
        clean_chapter_title = sanitize_filename(chapter_title)[:50]
        # 第一章节不加序号
        if chapter == 1:
            name = f"{clean_nick}_{video_date}_{clean_chapter_title}"
        else:
            name = f"{clean_nick}_{video_date}_{chapter}_{clean_chapter_title}"
    else:
        # 使用默认命名
        clean_title = sanitize_filename(title)[:50]
        if total > 1 and chapter > 1:
            # 多章节且不是第一章节，加序号
            name = f"{clean_nick}_{video_date}_{chapter}_{clean_title}"
        else:
            name = f"{clean_nick}_{video_date}_{clean_title}"
    
    # 切分后的part命名：第一部分不加，第二部分开始加
    if part > 1:
        name += f"_part{part}"
    
    return os.path.join(folder, f"{name}.mp4")

class Downloader:
    def __init__(self, m3u8dl_path: str = "N_m3u8DL-RE", max_workers: int = 3, thread_count: int = 16):
        self.m3u8dl_path = shutil.which(m3u8dl_path) or m3u8dl_path
        self.aria2c_path = shutil.which("aria2c") or "aria2c"
        self.ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"
        self.thread_count = thread_count
        self.max_workers = max_workers
        self.cookie = ""
        self.proxy = ""
        self.cancelled = False
        self.processes: Dict[int, subprocess.Popen] = {}  # chapter -> process
        self._lock = threading.Lock()
        self._progress_data: Dict[int, dict] = {}  # chapter -> {size, speed, status}
        self._process_logs: Dict[int, List[str]] = {}  # pid -> log lines
        self._all_processes: Dict[int, dict] = {}  # pid -> {cmd, start_time, status, chapter}
        self._db_file = os.path.join(os.path.dirname(__file__), "processes.json")
        self._load_process_db()
        
        # 切分配置：可自定义阈值和开关
        self.split_enabled = True       # 是否启用超长视频切分
        self.split_threshold_hours = 3.5  # 超过此时长的视频会被切分
        self.split_duration = 2.5         # 每段切分时长（小时）

    def set_tool_paths(self, m3u8dl_path: str = "", aria2c_path: str = "", ffmpeg_path: str = ""):
        if m3u8dl_path:
            self.m3u8dl_path = m3u8dl_path
        if aria2c_path:
            self.aria2c_path = aria2c_path
        if ffmpeg_path:
            self.ffmpeg_path = ffmpeg_path

    def set_split_config(self, enabled: bool = True, threshold_hours: float = 3.5, segment_hours: float = 2.5):
        """配置超长视频切分参数
        enabled: 是否启用切分
        threshold_hours: 超过此时长（小时）的视频才会被切分
        segment_hours: 每段切分时长（小时）
        """
        self.split_enabled = enabled
        self.split_threshold_hours = max(1.0, threshold_hours)  # 最低1小时
        self.split_duration = max(0.5, segment_hours)           # 最低0.5小时每段
    
    def _load_process_db(self):
        """从文件加载进程记录"""
        try:
            if os.path.exists(self._db_file):
                with open(self._db_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for pid_str, info in data.items():
                        pid = int(pid_str)
                        # 检查进程是否还在运行
                        if self._is_process_running(pid):
                            info["status"] = "orphan"  # 孤儿进程（上次异常退出遗留）
                            self._all_processes[pid] = info
                            self._process_logs[pid] = info.get("logs", [])
        except:
            pass
    
    def _save_process_db(self):
        """保存进程记录到文件"""
        try:
            with self._lock:
                data = {}
                for pid, info in self._all_processes.items():
                    # 只保存运行中或孤儿进程
                    if info.get("status") in ("running", "orphan"):
                        save_info = {k: v for k, v in info.items() if k != "process"}
                        save_info["logs"] = self._process_logs.get(pid, [])[-20:]  # 只保存最近20条日志
                        data[str(pid)] = save_info
                with open(self._db_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存进程记录失败: {e}")
    
    def _is_process_running(self, pid: int) -> bool:
        """检查进程是否在运行"""
        try:
            if os.name == 'nt':
                # Windows: 使用 tasklist
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW
                )
                return str(pid) in result.stdout.decode('gbk', errors='replace')
            else:
                # Unix: 发送信号0检查
                os.kill(pid, 0)
                return True
        except:
            return False
    
    def scan_download_processes(self) -> List[dict]:
        """扫描系统中所有已知下载器进程"""
        found = []
        try:
            if os.name == 'nt':
                process_names = ["N_m3u8DL-RE.exe", "aria2c.exe", "ffmpeg.exe"]
                for process_name in process_names:
                    result = subprocess.run(
                        ["wmic", "process", "where", f"name='{process_name}'", "get", "processid,commandline", "/format:csv"],
                        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    lines = result.stdout.decode('gbk', errors='replace').strip().split('\n')
                    for line in lines[1:]:
                        parts = line.strip().split(',')
                        if len(parts) >= 3:
                            try:
                                cmd = parts[1] if len(parts) > 2 else ""
                                pid = int(parts[-1])
                                if pid not in self._all_processes:
                                    found.append({"pid": pid, "cmd": cmd, "status": "external"})
                            except:
                                pass
            else:
                for process_name in ["N_m3u8DL-RE", "aria2c", "ffmpeg"]:
                    result = subprocess.run(["pgrep", "-a", process_name], capture_output=True, text=True)
                    for line in result.stdout.strip().split('\n'):
                        if line:
                            parts = line.split(' ', 1)
                            if len(parts) >= 1:
                                try:
                                    pid = int(parts[0])
                                    cmd = parts[1] if len(parts) > 1 else ""
                                    if pid not in self._all_processes:
                                        found.append({"pid": pid, "cmd": cmd, "status": "external"})
                                except:
                                    pass
        except:
            pass
        return found
    
    def set_cookie(self, cookie: str):
        self.cookie = cookie
    
    def set_proxy(self, proxy: str):
        self.proxy = proxy
    
    def cancel(self):
        self.cancelled = True
        with self._lock:
            for proc in self.processes.values():
                try:
                    proc.terminate()
                except:
                    pass
    
    def get_progress(self, chapter: int) -> dict:
        """获取指定章节的进度"""
        with self._lock:
            return self._progress_data.get(chapter, {})
    
    def _update_progress(self, chapter: int, **kwargs):
        """更新进度数据"""
        with self._lock:
            if chapter not in self._progress_data:
                self._progress_data[chapter] = {}
            self._progress_data[chapter].update(kwargs)
    
    def _register_process(self, proc: subprocess.Popen, cmd: str, chapter: int = 0):
        """注册进程用于管理"""
        with self._lock:
            self._all_processes[proc.pid] = {
                "pid": proc.pid,
                "cmd": cmd,
                "start_time": time.time(),
                "status": "running",
                "chapter": chapter,
                "process": proc
            }
            self._process_logs[proc.pid] = []
        self._save_process_db()
    
    def _unregister_process(self, pid: int, status: str = "completed"):
        """注销进程"""
        with self._lock:
            if pid in self._all_processes:
                self._all_processes[pid]["status"] = status
                self._all_processes[pid]["end_time"] = time.time()
                self._all_processes[pid].pop("process", None)
        self._save_process_db()
    
    def _add_log(self, pid: int, line: str):
        """添加进程日志"""
        with self._lock:
            if pid in self._process_logs:
                self._process_logs[pid].append(f"[{time.strftime('%H:%M:%S')}] {line}")
                # 只保留最近100行
                if len(self._process_logs[pid]) > 100:
                    self._process_logs[pid] = self._process_logs[pid][-100:]
    
    def get_all_processes(self) -> List[dict]:
        """获取所有进程信息（包括扫描到的外部进程）"""
        # 先扫描系统中的 N_m3u8DL-RE 进程
        external = self.scan_download_processes()
        for p in external:
            if p["pid"] not in self._all_processes:
                self._all_processes[p["pid"]] = {
                    "pid": p["pid"],
                    "cmd": p["cmd"],
                    "start_time": time.time(),
                    "status": "external",
                    "chapter": 0
                }
                self._process_logs[p["pid"]] = ["[系统扫描发现的进程]"]
        
        with self._lock:
            result = []
            for pid, info in list(self._all_processes.items()):
                proc = info.get("process")
                
                # 检查进程实际状态
                if info["status"] in ("running", "orphan", "external"):
                    if proc and proc.poll() is not None:
                        status = "completed"
                    elif self._is_process_running(pid):
                        status = info["status"]
                    else:
                        status = "dead"
                else:
                    status = info["status"]
                
                result.append({
                    "pid": pid,
                    "cmd": info["cmd"][:100] + "..." if len(info.get("cmd", "")) > 100 else info.get("cmd", ""),
                    "chapter": info.get("chapter", 0),
                    "status": status,
                    "start_time": info.get("start_time", 0),
                    "duration": time.time() - info.get("start_time", time.time())
                })
            return result
    
    def get_process_logs(self, pid: int) -> List[str]:
        """获取进程日志"""
        with self._lock:
            return list(self._process_logs.get(pid, [])[-50:])  # 返回副本
    
    def kill_process(self, pid: int) -> bool:
        """强制终止进程"""
        success = False
        with self._lock:
            info = self._all_processes.get(pid)
            if info and info.get("process"):
                try:
                    info["process"].kill()
                    info["status"] = "killed"
                    success = True
                except:
                    pass
        
        if not success:
            # 尝试用系统命令杀死
            try:
                if os.name == 'nt':
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], 
                                 capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    subprocess.run(["kill", "-9", str(pid)], capture_output=True)
                success = True
            except:
                pass
        
        if success:
            with self._lock:
                if pid in self._all_processes:
                    self._all_processes[pid]["status"] = "killed"
            self._save_process_db()
        
        return success
    
    def cleanup_finished(self):
        """清理已完成的进程记录"""
        with self._lock:
            to_remove = [pid for pid, info in self._all_processes.items() 
                        if info["status"] in ("completed", "killed", "failed", "dead")]
            for pid in to_remove:
                self._all_processes.pop(pid, None)
                self._process_logs.pop(pid, None)
        self._save_process_db()
        return len(to_remove)

    def _get_video_duration(self, video_path: str) -> Optional[float]:
        """获取视频时长（秒）- 后备方案"""
        try:
            cmd = [
                self.ffmpeg_path, "-i", video_path, "-hide_banner"
            ]
            result = subprocess.run(
                cmd, capture_output=True, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                timeout=30
            )
            output = result.stdout.decode('utf-8', errors='ignore')
            for line in output.split('\n'):
                if 'Duration:' in line:
                    try:
                        time_str = line.split('Duration:')[1].split(',')[0].strip()
                        parts = time_str.split(':')
                        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                    except:
                        pass
        except:
            pass
        return None

    def _split_long_video(self, video_path: str, duration_ms: int = 0, progress_cb: Optional[Callable[[int, str, int], None]] = None, chapter: int = 0) -> List[str]:
        """将超过3.5小时的视频按2.5小时切分
        
        duration_ms: 视频时长（毫秒），从章节信息获取
        
        切分逻辑：
        - 总时长超过3.5小时才切分
        - 每次切2.5小时，但切之前检查剩余部分
        - 如果剩余部分不足3.5小时，就不再切，直接作为最后一段
        
        切分命名规则：
        - 第一部分：保持原文件名（不加 _part1）
        - 第二部分开始：原文件名_part2, _part3...
        """
        # 使用传入的时长（毫秒转秒）
        if duration_ms > 0:
            duration = duration_ms / 1000
        else:
            # 后备方案：用 ffmpeg 获取
            duration = self._get_video_duration(video_path)
            if duration is None:
                print(f"[DEBUG] 无法获取视频时长: {video_path}")
                return [video_path]
        
        if not self.split_enabled:
            print(f"[DEBUG] 超长视频切分已禁用")
            return [video_path]

        threshold_seconds = self.split_threshold_hours * 3600
        if duration <= threshold_seconds:
            print(f"[DEBUG] 视频时长 {duration/3600:.2f}小时，无需切分")
            return [video_path]
        
        print(f"[DEBUG] 视频时长 {duration/3600:.2f}小时，超过{self.split_threshold_hours}小时，开始切分...")
        if progress_cb:
            progress_cb(chapter, f"视频超过{self.split_threshold_hours}小时，正在切分...", 9900)
        
        from pathlib import Path
        input_path = Path(video_path)
        directory = input_path.parent
        stem = input_path.stem
        suffix = input_path.suffix
        
        split_seconds = self.split_duration * 3600  # 每段切分时长
        
        # 动态计算需要切分成多少段
        # 逻辑：每次切2.5小时，但如果剩余不足3.5小时就停止
        remaining = duration
        num_parts = 0
        while remaining > threshold_seconds:
            remaining -= split_seconds
            num_parts += 1
        num_parts += 1  # 最后剩余的部分
        
        print(f"[DEBUG] 计划切分为 {num_parts} 段")
        
        output_files = []
        current_start = 0
        part_num = 0
        remaining_duration = duration
        
        while remaining_duration > 0:
            part_num += 1
            
            # 检查剩余时长：如果剩余不足3.5小时，这就是最后一段
            is_last_part = remaining_duration <= threshold_seconds
            
            # 第一部分保持原文件名，第二部分开始加 _part2, _part3...
            if part_num == 1:
                temp_path = directory / f"{stem}_temp{suffix}"
                actual_output = temp_path
            else:
                actual_output = directory / f"{stem}_part{part_num}{suffix}"
            
            if progress_cb:
                progress_cb(chapter, f"切分 {part_num}/{num_parts}...", 9900)
            
            print(f"[DEBUG] 正在生成 Part {part_num}, 剩余时长: {remaining_duration/3600:.2f}小时, 是否最后一段: {is_last_part}")
            
            if is_last_part:
                # 最后一段，从当前位置到结尾
                cmd = [
                    self.ffmpeg_path, "-y",
                    "-ss", str(current_start),
                    "-i", video_path,
                    "-c", "copy",
                    str(actual_output)
                ]
                current_duration = remaining_duration
            else:
                # 不是最后一段，切2.5小时
                cmd = [
                    self.ffmpeg_path, "-y",
                    "-ss", str(current_start),
                    "-i", video_path,
                    "-t", str(split_seconds),
                    "-c", "copy",
                    str(actual_output)
                ]
                current_duration = split_seconds
            
            try:
                result = subprocess.run(
                    cmd, capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                    timeout=3600
                )
                if result.returncode == 0 and actual_output.exists() and actual_output.stat().st_size > 1024:
                    output_files.append(str(actual_output))
                    print(f"[DEBUG] Part {part_num} 完成: {actual_output}")
                else:
                    print(f"[DEBUG] Part {part_num} 失败")
                    break
            except Exception as e:
                print(f"[DEBUG] 切分错误: {e}")
                break
            
            current_start += current_duration
            remaining_duration -= current_duration
            
            # 如果是最后一段，退出循环
            if is_last_part:
                break
        
        # 如果切分成功
        if len(output_files) == num_parts:
            # 删除原文件
            try:
                os.remove(video_path)
                print(f"[DEBUG] 已删除原文件: {video_path}")
            except Exception as e:
                print(f"[DEBUG] 删除原文件失败: {e}")
            
            # 将第一部分的临时文件重命名为原文件名
            temp_path = directory / f"{stem}_temp{suffix}"
            final_first_path = directory / f"{stem}{suffix}"
            if temp_path.exists():
                try:
                    temp_path.rename(final_first_path)
                    output_files[0] = str(final_first_path)
                    print(f"[DEBUG] 重命名第一部分: {temp_path} -> {final_first_path}")
                except Exception as e:
                    print(f"[DEBUG] 重命名失败: {e}")
            
            return output_files
        else:
            # 切分失败，清理已生成的分片，返回原文件
            for f in output_files:
                try:
                    os.remove(f)
                except:
                    pass
            # 清理可能的临时文件
            temp_path = directory / f"{stem}_temp{suffix}"
            if temp_path.exists():
                try:
                    os.remove(temp_path)
                except:
                    pass
            return [video_path]

    def _download_http_with_aria2(self, source_url: str, output_path: str, chapter: int,
                                  progress_cb: Optional[Callable[[int, str, int], None]] = None,
                                  duration_ms: int = 0) -> Tuple[bool, str]:
        output_dir = os.path.dirname(output_path)
        os.makedirs(output_dir, exist_ok=True)
        output_name = os.path.basename(output_path)

        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception as e:
                return False, f"删除已存在文件失败: {e}"

        cmd = [
            self.aria2c_path,
            "--dir", output_dir,
            "--out", output_name,
            "--file-allocation=none",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            "--summary-interval=1",
            "--console-log-level=notice",
            "-x", "16",
            "-s", "16",
            "-k", "1M",
            source_url
        ]
        if self.proxy:
            cmd.extend(["--all-proxy", self.proxy])

        cmd_str = " ".join(cmd)
        process = None

        try:
            if progress_cb:
                progress_cb(chapter, "启动 aria2 下载...", 0)

            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                encoding='utf-8', errors='replace'
            )
            self._register_process(process, cmd_str, chapter)
            self._add_log(process.pid, f"启动: {cmd_str[:100]}...")

            with self._lock:
                self.processes[chapter] = process

            while True:
                if self.cancelled:
                    process.terminate()
                    self._add_log(process.pid, "用户取消")
                    self._unregister_process(process.pid, "cancelled")
                    return False, "已取消"

                ret = process.poll()
                if ret is not None:
                    self._add_log(process.pid, f"进程结束, 返回码: {ret}")
                    break

                try:
                    line = process.stdout.readline()
                    if line:
                        line = line.strip()
                        self._add_log(process.pid, line)
                        if progress_cb and "%" in line:
                            percent_matches = re.findall(r'\((\d+)%\)', line)
                            progress_percent = int(percent_matches[-1]) if percent_matches else 0
                            progress_cb(chapter, line, progress_percent * 100)
                except:
                    pass

                time.sleep(0.1)

            with self._lock:
                self.processes.pop(chapter, None)

            if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                file_size = os.path.getsize(output_path)
                self._add_log(process.pid, f"下载完成: {file_size//1024//1024}MB")
                self._unregister_process(process.pid, "completed")
                split_results = self._split_long_video(output_path, duration_ms, progress_cb, chapter)
                if len(split_results) > 1:
                    if progress_cb:
                        progress_cb(chapter, f"完成 (已切分为{len(split_results)}段)", 10000)
                    return True, ";".join(split_results)
                if progress_cb:
                    progress_cb(chapter, f"完成 ({file_size//1024//1024}MB)", 10000)
                return True, output_path

            self._add_log(process.pid, "下载失败")
            self._unregister_process(process.pid, "failed")
            return False, f"下载失败 (返回码: {process.returncode if process else 'unknown'})"
        except FileNotFoundError:
            return False, "未找到 aria2c，请确保已安装并添加到 PATH"
        except Exception as e:
            if process:
                try:
                    process.terminate()
                    self._unregister_process(process.pid, "failed")
                except:
                    pass
            return False, str(e)
        finally:
            with self._lock:
                self.processes.pop(chapter, None)

    def download_single(self, m3u8_url: str, output_path: str, chapter: int,
                       progress_cb: Optional[Callable[[int, str, int], None]] = None,
                       duration_ms: int = 0, download_type: str = "m3u8") -> Tuple[bool, str]:
        """使用 N_m3u8DL-RE 下载单个章节
        
        duration_ms: 章节时长（毫秒），用于判断是否需要切分
        """
        if download_type == "http":
            return self._download_http_with_aria2(m3u8_url, output_path, chapter, progress_cb, duration_ms)

        # 确保目录存在
        output_dir = os.path.dirname(output_path)
        os.makedirs(output_dir, exist_ok=True)
        
        # 获取文件名（不含扩展名）
        save_name = os.path.splitext(os.path.basename(output_path))[0]
        
        # 如果文件已存在，先删除（允许重新下载）
        for ext in ['.mp4', '.ts', '.mkv']:
            existing = os.path.join(output_dir, save_name + ext)
            if os.path.exists(existing):
                try:
                    os.remove(existing)
                    print(f"[DEBUG] 删除已存在文件: {existing}")
                except Exception as e:
                    print(f"[DEBUG] 删除文件失败: {e}")
        
        # N_m3u8DL-RE 命令
        cmd = [
            self.m3u8dl_path,
            m3u8_url,
            "--save-dir", output_dir,
            "--save-name", save_name,
            "--thread-count", str(self.thread_count),
            "--auto-select",  # 自动选择最佳质量
            "--no-log",  # 不生成日志文件
        ]
        if self.proxy:
            cmd.extend(["--custom-proxy", self.proxy])
        cmd_str = " ".join(cmd)
        
        process = None
        
        try:
            if progress_cb:
                progress_cb(chapter, "启动下载...", 0)
            
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                encoding='utf-8', errors='replace'
            )
            
            # 注册进程
            self._register_process(process, cmd_str, chapter)
            self._add_log(process.pid, f"启动: {cmd_str[:100]}...")
            
            with self._lock:
                self.processes[chapter] = process
            
            # 读取输出并解析进度
            last_progress_msg = ""
            while True:
                if self.cancelled:
                    process.terminate()
                    self._add_log(process.pid, "用户取消")
                    self._unregister_process(process.pid, "cancelled")
                    return False, "已取消"
                
                # 检查进程状态
                ret = process.poll()
                if ret is not None:
                    self._add_log(process.pid, f"进程结束, 返回码: {ret}")
                    break
                
                # 读取输出
                try:
                    line = process.stdout.readline()
                    if line:
                        line = line.strip()
                        self._add_log(process.pid, line)
                        
                        # 解析进度信息 (N_m3u8DL-RE 输出格式)
                        # 例如: "Vid Kbps ... 7/32 21.88% 108.49MB/495.97MB 65.64MBps 00:00:20"
                        # 或者: "Vid 50.00% | Aud 50.00% | 50.23 MiB | 10.5 MiB/s"
                        if '%' in line or 'MiB' in line or 'GiB' in line or 'MB' in line:
                            last_progress_msg = line
                            
                            # 解析百分比 - 支持多种格式
                            progress_percent = 0
                            # 尝试匹配所有百分比数字，取最大的一个（通常是总进度）
                            percent_matches = re.findall(r'(\d+\.?\d*)%', line)
                            if percent_matches:
                                # 取所有匹配中的最大值作为进度
                                progress_percent = max(float(p) for p in percent_matches)
                            
                            if progress_cb:
                                # 第三个参数传递百分比（乘以100作为整数传递，保留精度）
                                progress_cb(chapter, line, int(progress_percent * 100))
                except:
                    pass
                
                time.sleep(0.1)
            
            with self._lock:
                self.processes.pop(chapter, None)
            
            # 检查下载结果 - N_m3u8DL-RE 默认输出 mp4
            final_path = os.path.join(output_dir, save_name + ".mp4")
            if not os.path.exists(final_path):
                # 尝试其他可能的扩展名
                for ext in ['.ts', '.mkv']:
                    alt_path = os.path.join(output_dir, save_name + ext)
                    if os.path.exists(alt_path):
                        final_path = alt_path
                        break
            
            if os.path.exists(final_path) and os.path.getsize(final_path) > 1024:
                file_size = os.path.getsize(final_path)
                self._add_log(process.pid, f"下载完成: {file_size//1024//1024}MB")
                self._unregister_process(process.pid, "completed")
                
                # 检查是否需要切分（超过3小时的视频）
                # 使用传入的 duration_ms 判断
                split_results = self._split_long_video(final_path, duration_ms, progress_cb, chapter)
                if len(split_results) > 1:
                    # 视频被切分了
                    if progress_cb:
                        progress_cb(chapter, f"完成 (已切分为{len(split_results)}段)", 10000)
                    return True, ";".join(split_results)
                else:
                    if progress_cb:
                        progress_cb(chapter, f"完成 ({file_size//1024//1024}MB)", 10000)
                    return True, final_path
            else:
                self._add_log(process.pid, f"下载失败")
                self._unregister_process(process.pid, "failed")
                return False, f"下载失败 (返回码: {process.returncode if process else 'unknown'})"
                    
        except FileNotFoundError:
            return False, "未找到 N_m3u8DL-RE，请确保已安装并添加到 PATH"
        except Exception as e:
            if process:
                try:
                    process.terminate()
                    self._unregister_process(process.pid, "failed")
                except:
                    pass
            return False, str(e)
        finally:
            with self._lock:
                self.processes.pop(chapter, None)

    def convert_to_mp4(self, ts_path_or_paths: str, progress_cb: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
        """将 TS 转换为 MP4，如果文件超过15GB则自动分片
        
        ts_path_or_paths: 单个路径或用分号分隔的多个路径
        """
        # 解析输入路径
        if ";" in ts_path_or_paths:
            ts_paths = ts_path_or_paths.split(";")
        else:
            ts_paths = [ts_path_or_paths]
        
        output_files = []
        
        for i, ts_path in enumerate(ts_paths):
            if not os.path.exists(ts_path):
                continue
            
            file_size = os.path.getsize(ts_path)
            file_size_gb = file_size / (1024 * 1024 * 1024)
            
            # 检查是否需要分片（超过15GB）
            if file_size_gb > 15:
                if progress_cb:
                    progress_cb(f"文件大小 {file_size_gb:.2f}GB，超过15GB限制，开始分片...")
                
                # 使用ffmpeg进行分片
                split_files = self._split_video_file(ts_path, progress_cb)
                if split_files:
                    output_files.extend(split_files)
                    # 删除原TS文件
                    try:
                        os.remove(ts_path)
                    except:
                        pass
                else:
                    # 分片失败，尝试直接转换
                    if progress_cb:
                        progress_cb("分片失败，尝试直接转换...")
                    mp4_path = ts_path.replace(".ts", ".mp4")
                    if self._convert_single_file(ts_path, mp4_path, progress_cb):
                        output_files.append(mp4_path)
                    else:
                        output_files.append(ts_path)
            else:
                # 文件小于15GB，直接转换
                mp4_path = ts_path.replace(".ts", ".mp4")
                
                if progress_cb:
                    if len(ts_paths) > 1:
                        progress_cb(f"转换分片 {i+1}/{len(ts_paths)}...")
                    else:
                        progress_cb("转换为MP4...")
                
                if self._convert_single_file(ts_path, mp4_path, progress_cb):
                    output_files.append(mp4_path)
                else:
                    # 转换失败，保留TS文件
                    output_files.append(ts_path)
        
        if output_files:
            if progress_cb:
                mp4_count = sum(1 for f in output_files if f.endswith(".mp4"))
                progress_cb(f"转换完成 ({mp4_count}/{len(output_files)})")
            return True, ";".join(output_files)
        return False, "转换失败"
    
    def _convert_single_file(self, ts_path: str, mp4_path: str, progress_cb: Optional[Callable[[str], None]] = None) -> bool:
        """转换单个TS文件为MP4"""
        cmd = [
            self.ffmpeg_path, "-y", "-i", ts_path,
            "-c", "copy",  # 不重新编码，直接复制
            mp4_path
        ]
        
        try:
            result = subprocess.run(
                cmd, capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                timeout=3600  # 1小时超时
            )
            if result.returncode == 0 and os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 1024:
                return True
        except subprocess.TimeoutExpired:
            if progress_cb:
                progress_cb(f"转换超时")
        except FileNotFoundError:
            if progress_cb:
                progress_cb("未安装ffmpeg")
        except Exception as e:
            if progress_cb:
                progress_cb(f"转换错误: {str(e)}")
        return False
    
    def _split_video_file(self, ts_path: str, progress_cb: Optional[Callable[[str], None]] = None) -> List[str]:
        """使用ffmpeg将大文件分片为多个15GB的文件"""
        if not os.path.exists(ts_path):
            return []
        
        # 获取文件大小
        file_size = os.path.getsize(ts_path)
        file_size_gb = file_size / (1024 * 1024 * 1024)
        
        # 获取视频时长
        duration_cmd = [
            self.ffmpeg_path, "-i", ts_path, "-hide_banner", "-f", "null", "-"
        ]
        
        duration_seconds = None
        try:
            result = subprocess.run(
                duration_cmd, capture_output=True, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                timeout=60
            )
            # 从stderr中提取时长信息
            for line in result.stderr.decode('utf-8', errors='ignore').split('\n'):
                if 'Duration:' in line:
                    try:
                        time_str = line.split('Duration:')[1].split(',')[0].strip()
                        parts = time_str.split(':')
                        duration_seconds = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                        break
                    except:
                        pass
        except:
            pass
        
        if duration_seconds is None or duration_seconds <= 0:
            # 无法获取时长，使用文件大小比例估算
            # 简单估算：假设每GB对应一定时长（粗略估算）
            estimated_duration = file_size_gb * 3600  # 粗略估算：1GB约1小时
            duration_seconds = estimated_duration
        
        # 计算需要分多少片（每片15GB）
        num_parts = int(file_size_gb / 15) + (1 if file_size_gb % 15 > 0 else 0)
        
        # 计算每片的时长（留一些余量，确保不超过15GB）
        part_duration = duration_seconds / num_parts * 0.95  # 留5%余量
        
        output_files = []
        base_path = ts_path.replace(".ts", "")
        max_part_size = 15 * 1024 * 1024 * 1024  # 15GB
        
        # 按时间分片，直接从TS分片并转换为MP4
        current_start = 0
        part_num = 1
        
        while current_start < duration_seconds:
            part_path = f"{base_path}_part{part_num}.mp4"
            
            if progress_cb:
                progress_cb(f"分片 {part_num}/{num_parts}...")
            
            # 计算当前片的时长
            remaining_duration = duration_seconds - current_start
            current_duration = min(part_duration, remaining_duration)
            
            cmd = [
                self.ffmpeg_path, "-y", "-i", ts_path,
                "-ss", str(current_start),
                "-t", str(current_duration),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                part_path
            ]
            
            try:
                result = subprocess.run(
                    cmd, capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                    timeout=3600
                )
                if result.returncode == 0 and os.path.exists(part_path):
                    part_size = os.path.getsize(part_path)
                    if part_size > 1024:
                        output_files.append(part_path)
                        # 如果当前片超过15GB，调整下一片的时长
                        if part_size > max_part_size:
                            # 当前片太大，需要减小下一片的时长
                            part_duration = part_duration * (max_part_size / part_size) * 0.9
                        current_start += current_duration
                        part_num += 1
                    else:
                        break
                else:
                    break
            except Exception as e:
                if progress_cb:
                    progress_cb(f"分片错误: {str(e)}")
                break
        
        return output_files

    def download_chapters_parallel(self, tasks: List[dict], 
                                   progress_cb: Optional[Callable[[int, str], None]] = None) -> List[dict]:
        """并行下载多个章节
        
        tasks: [{"m3u8_url": ..., "output_path": ..., "chapter": ..., "duration": ...}, ...]
        返回: [{"chapter": ..., "success": bool, "result": str}, ...]
        """
        self.cancelled = False
        self._progress_data.clear()
        results = []
        
        def download_task(task):
            chapter = task["chapter"]
            duration_ms = task.get("duration", 0)  # 获取时长（毫秒）
            ok, result = self.download_single(
                task["m3u8_url"],
                task["output_path"], 
                chapter,
                progress_cb,
                duration_ms,
                task.get("download_type", "m3u8")
            )
            return {"chapter": chapter, "success": ok, "result": result}
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(download_task, t): t for t in tasks}
            for future in as_completed(futures):
                if self.cancelled:
                    break
                try:
                    results.append(future.result())
                except Exception as e:
                    task = futures[future]
                    results.append({"chapter": task["chapter"], "success": False, "result": str(e)})
        
        return results
