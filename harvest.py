#!/usr/bin/env python3
"""从公开配置里挖出所有直连 CMS api,去掉已有的,产出新候选清单。

思路:别人维护的配置里躺着大量采集站地址,比凭记忆猜域名命中率高得多。
展开多仓(纯文本里一堆 url 的那种),收集 sites 里 type 0/1 的 api,
以及 ext.api 这种藏在扩展字段里的直连接口。
"""
from __future__ import annotations

import json
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe  # noqa: E402

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "candidates2.txt"


def host_of(api: str) -> str:
    try:
        return (urllib.parse.urlsplit(api).hostname or "").lower()
    except Exception:
        return ""


def collect(cfg: dict, src: str, acc: list[dict]) -> None:
    sites = cfg.get("sites")
    if not isinstance(sites, list):
        return
    for s in sites:
        if not isinstance(s, dict):
            continue
        ext = s.get("ext")
        ex_api = ext.get("api") if isinstance(ext, dict) else ""
        for api in (s.get("api") or "", ex_api or ""):
            if not isinstance(api, str) or not api.startswith("http"):
                continue
            acc.append({"name": s.get("name") or "?", "api": api, "src": src,
                        "type": s.get("type", 0)})


def main() -> None:
    seeds = []
    for fn in ("seeds.txt", "seeds_extra.txt"):
        p = ROOT / fn
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            seeds.append(line.split("#", 1)[0].strip())
    seeds = list(dict.fromkeys(seeds))
    print(f"种子 {len(seeds)} 条")

    acc: list[dict] = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        cfgs = list(ex.map(probe.load_config, seeds))
    sub: list[str] = []
    for s, cfg in zip(seeds, cfgs):
        if not cfg:
            continue
        if cfg.get("_links"):
            sub += cfg["_links"]
            continue
        collect(cfg, s, acc)
    sub = list(dict.fromkeys(sub))[:200]
    print(f"子仓 {len(sub)} 条")
    if sub:
        with ThreadPoolExecutor(max_workers=12) as ex:
            for s, cfg in zip(sub, ex.map(probe.load_config, sub)):
                if cfg and not cfg.get("_links"):
                    collect(cfg, s, acc)

    # 去重:同 host+path 视作同一站
    uniq: dict[str, dict] = {}
    for a in acc:
        k = urllib.parse.urlsplit(a["api"]).netloc.lower() + \
            urllib.parse.urlsplit(a["api"]).path.rstrip("/")
        if k not in uniq:
            uniq[k] = a
    print(f"配置里共 {len(acc)} 条 api,去重 {len(uniq)}")

    # 去掉已经在 my.json 里的 host
    have = set()
    if (ROOT / "my.json").exists():
        for s in json.loads((ROOT / "my.json").read_text(encoding="utf-8")).get("sites", []):
            h = host_of(s.get("api", ""))
            if h:
                have.add(h)
    cand = [a for a in uniq.values() if host_of(a["api"]) not in have]
    print(f"新 host 候选 {len(cand)}")
    lines = ["# 从公开配置挖出的新候选"]
    lines += [f"{a['api']}    # {a['name'][:20]}" for a in cand]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
