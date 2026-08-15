# SOOP / CHZZK 直播录像下载器

下载 SOOP（原 AfreecaTV）和 CHZZK（치지직）直播录像的 Web 应用，提供图形界面与下载管理功能。

## 功能特性

- SOOP Cookie 认证 + CHZZK Cookie / Device ID 认证
- 自动获取关注主播/频道列表（SOOP + CHZZK 双平台）
- 浏览主播历史录像，按日期/热度排序
- 粘贴链接解析（SOOP 主页/录像、CHZZK 频道/视频、手动 MPD/M3U8）
- 多章节并行下载（最多 3 个任务同时进行）
- 自动选择最高画质，下载后转为 MP4
- 超长视频自动切分（可自定义阈值和段长，支持关闭）
- 大文件自动分片（超过 15GB 按时间分割）
- 自动下载模式（SOOP + CHZZK 独立配置，按时间范围定时检测新录像）
- 下载历史记录（最近 50 条）
- 本地缓存（图片 7 天、VOD 列表 5 分钟、详情 24 小时）
- 进程管理（查看/终止下载器进程，孤儿进程自动检测）
- 分级代理设置（状态查询/图片加载/下载，可分别配置或继承全局代理）
- 磁盘空间监控
- Windows Toast / macOS / Linux 系统通知
- SOOP / CHZZK 双主题外观，自动切换品牌色

## 项目结构

```
.
├── app.py              # Flask 主应用，Web API + 自动下载线程
├── api.py              # SOOP API 封装（登录、录像列表、详情、章节标题、图片缓存）
├── chzzk_api.py        # CHZZK API 封装（关注列表、视频列表、详情、下载解析）
├── downloader.py       # 下载管理（N_m3u8DL-RE / aria2c / ffmpeg / yt-dlp）
├── templates/
│   └── index.html      # 前端单页应用（三栏布局：侧栏 + 主区 + 日志）
├── config.example.json # 配置模板（复制为 config.json 后填入凭据）
├── requirements.txt    # Python 依赖
└── cache/              # 运行时生成：API 数据与图片缓存
```

`config.json`、`downloaded.json`、`history.json`、`processes.json`、`cache/`、`Logs/` 为运行时文件，已在 `.gitignore` 中排除，不会进入仓库。

## 环境要求

