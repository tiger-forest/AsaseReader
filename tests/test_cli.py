"""cli.py 回归测试（v1.13.2）：命令行入口此前**零覆盖**。

重点不是刷覆盖率，而是把「CLI 默认参数必须落在风控红线内」钉死 ——
页面间隔 2~5s、并发 ≤2、每日上限 200 这些默认值本身就是红线的一部分，
而 GUI 那侧的 `test_ratelimit.py` 管不到 CLI 的 argparse 默认值：
哪天有人把 `--concurrency` 的 default 改成 8，除了这里没人会报错。

全程不发网络请求：`run()` 里的 `Fetcher` 换成返回夹具 HTML 的打桩类，
限速器也只是构造出来（打桩不调它，所以测试不用真等几秒）。

跑法（也由 tests/selftest.py 统一调用）：
    python tests/test_cli.py
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import os  # noqa: E402
from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402
ensure_tmp_env()
assert_tmp_guard(os.environ.get("LOCALAPPDATA"), "LOCALAPPDATA")

from asase_reader import __version__  # noqa: E402
from asase_reader import cli  # noqa: E402
from asase_reader.backend import MAX_CONCURRENCY, PROJECT_ROOT, get_data_dir  # noqa: E402
from asase_reader.errors import BiliOpusError  # noqa: E402

FIXTURE_HTML = (ROOT / "tests" / "fixtures" / "opus_1261150454223470609.html").read_text(
    encoding="utf-8"
)
SAMPLE_ID = "1261150454223470609"

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


CAPTURED: dict[str, object] = {}


class StubFetcher:
    """替掉真 Fetcher：页面返回夹具 HTML，抓图直接报错（CLI 测试不该落任何图）。"""

    def __init__(self, page_limiter, image_limiter, concurrency: int = 2, **kw) -> None:
        CAPTURED["concurrency"] = concurrency

    async def __aenter__(self) -> "StubFetcher":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def fetch_page(self, url: str) -> str:
        return FIXTURE_HTML

    async def fetch_image(self, url: str) -> bytes:
        raise AssertionError("CLI 测试不该真的抓图")


def parse(*argv: str):
    return cli.build_arg_parser().parse_args(list(argv))


def run_cli(*argv: str) -> int:
    CAPTURED.clear()
    real = cli.Fetcher
    cli.Fetcher = StubFetcher
    try:
        return asyncio.run(cli.run(parse(*argv)))
    finally:
        cli.Fetcher = real


print("--- 风控红线：CLI 的默认参数必须在红线内 ---")
a = parse()
check(a.delay_min == 2.0, f"页面间隔下限默认 2s（实际 {a.delay_min}）")
check(a.delay_max == 5.0, f"页面间隔上限默认 5s（实际 {a.delay_max}）")
check(2.0 <= a.delay_min <= a.delay_max <= 5.0, "页面随机延迟落在 2~5s 的区间内")
check(a.image_delay_min == 0.5, f"图片间隔下限默认 0.5s（实际 {a.image_delay_min}）")
check(a.image_delay_max == 1.5, f"图片间隔上限默认 1.5s（实际 {a.image_delay_max}）")
check(0.5 <= a.image_delay_min <= a.image_delay_max <= 1.5, "图片随机延迟落在 0.5~1.5s 区间内")
check(a.concurrency == 2, f"并发默认 2（红线是 1~2）（实际 {a.concurrency}）")
check(a.max_per_day == 200, f"每日上限默认 200（实际 {a.max_per_day}）")
check(a.max_per_day > 0, "每日总量上限必须是个正数（0 或 None 等于没有上限）")

print("")
print("--- 路径锚定：相对路径基于项目根目录，不受 cwd 影响 ---")
check(cli._root("outputs") == PROJECT_ROOT / "outputs", "相对路径锚到项目根")
check(cli._root("C:/x/y") == Path("C:/x/y"), "绝对路径原样返回")

print("")
print("--- collect_ids：URL / 纯 ID / 混在文字里都能提取，且去重保序 ---")
ids = cli.collect_ids(
    parse(
        f"https://www.bilibili.com/opus/{SAMPLE_ID}",
        SAMPLE_ID,
        f"这篇文章 https://www.bilibili.com/opus/{SAMPLE_ID} 挺好看",
    )
)
check(ids == [SAMPLE_ID], f"三种写法提取到同一个 ID 且去重（实际 {ids}）")
ids2 = cli.collect_ids(parse("1261150454223470608", "1261150454223470609"))
check(ids2 == ["1261150454223470608", SAMPLE_ID], f"多个 ID 保持原顺序（实际 {ids2}）")

print("")
print("--- -f 列表文件：# 注释与空行跳过，文件不存在要报错 ---")
with tempfile.TemporaryDirectory() as td:
    f = Path(td) / "urls.txt"
    f.write_text(
        f"# 这是注释\n\n  https://www.bilibili.com/opus/{SAMPLE_ID}  \n# 结尾注释\n",
        encoding="utf-8",
    )
    check(cli.collect_ids(parse("-f", str(f))) == [SAMPLE_ID], "注释与空行被跳过")
    try:
        cli.collect_ids(parse("-f", str(Path(td) / "nope.txt")))
        check(False, "列表文件不存在时应抛 BiliOpusError")
    except BiliOpusError:
        check(True, "列表文件不存在时抛 BiliOpusError")

print("")
print("--- 并发被夹在 1~2（红线本身就是 1~2，不是「防传 100」这种宽松夹取）---")
print("    （这里 2026-10-25 前写的是「夹在 1~4，红线是 1~2」，测试在替代码背书，已改正）")
with tempfile.TemporaryDirectory() as td:
    # 2.0.0 目录校验规则 7：书架不能在 state 目录里面，--out 与 --state-dir
    # 用兄弟目录（NOTE-007：历史用例同传一个 td 会被硬阻断）
    out = Path(td) / "out"
    state = Path(td) / "state"
    run_cli(SAMPLE_ID, "--concurrency", "100", "--dry-run", "--out", str(out), "--state-dir", str(state))
    check(CAPTURED.get("concurrency") == MAX_CONCURRENCY,
          f"--concurrency 100 被夹到红线上限 {MAX_CONCURRENCY}（实际 {CAPTURED.get('concurrency')}）")
    run_cli(SAMPLE_ID, "--concurrency", "3", "--dry-run", "--out", str(out), "--state-dir", str(state))
    check(CAPTURED.get("concurrency") == MAX_CONCURRENCY,
          f"--concurrency 3（红线之外）也被夹回 {MAX_CONCURRENCY}（实际 {CAPTURED.get('concurrency')}）")
    run_cli(SAMPLE_ID, "--concurrency", "0", "--dry-run", "--out", str(out), "--state-dir", str(state))
    check(CAPTURED.get("concurrency") == 1, f"--concurrency 0 被夹到 1（实际 {CAPTURED.get('concurrency')}）")

print("")
print("--- --dry-run：只解析统计，一个文件都不许落 ---")
with tempfile.TemporaryDirectory() as td:
    out = Path(td) / "out"
    state = Path(td) / "state"
    code = run_cli(SAMPLE_ID, "--dry-run", "--out", str(out), "--state-dir", str(state))
    written = [str(p) for p in out.rglob("*") if p.is_file()] if out.exists() else []
    check(code == 0, f"dry-run 正常返回 0（实际 {code}）")
    check(written == [], f"dry-run 没有落任何文件（实际 {written[:3]}）")

print("")
print("--- 没有可下载的链接：run() 返回 2（不是 0，也不是崩）---")
check(run_cli() == 2, "无链接时返回 2")

print("")
print("--- -V/--version 与 __version__ 一致 ---")
buf = io.StringIO()
code = None
with contextlib.redirect_stdout(buf):
    try:
        cli.main(["-V"])
        code = "没有退出"
    except SystemExit as e:
        code = e.code
check(code == 0, f"--version 以 0 退出（实际 {code}）")
check(__version__ in buf.getvalue(), f"--version 输出含版本号 {__version__}")

print("")
print("--- 不带任何参数：走 GUI（双击 exe 的场景）---")
fake_gui = types.ModuleType("asase_reader.gui")
called: dict[str, object] = {}


def _launch_gui(**kw):
    called.update(kw)


fake_gui.launch_gui = _launch_gui
saved = sys.modules.get("asase_reader.gui")
sys.modules["asase_reader.gui"] = fake_gui
try:
    rc = cli.main([])
finally:
    if saved is None:
        del sys.modules["asase_reader.gui"]
    else:
        sys.modules["asase_reader.gui"] = saved
check(rc == 0, f"无参数时返回 0（实际 {rc}）")
check(called.get("state_dir") == str(get_data_dir()),
      f"GUI 拿到默认 state_dir = DATA_DIR（实际 {called.get('state_dir')}）")

print("")
print(f"cli.py 测试：{'FAIL ' + str(len(fails)) + ' 项' if fails else 'ALL PASS'}")
sys.exit(1 if fails else 0)
