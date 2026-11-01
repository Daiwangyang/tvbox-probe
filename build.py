#!/usr/bin/env python3
"""把探测结果加工成最终配置:精选 → 规范命名 → 按延迟升序排。

为什么单独一步:probe.py 负责"测",这个脚本负责"排"。
影视仓的点播站点列表就是按 sites 数组顺序渲染的,所以数组顺序 = 用户看到的顺序,
想按延迟从低到高选源,唯一可行的做法就是在配置里把顺序排好。

选站规则:
  1. 同一品牌有多条线路(主接口 / from/xxx / at/xml)时,按「列表延迟 6 : 详情延迟 4」
     算综合分,挑综合最快的那条 —— 详情快才不会点开就转圈。
  2. 片库总量 <3 万的不收。
  3. 搜索时忽略关键词(返回全库)的站,searchable 置 0 —— 否则搜索结果会被无关数据刷屏。
  4. 名字里带真实延迟和片库规模,选源时一眼能比。

用法: python build.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
RANK = ROOT / "rank.json"
MY = ROOT / "my.json"
STATE = ROOT / "build_state.json"   # 每轮实测的沉淀,用于容错

# 品牌白名单:host 关键字 -> (emoji, 展示名)
# 不在表里的源一律不收;同一展示名出现多次时自动取综合最优的一条线路。
BRAND: dict[str, tuple[str, str]] = {
    "maoyanapi.top": ("🎯", "猫眼"),
    "cj.lziapi.com": ("🥑", "量子"),
    "caiji.dyttzyapi.com": ("🎬", "电影天堂"),
    "360zy.com": ("🦚", "360"),
    "360zyzz.com": ("🦚", "360"),
    "jyzyapi.com": ("🔰", "金鹰"),
    "hhzyapi.com": ("🦊", "火狐"),
    "cj.ffzyapi.com": ("🍀", "非凡"),
    "ffzy5.tv": ("🍀", "非凡"),
    "jszyapi.com": ("🎀", "极速"),
    "huyaapi.com": ("🐯", "虎牙"),
    "juliang.live": ("🐳", "巨量"),
    "zuidapi.com": ("📦", "最大"),
    "zuida.xyz": ("📦", "最大"),
    "hongniuzy2.com": ("🐂", "红牛"),
    "p2100.net": ("🍃", "飘零"),
    "iqiyizyapi.com": ("📺", "爱奇艺"),
    "guangsuapi.com": ("💨", "光速"),
    "moduapi.cc": ("🏙", "魔都"),
    "wujinapi.com": ("🌊", "无尽"),
    "apiyutu.com": ("🐰", "玉兔"),
    "apibdzy.com": ("🎏", "百度云"),
    "subocaiji.com": ("🚄", "速播"),
    "xinlangapi.com": ("📰", "新浪"),
}

# 实测淘汰:建安(搜索无视关键词、播放地址是 share 页需二次解析,留在第一屏只会让人点开黑屏)
# 魔都改用 caiji.moduapi.cc 线路(www.mdzyapi.com 三集里有一集超时)
MIN_TOTAL = 30000        # 片库低于这个数不收
FAKE_SEARCH_RATIO = 0.9  # 搜索结果数 ≥ 片库总量 90% 视为"忽略关键词"
DROP_AFTER = 3           # 连续几轮测不到才真正摘掉这个站
STALE_PENALTY = 5000     # 本轮没测到的站,排序时压到后面

# 手工维护站点:用实测 ms 参与排序
FIXED = [
    {
        "key": "s_77dpwqy",
        "name": "🔥弹幕仓",
        "type": 0,
        "api": "https://cf.ddngo.space/77",
        "searchable": 1,
        "quickSearch": 1,
        "filter": 1,
        "ms": 4067,          # /77 list 实测
    },
]


def host(api: str) -> str:
    return (urlsplit(api).hostname or "").lower()


def clean_api(api: str) -> str:
    """去掉探测时加的 ac 参数,保留 from/xxx 这类线路路径。"""
    p = urlsplit(api)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "ac"]
    path = p.path if p.path.endswith("/") else p.path + "/"
    return urlunsplit((p.scheme, p.netloc, path, urlencode(q), ""))


def key_of(api: str) -> str:
    """和历史记录绑定的稳定 key:由 host+path 派生,改名不丢观看进度。"""
    p = urlsplit(api)
    seed = (p.netloc + p.path).rstrip("/").lower()
    return "s_" + hashlib.sha1(seed.encode()).hexdigest()[:10]


def wan(total: int) -> str:
    v = total / 10000
    return f"{v:.0f}万" if v >= 10 else f"{v:.1f}万".replace(".0万", "万")


def med(hist: list[int]) -> int:
    """取中位数,抵消单轮抖动。"""
    v = sorted(x for x in hist if isinstance(x, int) and x > 0)
    if not v:
        return 99999
    return v[len(v) // 2]


def load_state() -> dict[str, dict]:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> None:
    rows = json.loads(RANK.read_text(encoding="utf-8")) if RANK.exists() else []
    prev = json.loads(MY.read_text(encoding="utf-8")) if MY.exists() else {}
    olds = {s.get("key"): s for s in prev.get("sites", [])}
    old_host = {host(s.get("api", "")): s.get("key")
                for s in prev.get("sites", []) if str(s.get("api", "")).startswith("http")}

    # 每轮结果沉淀成状态:某站这轮没测通就先按上轮数据保留,
    # 连续 DROP_AFTER 轮都不通才摘掉。否则一次网络抖动就能把配置掏空。
    # state 按完整接口地址存,不按域名 —— 同一家有多条线路时,
    # 按域名存会被后测的那条覆盖,容易把快的线路数据冲掉。
    state = load_state()
    seen: set[str] = set()
    for r in rows:
        if not r.get("ok"):
            continue
        k = clean_api(r["api"])
        seen.add(k)
        # 留最近 3 次的实测值取中位数:单轮网络抖动(某站忽快忽慢)不会让顺序乱跳
        hist = ((state.get(k) or {}).get("hist") or [])[-2:] + [r["ms"]]
        state[k] = {
            "api": k, "ms": med(hist), "raw": r["ms"], "hist": hist,
            "total": r.get("total", 0), "detail_ms": r.get("detail_ms"),
            "search_hits": r.get("search_hits", 0), "fail": 0,
        }
    dropped: list[str] = []
    for k in list(state):
        if k in seen:
            continue
        state[k]["fail"] = state[k].get("fail", 0) + 1
        if state[k]["fail"] >= DROP_AFTER:
            dropped.append(f"{host(k)} 连续{state[k]['fail']}轮不通")
            del state[k]
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    # 全挂说明是本轮网络的问题,不是站的问题 —— 这时不打乱顺序,照旧排
    fresh = sum(1 for r in state.values() if r.get("fail", 0) == 0)
    pen_all = 0 if fresh == 0 else STALE_PENALTY

    best: dict[str, dict] = {}
    for k, r in state.items():
        if r["total"] < MIN_TOTAL:
            continue
        brand = next((v for kw, v in BRAND.items() if kw in host(k)), None)
        if not brand:
            continue
        stale = k not in seen
        # 本轮没测到的站压低优先级:有新鲜数据的线路永远排在它前面
        pen = pen_all if stale else 0
        score = (r["ms"] + pen) * 0.6 + ((r.get("detail_ms") or r["ms"]) + pen) * 0.4
        cur = best.get(brand[1])
        if cur and cur["score"] <= score:
            continue
        best[brand[1]] = {
            "score": score, "ms": r["ms"], "api": r["api"],
            "total": r["total"], "emoji": brand[0], "stale": stale,
            "fake": r.get("search_hits", 0) >= r["total"] * FAKE_SEARCH_RATIO,
        }

    direct = []
    for name, p in best.items():
        searchable = 0 if p["fake"] else 1
        mark = "~" if p["stale"] else ""      # ~ = 本轮没测到,数字是上轮的
        direct.append({
            "key": old_host.get(host(p["api"])) or key_of(p["api"]),
            "name": f"{p['emoji']}{name}({wan(p['total'])})·{p['ms']}ms{mark}",
            "type": 0,
            "api": p["api"],
            # 搜索时忽略关键词的站不放进搜索,免得结果里全是无关片
            "searchable": searchable,
            "quickSearch": searchable,
            "filter": 1,
            "_ms": p["ms"] + (pen_all if p["stale"] else 0),
            "_tot": p["total"],
        })

    for f in FIXED:
        old = olds.get(f["key"], {})
        f["key"] = old.get("key", f["key"])
        f["name"] = f"{f['name']}·{f['ms']}ms"

    csp = [s for s in prev.get("sites", []) if str(s.get("api", "")).startswith("csp_")]

    merged = sorted(direct + [dict(f) for f in FIXED],
                    key=lambda x: x.get("_ms", x.get("ms", 99999)))
    for s in merged:
        s.pop("_ms", None)
        s.pop("ms", None)
        s.pop("_tot", None)

    doc = {
        "sites": merged + csp,
        "parses": prev.get("parses", []),
        "flags": prev.get("flags", []),
        "lives": prev.get("lives", []),
    }
    if prev.get("spider"):
        doc["spider"] = prev["spider"]
    MY.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    tot = sum(p["total"] for p in best.values())
    stale = sum(1 for p in best.values() if p["stale"])
    print(f"直连 {len(direct)} + 弹幕仓 1 + 蜘蛛 {len(csp)} = {len(doc['sites'])} 站")
    print(f"直连站片库合计 {tot/10000:.0f} 万(各家有重叠,非去重净量)")
    if stale:
        print(f"其中 {stale} 个本轮没测通,按上轮数据保留(名字带 ~)")
    for s in merged:
        flag = "" if s.get("searchable", 1) else "  [不参与搜索]"
        print(f"  {s['name']}{flag}")
    for d in dropped:
        print(f"  淘汰 {d}")
    print(f"→ {MY}")


if __name__ == "__main__":
    main()
