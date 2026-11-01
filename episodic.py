#!/usr/bin/env python3
"""对单站抽多集实测,判断"播不了"是单集问题还是整站问题。"""
import json
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, ".")
import playtest  # noqa: E402
from cmsprobe import fetch  # noqa: E402


def qs(b, **e):
    p = urllib.parse.urlsplit(b)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k not in e]
    q += list(e.items())
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path,
                                    urllib.parse.urlencode(q), ""))


def eps(api, n=3):
    _, _, b = fetch(qs(api, ac="videolist", wd="庆余年", pg="1"), 15)
    t = b.decode("utf-8", "ignore").replace("\\/", "/")
    ids = re.findall(r'"vod_id":\s*(\d+)', t)
    if not ids:
        return []
    _, _, d = fetch(qs(api, ac="detail", ids=ids[0]), 15)
    dt = d.decode("utf-8", "ignore").replace("\\/", "/")
    m = re.search(r'"vod_play_url":\s*"([^"]+)"', dt)
    if not m:
        return []
    out = []
    for part in m.group(1).split("#"):
        if "$" in part:
            out.append(part.split("$", 1)[1])
    return out[:n]


def main():
    cands = sys.argv[1:]
    if not cands:
        cands = ["https://api.maoyanapi.top/api.php/provide/vod/",
                 "http://caiji.dyttzyapi.com/api.php/provide/vod",
                 "http://154.219.117.232:9981/jacloudapi.php/provide/vod/"]
    for api in cands:
        urls = eps(api)
        if not urls:
            print(f"{api[:50]} 抽不到地址")
            continue
        with ThreadPoolExecutor(max_workers=3) as pool:
            rs = list(pool.map(playtest.play_ok, urls))
        txt = " | ".join(f"{r['frames']}帧{r['reason'][:24]}" for r in rs)
        ok = sum(1 for r in rs if r["ok"])
        print(f"{api[:50]:52} {ok}/{len(rs)} 可播  {txt}")


if __name__ == "__main__":
    main()
