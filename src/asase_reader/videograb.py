"""视频转图集：调用**用户自己的 ffmpeg**，按场景变化把视频抽成一组图片。

设计约定（改动前请先读 ROADMAP 的「视频转图集」一节，默认值都是实测定下来的）：

- **ffmpeg 不打包进 exe**：先 `shutil.which("ffmpeg")` 找 PATH，找不到才用配置里的
  绝对路径。省掉 +80MB 体积，也绕开 GPL 分发问题。
- **抽帧用场景变化检测，不是固定间隔**：这类视频每张图停几秒，固定间隔会抽出一堆
  一模一样的图；而真正该被抽到的画面，变化幅度可能很小（实测有 score 只有 0.1 的真切换）。
- **默认阈值 0.02**：13 个样本实测（详见 CHANGELOG 1.15.2）。去重后的唯一张数从
  0.025 起就见顶了（0.025=910 / 0.02=911 / 0.015=911 / 0.01=913），再往下只买到
  「整 5 秒切片边界上的噪声尖峰」；而 0.05 会漏掉真切换。取 0.02 保证召回最大。
- **不做去重**：阈值噪声重复约 0.8%，片源自带的重复（作者每章末尾的赘余组、黑色转场图）
  约 4%，两者都由用户自己在阅读器里删 —— 见 ROADMAP 的「章节内删除内容」。
- 本模块只负责「把图片抽出来」，**入库交给 `Library.import_local_images`** ——
  meta.json、封面、未入库语义那一套全部复用，不另写一遍。

ffmpeg 版本坑：**9.0 已经删掉了 `-vsync`**，必须写 `-fps_mode vfr`
（写 `-vsync` 会直接报 `Unrecognized option`，一张都抽不出来）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# 场景变化阈值：低于这个值就算同一张图。实测定 0.02，改这里要同步 README/设置页说明。
DEFAULT_THRESHOLD = 0.02
THRESHOLD_MIN = 0.01
THRESHOLD_MAX = 0.5

# mjpeg 的 q:v，2 是视觉无损档（范围 2~31，越小越好）。实测 1080p 每张约 0.26MB。
IMAGE_QUALITY = "2"

# Chromium（WebView2）能播的，也就是 ffmpeg 抽出来有意义的。mkv 不在内：
# Chromium 不 demux Matroska，抽出来也放不了（用户自己转格式，不做系统播放器兜底）。
VIDEO_EXTS = (".mp4", ".m4v", ".webm", ".mov")

_SCORE_MARK = "lavfi.scene_score"


class VideoGrabError(Exception):
    """ffmpeg 不在、抽帧失败等可预期的错误（前端直接显示 message）。"""


class VideoGrabCancelled(Exception):
    """用户在抽帧途中点了取消。"""


def clamp_threshold(value) -> float:
    """把阈值夹进 [0.01, 0.5]；脏输入（空/非数字/NaN）退回默认值。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD
    if n != n:  # NaN
        return DEFAULT_THRESHOLD
    return min(THRESHOLD_MAX, max(THRESHOLD_MIN, n))


def resolve_ffmpeg(configured: str | None = None) -> str | None:
    """优先 PATH，其次配置里的绝对路径；都没有返回 None（调用方负责报错文案）。"""
    found = shutil.which("ffmpeg")
    if found:
        return found
    raw = str(configured or "").strip()
    if not raw:
        return None
    try:
        cand = Path(raw)
        return str(cand) if cand.is_file() else None
    except (OSError, ValueError):
        return None


def is_video(path) -> bool:
    return str(path).lower().endswith(VIDEO_EXTS)


def spawn_kwargs() -> dict:
    """Windows 下启动 ffmpeg 的附加参数：**不让控制台窗口冒出来**。

    ffmpeg 是控制台程序，直接 Popen 会闪一个黑框（批量转几十个就闪几十次，很难看）。
    `CREATE_NO_WINDOW` 才是真正不创建控制台的那个开关；`STARTF_USESHOWWINDOW`
    是老写法，单独用不一定压得住，两个一起给最稳。非 Windows 不需要任何东西。
    """
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {"startupinfo": si, "creationflags": subprocess.CREATE_NO_WINDOW}


