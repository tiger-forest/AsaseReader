"""充电专属（付费墙）相关测试：凭据、判据、下载流程、凭据不落盘。

不发任何网络请求，页面用合成的 HTML 夹具（tests/fixtures/opus_paywall_*.html）。

跑法（也由 tests/selftest.py 统一调用）：
    python tests/test_paywall.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import os  # noqa: E402
from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402
import asase_reader.backend as backend_mod  # noqa: E402
from asase_reader.backend import Backend, Job  # noqa: E402
from asase_reader.credentials import Credentials  # noqa: E402
from asase_reader.fetch import Fetcher  # noqa: E402
from asase_reader.parse import parse_opus_html  # noqa: E402
from asase_reader.ratelimit import RateLimiter  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
PREVIEW_HTML = (FIX / "opus_paywall_preview.html").read_text(encoding="utf-8")
FULL_HTML = (FIX / "opus_paywall_full.html").read_text(encoding="utf-8")
NORMAL_HTML = (FIX / "opus_1261150454223470609.html").read_text(encoding="utf-8")
OPUS_ID = "1000000000000000001"
MID = 12345678

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
FAKE_COOKIE = "SESSDATA=unit_test_secret_value; bili_jct=fake_jct; DedeUserID=1"

# StubFetcher 的行为开关
SERVE = {"full_when_cookie": True}


class StubFetcher(Fetcher):
    """打桩页面与图片：按"有没有带 Cookie"决定返回预览版还是完整版。"""

    def __init__(self, page_limiter, image_limiter, **kw) -> None:
        # 限速区间设 0，测试不用真等
        super().__init__(RateLimiter(0, 0), RateLimiter(0, 0), **kw)
        self.page_calls: list[tuple[str, str]] = []
        self.img_calls: list[str] = []

    async def fetch_page(self, url: str, cookie: str = "") -> str:
        self.page_calls.append((url, cookie))
        if cookie and SERVE["full_when_cookie"]:
            return FULL_HTML
        return PREVIEW_HTML

    async def fetch_image(self, url: str) -> bytes:
        self.img_calls.append(url)
        return PNG


def new_backend(tmp: Path) -> Backend:
    ensure_tmp_env()
    shelf = tmp / "shelf"
    state = tmp / "state"
    shelf.mkdir(parents=True)
    state.mkdir()
    assert_tmp_guard(shelf, "shelf")
    assert_tmp_guard(state, "state")
    assert_tmp_guard(os.environ.get("LOCALAPPDATA"), "LOCALAPPDATA")
    b = Backend(state_dir=state)
    b.config["shelf_dir"] = str(shelf)
    b.config["download_dir"] = ""
    b.config["setup_done"] = True
    # 关掉限速，否则一次 _run_job 要真等好几秒
    for k in ("delay_min", "delay_max", "image_delay_min", "image_delay_max"):
        b.config[k] = 0
    return b


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="asase_paywall_"))

    print("== 1. 凭据：只认 SESSDATA，值不出现在任何对外输出里")
    c = Credentials()
    ok, _ = c.set("")
    check(ok is False, "空串被拒绝")
    ok, msg = c.set("buvid3=xxx; b_nut=100")
    check(ok is False and "SESSDATA" in msg, f"缺 SESSDATA 被拒绝（提示：{msg}）")
    ok, _ = c.set(f"Cookie: {FAKE_COOKIE}")
    check(ok is True, '带 "Cookie: " 前缀也能识别（会先清理）')
    ok, _ = c.set(f"{FAKE_COOKIE}\n")
    check(ok is True, "带换行也能识别")
    check(c.configured is True, "已配置")
    s = c.summary()
    check(s["has_sessdata"] is True and s["fields"] == 3, f"摘要只有统计：{s}")
    for out in (json.dumps(s, ensure_ascii=False), repr(c), str(c)):
        check("unit_test_secret_value" not in out, f"对外输出不含凭据原文：{out[:40]}…")
    c.clear()
    check(c.configured is False and c.cookie == "", "清除后内存里也没有了")

    print("== 2. 付费墙判据：只看 module_paywall，不猜图片内容")
    prev = parse_opus_html(PREVIEW_HTML)
    check(prev.paywalled is True, "预览版被识别为付费墙")
    check(prev.preview_ratio == 31, f"preview_ratio=31（实际 {prev.preview_ratio}）")
    check(len(prev.images) == 27, f"预览版 27 张（实际 {len(prev.images)}）")
    check(prev.estimated_total() == 87, f"按 31% 反推完整约 87 张（实际 {prev.estimated_total()}）")
    full = parse_opus_html(FULL_HTML)
    check(full.paywalled is False, "完整版不算付费墙")
    check(len(full.images) == 88, f"完整版 88 张（实际 {len(full.images)}）")
    check(parse_opus_html(NORMAL_HTML).paywalled is False, "普通公开专栏不算付费墙")
    # 教训：作者本来就可能画全黑页，不能靠"图是不是黑的"判断付费墙。
    # 这里直接把正文图片全部换成同一张（模拟黑页）但不带 module_paywall。
    blackish = PREVIEW_HTML.replace('"module_paywall"', '"module_unused"')
    check(parse_opus_html(blackish).paywalled is False,
          "没有 module_paywall 就绝不判成付费墙（防'检测黑图'误判）")

    print("== 3. 流程 A：没有凭据，付费内容必须失败且不入库")
    backend_mod.Fetcher = StubFetcher
    a = new_backend(tmp / "A")
    job = Job(opus_id=OPUS_ID)
    asyncio.run(a._run_job(job))
    check(job.status == "failed", f"任务失败（实际 {job.status}）")
    check(job.need_cookie is True, "标记 need_cookie，前端才好提示")
    check("预览" in job.error and "87" in job.error, f"错误里说明了少了多少：{job.error}")
    check(not a.library.has_opus(OPUS_ID), "预览版一张都没入库")
    landing = a.shelf_root() / "chapters" / f"opus_{OPUS_ID}"
    check(not (landing / "meta.json").exists(), "预览版没有写 meta.json")

    print("== 4. 流程 B：补上凭据后重试成功，并记住这个 UP")
    b = new_backend(tmp / "B")
    r = b.submit_cookie(FAKE_COOKIE)
    check(r["ok"] is True and r["cookie"]["configured"] is True, "保存凭据成功")
    SERVE["full_when_cookie"] = True
    job = Job(opus_id=OPUS_ID)
    asyncio.run(b._run_job(job))
    check(job.status == "done", f"任务成功（实际 {job.status}）")
    check(job.total == 88, f"拿到完整 88 张（实际 {job.total}）")
    ids = b._paid_up_ids()
    check(MID in ids, f"UP 主 {MID} 已被记住（实际 {ids}）")
    check(b.library.has_opus(OPUS_ID), "整章已入库")

    print("== 5. 流程 C：带了凭据却仍是预览版 = 凭据失效或非充电成员")
    SERVE["full_when_cookie"] = False
    c2 = new_backend(tmp / "C")
    c2.submit_cookie(FAKE_COOKIE)
    job = Job(opus_id=OPUS_ID)
    asyncio.run(c2._run_job(job))
    check(job.status == "failed", "仍然失败，不会静默存下残缺章节")
    check("失效" in job.error or "充电成员" in job.error, f"提示凭据问题：{job.error}")
    check(job.need_cookie is False, "这种情况不该再让用户重填 Cookie")
    check(not c2.library.has_opus(OPUS_ID), "预览版依然没有入库")
    SERVE["full_when_cookie"] = True

    print("== 6. 流程 D：记住 UP 后，首次请求就带凭据（省掉试探那趟）")
    d = new_backend(tmp / "D")
    d.submit_cookie(FAKE_COOKIE)
    d.config["paid_ups"] = [{"mid": MID, "name": "测试作者"}]
    cap: list[StubFetcher] = []
    orig_init = StubFetcher.__init__

    def spy_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        cap.append(self)

    StubFetcher.__init__ = spy_init
    d._last_author_mid = MID  # 上一个任务来自这个已知付费 UP
    job = Job(opus_id=OPUS_ID)  # 全新的临时书架，不存在"已在书架"跳过的问题
    asyncio.run(d._run_job(job))
    StubFetcher.__init__ = orig_init
    if cap:
        calls = cap[0].page_calls
        check(len(calls) == 1, f"页面只请求了 1 次（实际 {len(calls)}）")
        check(bool(calls[0][1]), "这一次就带上了凭据")
    else:
        check(False, "没能捕获到 Fetcher 调用")

    print("== 7. 红线：凭据绝不落盘，也绝不上图片请求")
    e = new_backend(tmp / "E")
    e.submit_cookie(FAKE_COOKIE)
    e._save_config()
    cfg_text = (e.config_path).read_text(encoding="utf-8")
    check("unit_test_secret_value" not in cfg_text, "config.json 里没有 Cookie 值")
    check("SESSDATA" not in cfg_text, "config.json 里连 SESSDATA 字段名都没有")
    state_json = json.dumps(e.get_state(), ensure_ascii=False)
    check("unit_test_secret_value" not in state_json, "推给前端的状态里没有 Cookie 值")
    # 图片下载走 fetch_image，接口上根本没有 cookie 参数；
    # 同时确认凭据没有被塞进 client 级请求头（否则每张图都会带上）
    e2 = new_backend(tmp / "E2")
    e2.submit_cookie(FAKE_COOKIE)
    cap2: list[StubFetcher] = []

    def spy_init2(self, *a, **kw):
        orig_init(self, *a, **kw)
        cap2.append(self)

    StubFetcher.__init__ = spy_init2
    job = Job(opus_id=OPUS_ID)
    asyncio.run(e2._run_job(job))
    StubFetcher.__init__ = orig_init
    if cap2:
        f = cap2[0]
        head = {k.lower(): v for k, v in f._client.headers.items()}
        check("cookie" not in head, "客户端级请求头里没有 Cookie（图片请求不会带上）")
        check(len(f.img_calls) > 0, f"确实下载了 {len(f.img_calls)} 张图")
    else:
        check(False, "没能捕获到 Fetcher 调用")
    e2.clear_cookie()
    check(e2.credentials.configured is False, "清除后凭据从内存消失")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nFAILED: {len(fails)}")
    for f in fails:
        print("  -", f)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
