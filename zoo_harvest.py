#!/usr/bin/env python3
"""从 zoo.ink 这类"接口汇总页"给的配置里,挖出可直连的 CMS api。

和 harvest.py 的差别:
  - 种子来自 zoo_seeds.txt(单个汇总页)
  - 中文域名要转 punycode,中文 query 要 percent-encode,否则 DNS/HTTP 直接失败
  - 只收 type 0/1 且 api 是 http(s) 的,spider/网盘线路一概不进
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe  # noqa: E402
from harvest import collect  # noqa: E402

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "zoo_candidates.txt"


def fix_url(u: str) -> str:
    """中文域名转 punycode,中文 path/query 转 percent-encoding。"""
    u = u.strip()
    p = urllib.parse.urlsplit(u)
    host = p.hostname or ""
    try:
        host = host.encode("idna").decode("ascii")
    except Exception:
        pass
    netloc = host
    if p.port:
        netloc = f"{host}:{p.port}"
    if p.username:
        netloc = f"{p.username}@{netloc}"
    path = urllib.parse.quote(p.path, safe="/%:@&=+$,~")
    query = urllib.parse.quote(p.query, safe="=&%:@+,$/")
    return urllib.parse.urlunsplit((p.scheme, netloc, path, query, ""))


def host_of(api: str) -> str:
    try:
        return (urllib.parse.urlsplit(api).hostname or "").lower()
    except Exception:
        return ""


def main() -> None:
    seeds: list[str] = []
    for line in (ROOT / "zoo_seeds.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        u = re.split(r"\s+#\s*", line, 1)[0].strip()
        if u.startswith("http"):
            seeds.append(fix_url(u))
    seeds = list(dict.fromkeys(seeds))
    print(f"种子 {len(seeds)} 条")

    acc: list[dict] = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        cfgs = list(ex.map(probe.load_config, seeds))
    sub: list[str] = []
    ok_seed = 0
    for s, cfg in zip(seeds, cfgs):
        if not cfg:
            print(f"  ✗ 抓不到  {s}")
            continue
        if cfg.get("_links"):
            sub += cfg["_links"]
            print(f"  ~ 多仓 {len(cfg['_links'])} 条  {s}")
            continue
        if cfg.get("_gate"):
            print(f"  ⊘ 引流页  {s}")
            continue
        n0 = len(acc)
        collect(cfg, s, acc)
        ok_seed += 1
        print(f"  ✓ sites={len(cfg.get('sites') or [])} 挖到{len(acc) - n0}  {s}")

    sub = list(dict.fromkeys([fix_url(u) for u in sub if u.startswith("http")]))[:300]
    print(f"\n多仓展开 {len(sub)} 条")
    if sub:
        with ThreadPoolExecutor(max_workers=12) as ex:
            for s, cfg in zip(sub, ex.map(probe.load_config, sub)):
                if cfg and not cfg.get("_links"):
                    collect(cfg, s, acc)

    uniq: dict[str, dict] = {}
    for a in acc:
        p = urllib.parse.urlsplit(a["api"])
        k = (p.netloc or "").lower() + p.path.rstrip("/")
        if k not in uniq:
            uniq[k] = a
    print(f"配置里共 {len(acc)} 条 api,同 host+path 去重后 {len(uniq)}")

    have = set()
    if (ROOT / "my.json").exists():
        for s in json.loads((ROOT / "my.json").read_text(encoding="utf-8")).get("sites", []):
            h = host_of(s.get("api", ""))
            if h:
                have.add(h)
    cand = [a for a in uniq.values() if host_of(a["api"]) not in have]
    print(f"排除已在 my.json 的 host 后,新候选 {len(cand)}")

    lines = ["# 从 zoo.ink 接口页挖出的新候选"]
    lines += [f"{a['api']}    # {a['name'][:24]} | {host_of(a['api'])}" for a in cand]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
