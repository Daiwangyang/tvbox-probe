#!/usr/bin/env python3
"""对「现有直连站 + 新候选」做一次统一定级,产出排序用的权威数据。

和 cmsprobe 的区别:这个脚本还会抽一条真实剧集,看播放地址是不是 m3u8 直链、
以及详情接口要多久 —— 决定这站点了之后会不会转圈。

输出 rank.json:
  api / ms / total / search / detail_ms / play_kind / play_ms
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cmsprobe import fetch, parse_list  # noqa: E402

ROOT = Path(__file__).resolve().parent
KW = "庆余年"


def qs(base: str, **extra) -> str:
    p = urllib.parse.urlsplit(base)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k not in extra]
    q += list(extra.items())
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path,
                                    urllib.parse.urlencode(q), ""))


def best_ms(url: str, n: int = 2) -> tuple[int, bytes]:
    best, body = 99999, b""
    for _ in range(n):
        st, ms, b = fetch(url)
        if st == 200 and b:
            if ms < best:
                best, body = round(ms), b
    return (best if body else -1), body


def rank(api: str, name: str = "") -> dict:
    api = api if api.endswith("/") else api + "/"
    r: dict = {"api": api, "name": name}
    ms, body = best_ms(qs(api, ac="list"))
    r["ms"] = ms
    if ms < 0:
        r["ok"] = False
        return r
    total, cls, _ = parse_list(body)
    r["total"], r["classes"] = total, cls

    # 搜索:影视仓打字搜索走这条
    sms, sbody = best_ms(qs(api, ac="videolist", wd=KW, pg="1"), 1)
    stot, _sc, _ = parse_list(sbody) if sbody else (0, 0, "")
    r["search_ms"], r["search_hits"] = sms, stot

    # 抽一条详情看播放地址
    if sbody:
        txt = sbody.decode("utf-8", "ignore")
        ids = re.findall(r'"?vod_id"?\s*[:=]\s*"?(\d+)', txt)
        if not ids:
            ids = re.findall(r'<id>(\d+)</id>', txt)
        if ids:
            dms, dbody = best_ms(qs(api, ac="detail", ids=ids[0]), 1)
            dtxt = dbody.decode("utf-8", "ignore").replace("\\/", "/")
            r["detail_ms"] = dms
            # 播放地址都在 vod_play_url 里,形如 第01集$https://x.m3u8#第02集$...
            m2 = re.search(r'"?vod_play_url"?\s*:\s*"([^"]+)"', dtxt)
            if not m2:
                m2 = re.search(r"<dd[^>]*>(.*?)</dd>", dtxt, re.S)
            urls = re.findall(r"https?://[^\s#$\]]+\.[a-z0-9]{2,5}(?:\?[^\s#$\]]*)?",
                              (m2.group(1) if m2 else ""))
            r["episodes"] = len(urls)
            kinds = set()
            for u in urls[:8]:
                if ".m3u8" in u:
                    kinds.add("m3u8")
                elif ".mp4" in u:
                    kinds.add("mp4")
                elif "/share/" in u or "pan." in u:
                    kinds.add("网盘")
                else:
                    kinds.add("解析")
            r["play_kind"] = ",".join(sorted(kinds)) or "无"
    r["ok"] = r.get("total", 0) > 0 and r.get("search_hits", 0) > 0
    return r


def load_seeds() -> list[tuple[str, str]]:
    out: dict[str, str] = {}
    my = ROOT / "my.json"
    if my.exists():
        for s in json.loads(my.read_text(encoding="utf-8")).get("sites", []):
            api = (s.get("api") or "").strip()
            if s.get("type", 0) in (0, 1) and api.startswith("http") \
                    and "cf.ddngo.space" not in api:
                out[api] = s.get("name", "?")
    for p in sorted(ROOT.glob("cmsprobe*.json")):
        for r in json.loads(p.read_text(encoding="utf-8")):
            if r.get("ok") and r.get("api"):
                out.setdefault(r["api"], r.get("note", ""))
    return list(out.items())


def main() -> None:
    seeds = load_seeds()
    print(f"待定级 {len(seeds)} 个直连站")
    rows: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(rank, a, n): a for a, n in seeds}
        for f in as_completed(futs):
            done += 1
            try:
                rows.append(f.result())
            except Exception as e:
                rows.append({"api": futs[f], "ok": False, "ms": -1, "err": str(e)[:60]})
            if done % 10 == 0:
                print(f"  {done}/{len(seeds)}", flush=True)
    rows.sort(key=lambda x: (not x.get("ok"), x.get("ms", 99999)))
    (ROOT / "rank.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{'延迟':>6} {'片库':>8} {'搜索':>5} {'详情':>6} 播放类型   接口")
    for r in rows:
        print(f"{str(r.get('ms', '-')):>6} {str(r.get('total', 0)):>8} "
              f"{str(r.get('search_hits', 0)):>5} {str(r.get('detail_ms', '-')):>6} "
              f"{str(r.get('play_kind', '-')):10} {r['api'][:56]}")


if __name__ == "__main__":
    main()
