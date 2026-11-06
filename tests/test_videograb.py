"""视频转图集（videograb）：参数拼装 + 有 ffmpeg 时真跑一次抽帧。

分两层，因为这两层能抓的 bug 完全不同：
- **纯函数层**（不需要 ffmpeg）：阈值夹取、ffmpeg 查找优先级、命令拼装。
  尤其是**命令里必须是 `-fps_mode` 而不是 `-vsync`** —— ffmpeg 9 已经删掉 `-vsync`，
  写错了会直接报 Unrecognized option、一张都抽不出来，而这条**只有断言能守**。
- **真实抽帧层**：本机有 ffmpeg 时用 lavfi 合成一段两色视频（红 1 秒 + 蓝 1 秒，
  相当于两张图），跑一遍预估与抽帧，确认真的抽出了 2 张。
  取消也真跑一次（should_cancel 立刻为真 → 必须抛 VideoGrabCancelled）。

没有 ffmpeg 时**明确打印 SKIP**（不是 PASS）—— 这个特性在那台机器上根本没法用，
不能假装绿。
"""

import inspect
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from asase_reader import videograb  # noqa: E402

fails = []


def ok(cond, msg):
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        fails.append(msg)


def eq(got, want, msg):
    ok(got == want, f"{msg}（实际 {got!r} / 期望 {want!r}）")


print("--- 阈值夹取 ---")
eq(videograb.clamp_threshold(None), videograb.DEFAULT_THRESHOLD, "None 退回默认")
eq(videograb.clamp_threshold(""), videograb.DEFAULT_THRESHOLD, "空串退回默认")
eq(videograb.clamp_threshold("abc"), videograb.DEFAULT_THRESHOLD, "非数字退回默认")
eq(videograb.clamp_threshold(float("nan")), videograb.DEFAULT_THRESHOLD, "NaN 退回默认")
eq(videograb.clamp_threshold(0.05), 0.05, "区间内的原样保留")
eq(videograb.clamp_threshold(0), videograb.THRESHOLD_MIN, "0 夹到下界")
eq(videograb.clamp_threshold(99), videograb.THRESHOLD_MAX, "99 夹到上界")
eq(videograb.clamp_threshold(-1), videograb.THRESHOLD_MIN, "负数夹到下界")
eq(videograb.clamp_threshold("0.05"), 0.05, "字符串数字也能用（配置里存的可能是字符串）")

print("--- ffmpeg 查找：PATH 优先，其次配置 ---")
_which = shutil.which
try:
    shutil.which = lambda _n: "/usr/bin/ffmpeg"
    eq(videograb.resolve_ffmpeg("/some/other/ffmpeg"), "/usr/bin/ffmpeg",
       "PATH 里有就用 PATH 的（配置被忽略）")
    shutil.which = lambda _n: None
    eq(videograb.resolve_ffmpeg(""), None, "PATH 没有且没配置 → None")
    eq(videograb.resolve_ffmpeg(None), None, "配置是 None → None")
    eq(videograb.resolve_ffmpeg("/definitely/not/here/ffmpeg"), None,
       "配置里的路径不存在 → None")
    probe = Path(tempfile.mkdtemp(prefix="asase_ffprobe_")) / "ffmpeg.exe"
    probe.write_text("x")
    eq(videograb.resolve_ffmpeg(str(probe)), str(probe),
       "PATH 没有时，配置指向的真实文件顶上")
    shutil.rmtree(probe.parent, ignore_errors=True)
finally:
    shutil.which = _which

print("--- Windows 下不许弹黑框 ---")
kw = videograb.spawn_kwargs()
if os.name == "nt":
    ok(kw.get("creationflags") == subprocess.CREATE_NO_WINDOW,
       "带 CREATE_NO_WINDOW（ffmpeg 是控制台程序，不给它就会闪黑框）")
    si = kw.get("startupinfo")
    ok(si is not None and bool(si.dwFlags & subprocess.STARTF_USESHOWWINDOW),
       "同时给 STARTF_USESHOWWINDOW（两个一起最稳）")
    # 抽帧和预估两条路都必须带上，少一条就有一半时候会闪
    src_text = inspect.getsource(videograb)
    ok(src_text.count("spawn_kwargs()") >= 3,
       "预估和抽帧两处都用上了（定义 1 处 + 调用至少 2 处）")
else:
    ok(kw == {}, "非 Windows 不加任何附加参数")

