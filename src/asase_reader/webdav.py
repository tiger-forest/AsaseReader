"""WebDAV 单向备份与恢复（零新依赖，复用 httpx）。

设计要点（详见 .workbuddy/memory/plan-webdav-backup.md）：
- 只管当前书架目录（shelf_root）全量内容（含视频章节、_covers、library.json），不含本机 config.json；
- 云端固定使用用户 URL 下的专属子目录 ``AsaseReader/``，只保留最新一份；
- 每次用带时间戳的新文件名上传（``asase_backup_YYYYMMDD_HHMMSS.zip``），完整上传后再写
  ``latest.json`` 指针，最后清理其余旧包（含历史断网残包），避免依赖 ``MOVE`` 覆盖；
- 适配 InfiniCLOUD：固定 ``Depth: 1``、流式上传/下载、放宽读写超时。
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import httpx

BACKUP_SUBDIR = "AsaseReader"
LATEST_MANIFEST = "latest.json"
BACKUP_PREFIX = "asase_backup_"
BACKUP_SUFFIX = ".zip"

# 放宽读写超时，适配含视频章节的整包流式传输
DEFAULT_TIMEOUT = httpx.Timeout(connect=20.0, read=600.0, write=600.0, pool=30.0)
CHUNK_SIZE = 256 * 1024

# 图片与视频本身已压缩，打包时直接 STORED，避免大文件白耗 CPU
_STORED_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif",
    ".mp4", ".m4v", ".webm", ".mov", ".zip",
})


class WebDavError(Exception):
    """WebDAV 操作失败（向用户展示明确提示）。"""


class WebDavCancelled(Exception):
    """用户在备份/恢复过程中取消了操作。"""


def normalize_dir_url(raw_url: str) -> str:
    """规范化 WebDAV 目录 URL，并固定落到专属子目录 ``AsaseReader/``。"""
    u = str(raw_url or "").strip()
    if not u:
        raise WebDavError("请先填写 WebDAV 地址")
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise WebDavError("WebDAV 地址格式无效，需以 http:// 或 https:// 开头")
    u = u.rstrip("/")
    if not u.endswith(f"/{BACKUP_SUBDIR}"):
        u = f"{u}/{BACKUP_SUBDIR}"
    return u + "/"


def _make_client(
    raw_url: str,
    username: str,
    password: str,
    transport: httpx.BaseTransport | None = None,
) -> tuple[httpx.Client, str]:
    dir_url = normalize_dir_url(raw_url)
    user = str(username or "").strip()
    pwd = str(password or "")
    if not user or not pwd:
        raise WebDavError("请填写 WebDAV 用户名和密码")
    client = httpx.Client(
        auth=(user, pwd),
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    )
    return client, dir_url


def _check_auth_or_raise(resp: httpx.Response, action: str) -> None:
    if resp.status_code in (401, 403):
        raise WebDavError("WebDAV 认证失败，请检查用户名或密码")
    if resp.status_code == 507:
        raise WebDavError("WebDAV 网盘空间不足，无法写入")
    if resp.status_code >= 400:
        raise WebDavError(f"{action}失败：HTTP {resp.status_code}")


def ensure_dir(client: httpx.Client, dir_url: str) -> None:
    """确保专属子目录存在且可访问（不存在时自动 MKCOL）。"""
    try:
        r = client.request("PROPFIND", dir_url, headers={"Depth": "1"})
    except httpx.HTTPError as e:
        raise WebDavError(f"连接 WebDAV 失败：{e}") from e

    if r.status_code in (200, 207):
        return
    if r.status_code == 404:
        try:
            mk = client.request("MKCOL", dir_url)
        except httpx.HTTPError as e:
            raise WebDavError(f"创建备份目录失败：{e}") from e
        if mk.status_code in (200, 201, 204, 405):
            return
        _check_auth_or_raise(mk, "创建备份目录")
    _check_auth_or_raise(r, "检查备份目录")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _parse_propfind(xml_text: str, dir_url: str) -> list[dict]:
    """解析 PROPFIND (Depth: 1) XML 响应，返回子项列表。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    base_path = urlparse(dir_url).path.rstrip("/")
    items: list[dict] = []
    for resp in root.iter():
        if _local_name(resp.tag) != "response":
            continue
        href = ""
        size = 0
        is_dir = False
        for child in resp.iter():
            ln = _local_name(child.tag)
            if ln == "href" and child.text and not href:
                href = child.text.strip()
            elif ln == "getcontentlength" and child.text:
                try:
                    size = int(child.text.strip())
                except ValueError:
                    pass
            elif ln == "collection":
                is_dir = True
        if not href:
            continue
        href_path = unquote(urlparse(href).path).rstrip("/")
        if href_path == base_path:
            continue
        name = href_path.rsplit("/", 1)[-1]
        if name:
            items.append({"name": name, "is_dir": is_dir, "size": size})
    return items


