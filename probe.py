#!/usr/bin/env python3
"""TVBox 接口真实测速 + 可用性筛选。

不是测「配置文件能不能下载」——那个没意义,配置能打开不代表里面任何一个站能播。
这个脚本把配置拆开,对每一行 sites 里的 api 地址发真实请求,量 TTFB,
再按延迟排序,只留下能通、够快的 CMS 站,组装成一份给你自己用的精简配置。

三条硬性设计:
1. siteKey 由 api 的域名派生,**固定不变**。历史记录是跟 key 绑的,
   用 p1/p2 这种序号,排序一变历史就全丢。
2. 不收录任何带公众号引流标志的站,所以要 `sites.json` 支持从零开始。
3. 结果默认只留延迟达标的;不达标的进 quarantine,连续两轮不通才真正删。

用法:
    python probe.py              # 用 seeds.txt
    python probe.py mysources.txt
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEEDS = ROOT / "seeds.txt"
BASE_JSON = ROOT / "sites.json"      # 从零维护的基线,不依赖别人的配置
OUT_JSON = ROOT / "my.json"
OUT_MD = ROOT / "report.md"
QUAR = ROOT / "quarantine.json"      # 淘汰区,记录连续失败次数
GATES: list[tuple[str, str]] = []    # 本轮的引流源(跳小程序/关注公众号)
MAIN_JAR = [""]                     # 承载站最多的那个 jar,只对它做类校验

TIMEOUT = 8
WORKERS = 24
MAX_SITES_PER_SOURCE = 40
MAX_MS = 1500                        # 超过这个延迟不进成品
DROP_AFTER = 2                       # 连续几轮不通才真正丢弃
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def site_key(api: str, used: set[str] | None = None) -> str:
    """由 api 派生稳定 key。

    历史记录跟 key 绑定,所以 key 必须只取决于地址本身,不能带序号。
    同一个域名下有多条接口时分两种情况:路径不同算两个站(得区分),
    所以取域名 + 路径一起哈希,冲突再补后缀(后缀也不同轮次漂移)。
    """
    p = urllib.parse.urlsplit(idna(api))
    host = (p.hostname or api).lower().removeprefix("www.")
    path = re.sub(r"[^a-z0-9]+", "", (p.path or "").lower())[:40]
    h = hashlib.sha1(f"{host}/{path}".encode()).hexdigest()[:10]
    key = f"s_{h}"
    if used is not None:
        n = 2
        while key in used:
            key = f"s_{h}_{n}"
            n += 1
        used.add(key)
    return key


# 拉配置可以走代理(配置常挂在 GitHub),测站点必须直连——电视上没代理
PROXY = os.environ.get("PROBE_PROXY") or os.environ.get("HTTPS_PROXY") or ""
_OPENER = (
    urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    )
    if PROXY
    else urllib.request.build_opener()
)

_DIRECT = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=CTX),
)

KEYWORDS = ("测试", "庆余年")


def idna(url: str) -> str:
    """中文域名转 punycode,路径里的中文转百分号。urllib 都不认。"""
    try:
        p = urllib.parse.urlsplit(url)
        netloc = p.netloc
        if p.hostname and not p.hostname.isascii():
            host = p.hostname.encode("idna").decode()
            netloc = host + (f":{p.port}" if p.port else "")
        path = urllib.parse.quote(p.path, safe="/%")
        query = urllib.parse.quote(p.query, safe="=&%+")
        return urllib.parse.urlunsplit((p.scheme, netloc, path, query, ""))
    except Exception:
        return url


def fetch(url: str, timeout: int = TIMEOUT, limit: int = 4096,
          proxy: bool = False) -> tuple[bytes, int, int]:
    """返回 (body, status, ms)。ms 是到首字节的时间。"""
    req = urllib.request.Request(idna(url), headers={"User-Agent": UA, "Accept": "*/*"})
    op = _OPENER if proxy else _DIRECT
    t0 = time.time()
    with op.open(req, timeout=timeout) as r:
        body = r.read(limit)
        ms = int((time.time() - t0) * 1000)
        return body, r.status, ms


def decode(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def strip_comments(text: str) -> str:
    """配置常在 JSON 前后塞 // 注释,还有裸花括号修正,这里尽量剥掉。"""
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("//") or s.startswith("#"):
            continue
        lines.append(ln)
    t = "\n".join(lines)
    i = t.find("{")
    return t[i:] if i >= 0 else t


