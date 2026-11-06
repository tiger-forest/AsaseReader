"""登录凭据（Cookie）管理：**只活在内存里**。

设计约束（不可违反）：
- 凭据**绝不落盘** —— 不写 config、不写日志、不写任何文件；
- 进程退出即消失，下次启动要重新粘贴一次；
- 对外只暴露「已配置 / 几个字段 / 有没有 SESSDATA」这类摘要，
  **任何方法都不返回凭据原文**，连 __repr__ 也不行（日志会打印对象）；
- 用途单一：只在抓取 opus 页面时作为请求头带上；图片下载一律不带。

为什么不用加密落盘：本机加密（DPAPI）只能防"别人翻你的文件"，
防不住"以你的账号运行的程序"——后者能用同一身份解开。
只有不落盘才是真的不落盘。
"""

from __future__ import annotations

REQUIRED_KEY = "SESSDATA"


class Credentials:
    """内存中的登录凭据。"""

    def __init__(self) -> None:
        self._cookie = ""

    # ---- 写入 ----

    def set(self, raw: str) -> tuple[bool, str]:
        """保存凭据。返回 (是否成功, 提示文案)。失败时不保留任何内容。"""
        text = (raw or "").strip()
        # 用户常常整行复制 "Cookie: xxx=1; yyy=2"，也常带换行，先清理
        for prefix in ("cookie:", "Cookie:"):
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].strip()
        text = " ".join(text.split())
        if not text:
            self._cookie = ""
            return False, "Cookie 是空的"

        keys = self.parse_keys(text)
        if REQUIRED_KEY not in keys:
            self._cookie = ""
            return (
                False,
                f"这段 Cookie 里没有 {REQUIRED_KEY}。"
                "请从浏览器的请求头里复制完整的一整串（含 SESSDATA）",
            )
        self._cookie = text
        return True, f"已保存（{len(keys)} 个字段，本次运行有效）"

    def clear(self) -> None:
        self._cookie = ""

    # ---- 读取 ----

    @property
    def cookie(self) -> str:
        """原始 Cookie 串。调用方只应把它放进请求头，不要打印、不要存。"""
        return self._cookie

    @property
    def configured(self) -> bool:
        return bool(self._cookie)

    def summary(self) -> dict:
        """给前端的摘要：只有统计信息，不含任何值。"""
        keys = self.parse_keys(self._cookie) if self._cookie else []
        return {
            "configured": bool(self._cookie),
            "fields": len(keys),
            "has_sessdata": REQUIRED_KEY in keys,
        }

    # ---- 工具 ----

    @staticmethod
    def parse_keys(raw: str) -> list[str]:
        """只提炼 Cookie 的键名 —— 用于校验和显示，值一律不碰。"""
        keys: list[str] = []
        for part in (raw or "").split(";"):
            k = part.split("=", 1)[0].strip()
            if k:
                keys.append(k)
        return keys

    def __repr__(self) -> str:
        # 绝不能让 str(credentials) 或日志打印出凭据原文
        s = self.summary()
        return f"<Credentials configured={s['configured']} fields={s['fields']}>"

    __str__ = __repr__