- Python 3.10+
- [N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE) — M3U8/MPD 下载器（核心）
- [aria2c](https://github.com/aria2/aria2) — HTTP 直链下载备选
- [FFmpeg](https://ffmpeg.org/) — 视频切分、TS→MP4 转换
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — CHZZK MPD 流下载备选

外部工具需自行安装，仓库不内置二进制。可放入项目目录或加入系统 PATH，启动后也可在设置中点击「自动识别」检测路径。

## 安装

```bash
git clone <repo-url>
cd SOOP_VOD_DL
pip install -r requirements.txt
```

## 使用方法

1. 复制配置模板并填入凭据（或在启动后通过设置页配置）：
   ```bash
   cp config.example.json config.json
   ```
2. 启动应用：
   ```bash
   python app.py
   ```
3. 浏览器访问 `http://127.0.0.1:5000`。
4. 首次使用：进入「设置」，填入 SOOP Cookie（浏览器 F12 → Application → Cookies 复制）；如需 CHZZK，填入 CHZZK Cookie 和 Device ID；设置下载目录；可选配置代理与外部工具路径。
5. 配置完成后：左侧关注列表点击主播查看录像 → 选择章节下载；或粘贴 SOOP/CHZZK 链接直接解析。

### 设置项说明

| 设置项 | 说明 |
|--------|------|
| Cookie | SOOP 网站完整 Cookie 字符串 |
| CHZZK Cookie | CHZZK 网站完整 Cookie |
| CHZZK Device ID | CHZZK 设备标识符 (UUID) |
| 下载目录 | 视频保存根目录 |
| N_m3u8DL-RE 路径 | 核心下载器路径 |
| aria2c 路径 | HTTP 直链下载器路径 |
| ffmpeg 路径 | 视频处理工具路径 |
| yt-dlp 路径 | CHZZK 备选下载器路径 |
| 全部应用代理 | 全局代理，下级代理留空时自动继承 |
| 状态查询代理 | 查询 API 使用的代理 |
| 图片加载代理 | 加载封面图使用的代理 |
| 下载代理 | 视频下载使用的代理 |
| 超长视频自动切分 | 下载后自动切分超长视频的开关 |
| 切分阈值 | 超过此时长（小时）才触发切分，默认 3.5 |
| 每段时长 | 切分后每段最大时长（小时），默认 2.5 |

## 文件命名规则

```
{主播名}/{主播名}_{录像日期YYYYMMDD}_{章节标题}.mp4
```

多章节时第二章节起加序号（`主播_日期_2_标题.mp4`）；切分后第二段起加 part 后缀（`主播_日期_标题_part2.mp4`）。

## 自动下载

两个平台各自独立的自动下载配置：

- 启用开关 — 开启后后台线程定时运行
- 时间范围 — 仅在指定时间段内检测（如 22:00-04:00）
- 检测间隔 — 每隔 N 分钟扫描一次
- 最早日期 — 早于此日期的录像不会自动下载
- 监控主播 — 从关注列表中勾选要监控的主播

检测到新录像时自动获取详情并开始下载，完成后记录到 `downloaded.json` 避免重复。

## API 接口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/config` | GET/POST | 获取/保存配置 |
| `/api/login` | POST | SOOP 账号密码登录（返回 Cookie） |
| `/api/favorites` | GET | 获取 SOOP 关注列表 |
| `/api/chzzk/followings` | GET | 获取 CHZZK 关注列表 |
| `/api/vods/<user_id>` | GET | 获取 SOOP 主播录像列表 |
| `/api/vod/<title_no>` | GET | 获取 SOOP 录像详情（含章节和 M3U8） |
| `/api/chzzk/channel/<id>/videos` | GET | 获取 CHZZK 频道视频列表 |
| `/api/chzzk/video/<video_no>` | GET | 获取 CHZZK 视频详情和下载链接 |
| `/api/parse_url` | POST | 解析 SOOP/CHZZK/MPD/M3U8 链接 |
| `/api/download` | POST | 开始下载任务 |
| `/api/download/status` | GET | 获取下载状态（支持按 task_id 查询） |
| `/api/download/cancel` | POST | 取消下载 |
| `/api/processes` | GET | 获取所有下载进程 |
| `/api/processes/<pid>/logs` | GET | 获取进程日志 |
| `/api/processes/<pid>/kill` | POST | 强制终止进程 |
| `/api/processes/cleanup` | POST | 清理已完成的进程记录 |
| `/api/history` | GET | 获取下载历史 |
| `/api/history/clear` | POST | 清空下载历史 |
| `/api/cache/stats` | GET | 获取缓存统计 |
| `/api/cache/clear` | POST | 清理过期缓存 |
| `/api/cache/clear_selective` | POST | 选择性清理缓存 |
| `/api/disk_space` | GET | 获取磁盘空间信息 |
| `/api/auto_download/config` | GET/POST | 获取/保存 SOOP 自动下载配置 |
| `/api/auto_download/status` | GET | 获取 SOOP 自动下载运行状态 |
| `/api/auto_download/trigger` | POST | 手动触发一次 SOOP 检测 |
| `/api/chzzk/auto_download/config` | GET/POST | 获取/保存 CHZZK 自动下载配置 |
| `/api/chzzk/auto_download/status` | GET | 获取 CHZZK 自动下载状态 |
| `/api/chzzk/auto_download/trigger` | POST | 手动触发一次 CHZZK 检测 |
| `/api/tool_paths/detect` | GET | 自动检测系统 PATH 中的外部工具 |
| `/api/image` | GET | 图片代理（带本地缓存） |
| `/api/open_folder` | POST | 打开下载目录 |

## 缓存策略

| 类型 | 过期时间 |
|------|----------|
| 图片 | 7 天 |
| VOD 列表 | 5 分钟 |
| VOD 详情 | 24 小时 |
| 关注列表 | 1 分钟 |

## 技术栈

- 后端：Flask + Requests（Python）
- 前端：原生 HTML/CSS/JavaScript（单页应用，三栏布局）
- 下载引擎：N_m3u8DL-RE（主）+ aria2c（HTTP 备选）+ yt-dlp（CHZZK 备选）
- 视频处理：FFmpeg（切分、TS→MP4 转换）
- 数据：JSON 文件存储 + 本地文件缓存
- 通知：Windows Toast / macOS osascript / Linux notify-send

## 安全说明

`config.json` 含有 SOOP / CHZZK 登录 Cookie 等敏感凭据，已在 `.gitignore` 中排除。切勿将其提交到仓库，模板文件 `config.example.json` 已留空字段供参考。

## License

MIT