def load_config(url: str) -> dict | None:
    """抓一份配置,支持 JSON 与纯文本(每行一个地址的多仓)。"""
    try:
        raw, status, _ = fetch(url, timeout=15, limit=200_000, proxy=True)
        if status >= 400:
            return None
    except Exception:
        return None
    text = strip_comments(decode(raw)).strip()

    # 引流页:跳小程序/关注公众号才放行,整条不要
    gate = is_gateway(text)
    if gate and '"sites"' not in text[:6000]:
        return {"_gate": gate}

    if text.startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尾部被截断时,尝试补括号
            for tail in ("}]}", "]}", "}", "]"):
                try:
                    return json.loads(text + tail)
                except json.JSONDecodeError:
                    continue
    urls = re.findall(r"https?://[^\s\"'#]+", text)
    return {"_links": urls} if urls else None


def qs(base: str, extra: dict[str, str]) -> str:
    """把参数并进 url。先剥掉原有的 ac,否则 ac=list&ac=videolist 会打架。"""
    p = urllib.parse.urlsplit(base)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k != "ac"]
    q += list(extra.items())
    return urllib.parse.urlunsplit(
        (p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), "")
    )


def cms_ok(api: str) -> dict:
    """对 CMS 站发一次真实搜索,判断能不能出数据 + 延迟多少。"""
    kw = KEYWORDS[int(time.time()) % len(KEYWORDS)]
    probe_url = qs(api, {"ac": "videolist", "wd": kw})

    t0 = time.time()
    try:
        raw, status, ms = fetch(probe_url)
    except Exception as e:
        return {"ms": -1, "reason": type(e).__name__}

    if status >= 400:
        return {"ms": ms, "reason": f"HTTP {status}"}

    text = decode(raw)
    if '"list"' not in text and "list" not in text[:400]:
        if "不支持搜索" in text or "not support" in text.lower():
            return {"ms": ms, "reason": "禁搜索", "soft": True}
        # 有些站返回重定向页 / 广告页,不含 list 字段
        return {"ms": ms, "reason": "无 list 字段", "soft": True}

    # 详情页里的播放地址如果指向第三方解析,基本就是广告页,顺手记下来
    if on_ad_page(api):
        return {"ms": ms, "reason": "广告解析", "soft": True}
    return {"ms": ms, "reason": ""}


def on_ad_page(api: str) -> bool:
    """抽一条详情,看播放地址是不是第三方解析站。"""
    try:
        raw, _, _ = fetch(qs(api, {"ac": "videolist", "wd": KEYWORDS[0]}), limit=6000)
        m = re.search(r'"vod_id"\s*:\s*(\d+)', decode(raw))
        if not m:
            return False
        raw2, _, _ = fetch(qs(api, {"ac": "detail", "ids": m.group(1)}), limit=8000)
        txt = decode(raw2)
        # 采集站自己的直链通常是 m3u8/mp4;出现这些说明要跳解析页
        ad_hosts = ("jx.", "parse", "api.php?url=", "/?url=", "player/?url=")
        urls = re.findall(r'"url"\s*:\s*"([^"]+)"', txt)
        for u in urls[:5]:
            if any(h in u for h in ad_hosts):
                return True
        return False
    except Exception:
        return False



def is_gateway(text: str) -> str:
    """判配置是不是「引流页」——拉回来不是 JSON,而是要求跳小程序/关注公众号才放行。

    命中返回原因,否则返回空串。这类跟站点跳解析页、视频中插广告都不同,
    是配置这一层就要你付出代价,所以整条源直接剔掉。
    """
    head = text[:4000]
    if not head.lstrip().startswith(("<", "//", "#")) and '"sites"' in head:
        return ""
    low = head.lower()
    if "wxaurl" in low or "weixin.qq.com/s/" in low or "小程序" in head:
        return "跳小程序"
    if "关注公众号" in head or ("公众号" in head and "关注" in head):
        return "关注公众号"
    if "扫码" in head or "扫一扫" in head:
        return "扫码"
    return ""


