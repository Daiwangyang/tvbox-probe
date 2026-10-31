#!/usr/bin/env python3
"""校验 jar 到底支不支持某个 csp 站 —— 只看 jar 里有没有对应类。

背景:`api` 写成 `csp_DouDou`,真实类路径是 `com/github/catvod/spider/DouDou`。
按这个映射去 dex 的 type_ids 里找,找到才算这个 jar 能跑这个站。

不解析 dex 字节码,只读字符串索引,几毫秒的事。
"""
from __future__ import annotations

import io
import time
import struct
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe  # noqa: E402

CACHE = Path(__file__).resolve().parent / ".jarcache"


def dex_classes(blob: bytes) -> set[str]:
    """读 dex 的 type_ids,返回所有类描述符(如 com/github/catvod/spider/DouDou)。"""
    if blob[:4] != b"dex\n":
        return set()
    try:
        u4 = lambda o: struct.unpack("<I", blob[o:o + 4])[0]  # noqa: E731
        n_str, off_str = u4(0x38), u4(0x3C)
        n_type, off_type = u4(0x40), u4(0x44)
        if n_type > 300_000 or off_type + n_type * 4 > len(blob):
            return set()

        cache: dict[int, str] = {}

        def s_at(idx: int) -> str:
            if idx in cache:
                return cache[idx]
            so = u4(off_str + idx * 4)
            j, sh, ln = so, 0, 0
            while j < len(blob):
                x = blob[j]
                ln |= (x & 0x7F) << sh
                j += 1
                sh += 7
                if not x & 0x80:
                    break
            v = blob[j:j + ln].decode("utf-8", "replace")
            cache[idx] = v
            return v

        out = set()
        for i in range(n_type):
            d = s_at(u4(off_type + i * 4))
            if d.startswith(("Lcom/github/catvod/spider/", "Lcsp")):
                out.add(d.lstrip("L").rstrip(";"))
        return out
    except Exception:
        return set()


def jar_classes(url: str) -> tuple[set[str], bool]:
    """下载并列出 jar 内的 csp 类。返回 (类集合, 下载是否成功)。

    下载失败不能当成「没有这个类」,否则好站会被误杀。
    """
    CACHE.mkdir(exist_ok=True)
    slug = probe.site_key(url)
    f = CACHE / f"{slug}.txt"
    if f.exists():
        txt = f.read_text(encoding="utf-8")
        return set(txt.split()), not txt.startswith("!")

    # 有些 jar 被伪装成 .jpg/.png,按内容判而不是后缀
    raw, st = b"", 0
    for attempt in range(3):
        try:
            raw, st, _ = probe.fetch(url, timeout=45, limit=12_000_000, proxy=True)
            if st == 200 and raw[:2] == b"PK":
                break
        except Exception:
            st = 0
        time.sleep(1.5)

    found: set[str] = set()
    ok = st == 200 and raw[:2] == b"PK"
    if ok:
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
            for n in z.namelist():
                if n.endswith(".dex"):
                    found |= dex_classes(z.read(n))
        except zipfile.BadZipFile:
            ok = False

    if ok:
        _safe_write(f, " ".join(sorted(found)))
    else:
        _safe_write(f, "!")   # 失败也缓存,但带标记
    return found, ok


def _safe_write(f: Path, data: str) -> None:
    """并发下同一个 jar 可能被多个线程同时拉,写冲突不算错。"""
    try:
        f.write_text(data, encoding="utf-8")
    except (PermissionError, OSError):
        pass


def supports(jar_url: str, api: str) -> bool:
    """api 是 csp_Xxx 时,校验 jar 里有没有 spider/Xxx。非 csp 一律放行。"""
    if not api.startswith("csp_"):
        return True
    cls = f"com/github/catvod/spider/{api[4:]}"
    found, ok = jar_classes(jar_url)
    # 下载失败时放行,宁可留着让 App 自己试
    return True if not ok else cls in found


if __name__ == "__main__":
    jar = sys.argv[1]
    for api in sys.argv[2:]:
        print(f"{api}: {'有' if supports(jar, api) else '无'}")