def list_dir(client: httpx.Client, dir_url: str) -> list[dict]:
    """列出专属子目录内容（固定 Depth: 1）。"""
    try:
        r = client.request("PROPFIND", dir_url, headers={"Depth": "1"})
    except httpx.HTTPError as e:
        raise WebDavError(f"读取云端目录失败：{e}") from e
    if r.status_code == 404:
        return []
    _check_auth_or_raise(r, "读取云端目录")
    return _parse_propfind(r.text, dir_url)


def _is_safe_backup_name(name: str) -> bool:
    if not name or "/" in name or "\\" in name or ".." in name:
        return False
    return name.startswith(BACKUP_PREFIX) and name.endswith(BACKUP_SUFFIX)


def read_latest_manifest(client: httpx.Client, dir_url: str) -> dict | None:
    """读取云端 latest.json；不存在返回 None。"""
    try:
        r = client.get(dir_url + LATEST_MANIFEST)
    except httpx.HTTPError as e:
        raise WebDavError(f"读取备份清单失败：{e}") from e
    if r.status_code == 404:
        return None
    _check_auth_or_raise(r, "读取备份清单")
    try:
        data = r.json()
    except ValueError as e:
        raise WebDavError("云端备份清单（latest.json）格式损坏") from e
    if not isinstance(data, dict) or not _is_safe_backup_name(str(data.get("filename") or "")):
        raise WebDavError("云端备份清单（latest.json）内容无效")
    return data


