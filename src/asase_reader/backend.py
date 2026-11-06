"""GUI 后端：任务队列 + 独立事件循环线程 + 暴露给前端的 JS API。

线程模型：
- pywebview 在自己的线程池里调用 Api 的方法（任意线程）；
- 下载器运行在专用后台线程的 asyncio 事件循环里；
- 双方通过 threading.RLock（可重入）保护的 job 列表交互，状态变更后 evaluate_js 推给前端。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .control import Control, OperationCancelled
from .credentials import Credentials
from .download import download_opus, opus_is_complete
from .errors import BiliOpusError, DailyLimitReached, RiskControlError
from .export import (
    NAMING_MODES,
    NAMING_NUMBERED,
    ExportCancelled,
    plan_all,
    plan_chapter,
    plan_work,
    run_plan,
)
from .fetch import Fetcher
from .library import Library, LibraryError, opus_chapter_id
from .parse import extract_opus_id, opus_url, parse_opus_html
from .ratelimit import RateLimiter
from .store import chapter_dir, opus_dir
from .videograb import (
    DEFAULT_THRESHOLD,
    VideoGrabCancelled,
    VideoGrabError,
    clamp_threshold,
    estimate_frames,
    extract_frames,
    is_video,
    resolve_ffmpeg,
    video_filter_label,
)

# 项目根目录：打包成 exe 后（sys.frozen）以 exe 所在目录为准，
# 源码运行时为 src/asase_reader/backend.py 上两级
import sys

if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[2]


def get_data_dir() -> Path:
    """固定小数据目录（config.json / pages_*.json / ui_settings.json / webview_data）。

    双锚点（方案 §3）：frozen 走 %LOCALAPPDATA%\\AsaseReader —— 用**环境变量**取、
    禁止用 Known Folder API 取 LOCALAPPDATA（它没有「文档」那种 OneDrive/域重定向
    语义，API 纯属多余；且环境变量取法天然支持测试与沙箱重定向：
    LOCALAPPDATA=<临时目录> 再跑 exe 即全程隔离、宿主零污染）。
    源码运行保持仓库根 .asase/，开发体验不变。
    注意：模块内部一律调用本函数而非裸名 DATA_DIR（见文件末尾 __getattr__ 的注记）。
    """
    if getattr(sys, "frozen", False):
        return Path(
            os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        ) / "AsaseReader"
    return PROJECT_ROOT / ".asase"


# 首启默认书库：模块级缓存（poll 每秒一次 get_state，别每次都调 SHGetKnownFolderPath；
# Known Folder 一个会话内不会变）。**缓存必须可重置**：测试在切换真实环境 / Mock
# 文档目录前后调用 _clear_default_shelf_dir_cache()，防跨用例脏缓存。
# 实现用模块全局变量 + 手动重置，不用 functools.lru_cache——其 cache_clear() 的
# 并发语义文档不保证，而 poll 是每秒一次的多线程调用（方案 §4.6）。
_DEFAULT_SHELF_DIR_CACHE: Path | None = None


def _clear_default_shelf_dir_cache() -> None:
    global _DEFAULT_SHELF_DIR_CACHE
    _DEFAULT_SHELF_DIR_CACHE = None


def default_shelf_dir() -> Path:
    """首启默认书库：「文档」Known Folder 下的 AsaseReaderLibrary。

    **永不 mkdir**（只用于预填与只读回退；真正建目录只发生在确认提交时）。
    「文档」必须走 SHGetKnownFolderPath（OneDrive / 域重定向语义），Path.home()/
    "Documents" 只作 try/except 降级；降级算出的候选若不存在，由提交时的
    mkdir(parents=True) 把整条路径建出来。
    ctypes 注意（§4.6 / v1.29.0 教训）：c_wchar_p 接串后 CoTaskMemFree；
    **别给 ctypes.windll.* 设全局 argtypes**（会连带炸 pywebview 的 move()）。
    """
    global _DEFAULT_SHELF_DIR_CACHE
    if _DEFAULT_SHELF_DIR_CACHE is not None:
        return _DEFAULT_SHELF_DIR_CACHE

    doc_path = None
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", wintypes.BYTE * 8),
                ]

            # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            folderid_documents = GUID(
                0xFDD39AD0,
                0x238F,
                0x46AF,
                (wintypes.BYTE * 8)(
                    0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7
                ),
            )
            p_path = ctypes.c_wchar_p()
            # wintypes 没有 HRESULT，用 c_long；整段 try/except 降级
            hr = ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(folderid_documents), 0, None, ctypes.byref(p_path)
            )
            if hr == 0 and p_path.value:
                doc_path = Path(p_path.value)
                ctypes.windll.ole32.CoTaskMemFree(p_path)
        except Exception:
            doc_path = None

    if doc_path is None:
        doc_path = Path.home() / "Documents"

    result = doc_path / "AsaseReaderLibrary"
    _DEFAULT_SHELF_DIR_CACHE = result
    return result


DEFAULT_CONFIG = {
    # shelf_dir 默认留空，由 default_shelf_dir() 在首启时预填，**确认时才写进 config**
    # ——不写进默认值，否则任何 Backend 实例化（含全部测试、CLI）的 shelf_root
    # 都指向真实文档目录，而 update_config 的 mkdir 段会碰它（方案 §3）。
    # download_dir 留空 = 单目录模式（直接下载到书架的 chapters/<chapter_id>/），
    # 填了才走"先落暂存目录再搬进书架"。
    "shelf_dir": "",
    "download_dir": "",
    "import_mode": "move",
    "delay_min": 2.0,
    "delay_max": 5.0,
    "image_delay_min": 0.5,
    "image_delay_max": 1.5,
    "concurrency": 2,
    "max_per_day": 200,
    "include_cover": True,
    # 首启引导完成标记：初始必须是 Python False（bool() 转换钉死在 update_config
    # 的遍历分支里，与 include_cover 同款；别写 0 / "false"）
    "setup_done": False,
    # 导出偏好：上次导到哪里、章节文件夹用哪种命名（不参与下载设置页的保存）
    "export_dir": "",
    "export_naming": "numbered",
    # 已知有充电专属内容的 UP 主：[{"mid": 22258156, "name": "木琳琅"}]
    # 只存 UP 主的数字 ID 与名字，**不含任何凭据**（凭据只在内存里）。
    "paid_ups": [],
    # 视频转图集：ffmpeg 路径留空 = 用 PATH 里的（shutil.which 自动找）；
    # 阈值默认 0.02 由实测定（13 个样本，唯一张数从 0.025 起见顶，见 CHANGELOG 1.15.2）。
    "ffmpeg_path": "",
    "video_threshold": DEFAULT_THRESHOLD,
}

PATH_KEYS = ("shelf_dir", "download_dir")

# 风控红线：并发上限 2。**全项目只有这一处定义**（2026-10-25 收紧）。
# 此前 cli.py / backend.py 各写了一个 `min(x, 4)` 的字面量、GUI 输入框也是 max="4"，
# 而 README / MEMORY / SESSION_START 三份文档都写「并发 1~2」——
# `git log -S` 查出这个 4 从 v1.0.1 基线就在，**从未经过任何决策**，属无意识漂移；
# 更要紧的是当时没有一处会在配置落盘前夹取，config.json 里存 3 就真的按 3 跑。
# 所以现在做成单一来源 + 每个入口都夹一层，并由 test_concurrency.py 钉住
# 「代码常量 = GUI 上限 = README 文案」三者一致。
MAX_CONCURRENCY = 2


def clamp_concurrency(value, default: int = 2) -> int:
    """把任意输入夹回红线的 1~MAX_CONCURRENCY。非数字回落 default（而非放行）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return clamp_concurrency(default, 2)
    return max(1, min(n, MAX_CONCURRENCY))


@dataclass
class Job:
    opus_id: str
    status: str = "queued"  # queued running done failed skipped cancelled
    title: str = ""
    done: int = 0
    total: int = 0
    error: str = ""
    dir: str = ""
    stage: str = ""  # downloading / registering / importing / imported
    added_at: float = field(default_factory=time.time)
    author_mid: int | None = None  # 解析出的 UP 主 ID，用于按 UP 记忆付费状态
    need_cookie: bool = False  # 因缺少登录态而失败，前端可就此提示输入 Cookie

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("added_at")
        return d