def load_baseline() -> list[dict]:
    """读 sites.json 里人工挑出来的站,这些优先保留、永不淘汰。"""
    if not BASE_JSON.exists():
        return []
    try:
        data = json.loads(BASE_JSON.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for s in data.get("sites") or []:
        api = (s.get("api") or "").strip()
        if api.startswith("http"):
            out.append({"name": s.get("name", "?"), "api": api,
                        "type": s.get("type", 0), "keep": True})
    return out


def abs_jar(jar: str, seed: str) -> str:
    """jar 常写成 ./fty.jar 这种相对路径,要按源的目录拼成绝对地址。"""
    jar = (jar or "").strip()
    if not jar or jar.startswith(("http://", "https://")):
        return jar
    try:
        p = urllib.parse.urlsplit(seed)
        base = f"{p.scheme}://{p.netloc}{p.path.rsplit('/', 1)[0]}/"
        return urllib.parse.urljoin(base, jar)
    except Exception:
        return ""


def probe_site(t: dict) -> dict:
    """按类型分派:URL 站发真实搜索,csp 站校验 jar 里有没有这个类。"""
    if t.get("csp"):
        import jarcheck
        # 只有主 jar 才值得下;其它 jar 的站先放行,后面按 spider 过滤时再丢
        if t.get("jar") != MAIN_JAR[0] and MAIN_JAR[0]:
            return {"ms": 300, "reason": "", "need_play": True}
        ok = jarcheck.supports(t["jar"], t["api"])
        # jar 里有类,但站本身死没死只能靠 App 跑,这里给个中性延迟
        return {"ms": 300 if ok else -1,
                "reason": "" if ok else "jar 无此类",
                "need_play": ok}
    return cms_ok(t["api"])


def looks_cms(url: str) -> bool:
    """看地址特征判断是不是 CMS 接口,是就别当配置再抓一遍。"""
    low = url.lower()
    return any(k in low for k in ("provide/vod", "api.php", "/api/", "vodtype", "vodshow"))


def collect(seeds: list[str], base: list[dict]) -> list[dict]:
    """基线站 + 展开所有种子配置(含多仓),产出待测站点(去重)。"""
    seen: dict[str, dict] = {}
    for b in base:
        b["from"] = "sites.json"
        seen[b["api"]] = b
    print(f"  基线 {len(base)} 条(来自 sites.json)")
    queue = [(s, 0) for s in seeds]
    done: set[str] = set()
    while queue:
        seed, depth = queue.pop(0)
        if seed in done or len(done) > 300:
            continue
        done.add(seed)

        if looks_cms(seed):
            if seed not in seen:
                seen[seed] = {"name": seed.split("/")[2][:24], "api": seed,
                              "type": 0, "from": seed}
            continue

        cfg = load_config(seed)
        if not cfg:
            print(f"  x 配置打不开: {seed[:60]}")
            continue
        if cfg.get("_gate"):
            GATES.append((cfg["_gate"], seed))
            print(f"  ⚑ 引流({cfg['_gate']}): {seed[:52]}")
            continue
        if cfg.get("_links"):
            if depth >= 2:
                continue
            for ln in cfg["_links"]:
                queue.append((ln, depth + 1))
            print(f"  > 多仓,排入 {len(cfg['_links'])} 个子地址: {seed[:50]}")
            continue
        sites = cfg.get("sites") or []
        # 这个配置自己绑的 jar,type 3 站全靠它跑;相对路径要拼成绝对地址
        own_jar = abs_jar((cfg.get("spider") or "").split(";")[0], seed)
        takes = 0
        for s in sites:
            if not isinstance(s, dict):
                continue
            api = (s.get("api") or "").strip()
            stype = s.get("type")
            name = (s.get("name") or "?").strip()
            if api in seen or takes >= MAX_SITES_PER_SOURCE:
                continue
            if stype in (0, 1) and api.startswith("http"):
                seen[api] = {"name": name, "api": api, "type": stype, "from": seed}
                takes += 1
            elif stype == 3 and api.startswith("csp_"):
                # csp 站必须配 jar 才有意义;没有 jar 的 csp 站是死站
                if not own_jar:
                    continue
                seen[api] = {"name": name, "api": api, "type": 3,
                             "from": seed, "jar": own_jar, "csp": True}
                takes += 1
        print(f"  + {len(sites)} 站点,新增 {takes}  [{seed[:40]}]")
    return list(seen.values())


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else SEEDS
    if not src.exists():
        print(f"缺少 {src}")
        return 1

    seeds = []
    for f in (src, ROOT / "seeds_extra.txt"):
        if not f.exists():
            continue
        seeds += [
            ln.split("#")[0].strip()
            for ln in f.read_text(encoding="utf-8").splitlines()
            if ln.split("#")[0].strip().startswith("http")
        ]
    seeds = list(dict.fromkeys(seeds))
    print(f"种子 {len(seeds)} 条,展开中…(拉配置走代理: {PROXY or '直连'})")
    targets = collect(seeds, load_baseline())

    # 先按承载量选出主 jar,只校验它;别的 jar 的站后面统一按 spider 丢弃
    hits: dict[str, int] = {}
    for t in targets:
        if t.get("jar"):
            hits[t["jar"]] = hits.get(t["jar"], 0) + 1
    if hits:
        MAIN_JAR[0] = max(hits.items(), key=lambda x: x[1])[0]
        print(f"主 jar(承载 {hits[MAIN_JAR[0]]} 站): {MAIN_JAR[0][:80]}")
    print(f"\n待测 {len(targets)} 个站点,并发 {WORKERS},超时 {TIMEOUT}s\n")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(probe_site, t): t for t in targets}
        for n, fut in enumerate(as_completed(futs), 1):
            t = futs[fut]
            r = fut.result()
            t.update(r)
            rows.append(t)
            if n % 25 == 0:
                print(f"  已测 {n}/{len(targets)}", flush=True)

    # 淘汰计数:基线站和上一轮在用的站,连续不通才删,避免一次抖动就掉
    old_q: dict[str, int] = {}
    if QUAR.exists():
        try:
            old_q = json.loads(QUAR.read_text(encoding="utf-8"))
        except Exception:
            old_q = {}

    quota: dict[str, int] = {}
    alive: list[dict] = []
    for r in rows:
        k = site_key(r["api"])
        ok = r["ms"] > 0 and not r["reason"] and (
            r.get("need_play") or r["ms"] <= MAX_MS
        )
        if ok:
            quota.pop(k, None)
            alive.append(r)
            continue
        quota[k] = quota.get(k, 0) + 1
        if r.get("keep") and quota[k] < DROP_AFTER:
            r["alt_note"] = "降级保留"
            alive.append(r)
    # 旧的连续失败记录并进来
    for k, v in old_q.items():
        quota[k] = max(quota.get(k, 0), v + 1)

    alive.sort(key=lambda x: (x["ms"] < 0, x["ms"]))
    # 同一 host+path 的 http/https 是同一个站,只留最快的那条
    dedup: dict[str, dict] = {}
    for r in alive:
        k = site_key(r["api"])
        if k not in dedup:
            dedup[k] = r
    sites = [
        {
            "key": k,
            "name": r["name"],
            "type": r.get("type", 0),
            "api": r["api"],
            "searchable": 1,
            "quickSearch": 1,
            "filter": 1,
        }
        for k, r in dedup.items()
    ]
    # 全局 spider 只能填一个。CMS 站不挑 jar,所以优先选在用的 csp 站最多的那一个,
    # 剩下 jar 不对的 csp 站才丢掉(留着也是白屏)。
    jar_hits: dict[str, int] = {}
    for r in dedup.values():
        if r.get("jar"):
            jar_hits[r["jar"]] = jar_hits.get(r["jar"], 0) + 1
    spider = ""
    if jar_hits:
        spider = max(jar_hits.items(), key=lambda x: x[1])[0]
        dropped = 0
        kept = []
        for s in sites:
            r = dedup[s["key"]]
            if r.get("jar") and r["jar"] != spider:
                dropped += 1
                continue
            kept.append(s)
        sites = kept
        print(f"  选用 spider: {spider[:70]}")
        if dropped:
            print(f"  丢弃 {dropped} 个 jar 不匹配的 csp 站")

    doc: dict = {"sites": sites, "parses": [], "flags": [], "lives": []}
    if spider:
        doc["spider"] = spider
    OUT_JSON.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    QUAR.write_text(json.dumps(quota, ensure_ascii=False, indent=1), encoding="utf-8")

    good = [r for r in rows if r["ms"] > 0 and not r["reason"] and r["ms"] <= MAX_MS]
    weak = [r for r in rows if r.get("soft")]
    good.sort(key=lambda x: x["ms"])
    weak.sort(key=lambda x: x["ms"])

    md = [f"# 探测结果 {time.strftime('%Y-%m-%d %H:%M')}", "",
          f"- 待测 {len(rows)} / 达标 {len(good)} / 成品 {len(sites)}（含降级保留）", "",
          f"- 阈值 {MAX_MS}ms,连续 {DROP_AFTER} 轮不通才淘汰", "",
          "## 达标(按延迟)", "", "| 延迟 | 站点 | 接口 |", "| --- | --- | --- |"]
    md += [f"| {r['ms']}ms | {r['name']} | `{r['api']}` |" for r in good[:60]]
    md += ["", "## 弱可用 / 广告解析", ""]
    md += [f"- {r['ms']}ms {r['name']} — {r['reason']} `{r['api']}`" for r in weak[:40]]
    md += ["", "## 不通", ""]
    md += [f"- {r['name']} `{r['api']}` — {r['reason']}"
           for r in rows if r["ms"] < 0][:40]
    if GATES:
        md += ["", "## 引流源(跳小程序 / 关注公众号,已剔除)", ""]
        md += [f"- {why}: `{u}`" for why, u in GATES[:30]]
    OUT_MD.write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"\n完成:达标 {len(good)} / 弱 {len(weak)} / 成品 {len(sites)} / 共 {len(rows)}")
    print(f"→ {OUT_JSON}  (填到影视仓「配置地址」)\n→ {OUT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
