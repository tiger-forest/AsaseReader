"""命令行入口（浅濑阅读器 Asase Reader 的下载功能）。

用法：
    python -m asase_reader <url> [url ...] [选项]
    python -m asase_reader -f urls.txt [选项]
    python -m asase_reader --gui    启动图形界面
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import __version__
from .backend import (
    MAX_CONCURRENCY,
    PROJECT_ROOT,
    Backend,
    clamp_concurrency,
    default_shelf_dir,
    get_data_dir,
)
from .download import DownloadStats, download_opus, opus_is_complete
from .errors import BiliOpusError, DailyLimitReached, RiskControlError
from .fetch import Fetcher
from .parse import extract_opus_id, opus_url, parse_opus_html
from .ratelimit import RateLimiter
from .store import opus_dir

console = Console()


def _root(p: str) -> Path:
    """把配置里的相对路径锚定到项目根目录，避免受启动时 cwd 影响。"""
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="asase-reader",
        description="浅濑阅读器 Asase Reader —— 批量下载 B 站专栏（opus）图片，仅个人离线阅读使用，请勿二次传播。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("urls", nargs="*", help="一个或多个专栏链接（可混入纯文本，会自动提取 ID）")
    p.add_argument("-f", "--file", help="URL 列表文件（每行一条，# 开头为注释）")
    p.add_argument("-V", "--version", action="version", version=f"浅濑阅读器 Asase Reader v{__version__}")
    p.add_argument("-o", "--out", default=None, help="输出根目录（相对路径基于项目根目录；默认读配置里的书架目录，未配置时用「文档」下的默认书库）")
    p.add_argument("--delay-min", type=float, default=2.0, help="页面请求最小间隔（秒）")
    p.add_argument("--delay-max", type=float, default=5.0, help="页面请求最大间隔（秒）")
    p.add_argument("--image-delay-min", type=float, default=0.5, help="图片下载最小间隔（秒）")
    p.add_argument("--image-delay-max", type=float, default=1.5, help="图片下载最大间隔（秒）")
    p.add_argument(
        "--concurrency",
        type=int,
        default=2,
        # 上限由 MAX_CONCURRENCY 决定，不再写死数字 —— 曾经这里写 4 而红线写 1~2
        help=f"并发数（风控红线上限 {MAX_CONCURRENCY}，填大也会被夹回来）",
    )
    p.add_argument("--max-per-day", type=int, default=200, help="每日页面请求上限")
    p.add_argument("--no-cover", action="store_true", help="不下载头图")
    p.add_argument("--overwrite", action="store_true", help="覆盖已存在的文件重新下载")
    p.add_argument("--dry-run", action="store_true", help="只解析统计，不下载")
    p.add_argument("--state-dir", default=None, help="每日计数等状态文件目录（相对路径基于项目根目录；默认指向程序数据目录：frozen 为 %LOCALAPPDATA%\\AsaseReader，源码为项目 .asase）")
    p.add_argument("--gui", action="store_true", help="启动图形界面（pywebview）")
    return p


def collect_ids(args) -> list[str]:
    raw: list[str] = list(args.urls)
    if args.file:
        fpath = Path(args.file)
        if not fpath.exists():
            raise BiliOpusError(f"URL 列表文件不存在: {fpath}")
        for line in fpath.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raw.append(line)
    ids: list[str] = []
    seen: set[str] = set()
    for item in raw:
        oid = extract_opus_id(item)
        if oid not in seen:
            seen.add(oid)
            ids.append(oid)
    return ids


def _fill_defaults(args) -> None:
    """--state-dir / --out 的后置回填（方案 §4.2 第 6 条 / GLM C6 + P-5）。

    两个参数都 default=None + 解析后回填，**别把动态绝对路径写进 argparse 的
    default**（帮助文案会变；且 from-import 一次固化后 monkeypatch sys.frozen
    不生效——这里全部用 get_data_dir() / default_shelf_dir() 函数调用）。
    """
    if args.state_dir is None:
        args.state_dir = str(get_data_dir())
    if args.out is None:
        # --out 优先读 config 的 shelf_dir，回退 default_shelf_dir()，
        # **绝不指向 DATA_DIR**（内容与程序小数据不混放）。
        cfg_file = _root(args.state_dir) / "config.json"
        shelf = ""
        try:
            if cfg_file.exists():
                data = json.loads(cfg_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    shelf = str(data.get("shelf_dir") or "").strip()
        except Exception:
            pass
        args.out = shelf or str(default_shelf_dir())


async def run(args) -> int:
    ids = collect_ids(args)
    if not ids:
        console.print("[red]没有可下载的链接[/red]")
        return 2

    out_root = _root(args.out)
    state_dir = _root(args.state_dir)

    # 目录合法性校验：与 GUI 同一套 11 规则（§4.5「CLI 复用同一套目录校验」）。
    # dry-run 必须豁免探针（§4.3 规则 9 / NOTE-007）：dry-run 语义 = 零落盘，
    # 探针却要 mkdir + 写一个临时文件——直接矛盾。
    backend = Backend(state_dir=state_dir)
    res_val = backend._validate_path(str(out_root), skip_probe=bool(args.dry_run))
    if not res_val["ok"]:
        console.print(f"[bold red]输出目录不可用：{res_val['error']}[/bold red]")
        return 2
    for w in res_val.get("warnings") or []:
        console.print(f"[yellow]提示：{w['message']}[/yellow]")

    include_cover = not args.no_cover

    page_limiter = RateLimiter(
        min_interval=args.delay_min,
        max_interval=args.delay_max,
        daily_limit=args.max_per_day,
        state_path=state_dir / f"pages_{date.today():%Y-%m-%d}.json",
    )
    image_limiter = RateLimiter(
        min_interval=args.image_delay_min,
        max_interval=args.image_delay_max,
        daily_limit=None,
        state_path=None,
    )
    concurrency = clamp_concurrency(args.concurrency)

    results: list[tuple[str, str, DownloadStats | None, str]] = []
    aborted_reason: str | None = None

    console.print(
        f"[bold]B 站专栏图片下载器[/bold]  共 [cyan]{len(ids)}[/cyan] 篇  "
        f"并发 {concurrency}  页面间隔 {args.delay_min}~{args.delay_max}s  "
        f"图片间隔 {args.image_delay_min}~{args.image_delay_max}s  今日已用 {page_limiter.daily_count}/{args.max_per_day}"
    )

    async with Fetcher(page_limiter, image_limiter, concurrency=concurrency) as fetcher:
        for i, oid in enumerate(ids, 1):
            url = opus_url(oid)
            console.rule(f"[{i}/{len(ids)}] {url}")
            try:
                html = await fetcher.fetch_page(url)
                opus = parse_opus_html(html, source_url=url)
                if not opus.images and not opus.cover:
                    console.print("[yellow]该专栏没有图片，跳过[/yellow]")
                    continue

                if args.dry_run:
                    console.print(
                        f"[cyan]DRY RUN[/cyan] 标题: {opus.title}  作者: {opus.author_name}"
                        f"  正文图片 {len(opus.images)} 张"
                        + (f"  头图 1 张" if opus.cover else "")
                    )
                    continue

                # 空间防御 B 档（§4.5）：每章节查一次，<50MB 直接中止、不再写入；
                # disk_usage 传输出目录本身（不存在时查最近已存在祖先）
                try:
                    chk = out_root if out_root.exists() else backend._existing_ancestor(out_root)
                    if shutil.disk_usage(chk).free < 50 * 1024 * 1024:
                        console.print("[bold red]磁盘可用空间不足 50 MB，任务中止[/bold red]")
                        aborted_reason = "磁盘可用空间不足 50 MB"
                        break
                except Exception:
                    pass

                target_dir = opus_dir(out_root, opus)
                if not args.overwrite and opus_is_complete(target_dir, opus, include_cover):
                    console.print(
                        f"[green]已存在且完整，跳过[/green]（如需重新下载加 --overwrite）"
                    )
                    continue

                dir_path, stats = await download_opus(
                    fetcher,
                    opus,
                    target_dir,
                    include_cover=include_cover,
                    overwrite=args.overwrite,
                    log=lambda m: console.print(m, highlight=False),
                )
                results.append((opus.title, str(dir_path), stats, ""))
                console.print(
                    f"[green]完成[/green] {opus.title}  "
                    f"下载 {stats.downloaded} / 跳过 {stats.skipped} / "
                    f"复用 {stats.reused} / 失败 {stats.failed}"
                )
            except DailyLimitReached as e:
                results.append((oid, url, None, str(e)))
                aborted_reason = str(e)
                break
            except RiskControlError as e:
                results.append((oid, url, None, str(e)))
                aborted_reason = str(e)
                break
            except BiliOpusError as e:
                results.append((oid, url, None, str(e)))
                console.print(f"[red]失败[/red] {e}")
                continue

    if results:
        table = Table(title="汇总")
        table.add_column("专栏")
        table.add_column("结果")
        for title, path, stats, err in results:
            if err:
                table.add_row(title, f"[red]{err[:60]}[/red]")
            elif stats:
                table.add_row(
                    title,
                    f"[green]{stats.downloaded}↓ {stats.skipped}跳 {stats.reused}复用 {stats.failed}失败[/green]",
                )
        console.print(table)

    if aborted_reason:
        console.print(f"[bold red]任务中止: {aborted_reason}[/bold red]")
        return 1
    return 0


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    # 后置回填（见 _fill_defaults 注记）：--state-dir → DATA_DIR、--out → config
    # 的 shelf_dir → default_shelf_dir()，之后 main/run 用的都是回填后的值
    _fill_defaults(args)
    # 双击 exe（无任何参数）时默认进 GUI——浅濑阅读器的主体是图形界面
    if args.gui or (not args.urls and not args.file):
        from .gui import launch_gui

        launch_gui(state_dir=args.state_dir)
        return 0
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        console.print("\n[yellow]用户中断[/yellow]")
        return 130
    except BiliOpusError as e:
        console.print(f"[bold red]{e}[/bold red]")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