class Backend:
    def __init__(self, state_dir: Path, config_path: Path | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.config_path = Path(config_path or self.state_dir / "config.json")
        self.config = self._load_config()
        # 必须是可重入锁：log() / _notify_state() 本身要拿这把锁，而它们经常
        # 在已经持锁的库操作里被调用（import_files / delete_chapter /
        # purge_incomplete 都是）。用普通 Lock 会直接死锁，整个界面卡住。
        self.lock = threading.RLock()
        self.jobs: list[Job] = []
        self.logs: list[tuple[float, str]] = []  # (ts, line) 环形缓冲
        self.control = Control()
        self.window = None  # gui 入口注入 webview.Window
        self.static_server = None  # gui 入口注入 StaticServer（换书架目录时同步 root）
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = False
        self._edit: dict | None = None  # 编辑页会话快照（begin_edit / cancel_edit）
        # 登录凭据**只在内存**：不落盘、不进日志，进程退出即消失
        self.credentials = Credentials()
        # 上一个任务的 UP 主：连载追更时队列里通常是同一个 UP，
        # 记住它可以省掉后续章节的"先无凭据试探"那一趟请求。
        self._last_author_mid: int | None = None

    # ---------- 目录 ----------

    def _resolve_dir(self, key: str) -> Path | None:
        """配置项里的目录：支持绝对路径，相对路径锚定 PROJECT_ROOT，留空返回 None。"""
        raw = str(self.config.get(key) or "").strip()
        if not raw:
            return None
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p

    def shelf_root(self) -> Path:
        """书架目录：唯一的内容库位置。

        shelf_dir 留空时回退 default_shelf_dir()（「文档」下的默认书库）。
        **该回退只用于只读与预填**（StaticServer root 指向不存在的目录只是 404，
        无副作用）；真正建目录只发生在首启确认 / 目录保存的提交路径。
        """
        return self._resolve_dir("shelf_dir") or default_shelf_dir()

    def download_root(self) -> Path | None:
        """下载暂存目录；None 表示单目录模式（直接下载到书架）。"""
        return self._resolve_dir("download_dir")

    def shelf_dir_display(self) -> str:
        return str(self.shelf_root())

    def download_dir_display(self) -> str:
        d = self.download_root()
        return str(d) if d else ""

    def _landing_dir(self, opus_id: str, opus=None) -> Path:
        """本次下载的落点：单目录模式直接进书架，双目录模式进暂存目录。"""
        staging = self.download_root()
        if staging is None:
            return chapter_dir(self.shelf_root(), opus_chapter_id(opus_id))
        return opus_dir(staging, opus)

    def _register(self, job: Job, landing: Path) -> Path:
        """下载产物 -> 书架里的章节。

        单目录模式只差登记（文件本来就在 chapters/<id>/）；双目录模式要先搬过去。
        注意：别在持锁时调 _notify_state()（它也要拿这把锁，会死锁）。
        """
        staging = self.download_root()
        if staging is None:
            cid = opus_chapter_id(job.opus_id)
            job.stage = "registering"
            with self.lock:
                self.library.register_chapter(cid, source="opus", opus_id=job.opus_id)
            job.stage = "imported"
            return chapter_dir(self.shelf_root(), cid)
        job.stage = "importing"
        with self.lock:
            ch = self.library.import_opus_dir(
                landing, mode=str(self.config.get("import_mode") or "move")
            )
        job.stage = "imported"
        return self.library.shelf_root() / ch["dir"]

    @property
    def library(self) -> Library:
        """按 shelf_root 懒建并缓存的书库对象。

        未完成首启引导（setup_done=False）时以 readonly=True 构造：
        load() 跳过 migrate / heal / _backup / _drop_dangling 四处修复动作、
        save() 直接 return——「未确认不落盘」的闸门在 Library 内部兜住。

        缓存重建条件（三重，M12 守）：key 变了（换书架目录）、无缓存、
        **缓存的 readonly 状态与当前需要不符**（首启确认后：同一 shelf_root、
        key 不变，但 readonly 实例必须换成可写实例——否则幂等确认路径下
        当次会话所有写库操作静默失效直到重启）。
        """
        root = self.shelf_root()
        key = str(root)
        readonly = not bool(self.config.get("setup_done", False))
        lib = getattr(self, "_library_obj", None)
        if (
            lib is None
            or getattr(self, "_library_key", None) != key
            or getattr(lib, "readonly", False) != readonly
        ):
            lib = Library(root / "library.json", root, readonly=readonly)
            self._library_obj = lib
            self._library_key = key
        return lib

    # ---------- 作品库 ----------

    def library_state(self) -> dict:
        with self.lock:
            lib = self.library
            chapters = lib.scan_chapters()
            works = []
            for w in lib.works:
                works.append(
                    {
                        "id": w["id"],
                        "name": w["name"],
                        "author": w["author"],
                        "cover_url": lib.work_cover_url(w),
                        "chapter_count": len(w["chapters"]),
                    }
                )
            orphans = [c for c in chapters if not c["assigned"]]
            # 双目录模式下，暂存目录里还没进书架的章节（不会静默搬运）
            staging = self.download_root()
            pending = lib.scan_pending_imports(staging) if staging else []
            return {
                "works": works,
                "chapters": chapters,
                "orphans": orphans,
                "pending": pending,
            }

    def list_works(self) -> dict:
        """全部作品的轻量列表（**不扫盘**）：给书架首页与「所有作品」页用。

        library_state() 每次都会 scan_chapters()（读每个章节的 meta.json），
        作品列表只需要 works 本身，不该为一次列表渲染付全库扫描的代价。
        排序：取 **max(最近更新, 最近阅读)** —— 刚下了新章节的、刚读过的都靠前；
        两个都空的（旧库迁移来的）沉底且保持书库原序（稳定排序）。
        """
        with self.lock:
            lib = self.library
            works = [
                {
                    "id": w["id"],
                    "name": w["name"],
                    "author": w["author"],
                    "cover_url": lib.work_cover_url(w),
                    "chapter_count": len(w["chapters"]),
                    "tags": list(w.get("tags") or []),
                    "created_at": w.get("created_at") or "",
                    "updated_at": w.get("updated_at") or "",
                    "last_read_at": w.get("last_read_at") or "",
                }
                for w in lib.works
            ]
            works.sort(
                key=lambda x: max(x["updated_at"], x["last_read_at"]), reverse=True
            )
            return {"works": works}

    def work_detail(self, wid: str) -> dict:
        with self.lock:
            lib = self.library
            w = lib.get_work(wid)
            chapters = []
            for i, cid in enumerate(w["chapters"], 1):
                c = lib.chapter_by_id(cid)
                if not c:
                    continue
                chapters.append(
                    {
                        "index": i,
                        "chapter_id": c["id"],
                        "opus_id": c["opus_id"] or "",
                        "source": c["source"],
                        "title": c["title"] or c["id"],
                        "note": c["note"],
                        "cover_url": lib.chapter_cover_url(c),
                        "path": c["dir"],
                    }
                )
            return {
                "id": w["id"],
                "name": w["name"],
                "author": w["author"],
                # 作品页要展示标签、编辑页要读写标签，两边都走这一个接口
                "tags": list(w.get("tags") or []),
                # 没设自定义封面时回落到默认（第一个有封面的章节的头图），
                # 与书架卡片保持一致——否则"恢复默认"后编辑页预览会因为空 src 变碎图
                "cover_url": lib.work_cover_url(w),
                "chapters": chapters,
            }

    def chapter_images(self, chapter_id: str) -> dict:
        with self.lock:
            # media 只有视频章节才有（图片章节是 None）：前端一个 if (r.media) 就能分流，
            # 不用再发一次请求，也不用靠「images 为空」去猜。
            return {
                "images": self.library.chapter_images(chapter_id),
                "media": self.library.chapter_media(chapter_id),
            }

    def create_work(self, name: str, author: str) -> dict:
        with self.lock:
            w = self.library.create_work(name or "未命名作品", author or "")
            return {"id": w["id"]}

    def update_work(self, wid: str, patch: dict) -> dict:
        with self.lock:
            w = self.library.update_work(wid, patch or {})
            return {"ok": True, "name": w["name"], "author": w["author"]}

    def mark_read(self, wid: str) -> dict:
        """进入阅读器时记一次，供「最近阅读」参与排序。作品不存在也只是静默跳过。"""
        with self.lock:
            try:
                self.library.mark_read(wid)
                return {"ok": True}
            except LibraryError:
                return {"ok": False}

    def delete_work(self, wid: str) -> dict:
        with self.lock:
            self.library.delete_work(wid)
            return {"ok": True}

    def add_chapters(self, wid: str, chapter_ids: list) -> dict:
        with self.lock:
            w = self.library.add_chapters(wid, list(chapter_ids or []))
            return {"ok": True, "chapter_count": len(w["chapters"])}

    def remove_chapter(self, wid: str, chapter_id: str) -> dict:
        with self.lock:
            self.library.remove_chapter(wid, chapter_id)
            return {"ok": True}

    def move_chapter(self, wid: str, chapter_id: str, delta: int) -> dict:
        with self.lock:
            self.library.move_chapter(wid, chapter_id, int(delta))
            return {"ok": True}

    def update_chapter(self, wid: str, chapter_id: str, patch: dict) -> dict:
        with self.lock:
            self.library.update_chapter(wid, chapter_id, patch or {})
            return {"ok": True}

    def set_work_cover_file(self, wid: str, src: str) -> dict:
        with self.lock:
            return {"ok": True, "cover_url": self.library.set_work_cover(wid, src)}

    def set_chapter_cover_file(self, wid: str, chapter_id: str, src: str) -> dict:
        with self.lock:
            return {
                "ok": True,
                "cover_url": self.library.set_chapter_cover(wid, chapter_id, src),
            }

    def save_chapter_cover_data(self, wid: str, chapter_id: str, data_url: str) -> dict:
        """存前端抓下来的封面画面（视频章节：当前这一帧）。"""
        with self.lock:
            return {
                "ok": True,
                "cover_url": self.library.save_chapter_cover_data(
                    wid, chapter_id, data_url or ""),
            }

    def clear_work_cover(self, wid: str) -> dict:
        return self.update_work(wid, {"clear_cover": True})

    def clear_chapter_cover(self, wid: str, chapter_id: str) -> dict:
        return self.update_chapter(wid, chapter_id, {"clear_cover": True})

    # ---------- 章节导入（本地图片 / 暂存目录） ----------

    def import_files(self, title: str, author: str, paths: list) -> dict:
        """一组本地图片 -> 一个章节（复制，不动用户原图）。"""
        # setup_done 闸门：未完成首启引导时导入 = 往 default_shelf_dir 回退值
        # 落盘，一律拒绝（方案 §3 写入闸门）
        if not bool(self.config.get("setup_done", False)):
            return {"ok": False, "error": "尚未完成首次启动引导，请先确认书库目录"}
        with self.lock:
            try:
                ch = self.library.import_local_images(
                    (title or "").strip() or f"导入章节 {date.today():%Y-%m-%d}",
                    author or "",
                    [str(p) for p in paths or []],
                )
            except LibraryError as e:
                return {"ok": False, "error": str(e)}
            self.log(f"已导入章节：{ch['title']}")
            return {"ok": True, "chapter_id": ch["id"], "title": ch["title"]}

    def import_folder(self, title: str, author: str, path: str) -> dict:
        """一个文件夹里的图片 -> 一个章节。"""
        if not bool(self.config.get("setup_done", False)):
            return {"ok": False, "error": "尚未完成首次启动引导，请先确认书库目录"}
        folder = Path(path or "")
        if not folder.is_dir():
            return {"ok": False, "error": "目录不存在"}
        files = [
            str(p)
            for p in sorted(folder.iterdir(), key=lambda p: p.name)
            if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp",
                                                    ".gif", ".bmp")
        ]
        if not files:
            return {"ok": False, "error": "这个目录里没有可用图片"}
        return self.import_files(title or folder.name, author or "", files)

    def import_pending(self, opus_ids: list) -> dict:
        """把暂存目录里已完成的章节搬进书架（双目录模式）。"""
        if not bool(self.config.get("setup_done", False)):
            return {"ok": False, "error": "尚未完成首次启动引导，请先确认书库目录"}
        staging = self.download_root()
        if staging is None:
            return {"ok": False, "error": "当前是单目录模式，没有暂存目录"}
        mode = str(self.config.get("import_mode") or "move")
        done, failed = [], []
        with self.lock:
            for p in self.library.scan_pending_imports(staging):
                if str(p["opus_id"]) not in {str(x) for x in opus_ids or []}:
                    continue
                try:
                    self.library.import_opus_dir(Path(p["path"]), mode=mode)
                    done.append(p["opus_id"])
                except Exception as e:
                    failed.append(f"{p['opus_id']}: {e}")
        if done:
            self.log(f"已导入 {len(done)} 个章节到书架")
        return {"ok": not failed, "imported": done, "failed": failed}

    def rename_chapter(self, chapter_id: str, title: str, author: str) -> dict:
        """未入库章节也能改名/改作者。"""
        with self.lock:
            try:
                ch = self.library.rename_chapter(chapter_id, title or "", author)
            except LibraryError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "title": ch["title"], "author": ch["author"]}

    def delete_chapter_images(self, chapter_id: str, files: list) -> dict:
        """删除章节内的部分图片（含磁盘文件），剩下的重新编号。不可逆。

        删除与"所属作品"无关：未入库的章节同样能删（和 rename_chapter 一个口径）。
        """
        with self.lock:
            try:
                r = self.library.delete_chapter_images(chapter_id, list(files or []))
            except LibraryError as e:
                return {"ok": False, "error": str(e)}
            self.log(f"已删除 {r['removed']} 张图片（该章还剩 {r['remaining']} 张）")
            return {"ok": True, **r}

    def delete_chapter(self, chapter_id: str) -> dict:
        """把章节从书库彻底移除（含磁盘文件）。"""
        with self.lock:
            try:
                self.library.delete_chapter(chapter_id)
            except LibraryError as e:
                return {"ok": False, "error": str(e)}
            self.log(f"已从书库移除章节 {chapter_id}")
            return {"ok": True}

    def list_incomplete(self) -> dict:
        """chapters/ 下没有 meta.json 的残目录（下载中断留下的）。"""
        with self.lock:
            return {"items": self.library.find_incomplete()}

    def purge_incomplete(self, names: list) -> dict:
        with self.lock:
            n = self.library.purge_incomplete(list(names or []))
            self.log(f"已清理 {n} 个未完成的下载残留")
            return {"ok": True, "removed": n}

    # ---------- 导出（把书架内容复制成普通文件夹） ----------
    #
    # 导出只是**复制**：书架里的原件原封不动，代价是磁盘多占一份。
    # 规划阶段在锁内完成（要读 library），搬运放在独立线程，前端轮询 export_status。

    def _export_state(self) -> dict:
        st = getattr(self, "_export", None)
        if st is None:
            st = {
                "running": False, "finished": False, "cancelled": False,
                "done": 0, "total": 0, "current": "", "copied": 0,
                "skipped": 0, "failed": 0, "dest": "", "error": "",
            }
            self._export = st
        return st

    def export_start(self, kind: str, target_id: str, dest: str,
                     naming: str = NAMING_NUMBERED) -> dict:
        dest = str(dest or "").strip()
        if not dest:
            return {"ok": False, "error": "先选一个导出目录"}
        try:
            d = Path(os.path.expanduser(dest))
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"ok": False, "error": f"导出目录不可用：{e}"}
        # 导进书架目录里会变成"没有 meta.json 的目录"，被当成未完成的下载残留
        try:
            shelf, here = self.shelf_root().resolve(), d.resolve()
        except OSError:
            shelf = here = None
        if shelf is not None and (here == shelf or shelf in here.parents):
            return {"ok": False, "error": "导出目录不能放在书架目录里面（会被认成未完成的下载残留）"}
        mode = naming if naming in NAMING_MODES else NAMING_NUMBERED
        kind = str(kind or "")
        try:
            with self.lock:
                lib = self.library
                if kind == "chapter":
                    items = plan_chapter(lib, target_id, d, mode)
                elif kind == "work":
                    items = plan_work(lib, target_id, d, mode)
                elif kind == "all":
                    items = plan_all(lib, d, mode)
                else:
                    return {"ok": False, "error": f"未知的导出范围：{kind}"}
        except LibraryError as e:
            return {"ok": False, "error": str(e)}
        if not items:
            return {"ok": False, "error": "这个范围内没有可导出的图片"}
        st = self._export_state()
        st.update({
            "running": True, "finished": False, "cancelled": False,
            "done": 0, "total": len(items), "current": "", "copied": 0,
            "skipped": 0, "failed": 0, "dest": str(d), "error": "",
        })
        self.config["export_dir"] = str(d)
        self.config["export_naming"] = mode
        self._save_config()
        threading.Thread(target=self._run_export, args=(items, st), daemon=True).start()
        return {"ok": True, "total": len(items), "dest": str(d)}

    def _run_export(self, items, st: dict) -> None:
        try:
            res = run_plan(
                items,
                on_progress=lambda i, _total, name: st.update({"done": i, "current": name}),
                should_cancel=lambda: st["cancelled"],
            )
            st.update({
                "copied": res["copied"], "skipped": res["skipped"], "failed": res["failed"],
            })
            msg = f"导出完成：{res['copied']} 个文件 → {st['dest']}"
            if res["skipped"]:
                msg += f"（跳过 {res['skipped']} 个已存在的）"
            self.log(msg)
        except ExportCancelled:
            st["cancelled"] = True
            self.log("导出已取消")
        except Exception as e:  # 兜底：不让导出线程炸掉界面
            st["error"] = str(e)
            self.log(f"导出失败：{e}")
        finally:
            st["running"] = False
            st["finished"] = True

    def export_status(self) -> dict:
        return dict(self._export_state())

    def export_cancel(self) -> dict:
        self._export_state()["cancelled"] = True
        return {"ok": True}

    # ---------- 视频转图集（v1.15） ----------
    #
    # 沿用导出那套「start → 后台线程 → status 轮询 → cancel」的状态机，不另起炉灶。
    # 分两段：**先预估（不落盘）→ 前端弹确认 → 再真正抽帧入库**，因为一批二十几个视频
    # 抽错阈值要重跑十几分钟，先花几秒报个总数是很便宜的保险。
    # 取消令牌**独立于下载用的 Control**：两条流水线共用一个令牌迟早会互相踩
    # （v1.3.1 就是"取消一次后令牌停在已取消、之后怎么搞都立刻取消"）。

    def _video_state(self) -> dict:
        st = getattr(self, "_video", None)
        if st is None:
            st = {
                "running": False, "finished": False, "cancelled": False,
                "phase": "",  # estimating / confirm / extracting
                "done": 0, "total": 0, "current": "",
                "frames": 0, "made": 0, "error": "", "files": [],
                "started_at": 0.0,
            }
            self._video = st
        return st

    def video_tool_state(self) -> dict:
        """设置页用：ffmpeg 找到了没、用的哪一个、当前阈值、支持的后缀。"""
        configured = str(self.config.get("ffmpeg_path") or "").strip()
        from_path = shutil.which("ffmpeg")
        found = resolve_ffmpeg(configured)
        return {
            "found": bool(found),
            "path": found or "",
            "source": "config" if (found and not from_path) else ("path" if found else ""),
            "configured": configured,
            "threshold": clamp_threshold(self.config.get("video_threshold")),
            "exts": video_filter_label(),
        }

    def video_import_start(self, paths: list, mode: str = "frames") -> dict:
        """导入本地视频，两条并列的去处：

        - ``mode="frames"``：抽帧成图片章节（三段式：预估 → confirm → 抽取，慢）；
        - ``mode="video"``：原样入库成视频章节（复制 + 截首帧，同步很快，
          所以**不走**预估/confirm 那套阶段机）。
        """
        # setup_done 闸门：mode="video" 当场落盘、frames 走到 confirm 也落盘，
        # 入口就拦，别让用户选完视频点确认才被拒
        if not bool(self.config.get("setup_done", False)):
            return {"ok": False, "error": "尚未完成首次启动引导，请先确认书库目录"}
        files = [str(p) for p in (paths or []) if is_video(p)]
        if not files:
            return {"ok": False,
                    "error": f"没有可用的视频文件（目前支持 {video_filter_label()}）"}
        if mode == "video":
            return self._video_import_as_chapters(files)
        ffmpeg = resolve_ffmpeg(self.config.get("ffmpeg_path"))
        if not ffmpeg:
            return {"ok": False,
                    "error": "没有找到 ffmpeg：请在「设置 → 视频转图集」里填写路径，"
                             "或把 ffmpeg 所在目录加入 PATH"}
        st = self._video_state()
        st.update({
            "running": True, "finished": False, "cancelled": False,
            "phase": "estimating", "done": 0, "total": len(files),
            "current": "", "frames": 0, "made": 0, "error": "",
            "files": files, "started_at": time.time(),
        })
        threading.Thread(target=self._run_video_estimate, args=(ffmpeg, st),
                         daemon=True).start()
        return {"ok": True, "total": len(files)}

    def _video_import_as_chapters(self, files: list[str]) -> dict:
        """导入为视频章节：复制进书架 + 截首帧封面，同步做完。

        没有 ffmpeg 也能导（只是没有首帧封面），这条**不依赖**抽帧那条路的 ffmpeg 校验。
        """
        ffmpeg = resolve_ffmpeg(self.config.get("ffmpeg_path"))
        made: list[str] = []
        for f in files:
            try:
                with self.lock:
                    ch = self.library.import_video_file(Path(f), ffmpeg=ffmpeg)
                made.append(ch["id"])
                self.log(f"已导入视频章节：{ch['title']}")
            except (LibraryError, VideoGrabError, OSError) as e:
                self.log(f"{Path(f).name} 导入失败：{e}")
        if not made:
            return {"ok": False, "error": "没有一个视频导入成功，详见日志"}
        self.log(f"视频章节导入完成：{len(made)} 个已放入「未入库的章节」")
        return {
            "ok": True,
            "mode": "video",
            "made": len(made),
            "chapter_ids": made,
            "no_ffmpeg": not ffmpeg,  # 前端据此提示「没截到首帧封面」
        }

    def _run_video_estimate(self, ffmpeg: str, st: dict) -> None:
        th = clamp_threshold(self.config.get("video_threshold"))
        total = 0
        try:
            for i, f in enumerate(st["files"], 1):
                if st["cancelled"]:
                    break
                st["done"] = i
                st["current"] = Path(f).name
                try:
                    total += estimate_frames(ffmpeg, f, th)
                except VideoGrabError:
                    pass  # 单个读不出来先记 0，抽取阶段会给出真正的报错
            st["frames"] = total
            st["phase"] = "" if st["cancelled"] else "confirm"
        except Exception as e:  # 兜底：不让后台线程炸掉界面
            st["error"] = str(e)
            st["phase"] = ""
        finally:
            if st["phase"] != "confirm":
                st["running"] = False
                st["finished"] = True

    def video_import_confirm(self) -> dict:
        """预估完成、用户确认后开始真正抽帧入库。"""
        if not bool(self.config.get("setup_done", False)):
            return {"ok": False, "error": "尚未完成首次启动引导，请先确认书库目录"}
        st = self._video_state()
        if st.get("phase") != "confirm":
            return {"ok": False, "error": "当前没有待确认的转换"}
        ffmpeg = resolve_ffmpeg(self.config.get("ffmpeg_path"))
        if not ffmpeg:
            return {"ok": False, "error": "没有找到 ffmpeg"}
        # 空间 B 档（§4.5「视频开始前」）：抽帧产物落临时目录再复制进书架，
        # 按书架盘查一次，<50MB 拒绝开始
        try:
            tgt = self.shelf_root()
            tgt = tgt if tgt.exists() else self._existing_ancestor(tgt)
            free = shutil.disk_usage(tgt).free
            if free < 50 * 1024 * 1024:
                return {
                    "ok": False,
                    "error": (
                        f"磁盘可用空间不足 50 MB"
                        f"（剩余 {free // (1024 * 1024)} MB），已取消转换"
                    ),
                }
        except Exception as e:
            self.log(f"检查磁盘空间异常（放行）: {e}")
        st.update({
            "running": True, "finished": False, "cancelled": False,
            "phase": "extracting", "done": 0, "made": 0, "current": "",
            "error": "", "started_at": time.time(),
        })
        threading.Thread(target=self._run_video_extract, args=(ffmpeg, st),
                         daemon=True).start()
        return {"ok": True}

    def _run_video_extract(self, ffmpeg: str, st: dict) -> None:
        th = clamp_threshold(self.config.get("video_threshold"))
        made: list[str] = []
        try:
            for i, f in enumerate(st["files"], 1):
                if st["cancelled"]:
                    break
                st["done"] = i
                st["current"] = Path(f).name
                # 抽到临时目录再交给 import_local_images（它是「复制」语义，
                # 这样抽帧产物和书库归档完全走同一条路：meta.json、封面、未入库语义全复用）
                tmp = Path(tempfile.mkdtemp(prefix="asase_vg_"))
                try:
                    n = extract_frames(ffmpeg, f, th, tmp,
                                       should_cancel=lambda: st["cancelled"])
                    if n <= 0:
                        self.log(f"{Path(f).name}：一张都没抽出来，已跳过")
                        continue
                    paths = [str(p) for p in sorted(tmp.glob("*.jpg"))]
                    with self.lock:
                        ch = self.library.import_local_images(Path(f).stem, "", paths)
                    made.append(ch["id"])
                    self.log(f"已从视频导入：{Path(f).stem}（{n} 张）")
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
            st["made"] = len(made)
            if made:
                self.log(f"视频转图集完成：{len(made)} 个章节已放入「未入库的章节」")
        except VideoGrabCancelled:
            st["cancelled"] = True
            self.log("视频转图集已取消")
        except Exception as e:  # 兜底
            st["error"] = str(e)
            self.log(f"视频转图集失败：{e}")
        finally:
            st["phase"] = ""
            st["running"] = False
            st["finished"] = True

    def video_import_status(self) -> dict:
        st = dict(self._video_state())
        st.pop("files", None)  # 路径列表不用回传前端
        st["elapsed"] = round(time.time() - float(st.get("started_at") or time.time()), 1)
        return st

    def video_import_cancel(self) -> dict:
        st = self._video_state()
        st["cancelled"] = True
        if not st.get("running"):
            st["phase"] = ""  # 停在「待确认」时取消：直接收摊
        return {"ok": True}

    # ---------- 编辑会话（进入编辑页时快照，"返回"整体回滚） ----------
    #
    # 编辑页里的每一步操作（改名/注释、排序、增删章节、设封面）都直接落到
    # library.json，只有在**进入编辑页时**先存一份快照，用户点「← 返回」时
    # 整体还原，点「保存并返回」才丢弃快照。这样"返回=放弃全部改动"成立，
    # 又不必在前端维护一份草稿。

    def begin_edit(self, wid: str) -> dict:
        with self.lock:
            lib = self.library
            raw = lib.path.read_text(encoding="utf-8") if lib.path.exists() else None
            born = None
            for w in lib.works:
                if w["id"] == wid:
                    born = json.loads(json.dumps(w))  # 深拷贝：新建的作品不被回滚掉
                    break
            self._edit = {
                "wid": wid,
                "snapshot": raw,
                "born": born,
                "covers": self._cover_files_snapshot(),
            }
            return {"ok": True}

    def commit_edit(self) -> dict:
        with self.lock:
            self._edit = None
            return {"ok": True}

    def cancel_edit(self) -> dict:
        with self.lock:
            snap = getattr(self, "_edit", None)
            self._edit = None
            if not snap:
                return {"ok": True}
            lib = self.library
            if snap["snapshot"] is None:
                try:
                    lib.path.unlink()
                except FileNotFoundError:
                    pass
            else:
                lib.path.parent.mkdir(parents=True, exist_ok=True)
                lib.path.write_text(snap["snapshot"], encoding="utf-8")
            lib.load()
            # 编辑页里新建的作品（快照里没有）保留下来，回到刚创建时的状态
            if snap["born"] and not any(w["id"] == snap["wid"] for w in lib.works):
                lib.works.append(snap["born"])
                lib.save()
            self._restore_cover_files(snap["covers"])
            return {"ok": True}

    def edit_dirty(self) -> dict:
        """本次编辑会话是否改动过作品库（用于「返回」时提示）。"""
        with self.lock:
            snap = getattr(self, "_edit", None)
            if not snap:
                return {"dirty": False}
            if snap["snapshot"] is None:
                old: list = []
            else:
                try:
                    old = json.loads(snap["snapshot"]).get("works", [])
                except Exception:
                    return {"dirty": True}
            cur = self.library.works
            return {
                "dirty": json.dumps(old, ensure_ascii=False, sort_keys=True)
                != json.dumps(cur, ensure_ascii=False, sort_keys=True)
            }

    def _cover_files_snapshot(self) -> dict:
        """_covers 下每个文件的字节快照（回滚时用来还原被覆盖/删除的封面）。"""
        d = self.library.shelf_root / "_covers"
        out: dict = {}
        if d.is_dir():
            for p in d.iterdir():
                if p.is_file() and p.stat().st_size <= 8 * 1024 * 1024:
                    try:
                        out[p.relative_to(self.library.shelf_root).as_posix()] = p.read_bytes()
                    except Exception:
                        pass
        return out

    def _restore_cover_files(self, snap: dict) -> None:
        root = self.library.shelf_root
        d = root / "_covers"
        if d.is_dir():
            for p in list(d.iterdir()):
                if not p.is_file():
                    continue
                rel = p.relative_to(root).as_posix()
                try:
                    if rel in snap:
                        if p.read_bytes() != snap[rel]:
                            p.write_bytes(snap[rel])
                    else:
                        p.unlink()  # 本次会话新复制进来的封面，回滚即删除
                except Exception:
                    pass
        for rel, data in snap.items():  # 会话中被删掉的封面，补回来
            p = root / rel
            if not p.exists():
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                except Exception:
                    pass

    # ---------- 配置 ----------

    def _load_config(self) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        try:
            if self.config_path.exists():
                cfg.update(json.loads(self.config_path.read_text(encoding="utf-8")))
        except Exception:
            pass
        # 兼容旧版：以前只有一个 out 键，同时决定下载落点和书库位置
        if not str(cfg.get("shelf_dir") or "").strip():
            legacy = str(cfg.get("out") or "").strip()
            if legacy:
                cfg["shelf_dir"] = legacy
        # 磁盘上的配置可能被手改成 3/4/99（或来自放宽年代留下的旧配置），
        # 读进来就必须先夹回红线，否则界面上显示 3、下载也真的按 3 跑。
        cfg["concurrency"] = clamp_concurrency(cfg.get("concurrency"))
        # 同理：磁盘上的阈值可能被手改成 0 或 99，读进来就夹回可用区间
        cfg["video_threshold"] = clamp_threshold(cfg.get("video_threshold"))
        return cfg

    def _save_config(self) -> None:
        try:
            data = dict(self.config)
            # 保留旧键 out 指向书架目录，万一回滚到旧版 exe 也还能用
            data["out"] = self.config.get("shelf_dir")
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            # 静默吞掉连日志都没有的旧毛病改掉：ok:True 但 config.json 实际没写上、
            # 下次启动配置回退，用户毫无线索。记警告但不翻成 ok:False——
            # 调到这里时内存与目录往往已提交，回滚不成比例；首启确认落盘失败
            # 只意味着下次再弹一次模态，可接受（方案 §4.3 / Claude 二轮 P-3）。
            try:
                self.log(f"config.json 写入失败（下次启动配置会回退）: {e}")
            except Exception:
                pass

    def _existing_ancestor(self, p: Path) -> Path:
        """往上级找最近的已存在祖先目录（空间检查用，不 mkdir）。"""
        cur = Path(p)
        while not cur.exists():
            if cur.parent == cur:
                return Path(p).anchor  # 兜底：一路走到盘符根
            cur = cur.parent
        return cur

    def _check_free_space(self, p: Path) -> tuple[int | None, list[dict]]:
        """A 档空间检查（§4.5）：disk_usage 传目标目录本身——卷挂载点 / 目录联接
        下目标与盘符根不是同一卷，别传盘符根。目录不存在时查最近的已存在祖先
        （preview_dir 不 mkdir）。返回 (free_bytes, warnings)；不足 100MB 由
        调用方判阻断（本方法只收集，不抛）。
        """
        try:
            target = p if p.exists() else self._existing_ancestor(p)
            usage = shutil.disk_usage(target)
            warnings: list[dict] = []
            if usage.free < 2 * 1024 * 1024 * 1024:
                warnings.append(
                    {
                        "code": "low_space",
                        "message": (
                            f"该磁盘可用空间不足 2 GB"
                            f"（剩余 {usage.free // (1024 * 1024)} MB），"
                            "书库内容会持续增长，建议选空间更充裕的盘"
                        ),
                    }
                )
            return usage.free, warnings
        except Exception as e:
            self.log(f"检查磁盘空间异常（按可用处理）: {e}")
            return None, []

    def _validate_path(self, raw_path: str, *, skip_probe: bool = False) -> dict:
        """单条目录路径的严格校验（方案 §4.3 的规则表，preview_dir 与
        update_config 共用这一份实现，杜绝两处漂移）。

        返回 {ok, error, warnings: [{code, message}], free_bytes}。
        规则 9（真实可写探针）只在提交路径执行：探针要 mkdir + 落一个
        立即删除的临时文件；preview_dir 传 skip_probe=True——不落盘、不提交、
        不 mkdir，给前端「确认前黄字」取数用。
        """
        warnings: list[dict] = []

        def fail(err: str, free_bytes: int | None = None) -> dict:
            return {"ok": False, "error": err, "warnings": warnings, "free_bytes": free_bytes}

        raw = str(raw_path or "").strip()
        if not raw:
            return fail("路径不能为空")

        # 规则 1：路径规范化，再判 UNC——避免误拒 \\?\D:\... 长路径形式
        norm = os.path.normpath(raw)
        check_p = norm
        if check_p.startswith("\\\\?\\UNC\\"):  # \\?\UNC\server\share = 真 UNC
            return fail("不支持网络路径（UNC），请选择本地固定磁盘上的目录")
        if check_p.startswith("\\\\?\\"):  # \\?\D:\... 长路径前缀 = 本地路径
            check_p = check_p[4:]
        p = Path(check_p)

        # 规则 2：必须绝对路径（首启只收绝对；相对路径沿用既有锚定，不走这里）
        if not p.is_absolute():
            return fail("必须是绝对路径")

        # 规则 3：拒 UNC / 网络路径
        if check_p.startswith("\\\\") or raw.startswith("//"):
            return fail("不支持网络路径（UNC），请选择本地固定磁盘上的目录")

        # 规则 4：只收 DRIVE_FIXED(3)——**先取盘符根**再查：直接传子目录会返回
        # 1（UNKNOWN），误杀全部合法目录（真机探针坐实）。
        if sys.platform == "win32":
            try:
                import ctypes

                drive_root = os.path.splitdrive(os.path.abspath(check_p))[0] + "\\"
                # 别设全局 argtypes（v1.29.0 教训：会连带炸 pywebview 的 move()）
                dtype = ctypes.windll.kernel32.GetDriveTypeW(str(drive_root))
                if dtype == 4:  # DRIVE_REMOTE
                    return fail("不支持网络驱动器，请选择本地磁盘上的目录")
                if dtype != 3:  # 只收 DRIVE_FIXED；可移动/光驱/RAMDISK/UNKNOWN 一律拒
                    return fail(
                        "只支持本地固定磁盘（不支持 U 盘、光驱、网络盘、虚拟可移动盘）"
                    )
            except Exception as e:
                # API 挂了不阻断（老系统 / 权限异常），后面还有真实可写探针兜底
                self.log(f"检查驱动器类型异常（放行交由探针兜底）: {e}")

        # 规则 5：拒盘符根（rp == rp.anchor 即盘根；原稿 Path==str 永假的坑已修）
        rp = p.resolve()
        if rp == Path(rp.anchor):
            return fail("不能直接用整个磁盘当书库，请选一个子目录")

        # 规则 6：拒用户主目录根（home 根）与「文档」Known Folder 根——
        # _migrate_old_dirs 会 glob */*/meta.json 并移动目录，把这些根当书库
        # 有实际破坏面；「文档」根更易手滑命中：默认值就在文档目录里，
        # 选到父目录 = library.json/chapters 直接堆进文档根。
        if rp == Path.home().resolve():
            return fail("不能直接用用户主目录当书库，请选它的子目录")
        if rp == default_shelf_dir().parent.resolve():
            return fail("不能直接用「文档」目录当书库，请选它的子目录")

        # 规则 7：拒 DATA_DIR 及其子目录（配置 / webview_data 与内容混放）
        for ddir in (Path(self.state_dir).resolve(), Path(get_data_dir()).resolve()):
            if rp == ddir or ddir in rp.parents:
                return fail("书库 / 暂存不能放在程序数据目录里面")

        # 规则 8：安装目录及其子目录 = 黄字警告、**不阻断**（frozen 的
        # PROJECT_ROOT = exe 旁；迁移指引选回 exe 旁的旧 outputs 正是子目录）
        install_dir = Path(PROJECT_ROOT).resolve()
        if rp == install_dir or install_dir in rp.parents:
            warnings.append(
                {
                    "code": "install_dir",
                    "message": "该目录在程序所在目录下面：升级 / 换 exe 时容易被当成程序文件处理，建议选独立目录（不阻止保存）",
                }
            )

        # 规则 9：真实可写（探针）。mkdir(parents=True) 本就是提交段动作，
        # 放在全部静态规则之后 = 「静态规则不过不得 mkdir」；探针失败时
        # config / set_root / config.json 都还没动（先校验后提交）。
        if not skip_probe:
            try:
                rp.mkdir(parents=True, exist_ok=True)
                probe_file = rp / f".asase_probe_{uuid.uuid4().hex}.tmp"
                try:
                    probe_file.write_bytes(b"probe")
                finally:
                    probe_file.unlink(missing_ok=True)
            except Exception as e:
                return fail(f"目录不可写或无法创建：{e}")

        # 规则 11：可用空间（A 档）——<100MB 阻断；<2GB 黄字不阻断
        free_bytes, space_warnings = self._check_free_space(rp)
        warnings.extend(space_warnings)
        if free_bytes is not None and free_bytes < 100 * 1024 * 1024:
            return fail(
                f"磁盘可用空间不足 100 MB（剩余 {free_bytes // (1024 * 1024)} MB）",
                free_bytes,
            )

        return {"ok": True, "error": None, "warnings": warnings, "free_bytes": free_bytes}

    def preview_dir(self, path: str) -> dict:
        """前端确认前的目录预检：跑规则 1~8、10、11（规则 10 由调用方
        update_config 场景自行处理；此处单路径），**跳过规则 9 探针**——
        不落盘、不提交、不 mkdir。返回 {ok, error, warnings, free_bytes}，
        warnings 结构化（code: low_space / install_dir），前端按 code 渲染。
        """
        return self._validate_path(path, skip_probe=True)

    def _validate_dirs(self, shelf_raw: str, dl_raw: str) -> str | None:
        """两个目录的包含关系校验（规则 10），返回错误文案（None 表示没问题）。

        统一口径：两边都先 resolve() 再用 relative_to 判包含，
        避免盘符大小写与 Windows 长路径前缀的假阴性（GLM C5）。
        """
        def expand(v: str) -> Path | None:
            v = str(v or "").strip()
            if not v:
                return None
            p = Path(os.path.expanduser(v))
            return p.resolve() if p.is_absolute() else (PROJECT_ROOT / p).resolve()

        shelf = expand(shelf_raw) or default_shelf_dir().resolve()
        dl = expand(dl_raw)
        if dl is None:
            return None
        if shelf == dl:
            return None  # 相同即等价单目录模式（现状语义，方案 §4.3「沿用」）
        try:
            if dl.relative_to(shelf):
                return "下载暂存目录不能放在书架目录里面（否则下载会被当成已入库章节）"
        except ValueError:
            pass
        try:
            if shelf.relative_to(dl):
                return "书架目录不能放在下载暂存目录里面（否则会被自己扫进去）"
        except ValueError:
            pass
        return None

    # ---------- 登录凭据（仅充电专属内容需要） ----------

    def _paid_up_ids(self) -> set:
        """已知有付费内容的 UP 主 ID 集合。只存数字 ID，不含任何凭据。"""
        ids: set = set()
        for u in self.config.get("paid_ups") or []:
            try:
                ids.add(int(u.get("mid")))
            except (TypeError, ValueError):
                continue
        return ids

    def _remember_paid_up(self, mid, name: str = "") -> None:
        """把 UP 记进「需要登录态」列表。只记 ID 与名字，不记凭据。"""
        if mid is None:
            return
        with self.lock:
            ups = [dict(u) for u in (self.config.get("paid_ups") or [])]
            for u in ups:
                if str(u.get("mid")) == str(mid):
                    if name and not u.get("name"):
                        u["name"] = name
                        self.config["paid_ups"] = ups
                        self._save_config()
                    return
            ups.append({"mid": int(mid), "name": name or ""})
            self.config["paid_ups"] = ups
            self._save_config()
        self._notify_state()

    def cookie_state(self) -> dict:
        """给前端的凭据状态（只有统计信息）。"""
        return self.credentials.summary()

    def submit_cookie(self, cookie: str) -> dict:
        """把登录凭据存进内存，并把因缺凭据而失败的任务重新排队。"""
        ok, msg = self.credentials.set(cookie)
        if not ok:
            return {"ok": False, "error": msg, "cookie": self.credentials.summary()}
        self.log("登录凭据已保存（仅存内存，程序退出即失效）")
        retried = 0
        with self.lock:
            for j in self.jobs:
                if j.need_cookie and j.status == "failed":
                    j.status = "queued"
                    j.error = ""
                    j.need_cookie = False
                    retried += 1
        if retried:
            self._clear_cancel_if_idle()
            self.log(f"已重新排队 {retried} 个等待登录态的任务")
        self._notify_state()
        return {
            "ok": True,
            "error": "",
            "retried": retried,
            "cookie": self.credentials.summary(),
        }

    def clear_cookie(self) -> dict:
        """从内存中抹掉登录凭据。"""
        self.credentials.clear()
        self.log("登录凭据已清除")
        self._notify_state()
        return {"ok": True, "cookie": self.credentials.summary()}

    def add_paid_up(self, mid: str, name: str = "") -> dict:
        """手动登记一个需要登录态的 UP 主。"""
        try:
            mid_int = int(str(mid).strip())
        except (TypeError, ValueError):
            return {"ok": False, "error": "UP 主 ID 必须是纯数字"}
        self._remember_paid_up(mid_int, str(name or "").strip())
        return {"ok": True, "paid_ups": self.config.get("paid_ups") or []}

    def remove_paid_up(self, mid: str) -> dict:
        """取消某个 UP 主的登录态标记。"""
        try:
            mid_int = int(str(mid).strip())
        except (TypeError, ValueError):
            return {"ok": False, "error": "UP 主 ID 必须是纯数字"}
        with self.lock:
            ups = [
                u for u in (self.config.get("paid_ups") or [])
                if str(u.get("mid")) != str(mid_int)
            ]
            self.config["paid_ups"] = ups
            self._save_config()
        self._notify_state()
        return {"ok": True, "paid_ups": self.config.get("paid_ups") or []}

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run_loop, name="asase-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        if self._loop:
            self._loop.call_soon_threadsafe(self.control.cancel)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._worker())
        except Exception:
            pass
        finally:
            loop.close()

    # ---------- 前端通信 ----------

    def _js(self, expr: str) -> None:
        w = self.window
        if w is None:
            return
        try:
            w.evaluate_js(expr)
        except Exception:
            pass  # 窗口可能尚未就绪或已关闭

    def _notify_state(self) -> None:
        payload = json.dumps(self.get_state(), ensure_ascii=False)
        self._js(f"window.__onState && window.__onState({payload})")

    def log(self, line: str) -> None:
        with self.lock:
            self.logs.append((time.time(), line))
            if len(self.logs) > 500:
                self.logs = self.logs[-500:]
        text = json.dumps(line, ensure_ascii=False)
        self._js(f"window.__onLog && window.__onLog({text})")

    # ---------- 状态 ----------

    def get_state(self) -> dict:
        # 锁内一次性取齐三件快照（config / shelf_disp / dl_disp）：
        # 只快照 config 一件不够——shelf_dir_display / download_dir_display 内部
        # 也读 self.config，三个值来自不同时刻照样撕裂（并发 update_config 时
        # 会互相矛盾）。return 全用快照（方案 §2 第 5 条 / Claude B-3 + P-1）。
        with self.lock:
            jobs = [j.to_dict() for j in self.jobs]
            running = any(j.status == "running" for j in self.jobs)
            pending = sum(1 for j in self.jobs if j.status == "queued")
            cfg_snap = dict(self.config)
            shelf_disp = self.shelf_dir_display()
            dl_disp = self.download_dir_display()
        today_file = self.state_dir / f"pages_{date.today():%Y-%m-%d}.json"
        used = 0
        try:
            if today_file.exists():
                used = int(json.loads(today_file.read_text(encoding="utf-8")).get("count", 0))
        except Exception:
            pass
        return {
            "jobs": jobs,
            "config": cfg_snap,
            "shelf_dir": shelf_disp,
            "download_dir": dl_disp,
            # 首启模态预填用的默认书库（「文档」下的 AsaseReaderLibrary）；
            # default_shelf_dir() 有模块级缓存，poll 每秒一次也不怕
            "default_shelf_dir": str(default_shelf_dir()),
            "daily_used": used,
            "running": running,
            "paused": self.control.paused,
            "pending": pending,
            # 只有"有没有 / 几个字段"这类统计，绝不含凭据原文
            "cookie": self.credentials.summary(),
            # 界面设置的持久化副本：启动时前端把它写回 localStorage，
            # 这样即使 WebView2 换了配置目录 / 端口变了，偏好也不会丢
            "ui_settings": self.get_ui_settings(),
        }

    # ---------- 界面设置（localStorage 的兜底副本） ----------
    # 只存显示偏好（主题 / 明暗 / 卡片大小 / 阅读模式…），与 config.json 无关；
    # 存的是字符串，跟 localStorage 一个口径。坏了就当没存过，绝不让程序起不来。

    def get_ui_settings(self) -> dict:
        p = self.state_dir / "ui_settings.json"
        try:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass      # 文件坏了 / 不是 json：宁可全部回默认值，也不能启动失败
        return {}

    def save_ui_settings(self, settings: dict) -> dict:
        try:
            current = self.get_ui_settings()
            current.update({k: str(v) for k, v in dict(settings or {}).items()})
            self.state_dir.mkdir(parents=True, exist_ok=True)
            p = self.state_dir / "ui_settings.json"
            p.write_text(json.dumps(current, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- 供 JS 调用的命令（可能在任意线程） ----------

    def add_urls(self, text: str) -> list[str]:
        """从一段文本中提取所有 opus ID，加入队列。返回新增列表。"""
        added = []
        with self.lock:
            known = {j.opus_id for j in self.jobs}
            for line in (text or "").replace(",", "\n").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    oid = extract_opus_id(line)
                except BiliOpusError:
                    continue
                if oid not in known:
                    known.add(oid)
                    self.jobs.append(Job(opus_id=oid))
                    added.append(oid)
        if added:
            # 新任务入队 = 新的一轮开始，取消令牌该复位（见 _clear_cancel_if_idle）
            self._clear_cancel_if_idle()
            self.log(f"已添加 {len(added)} 篇到队列")
        self._notify_state()
        return added

    def remove_job(self, opus_id: str) -> None:
        with self.lock:
            job = next((j for j in self.jobs if j.opus_id == opus_id), None)
            if job and job.status != "running":
                self.jobs.remove(job)
        self._notify_state()

    def clear_finished(self) -> None:
        with self.lock:
            self.jobs = [j for j in self.jobs if j.status in ("queued", "running")]
        self._notify_state()

    def retry_job(self, opus_id: str) -> None:
        with self.lock:
            job = next((j for j in self.jobs if j.opus_id == opus_id), None)
            if job and job.status in ("failed", "cancelled", "skipped"):
                job.status = "queued"
                job.error = ""
                job.done = 0
                job.total = 0
                # 用户明确要重跑这一个 = 新一轮开始，取消令牌该复位，
                # 否则它会在第一个 checkpoint 又被判成已取消
                self._clear_cancel_if_idle()
        self._notify_state()

    def pause(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self.control.pause)
        self.log("已暂停（完成当前这张图后停下）")
        self._notify_state()

    def resume(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self.control.resume)
        self._notify_state()

    def cancel_all(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self.control.cancel)
        with self.lock:
            for j in self.jobs:
                if j.status == "queued":
                    j.status = "cancelled"
        self._notify_state()

    def update_config(self, new_cfg: dict) -> dict:
        """先校验后提交（方案 §4.3）。

        作用域（Kimi A3）：仅当 PATH_KEYS（或首启确认，确认必带 shelf_dir）出现在
        new_cfg 时才做目录级校验与磁盘动作；不带目录 key 的保存（下载参数 / 视频）
        **不得触碰书库/暂存盘**——否则改并发数也 mkdir + 探针，书库盘掉线时连
        延迟都保存不了。

        失败四断言：内存 config 不变、set_root 未调、config.json 未写、
        返回体与 _notify_state 推送不含新值（失败最干净是不 notify）。
        """
        new_cfg = dict(new_cfg or {})
        # 保存入口是第一道闸：GUI 的 max 可以被前端绕过，旁路调用 API 更是没有上限，
        # 所以在写进 config 之前就夹平，界面显示的值 = 真正生效的值。
        if "concurrency" in new_cfg:
            new_cfg["concurrency"] = clamp_concurrency(new_cfg["concurrency"])

        with self.lock:
            # 失败快照：锁内即时固定「当前」状态，失败返回体不带任何新值
            fail_snap = {
                "config": dict(self.config),
                "shelf_dir": self.shelf_dir_display(),
                "download_dir": self.download_dir_display(),
            }

            def result(ok: bool, error: str = "", snap: dict | None = None) -> dict:
                s = snap or fail_snap
                return {
                    "ok": ok,
                    "error": error,
                    "config": s["config"],
                    "shelf_dir": s["shelf_dir"],
                    "download_dir": s["download_dir"],
                }

            running = any(j.status == "running" for j in self.jobs)
            # 跑一半换目录会让文件落在一个库、记录写进另一个库，直接拦下
            for key in PATH_KEYS:
                if key in new_cfg:
                    val = str(new_cfg[key] or "").strip()
                    if val != str(self.config.get(key) or "").strip() and running:
                        return result(False, "有任务正在下载，请先暂停或等待完成后再改目录")

            # ---- 目录级校验（只有带 PATH_KEYS 的保存才做；首启确认必带 shelf_dir）----
            has_path_keys = any(k in new_cfg for k in PATH_KEYS)
            if has_path_keys:
                cand_shelf = (
                    str(new_cfg.get("shelf_dir") or "").strip()
                    if "shelf_dir" in new_cfg
                    else str(self.config.get("shelf_dir") or "").strip()
                )
                cand_dl = (
                    str(new_cfg.get("download_dir") or "").strip()
                    if "download_dir" in new_cfg
                    else str(self.config.get("download_dir") or "").strip()
                )
                # 纯校验：用候选路径，不碰 self.config、不碰 set_root。
                # 探针（规则 9）在 _validate_path 内部、全部静态规则之后才 mkdir，
                # 失败时本方法什么都没动。
                if "shelf_dir" in new_cfg and cand_shelf:
                    vr = self._validate_path(cand_shelf)
                    if not vr["ok"]:
                        return result(False, vr["error"] or "书架目录校验失败")
                if "download_dir" in new_cfg and cand_dl:
                    vr = self._validate_path(cand_dl)
                    if not vr["ok"]:
                        return result(False, vr["error"] or "下载暂存目录校验失败")
                err = self._validate_dirs(cand_shelf, cand_dl)
                if err:
                    return result(False, err)

            # ---- 校验全过，一次性提交（锁内改内存，锁外持久化）----
            before_shelf = self.shelf_dir_display()
            for key in DEFAULT_CONFIG:
                if key not in new_cfg:
                    continue
                val = new_cfg[key]
                if key in PATH_KEYS:
                    self.config[key] = str(val or "").strip()
                elif key in ("include_cover", "setup_done"):
                    # setup_done 的 bool() 转换钉死在这里（与 include_cover 同款），
                    # 锁外预处理救不了它（方案 §4.1 / Claude B-5）
                    self.config[key] = bool(val)
                elif key == "import_mode":
                    self.config[key] = "copy" if str(val) == "copy" else "move"
                elif key in ("export_dir", "ffmpeg_path"):
                    self.config[key] = str(val or "").strip()
                elif key == "video_threshold":
                    # 和 concurrency 一样：保存前先夹平，界面显示的值 = 真正生效的值
                    self.config[key] = clamp_threshold(val)
                else:
                    try:
                        self.config[key] = float(val) if "delay" in key else int(val)
                    except (TypeError, ValueError):
                        pass

            shelf = self.shelf_root()
            staging = self.download_root()
            changed_shelf = self.shelf_dir_display() != before_shelf
            confirmed = bool(self.config.get("setup_done", False))

            # 确认成功后作废 library 缓存（M12 与 property 的 readonly 检查双保险）：
            # 首启确认前若已有 readonly 实例被缓存（例如其它 API 先访问 library_state），
            # 幂等确认（shelf 不变）下 key 不变，不作废就复用旧实例——不只
            # migrate/_backup 迟到，save() 直接 return，当次会话写库全部静默失效。
            if has_path_keys and confirmed:
                self._library_obj = None
                self._library_key = None
                # mkdir 只发生在「带目录 key 且已确认」的保存（Kimi A3 作用域）；
                # 目录本身多半已被探针建出（exist_ok 幂等），此处兜个别处
                for p, label in ((shelf, "书架目录"), (staging, "下载暂存目录")):
                    if p is None:
                        continue
                    try:
                        p.mkdir(parents=True, exist_ok=True)
                    except Exception as e:
                        self.log(f"无法创建{label} {p}: {e}")

            if self.static_server is not None:
                self.static_server.set_root(shelf)
            if changed_shelf:
                self.log(f"书架目录已切换为 {shelf}")
                if not (shelf / "library.json").exists():
                    self.log("提示：这个目录下还没有 library.json，会从空书架开始（不影响原目录）")

            # 成功快照：提交段锁内组装好返回体，锁外只 return（P-2）
            ok_snap = {
                "config": dict(self.config),
                "shelf_dir": self.shelf_dir_display(),
                "download_dir": self.download_dir_display(),
            }

        self._save_config()
        self._notify_state()
        return result(True, snap=ok_snap)

    def read_url_file(self, path: str) -> str:
        try:
            p = Path(path)
            if p.stat().st_size > 2 * 1024 * 1024:
                self.log("文件太大（>2MB），不像是链接列表")
                return ""
            for enc in ("utf-8", "utf-8-sig", "gbk"):
                try:
                    return p.read_text(encoding=enc)
                except UnicodeDecodeError:
                    continue
            return p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            self.log(f"读取文件失败: {e}")
            return ""

    def open_dir(self, path: str) -> None:
        import os

        p = Path(path)
        if p.exists():
            os.startfile(str(p))  # noqa: S606 Windows only
        else:
            self.log(f"目录不存在: {p}")

    # ---------- 下载工作循环（在事件循环线程） ----------

    def _clear_cancel_if_idle(self) -> None:
        """这一批取消已消费完就复位令牌。

        取消时：正在跑的那个 job 会在下一个 checkpoint 抛 OperationCancelled，
        排队的则已被 cancel_all 直接标成 cancelled。所以走到这里（没有 job 在跑）
        就说明这一轮取消已经作用完毕，可以让令牌复位了。

        不复位的话，令牌会一直停在已取消状态，之后重试或新增的每一个任务
        都会在第一个 checkpoint 被立刻取消 —— 也就是"取消一次后怎么下都自动取消"。
        """
        with self.lock:
            if not self.control.cancelled:
                return
            if any(j.status == "running" for j in self.jobs):
                return
        self.control.reset()

    async def _worker(self) -> None:
        while not self._stop:
            job = None
            with self.lock:
                for j in self.jobs:
                    if j.status == "queued":
                        job = j
                        j.status = "running"
                        break
            if job is None:
                self._clear_cancel_if_idle()
                await asyncio.sleep(0.3)
                continue
            self._notify_state()
            try:
                await self._run_job(job)
            except Exception as e:  # 兜底：不让 worker 崩掉
                job.status = "failed"
                job.error = str(e)
            finally:
                self._notify_state()

    async def _run_job(self, job: Job) -> None:
        cfg = self.config
        page_limiter = RateLimiter(
            min_interval=float(cfg["delay_min"]),
            max_interval=float(cfg["delay_max"]),
            daily_limit=int(cfg["max_per_day"]),
            state_path=self.state_dir / f"pages_{date.today():%Y-%m-%d}.json",
        )
        image_limiter = RateLimiter(
            min_interval=float(cfg["image_delay_min"]),
            max_interval=float(cfg["image_delay_max"]),
            daily_limit=None,
        )
        include_cover = bool(cfg["include_cover"])
        url = opus_url(job.opus_id)
        job.stage = "downloading"
        job.need_cookie = False

        # 上一个任务来自已知有付费内容的 UP → 这次直接带登录态，
        # 省掉"先无凭据试探一次"那趟请求（追更连载时队列里通常都是同一 UP）。
        with self.lock:
            use_cookie = (
                self.credentials.configured
                and self._last_author_mid in self._paid_up_ids()
            )
        cookie = self.credentials.cookie if use_cookie else ""

        try:
            await self.control.checkpoint()
            async with Fetcher(
                # 最后一道闸：即使有人绕过了前两道，真正交给 Fetcher 的值也不会越线
                page_limiter, image_limiter, concurrency=clamp_concurrency(cfg["concurrency"])
            ) as fetcher:
                self.log(f"[{job.opus_id}] 抓取页面…" + ("（带登录态）" if cookie else ""))
                html = await fetcher.fetch_page(url, cookie=cookie)
                await self.control.checkpoint()
                opus = parse_opus_html(html, source_url=url)
                job.author_mid = opus.author_mid
                self._last_author_mid = opus.author_mid

                # 充电专属内容：当前身份只拿到预览版（完整内容的前缀）
                used_creds = bool(cookie)
                if opus.paywalled and not used_creds and self.credentials.configured:
                    self.log(f"[{job.opus_id}] 充电专属内容，改用登录态重试…")
                    await self.control.checkpoint()
                    html = await fetcher.fetch_page(url, cookie=self.credentials.cookie)
                    await self.control.checkpoint()
                    opus = parse_opus_html(html, source_url=url)
                    job.author_mid = opus.author_mid
                    used_creds = True

                if opus.paywalled:
                    # 预览版一律不下载、不入库 —— 否则会静默存进一章残缺内容
                    job.title = opus.title
                    job.total = len(opus.images)
                    self._notify_state()
                    shown = len(opus.images)
                    est = opus.estimated_total()
                    tail = f"（完整约 {est} 张）" if est else ""
                    if used_creds:
                        job.error = (
                            f"已带登录态却仍是预览版：只拿到 {shown} 张{tail}。"
                            "可能 Cookie 已失效，或你不是该 UP 主的充电成员"
                        )
                    else:
                        job.need_cookie = True
                        job.error = (
                            f"这是充电专属内容：只拿到预览 {shown} 张{tail}，"
                            "需要登录态才能下全"
                        )
                    job.status = "failed"
                    self.log(f"[{opus.title}] {job.error}")
                    return

                if used_creds:
                    self._remember_paid_up(opus.author_mid, opus.author_name)

                job.title = opus.title
                job.total = len(opus.images)
                self._notify_state()

                if not opus.images and not opus.cover:
                    job.status = "failed"
                    job.error = "该专栏没有图片"
                    self.log(f"[{job.opus_id}] 没有图片")
                    return

                # setup_done 闸门：未完成首启引导，下载入队一律拒绝
                # （方案 §3：下载 _landing_dir 属于写入点；此刻书架多半还是
                # default_shelf_dir 回退值，写下去 = 往「文档」默认库落盘）
                if not bool(self.config.get("setup_done", False)):
                    job.status = "failed"
                    job.error = "尚未完成首次启动引导，请先确认书库目录"
                    self.log(f"[{job.opus_id}] 未完成首启引导，拒绝下载")
                    return

                # 空间 B 档（§4.5）：每章节开始前查一次，<50MB 直接标 failed、
                # 不再写入；不新造「暂停」状态。双目录模式暂存盘与书架盘都查
                # （跨盘 move 峰值约需 2 倍空间）。disk_usage 传目标目录本身。
                chk_targets = [self.shelf_root()]
                staging_chk = self.download_root()
                if staging_chk is not None:
                    chk_targets.append(staging_chk)
                for chk_p in chk_targets:
                    try:
                        tgt = chk_p if chk_p.exists() else self._existing_ancestor(chk_p)
                        free = shutil.disk_usage(tgt).free
                        if free < 50 * 1024 * 1024:
                            job.status = "failed"
                            job.error = (
                                f"磁盘可用空间不足 50 MB"
                                f"（剩余 {free // (1024 * 1024)} MB），本章节中止"
                            )
                            self.log(f"[{job.opus_id}] {job.error}")
                            return
                    except Exception as e:
                        # 查不了就放行走老流程，别因为检查本身挂了而误伤下载
                        self.log(f"检查磁盘空间异常（放行）: {e}")

                # 已经在书架上就别再下一遍：单目录模式下文件本就落在 chapters/，
                # 双目录模式下导入后暂存目录会被清空，光看目录内容判断会误判成"没下过"
                if self.library.has_opus(opus.opus_id):
                    job.status = "skipped"
                    job.done = job.total
                    job.stage = "imported"
                    job.dir = str(
                        chapter_dir(self.shelf_root(), opus_chapter_id(opus.opus_id))
                    )
                    self.log(f"[{opus.title}] 已在书架中，跳过")
                    return

                landing = self._landing_dir(opus.opus_id, opus)
                if opus_is_complete(landing, opus, include_cover):
                    # 图已下全但还没登记（比如写完 meta 后崩了）：补登记即可
                    job.status = "skipped"
                    job.done = job.total
                    job.dir = str(self._register(job, landing))
                    self.log(f"[{opus.title}] 已存在且完整，直接入库")
                    return

                def on_event(e: dict) -> None:
                    if e["type"] == "image":
                        job.done = max(job.done, e["index"])
                        if not e["ok"]:
                            self.log(f"[{job.title}] 第 {e['index']} 张失败: {e.get('error')}")
                    self._notify_state()

                dir_path, _stats = await download_opus(
                    fetcher,
                    opus,
                    landing,
                    include_cover=include_cover,
                    overwrite=False,
                    log=lambda m: None,
                    control=self.control,
                    on_event=on_event,
                )
                job.done = job.total
                # 单目录模式：文件已在 chapters/<id>/，登记一下；
                # 双目录模式：搬到书架再登记
                job.dir = str(self._register(job, dir_path))
                job.status = "done"
                self.log(f"[{opus.title}] 完成 → {job.dir}")
        except OperationCancelled:
            job.status = "cancelled"
            job.error = "已取消"
            self.log(f"[{job.opus_id}] 已取消")
        except DailyLimitReached as e:
            job.status = "failed"
            job.error = str(e)
            self.log(f"[{job.opus_id}] {e}")
            # 达到每日上限：把剩余任务标记取消，避免空转重试
            with self.lock:
                for j in self.jobs:
                    if j.status == "queued":
                        j.status = "cancelled"
                        j.error = "每日上限，明天再跑"
        except RiskControlError as e:
            job.status = "failed"
            job.error = str(e)
            self.log(f"[{job.opus_id}] 触发风控，停止: {e}")
            with self.lock:
                for j in self.jobs:
                    if j.status == "queued":
                        j.status = "cancelled"
                        j.error = "风控保护，已停止"
        except BiliOpusError as e:
            job.status = "failed"
            job.error = str(e)
            self.log(f"[{job.opus_id}] 失败: {e}")


def __getattr__(name: str):
    """PEP 562：DATA_DIR 不设模块级全局变量，动态导出（NOTE-009）。

    若顶层写 `DATA_DIR = get_data_dir()`，模块 __dict__ 已有该全局，外部
    `backend.DATA_DIR` 永远命中静态值——monkeypatch sys.frozen（frozen 双锚点
    测试）就不生效；而模块内部函数若用裸名 DATA_DIR，globals() 查不到又抛
    NameError。定稿口径：**模块内部一律调 get_data_dir()**，只有外部访问
    `backend.DATA_DIR` 走这里的动态求值。
    """
    if name == "DATA_DIR":
        return get_data_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
