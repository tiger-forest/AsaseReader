"""WebDAV 单向备份与恢复测试（基于 httpx.MockTransport，完全离线）。

覆盖：
- 连接测试（含自动 MKCOL、Depth: 1 校验、读取 latest.json、401 鉴权失败）；
- 整包打包与上传（含视频章节、latest.json 指针、清理旧包与断网残包、本地临时 zip 清理）；
- 上传中途失败时不覆盖 latest.json 与旧包；
- 恢复流程（空目录直接恢复、已有书库自动旁移留档、非法 .. 路径拒绝）；
- Backend 集成（config 持久化、写入冻结闸门、缓存重载、首启引导恢复）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402

ensure_tmp_env()

from asase_reader.backend import Backend  # noqa: E402
from asase_reader.store import write_meta  # noqa: E402
from asase_reader.webdav import (  # noqa: E402
    BACKUP_SUBDIR,
    LATEST_MANIFEST,
    WebDavError,
    download_and_restore,
    test_connection,
    upload_backup,
)

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        fails.append(msg)


class FakeWebDavServer:
    """内存 WebDAV 模拟器：记录请求、校验 Basic Auth 与 Depth: 1。"""

    def __init__(self, user: str = "u1", pwd: str = "p1") -> None:
        self.user = user
        self.pwd = pwd
        self.dirs: set[str] = {"/dav"}
        self.files: dict[str, bytes] = {}
        self.depths: list[str | None] = []
        self.fail_next_put_zip = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        # 校验 Basic Auth
        import base64
        expected = "Basic " + base64.b64encode(f"{self.user}:{self.pwd}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, text="Unauthorized")

        path = urlparse(str(request.url)).path
        method = request.method.upper()

        if method == "PROPFIND":
            self.depths.append(request.headers.get("depth"))
            clean = path.rstrip("/")
            if clean not in self.dirs:
                return httpx.Response(404, text="Not Found")
            xml_items = [
                f"<D:response><D:href>{clean}/</D:href><D:propstat><D:prop>"
                f"<D:resourcetype><D:collection/></D:resourcetype>"
                f"</D:prop></D:propstat></D:response>"
            ]
            prefix = clean + "/"
            for fpath, data in sorted(self.files.items()):
                if fpath.startswith(prefix) and "/" not in fpath[len(prefix):]:
                    xml_items.append(
                        f"<D:response><D:href>{fpath}</D:href><D:propstat><D:prop>"
                        f"<D:getcontentlength>{len(data)}</D:getcontentlength>"
                        f"<D:resourcetype/></D:prop></D:propstat></D:response>"
                    )
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<D:multistatus xmlns:D="DAV:">' + "".join(xml_items) + "</D:multistatus>"
            )
            return httpx.Response(207, text=body, headers={"Content-Type": "application/xml"})

        if method == "MKCOL":
            clean = path.rstrip("/")
            if clean in self.dirs:
                return httpx.Response(405, text="Already exists")
            self.dirs.add(clean)
            return httpx.Response(201, text="Created")

        if method == "PUT":
            if self.fail_next_put_zip and path.endswith(".zip"):
                self.fail_next_put_zip = False
                # 模拟断网留下半截坏包
                self.files[path] = b"PARTIAL_BROKEN_ZIP"
                raise httpx.ConnectError("simulated network drop")
            self.files[path] = request.read()
            return httpx.Response(201, text="Created")

        if method == "GET":
            if path not in self.files:
                return httpx.Response(404, text="Not Found")
            data = self.files[path]
            return httpx.Response(200, content=data, headers={"Content-Length": str(len(data))})

        if method == "DELETE":
            if path not in self.files:
                return httpx.Response(404, text="Not Found")
            del self.files[path]
            return httpx.Response(204)

        return httpx.Response(405)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


TMP = Path(tempfile.mkdtemp(prefix="asase_wd_"))
assert_tmp_guard(TMP, "TMP")


def _make_shelf(root: Path, title: str = "测试作品", with_video: bool = True) -> None:
    cid = "c_wd01"
    ch_dir = root / "chapters" / cid
    (ch_dir / "images").mkdir(parents=True, exist_ok=True)
    (ch_dir / "images" / "001.jpg").write_bytes(b"\xff\xd8\xff\xe0imgdata")
    meta = {
        "source": "video" if with_video else "local",
        "title": "第1话",
        "author": {"name": "作者"},
        "images": [{"index": 1, "file": "images/001.jpg", "width": 100, "height": 100}],
    }
    if with_video:
        (ch_dir / "video.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"V" * 128)
        meta["video"] = {"file": "video.mp4", "ext": ".mp4"}
    write_meta(ch_dir, meta)
    lib_data = {
        "version": 2,
        "chapters": [{"id": cid, "dir": f"chapters/{cid}", "title": "第1话",
                      "author": "作者", "source": meta["source"]}],
        "works": [{"id": "w_wd01", "name": title, "author": "作者",
                   "chapters": [cid], "tags": ["测试"]}],
    }
    (root / "library.json").write_text(json.dumps(lib_data, ensure_ascii=False), encoding="utf-8")


# 1. 测试连接：自动 MKCOL、Depth: 1、鉴权失败
srv = FakeWebDavServer("u1", "p1")
tr = srv.transport()
res = test_connection("https://example.com/dav/", "u1", "p1", transport=tr)
check(res["ok"] is True, "test_connection 成功")
check(f"/dav/{BACKUP_SUBDIR}" in srv.dirs, "test_connection 自动 MKCOL 创建专属子目录")
check(res["latest"] is None, "初次连接云端暂无备份")
check(all(d == "1" for d in srv.depths), "所有 PROPFIND 固定 Depth: 1（不用 infinity）")

res_bad = None
try:
    test_connection("https://example.com/dav/", "u1", "wrong", transport=tr)
except WebDavError as e:
    res_bad = str(e)
check(res_bad is not None and "认证" in res_bad, "密码错误抛出明确认证失败")

# 2. 上传备份：含视频章节 + latest.json + 清理旧包与残包
shelf1 = TMP / "shelf1"
tmp1 = TMP / "tmp1"
_make_shelf(shelf1, title="第一版书架", with_video=True)
# 预先塞入一个历史旧包和半截残包，验证会被清理
srv.files[f"/dav/{BACKUP_SUBDIR}/asase_backup_20260101_000000.zip"] = b"OLD_ZIP"
srv.files[f"/dav/{BACKUP_SUBDIR}/asase_backup_20260102_000000.zip"] = b"BROKEN_ZIP"

m1 = upload_backup(
    "https://example.com/dav/", "u1", "p1", shelf1, tmp1, transport=tr
)
check(m1["filename"].startswith("asase_backup_") and m1["filename"].endswith(".zip"), "生成带时间戳文件名")
check(f"/dav/{BACKUP_SUBDIR}/{LATEST_MANIFEST}" in srv.files, "写入 latest.json")
zips_on_srv = [k for k in srv.files if k.endswith(".zip")]
check(zips_on_srv == [f"/dav/{BACKUP_SUBDIR}/{m1['filename']}"], "清理旧包与残包，云端只留最新一份")
check(not list(tmp1.glob("*.zip")) if tmp1.exists() else True, "上传后清理本地临时 zip")

# 验证包内含视频文件与 library.json，不含 config.json
uploaded_zip = TMP / "inspect.zip"
uploaded_zip.write_bytes(srv.files[zips_on_srv[0]])
with zipfile.ZipFile(uploaded_zip, "r") as zf:
    names = set(zf.namelist())
check("library.json" in names, "备份包包含 library.json")
check("chapters/c_wd01/video.mp4" in names, "备份包包含视频章节文件")
check("config.json" not in names, "备份包不含本机 config.json")

# 3. 上传中途断网：旧 latest.json 与旧完整包不受损
srv.fail_next_put_zip = True
err_drop = None
try:
    upload_backup("https://example.com/dav/", "u1", "p1", shelf1, tmp1, transport=tr)
except WebDavError:
    err_drop = True
check(err_drop is True, "上传断网抛出 WebDavError")
manifest_after_drop = json.loads(srv.files[f"/dav/{BACKUP_SUBDIR}/{LATEST_MANIFEST}"].decode())
check(manifest_after_drop["filename"] == m1["filename"], "断网后 latest.json 仍指向上一份完整备份")

# 4. 恢复到空目录 + 恢复到已有书库（自动旁移留档）
restore_empty = TMP / "restore_empty"
r_empty = download_and_restore(
    "https://example.com/dav/", "u1", "p1", restore_empty, tmp1, transport=tr
)
check((restore_empty / "library.json").is_file(), "恢复到空目录成功")
check((restore_empty / "chapters" / "c_wd01" / "video.mp4").is_file(), "视频章节一起恢复")
check(r_empty["rollback_dir"] == "", "空目录恢复不产生旁移留档目录")

restore_existing = TMP / "restore_existing"
_make_shelf(restore_existing, title="本地旧书架", with_video=False)
(restore_existing / "local_marker.txt").write_text("keep_me_in_rollback", encoding="utf-8")
r_exist = download_and_restore(
    "https://example.com/dav/", "u1", "p1", restore_existing, tmp1, transport=tr
)
rb = Path(r_exist["rollback_dir"])
check(rb.is_dir() and (rb / "local_marker.txt").is_file(), "已有书库恢复前自动旁移留档")
restored_lib = json.loads((restore_existing / "library.json").read_text(encoding="utf-8"))
check(restored_lib["works"][0]["name"] == "第一版书架", "恢复后书库内容已替换")

# 5. 非法路径穿越拦截
bad_zip_name = "asase_backup_20261125_999999.zip"
bad_zip_bytes_io = TMP / "bad.zip"
with zipfile.ZipFile(bad_zip_bytes_io, "w") as zf:
    zf.writestr("../evil.txt", "boom")
srv.files[f"/dav/{BACKUP_SUBDIR}/{bad_zip_name}"] = bad_zip_bytes_io.read_bytes()
srv.files[f"/dav/{BACKUP_SUBDIR}/{LATEST_MANIFEST}"] = json.dumps({"filename": bad_zip_name}).encode()
bad_caught = False
try:
    download_and_restore("https://example.com/dav/", "u1", "p1", TMP / "r_bad", tmp1, transport=tr)
except WebDavError as e:
    bad_caught = "非法路径" in str(e)
check(bad_caught, "拒绝包含 .. 路径穿越的备份包")
# 恢复 manifest 指向 m1 供下面 Backend 测试使用
srv.files[f"/dav/{BACKUP_SUBDIR}/{LATEST_MANIFEST}"] = json.dumps(m1).encode()

# 6. Backend 集成：配置持久化 + 写入冻结闸门 + 首启恢复重载
state_dir = TMP / "state"
state_dir.mkdir()
b = Backend(state_dir=state_dir)
b._webdav_transport = tr

# update_config 持久化 WebDAV 凭据（不触发 PATH_KEYS 目录校验）
uc = b.update_config({
    "webdav_url": "https://example.com/dav/",
    "webdav_username": "u1",
    "webdav_password": "p1",
})
check(uc["ok"] is True, "update_config 保存 WebDAV 凭据成功")
saved_cfg = json.loads((state_dir / "config.json").read_text(encoding="utf-8"))
check(saved_cfg["webdav_password"] == "p1", "WebDAV 密码按约定本地落盘")

# 首启恢复（setup_done=False）：恢复到新目录并自动提交 setup_done=True + 重载 Library
boot_shelf = TMP / "boot_shelf"
rs = b.webdav_restore_start(str(boot_shelf))
check(rs["ok"] is True, "首启引导调用 webdav_restore_start 成功")
for _ in range(100):
    if not b.webdav_status()["running"]:
        break
    time.sleep(0.05)
st = b.webdav_status()
check(st["finished"] and not st["error"], f"后台恢复完成（error={st.get('error')}）")
check(b.config.get("setup_done") is True and b.shelf_root() == boot_shelf, "首启恢复自动完成 setup_done 并切换 shelf_root")
works = b.list_works()["works"]
check(len(works) == 1 and works[0]["name"] == "第一版书架", "恢复后缓存已作废重载，list_works 立即读到恢复作品")

# 写入冻结闸门：冻结期间 UI 写操作返回 ok=False，且 Library.readonly=True
b._set_library_frozen(True)
frozen_res = b.create_work("冻结期间", "作者")
check(frozen_res.get("ok") is False, "冻结期间 create_work 被拦下")
check(b.library.readonly is True, "冻结期间 Library.readonly 为 True（后台 save 不落盘）")
b._set_library_frozen(False)
check(b.library.readonly is False, "解冻后 Library.readonly 恢复 False")

print(f"\n{'PASS' if not fails else 'FAIL'} ({len(fails)} failures)")
sys.exit(1 if fails else 0)