def test_connection(
    url: str,
    username: str,
    password: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """测试 WebDAV 连通性与目录权限，并回显云端最近备份信息。"""
    client, dir_url = _make_client(url, username, password, transport=transport)
    with client:
        ensure_dir(client, dir_url)
        latest = read_latest_manifest(client, dir_url)
    return {"ok": True, "dir_url": dir_url, "latest": latest}


def pack_shelf_zip(
    shelf_root: Path,
    zip_path: Path,
    *,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> tuple[int, int]:
    """把整个书架目录打包为 zip（含视频章节与封面）。"""
    root = Path(shelf_root)
    if not root.exists():
        raise WebDavError("书架目录不存在，无法打包")

    files: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".tmp"):
                continue
            files.append(Path(dirpath) / fn)

    if not files:
        raise WebDavError("书架目录为空，没有可备份的内容")

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    total = len(files)
    with zipfile.ZipFile(zip_path, "w", allowZip64=True) as zf:
        for idx, fp in enumerate(files, 1):
            if should_cancel and should_cancel():
                raise WebDavCancelled()
            rel = fp.relative_to(root).as_posix()
            ctype = zipfile.ZIP_STORED if fp.suffix.lower() in _STORED_EXTS else zipfile.ZIP_DEFLATED
            zf.write(fp, arcname=rel, compress_type=ctype)
            if on_progress:
                on_progress(idx, total, rel)

    return total, zip_path.stat().st_size


def _iter_file_chunks(
    path: Path,
    total_bytes: int,
    should_cancel: Callable[[], bool] | None,
    on_progress: Callable[[int, int, str], None] | None,
):
    sent = 0
    with open(path, "rb") as f:
        while True:
            if should_cancel and should_cancel():
                raise WebDavCancelled()
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            sent += len(chunk)
            if on_progress:
                on_progress(sent, total_bytes, path.name)
            yield chunk


def upload_backup(
    url: str,
    username: str,
    password: str,
    shelf_root: Path,
    tmp_dir: Path,
    *,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[str, int, int, str], None] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """打包书架并上传到 WebDAV，完成后更新 latest.json 并清理旧包。"""
    client, dir_url = _make_client(url, username, password, transport=transport)
    ts = datetime.now(timezone.utc).astimezone()
    filename = f"{BACKUP_PREFIX}{ts.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}{BACKUP_SUFFIX}"
    local_zip = Path(tmp_dir) / filename

    try:
        with client:
            ensure_dir(client, dir_url)

            def pack_cb(done: int, total: int, name: str) -> None:
                if on_progress:
                    on_progress("packing", done, total, name)

            file_count, zip_size = pack_shelf_zip(
                shelf_root, local_zip, should_cancel=should_cancel, on_progress=pack_cb
            )

            def up_cb(done: int, total: int, name: str) -> None:
                if on_progress:
                    on_progress("uploading", done, total, name)

            try:
                r = client.put(
                    dir_url + filename,
                    content=_iter_file_chunks(local_zip, zip_size, should_cancel, up_cb),
                    headers={"Content-Length": str(zip_size)},
                )
            except WebDavCancelled:
                raise
            except httpx.HTTPError as e:
                raise WebDavError(f"上传备份失败：{e}") from e
            _check_auth_or_raise(r, "上传备份")

            manifest = {
                "filename": filename,
                "created_at": ts.isoformat(timespec="seconds"),
                "size": zip_size,
                "files": file_count,
            }
            try:
                mr = client.put(
                    dir_url + LATEST_MANIFEST,
                    content=json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
            except httpx.HTTPError as e:
                raise WebDavError(f"写入备份清单失败：{e}") from e
            _check_auth_or_raise(mr, "写入备份清单")

            # 清理其余旧包（含历史断网残包）；单条删除失败不推翻本次已成功的备份
            try:
                for entry in list_dir(client, dir_url):
                    name = entry["name"]
                    if not entry["is_dir"] and _is_safe_backup_name(name) and name != filename:
                        try:
                            client.delete(dir_url + name)
                        except httpx.HTTPError:
                            pass
            except WebDavError:
                pass

            return manifest
    finally:
        try:
            if local_zip.exists():
                local_zip.unlink()
        except OSError:
            pass


def _safe_extract_zip(zip_path: Path, stage_dir: Path, should_cancel=None) -> int:
    """校验压缩包成员路径安全性并解压到临时目录，返回文件数。"""
    stage_resolved = stage_dir.resolve()
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = zf.infolist()
            count = 0
            for info in infos:
                if should_cancel and should_cancel():
                    raise WebDavCancelled()
                if info.is_dir():
                    continue
                target = (stage_dir / info.filename).resolve()
                if target != stage_resolved and stage_resolved not in target.parents:
                    raise WebDavError(f"备份包内含非法路径：{info.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK_SIZE)
                count += 1
            return count
    except zipfile.BadZipFile as e:
        raise WebDavError("下载的备份包已损坏，无法解压") from e


def download_and_restore(
    url: str,
    username: str,
    password: str,
    target_dir: Path,
    tmp_dir: Path,
    *,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[str, int, int, str], None] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """从 WebDAV 下载最新备份并恢复到 target_dir（已有非空目录自动旁移留档）。"""
    client, dir_url = _make_client(url, username, password, transport=transport)
    target = Path(target_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_zip = Path(tmp_dir) / f"restore_{stamp}.zip"
    stage_dir = target.parent / f".asase_restore_stage_{stamp}"
    rollback_dir: Path | None = None

    try:
        with client:
            ensure_dir(client, dir_url)
            latest = read_latest_manifest(client, dir_url)
            if not latest:
                raise WebDavError("云端暂无备份（未找到 latest.json）")
            filename = latest["filename"]
            expected_size = int(latest.get("size") or 0)

            local_zip.parent.mkdir(parents=True, exist_ok=True)
            try:
                with client.stream("GET", dir_url + filename) as r:
                    _check_auth_or_raise(r, "下载备份")
                    total = int(r.headers.get("Content-Length") or expected_size or 0)
                    done = 0
                    with open(local_zip, "wb") as f:
                        for chunk in r.iter_bytes(CHUNK_SIZE):
                            if should_cancel and should_cancel():
                                raise WebDavCancelled()
                            f.write(chunk)
                            done += len(chunk)
                            if on_progress:
                                on_progress("downloading", done, total, filename)
            except WebDavCancelled:
                raise
            except httpx.HTTPError as e:
                raise WebDavError(f"下载备份失败：{e}") from e

        if on_progress:
            on_progress("extracting", 0, 1, filename)
        stage_dir.mkdir(parents=True, exist_ok=True)
        file_count = _safe_extract_zip(local_zip, stage_dir, should_cancel=should_cancel)

        # 已有非空目录：先旁移留档，再把暂存目录上位，保证不发生半覆盖
        if target.exists() and any(target.iterdir()):
            cand = target.parent / f"{target.name}_恢复前备份_{stamp}"
            idx = 1
            while cand.exists():
                cand = target.parent / f"{target.name}_恢复前备份_{stamp}_{idx}"
                idx += 1
            try:
                target.rename(cand)
            except OSError as e:
                raise WebDavError(f"无法留档当前书架目录（可能有文件被占用）：{e}") from e
            rollback_dir = cand
        elif target.exists():
            try:
                target.rmdir()
            except OSError:
                pass

        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            stage_dir.rename(target)
        except OSError as e:
            # 上位失败时尽量把原书架移回原位
            if rollback_dir is not None and rollback_dir.exists() and not target.exists():
                try:
                    rollback_dir.rename(target)
                except OSError:
                    pass
            raise WebDavError(f"恢复目录替换失败：{e}") from e

        return {
            "filename": filename,
            "created_at": latest.get("created_at", ""),
            "size": latest.get("size", 0),
            "files": file_count,
            "rollback_dir": str(rollback_dir) if rollback_dir else "",
        }
    finally:
        try:
            if local_zip.exists():
                local_zip.unlink()
        except OSError:
            pass
        try:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
        except OSError:
            pass