def video_filter_label() -> str:
    """设置页 / 报错里用到的可读后缀列表。"""
    return " / ".join(e.lstrip(".") for e in VIDEO_EXTS)


# ffmpeg 的 filtergraph 用逗号分隔滤镜，所以**表达式内部的逗号必须转义成 `\,`** ——
# 这是给 ffmpeg 看的，不是给 shell 看的（全程列表传参，不经 shell）。
def cut_expr(threshold: float) -> str:
    """只选「发生切换」的帧（预估用，首帧后面 +1 单独算）。"""
    return rf"gt(scene\,{threshold})"


def select_expr(threshold: float) -> str:
    """首帧必留 + 之后的每次切换（真正抽帧用）。"""
    return rf"eq(n\,0)+gt(scene\,{threshold})"


def build_estimate_cmd(ffmpeg: str, src: str, threshold: float) -> list[str]:
    """不落盘跑一遍场景检测，把每次切换打到 stdout（数行数就知道会抽几张）。"""
    return [
        ffmpeg, "-hide_banner", "-nostats", "-i", src,
        "-vf", f"select={cut_expr(threshold)},metadata=print:file=-",
        "-f", "null", "-",
    ]


def build_extract_cmd(ffmpeg: str, src: str, threshold: float, out_pattern: str) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-nostats", "-y", "-i", src,
        "-vf", f"select={select_expr(threshold)}",
        "-fps_mode", "vfr",  # **不是 -vsync**：ffmpeg 9 已删除该选项
        "-q:v", IMAGE_QUALITY,
        out_pattern,
    ]


def build_thumb_cmd(ffmpeg: str, src: str, out_path: str) -> list[str]:
    """取视频第 1 帧当视频章节的封面。

    只取一帧，不解码整片（`-frames:v 1` 在输入之后、输出之前生效），几十毫秒就完事。
    """
    return [
        ffmpeg, "-hide_banner", "-nostats", "-y", "-i", src,
        "-frames:v", "1",
        "-q:v", IMAGE_QUALITY,
        out_path,
    ]


def extract_thumb(ffmpeg: str, src: str, out_path: Path) -> bool:
    """截首帧写到 out_path；失败抛 VideoGrabError（调用方决定降级成占位封面）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(
        build_thumb_cmd(ffmpeg, src, str(out_path)),
        capture_output=True, text=True, errors="replace", **spawn_kwargs(),
    )
    if p.returncode != 0 or not out_path.is_file():
        raise VideoGrabError(f"ffmpeg 截帧失败：{(p.stderr or '')[-200:].strip()}")
    return True


def estimate_frames(ffmpeg: str, src: str, threshold: float) -> int:
    """预估会抽出几张（含首帧）。不写盘，4 分钟的视频约 2 秒。"""
    p = subprocess.run(
        build_estimate_cmd(ffmpeg, src, threshold),
        capture_output=True, text=True, errors="replace", **spawn_kwargs(),
    )
    if p.returncode != 0:
        raise VideoGrabError(f"无法读取视频：{(p.stderr or '')[-200:].strip()}")
    return p.stdout.count(_SCORE_MARK) + 1  # +1 是永远保留的首帧


def extract_frames(ffmpeg: str, src: str, threshold: float, outdir: Path,
                   should_cancel=None) -> int:
    """按阈值把视频抽成 outdir/0001.jpg… 返回张数。should_cancel 为真时杀掉子进程。"""
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = build_extract_cmd(ffmpeg, src, threshold, str(outdir / "%04d.jpg"))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, errors="replace", **spawn_kwargs(),
    )
    cancelled = False
    while True:
        if should_cancel is not None and should_cancel():
            proc.kill()
            cancelled = True
            break
        try:
            proc.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            continue
    # 杀掉之后 stderr 里可能还有半截输出，读干净再收尾，避免留下僵尸/堵住的管道
    try:
        err = (proc.stderr.read() if proc.stderr else "") or ""
    except Exception:
        err = ""
    try:
        if proc.stderr:
            proc.stderr.close()
    except Exception:
        pass
    proc.wait(timeout=10)
    if cancelled:
        raise VideoGrabCancelled()
    if proc.returncode != 0:
        raise VideoGrabError(f"ffmpeg 抽帧失败：{err[-300:].strip()}")
    return len(list(outdir.glob("*.jpg")))
