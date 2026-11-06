"""一键自检：把项目里所有自动化测试跑一遍，最后给一句 PASS/FAIL 汇总。

为什么要有这个东西：
本项目是 GUI 程序，很多错误（属性名改错、签名对不上、死锁）**只有真的调用才会暴露**，
光读代码看不出来。1.1.0 就因为只测了"我改过的那几个函数"，漏掉了三个运行时故障。
所以把全部测试收成一个命令，改完代码先跑它，也方便任何一次新对话快速验证。

用法：
    python tests/selftest.py            # 跑全部（JS 默认 4 路并行）
    python tests/selftest.py --serial   # JS 退回串行（排查 flaky 时用）
    python tests/selftest.py --jobs=8   # 调并行度
    python tests/selftest.py --skip-js  # 只跑 Python（没装 jsdom 时用）

退出码 0 = 全通过，1 = 有失败。**并行只影响跑法，不影响判定**：
每个测试的退出码都单独收集，只要有一个非 0，整体就非 0（见 main() 末尾的 bad）。

JS 测试依赖 jsdom：cd tests && npm install（只需一次）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# cp936 控制台下，失败输出里若混进无法编码的字符（如 replacement char），
# UnicodeEncodeError 会让汇总程序自己崩掉、掩盖真实失败原因——
# 统一转 utf-8 + errors=replace，宁可显示替字符也不许假死（方案 v2 §5.5 / NOTE-004）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass  # 非 Windows / 无 reconfigure 的解释器：保持默认行为

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from test_helpers import ensure_tmp_env  # noqa: E402

PY = sys.executable

# jsdom 可能装在 tests/node_modules（推荐）、开发期的 .cache/node_modules，
# 或由 NODE_PATH 环境变量指定。这里不放任何本机专属绝对路径，
# 便于公开快照里的测试也能在别人的机器上跑。
NODE_DIRS = [
    TESTS / "node_modules",
    ROOT / ".cache" / "node_modules",
]
if os.environ.get("NODE_PATH"):
    NODE_DIRS.append(Path(os.environ["NODE_PATH"]))

PY_TESTS = [
    ("数据层 / 迁移 / 导入 / 后端入库", "test_library.py"),
    ("导出：目录结构 / 命名 / 冲突跳过 / work.json", "test_export.py"),
    ("离线下载全链路（真实 HTML 夹具）", "test_download_offline.py"),
    ("取消令牌复位（取消一次后不能一直自动取消）", "test_cancel_reset.py"),
    ("路径 / 静态服务 / 封面 / URL 解析", "test_paths_and_server.py"),
    ("v2 数据目录：严格校验 / 先校验后提交 / 零写入 / 双锚点 / 空间与快照", "test_installer_data_dir.py"),
    ("Library readonly：零迁移 / 零备份 / 零清库 / 根数组防御", "test_library_readonly.py"),
    ("安装包双轨构建：spec / 版本资源 / .iss / 一键出包链路", "test_release_package.py"),
    ("公开快照：白名单 / 扫描器 / 镜像仓库 / 许可", "test_public_snapshot.py"),
    ("静态服务 Range：206 切片 / 416 越界 / 无效退回 200 / 图片零回归", "test_staticserver_range.py"),
    ("风控红线：412 退避 / 每日上限 / 跨天重置", "test_ratelimit.py"),
    ("充电专属：付费墙判据 / 凭据 / 不落盘 / 拒绝预览版", "test_paywall.py"),
    ("GuiApi 全方法冒烟", "test_api_smoke.py"),
    ("前端 api() 调用面校验", "test_api_surface.py"),
    ("前端内部函数引用链（改名漏改调用点）", "test_frontend_refs.py"),
    ("返回记账静态检查（进页必须带来源参数）", "test_nav_backrefs.py"),
    ("未定义属性静态扫描", "test_attr_scan.py"),
    ("全屏 API 契约：无窗口 / 抛错 / 状态记账", "test_fullscreen_api.py"),
    ("CLI 入口：风控默认值 / URL 提取 / dry-run / 无参数进 GUI", "test_cli.py"),
    ("并发红线：上限 2 在代码 / GUI / README 三处同源 + 读写入口都夹住", "test_concurrency.py"),
    ("视频转图集：阈值夹取 / ffmpeg 查找 / 命令拼装（-fps_mode） + 真实抽帧", "test_videograb.py"),
    ("视频章节：复制入库 / 保留原扩展名 / media 接口 / 无 ffmpeg 降级 + 真实截帧", "test_video_chapter.py"),
    ("章节内删除图片：真删文件 / 剩余重新编号 / 非法请求一个都不删", "test_chapter_images.py"),
    ("原生无边框 + DWM 过渡：钩子幂等 / 方向权威 / 禁过渡时序 / 补间不回潮", "test_window_anim.py"),
    ("界面设置持久化：后端副本 / 固定端口 / 无痕模式 / setUiPref / 启动写回", "test_ui_settings.py"),
    ("应用图标：ICO 结构 / 源码与打包两种跑法 / spec 声明 / favicon 路由", "test_app_icon.py"),
]

JS_TESTS = [
    ("界面结构 / 三页签 / 导入下拉", "test_ui.js"),
    ("无边框边缘缩放：四边八角判定 / 拖拽换算 / 单击不动窗口", "test_window_resize.js"),
    ("原生 DWM 过渡（前端）：开关透传 / 图标按后端权威翻转 / 旧补间不回潮", "test_win_anim.js"),
    ("启动竞态（pywebviewready）", "test_boot_race.js"),
    ("首启目录引导：预填 / 预检 / 不可跳过 / 成败路径 / 空间提示", "test_first_setup.js"),
    ("导航按钮与文案", "test_ui_nav.js"),
    ("阅读器模式切换（连续滚动回归）", "test_reader_mode.js"),
    ("单图模式：图片适应窗口（不出滚动条）", "test_reader_fit.js"),
    ("阅读器全屏：单击进出 / Esc / 降级", "test_reader_fullscreen.js"),
    ("章末页增强：下一章标题 / 已读 x-y 章 / 末章读完", "test_reader_end.js"),
    ("全屏悬浮条：唤出 / 自动收起 / 停在条上不收 / 退出复位", "test_reader_hud.js"),
    ("缩略图带：渲染 / 跳页 / 高亮 / 三档行为 / 界面设置", "test_reader_thumbs.js"),
    ("导出对话框：三个入口 / 进度 / 取消", "test_export_ui.js"),
    ("充电专属 UI：凭据输入 / 明文不残留 / UP 列表", "test_paywall_ui.js"),
    ("所有作品页 / 分页 / 首页限流 12 / 右键浮层菜单", "test_allworks.js"),
    ("标签与筛选：复选 / 任一-全部 / 作品页联动 / 编辑页标签", "test_tags.js"),
    ("界面设置：悬浮 / 阴影 / 封面三档 + 美化 CSS 声明", "test_ui_settings.js"),
    ("点击反馈：按下态 / 处理中 / 浮层淡入 / 列表增删 + 无原生 confirm", "test_click_feedback.js"),
    ("章节编辑页：三处入口 / Shift 范围选 / 不点保存绝不删", "test_chapter_edit.js"),
    ("单图缩放：倍率夹取 / 锚点 / 拖动边界 / 翻页复位 / 长图平移到底才翻页", "test_reader_zoom.js"),
    ("视频章节：旁路 / 键盘自接管 / 章末页复用 / 错误占位 / 切回复位", "test_reader_video.js"),
    ("章节封面：视频截帧 / 从章节内容选取 / 按钮显隐", "test_reader_cover.js"),
]


def run(cmd: list[str], env_extra: dict | None = None, cwd: Path | None = None) -> tuple[int, str]:
    import os

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd or ROOT), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300,
        )
    except FileNotFoundError:
        return 127, f"找不到可执行文件：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "超时（300s）——多半是某处卡死/死锁了，重点排查"
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out


def main() -> None:
    ensure_tmp_env()
    skip_js = "--skip-js" in sys.argv
    jobs = 4  # JS 并行度：保守取 4。阅读器那几个测试靠 sleep 判时序，
    # 并行度太高会因 CPU 争抢变成 flaky。--serial 退回串行，--jobs=N 手动调。
    for a in sys.argv[1:]:
        if a.startswith("--jobs="):
            jobs = max(1, int(a.split("=", 1)[1] or "4"))
    if "--serial" in sys.argv:
        jobs = 1
    results: list[tuple[str, bool, str]] = []

    print("=" * 62)
    print("浅濑阅读器 自检")
    print("=" * 62)

    for title, name in PY_TESTS:
        code, out = run([str(PY), "-u", str(TESTS / name)])
        results.append((title, code == 0, out))
        print(f"[{'PASS' if code == 0 else 'FAIL'}] {title}")
        if code != 0:
            print(indent(tail(out, 25)))

    if skip_js:
        print("[SKIP] JS 测试（--skip-js）")
    else:
        node = find_node()
        if node is None:
            print("[SKIP] JS 测试（没找到 node）")
        else:
            nm = find_node_modules()
            if nm is None:
                print("[SKIP] JS 测试（没找到 jsdom，先 cd tests && npm install）")
            else:
                import os

                env = {"NODE_PATH": str(nm)}
                ordered: list[tuple[str, int, str] | None] = [None] * len(JS_TESTS)

                def work(item: tuple[int, tuple[str, str]]) -> tuple[int, str, int, str]:
                    i, (title, name) = item
                    code, out = run([node, str(TESTS / name)], env_extra=env)
                    return i, title, code, out

                if jobs > 1 and len(JS_TESTS) > 1:
                    from concurrent.futures import ThreadPoolExecutor

                    print(f"[INFO] JS 测试并行 {min(jobs, len(JS_TESTS))} 路（--serial 可退回串行）")
                    with ThreadPoolExecutor(max_workers=min(jobs, len(JS_TESTS))) as ex:
                        for i, title, code, out in ex.map(work, list(enumerate(JS_TESTS))):
                            ordered[i] = (title, code, out)
                else:
                    for item in enumerate(JS_TESTS):
                        i, title, code, out = work(item)
                        ordered[i] = (title, code, out)

                for entry in ordered:
                    assert entry is not None, "并行跑完却有测试没结果（绝不能当成通过）"
                    title, code, out = entry
                    results.append((title, code == 0, out))
                    print(f"[{'PASS' if code == 0 else 'FAIL'}] {title}")
                    if code != 0:
                        print(indent(tail(out, 25)))

    bad = [t for t, ok, _ in results if not ok]
    print()
    print("=" * 62)
    if bad:
        print(f"自检失败：{len(bad)}/{len(results)} 项未通过")
        for t in bad:
            print("  -", t)
        print("\n建议：先看上面 FAIL 块里的具体断言，再改代码。")
        sys.exit(1)
    print(f"自检通过：{len(results)}/{len(results)} 项全绿")
    sys.exit(0)


def find_node() -> str | None:
    # 只用 PATH 上的 node 与通用安装位置；不写任何本机专属绝对路径。
    for c in ("node", "node.exe", "C:/Program Files/nodejs/node.exe"):
        try:
            p = subprocess.run([c, "-v"], capture_output=True, text=True)
            if p.returncode == 0:
                return c
        except Exception:
            continue
    return None


def find_node_modules() -> Path | None:
    for d in NODE_DIRS:
        if (d / "jsdom").is_dir():
            return d
    return None


def tail(text: str, n: int) -> str:
    lines = [x for x in text.splitlines() if x.strip()]
    return "\n".join(lines[-n:])


def indent(text: str) -> str:
    return "\n".join("      " + x for x in text.splitlines())


if __name__ == "__main__":
    main()