print("--- 视频后缀识别 ---")
ok(videograb.is_video("a.mp4"), "mp4 算视频")
ok(videograb.is_video("A.MP4"), "大写后缀也算")
ok(not videograb.is_video("a.mkv"), "mkv 不算（Chromium 放不了）")
ok(not videograb.is_video("a.jpg"), "jpg 不算")

print("--- 命令拼装（守 ffmpeg 9 的 -vsync 陷阱）---")
cmd = videograb.build_extract_cmd("ffmpeg", "in.mp4", 0.05, "out/%04d.jpg")
ok("-fps_mode" in cmd, "抽帧命令用 -fps_mode")
ok(not any(x == "-vsync" for x in cmd),
   "抽帧命令里**不能**出现 -vsync（ffmpeg 9 已删除该选项，写了就一张都抽不出来）")
ok("vfr" in cmd, "-fps_mode 的值是 vfr")
ok(any("scene" in x for x in cmd), "用了场景变化检测")
ok(any("0.05" in x for x in cmd), "阈值进了命令")
ok(r"eq(n\,0)" in " ".join(cmd), "首帧必留，且表达式内的逗号已转义")
ecmd = videograb.build_estimate_cmd("ffmpeg", "in.mp4", 0.05)
ok("-f" in ecmd and "null" in ecmd, "预估命令不落盘（-f null）")
ok(any("metadata=print" in x for x in ecmd), "预估命令打印 scene score")
ok(not any("eq(n" in x for x in ecmd),
   "预估表达式不含 eq(n,0)（首帧是在结果里 +1 单独算的，否则会多数一张）")

ffmpeg = videograb.resolve_ffmpeg()
if not ffmpeg:
    print()
    print("  SKIP  这台机器没有 ffmpeg，跳过真实抽帧（上面的参数断言已通过）")
    print("        视频转图集在这台机器上不可用：设置里填路径或把 ffmpeg 加进 PATH。")
    print()
    # **SKIP 不能吞掉已经失败的断言**：否则前面 FAIL 了却因为走到这里而退出 0，
    # 那就是假绿灯（变异测试抓出来的：把 ffmpeg 查找改坏 → 有 FAIL 却退出 0）。
    if fails:
        print(f"视频转图集：{len(fails)} 项失败（真实抽帧已跳过）")
        sys.exit(1)
    print("参数断言完成；真实抽帧 SKIP")
    sys.exit(0)

print(f"--- 真实抽帧（{ffmpeg}）---")
tmp = Path(tempfile.mkdtemp(prefix="asase_vgtest_"))
try:
    src = tmp / "clip.mp4"
    # 红 1 秒 + 蓝 1 秒 = 两张图（用 lavfi 合成，不依赖任何外部素材）
    gen = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-y",
         "-f", "lavfi", "-i", "color=c=red:s=160x120:d=1:r=10",
         "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=1:r=10",
         "-filter_complex", "[0][1]concat=n=2:v=1:a=0",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        capture_output=True, text=True, errors="replace")
    if gen.returncode != 0 or not src.is_file():
        ok(False, f"合成测试视频失败：{(gen.stderr or '')[-300:]}")
    else:
        n = videograb.estimate_frames(ffmpeg, str(src), 0.05)
        eq(n, 2, "预估张数（首帧 + 1 次切换）")

        out = tmp / "out"
        got = videograb.extract_frames(ffmpeg, str(src), 0.05, out)
        eq(got, 2, "实际抽出张数")
        ok((out / "0001.jpg").is_file() and (out / "0002.jpg").is_file(),
           "文件名是 0001.jpg / 0002.jpg（连续编号，natural_key 排序不会乱）")

        out2 = tmp / "out_cancel"
        cancelled = False
        try:
            videograb.extract_frames(ffmpeg, str(src), 0.05, out2,
                                     should_cancel=lambda: True)
        except videograb.VideoGrabCancelled:
            cancelled = True
        ok(cancelled, "取消：should_cancel 为真时抛 VideoGrabCancelled（并杀掉 ffmpeg 子进程）")

        try:
            videograb.extract_frames(ffmpeg, str(tmp / "不存在.mp4"), 0.05, tmp / "out_bad")
            ok(False, "不存在的视频应该报错")
        except videograb.VideoGrabError:
            ok(True, "不存在的视频抛 VideoGrabError（前端直接显示错误信息）")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if fails:
    print(f"视频转图集：{len(fails)} 项失败")
    sys.exit(1)
print("视频转图集：全部通过")
