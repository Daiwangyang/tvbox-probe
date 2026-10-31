#!/usr/bin/env python3
"""从镜像拉取种子源清单,写入 seeds.txt。

raw.githubusercontent.com 在国内经常握不上手,所以按顺序尝试多个加速镜像。
"""
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "seeds.txt"

PATH = "TVboxorg/TVbox/main/sources.yaml"
MIRRORS = [
    f"https://cdn.jsdelivr.net/gh/{PATH}",
    f"https://gh-proxy.com/https://raw.githubusercontent.com/{PATH}",
    f"https://ghfast.top/https://raw.githubusercontent.com/{PATH}",
    f"https://raw.githubusercontent.com/{PATH}",
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tvbox-probe/1.0"


def grab(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def parse_yaml(text: str) -> list[tuple[str, str, str]]:
    """极简解析 sources.yaml,只取 name/url/group。"""
    out, cur = [], {}
    for line in text.splitlines():
        if line.startswith("  - id:"):
            if cur.get("url"):
                out.append((cur.get("name", "?"), cur["url"], cur.get("group", "?")))
            cur = {}
            continue
        m = re.match(r"\s+(name|url|group|enabled):\s*(.*)$", line)
        if m and cur is not None:
            k, v = m.group(1), m.group(2).strip().strip('"')
            cur[k] = v
    if cur.get("url"):
        out.append((cur.get("name", "?"), cur["url"], cur.get("group", "?")))
    return out


def main() -> int:
    for m in MIRRORS:
        try:
            text = grab(m)
        except Exception as e:
            print(f"  x {type(e).__name__} {m[:70]}")
            continue
        items = parse_yaml(text)
        if len(items) < 5:
            print(f"  ? 内容太短,疑似镜像返回错误页: {m[:70]}")
            continue
        lines = [f"# 种子源 {len(items)} 条,来自 {m}"]
        lines += [f"{url}  # {name}" for name, url, _ in items]
        OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  + 写入 {len(items)} 条 -> {OUT}")
        return 0
    print("所有镜像都失败了,请手动编辑 seeds.txt")
    return 1


if __name__ == "__main__":
    sys.exit(main())
