#!/usr/bin/env python3
"""直连苹果CMS(MacCMS v10)采集源体检器。

只测三件在真机上有意义的事:
  1. TTFB —— 打开列表页要多久(点开站点到出片单的体感)
  2. total —— 片库总量(资源全不全)
  3. 搜索 —— 能不能按关键词搜(影视仓搜索走 ac=videolist&wd=)

不测"配置能不能下载"这种假指标。输出按延迟升序,达标源直接可入 my.json。

用法: python cmsprobe.py candidates.txt [-o out.json]
候选文件一行一个 api 地址,# 后是备注。
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TIMEOUT = 8
WORKERS = 20
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def fetch(url: str, timeout: float = TIMEOUT) -> tuple[int, float, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            body = r.read(1 << 20)
            return r.status, (time.perf_counter() - t0) * 1000, body
    except urllib.error.HTTPError as e:
        return e.code, (time.perf_counter() - t0) * 1000, b""
    except Exception as e:
        return -1, (time.perf_counter() - t0) * 1000, str(e).encode()[:120]


def norm(api: str) -> str:
    """规范化 api:补尾斜杠、剥掉 ac 之类的 query(否则会拼出 ac=list?ac=list)。"""
    api = api.strip()
    p = urllib.parse.urlsplit(api)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k != "ac"]
    path = p.path if p.path.endswith("/") else p.path + "/"
    return urllib.parse.urlunsplit(
        (p.scheme, p.netloc, path, urllib.parse.urlencode(q), ""))


def parse_list(body: bytes) -> tuple[int, int, str]:
    """返回 (total, 分类数, 疑似类型)。兼容 xml / json 两种 CMS 输出。"""
    txt = body.decode("utf-8", "ignore")
    total = 0
    cls = 0
    typ = "?"
    m = re.search(r"<total>(\d+)</total>", txt)
    if m:
        total = int(m.group(1))
        cls = len(re.findall(r"<ty id=", txt))
        typ = "xml"
        return total, cls, typ
    try:
        j = json.loads(txt)
    except Exception:
        return 0, 0, "bad"
    if isinstance(j, dict):
        total = int(j.get("total") or 0)
        cats = j.get("class") or []
        cls = len(cats) if isinstance(cats, list) else 0
        typ = "json"
    return total, cls, typ


def qs(base: str, **extra) -> str:
    """把参数并进 url,同名参数先剥掉,避免 ac=list&ac=videolist 打架。"""
    p = urllib.parse.urlsplit(base)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k not in extra]
    q += list(extra.items())
    return urllib.parse.urlunsplit(
        (p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


def probe_one(api: str, note: str = "") -> dict:
    api = norm(api)
    r: dict = {"api": api, "note": note}
    st, ms, body = fetch(qs(api, ac="list"))
    r["ms"] = round(ms)
    r["status"] = st
    if st != 200 or not body:
        r["ok"] = False
        r["why"] = f"http {st}"
        return r
    total, cls, typ = parse_list(body)
    r["type"] = 0 if typ == "xml" else 1
    r["kind"] = typ
    r["total"] = total
    r["classes"] = cls
    if typ not in ("xml", "json"):
        r["ok"] = False
        r["why"] = "非CMS输出"
        return r
    # 搜索能力:影视仓的搜索走 ac=videolist&wd=
    wd = urllib.parse.quote("庆余年")
    st2, ms2, b2 = fetch(qs(api, ac="videolist", wd="庆余年", pg="1"), TIMEOUT)
    if st2 == 200 and b2:
        t2, _, _ = parse_list(b2)
        r["search_ms"] = round(ms2)
        r["search_hits"] = t2
        r["search"] = t2 > 0
    else:
        r["search"] = False
        r["search_hits"] = 0
    r["ok"] = True
    return r


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "candidates.txt"
    out_json = ROOT / "cmsprobe.json"
    for i, a in enumerate(sys.argv):
        if a in ("-o", "--out") and i + 1 < len(sys.argv):
            out_json = ROOT / sys.argv[i + 1]
    cands: list[tuple[str, str]] = []
    for line in src.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        note = ""
        if "#" in line:
            line, note = line.split("#", 1)
        cands.append((line.strip(), note.strip()))
    print(f"候选 {len(cands)} 条,开始实测…", flush=True)

    rows: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(probe_one, u, n): u for u, n in cands}
        for f in as_completed(futs):
            done += 1
            try:
                rows.append(f.result())
            except Exception as e:
                rows.append({"api": futs[f], "ok": False, "why": str(e)[:80], "ms": -1})
            if done % 10 == 0:
                print(f"  {done}/{len(cands)}", flush=True)

    rows.sort(key=lambda x: (not x.get("ok"), x.get("ms", 99999)))
    out_json.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    good = [r for r in rows if r.get("ok") and r.get("search") and r.get("total", 0) >= 30000]
    print(f"\n达标(≥3万片+可搜) {len(good)} / {len(rows)}")
    for r in good[:40]:
        print(f"  {r['ms']:>5}ms  片库{r['total']:>7}  类{r['classes']:>3}  "
              f"{'XML' if r['type'] == 0 else 'JSON'}  {r['api']}")
    print(f"\n明细已写 {out_json.name}")


if __name__ == "__main__":
    main()
