#!/usr/bin/env python3
"""把探测结果加工成最终配置:精选 → 规范命名 → 按延迟升序排。

为什么单独一步:probe.py 负责"测",这个脚本负责"排"。
影视仓的点播站点列表就是按 sites 数组顺序渲染的,所以数组顺序 = 用户看到的顺序,
想按延迟从低到高选源,唯一可行的做法就是在配置里把顺序排好。

用法:
    python build.py            # 用 rank.json 的实测数据重建 my.json
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RANK = ROOT / "rank.json"
MY = ROOT / "my.json"

# 展示名:host 关键字 -> (emoji, 中文名)
# 同一家资源只留最快的 1~2 个 CDN,写在这里就是白名单,不在表里的一律不收。
BRAND: dict[str, tuple[str, str]] = {
    "154.219.117.232": ("🏗", "建安"),
    "ffzy5.tv": ("🍀", "非凡"),
    "cj.ffzyapi.com": ("🍀", "非凡⑵"),
    "caiji.dyttzyapi.com": ("🎬", "电影天堂"),
    "360zy.com": ("🦚", "360"),
    "cj.lziapi.com": ("🥑", "量子"),
    "api.wujinapi.com": ("🌊", "无尽"),
    "huyaapi.com": ("🐯", "虎牙"),
    "jszyapi.com": ("🎀", "极速"),
    "juliang.live": ("🐳", "巨量"),
    "hhzyapi.com": ("🦊", "火狐"),
    "p2100.net": ("🍃", "飘零"),
    "hongniuzy2.com": ("🐂", "红牛"),
    "bfzyapi.com": ("🦔", "暴风"),
    "jyzyapi.com": ("🔰", "金鹰"),
    "apibdzy.com": ("🎏", "百度云"),
    "zuida.xyz": ("📦", "最大"),
    "caiji.moduapi.cc": ("🏙", "魔都"),
    "subocaiji.com": ("🚄", "速播"),
    "guangsuapi.com": ("💨", "光速"),
}

# 手工维护站点:插在延迟排序里的固定位置(用实测 ms 参与排序)
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
    from urllib.parse import urlsplit
    return (urlsplit(api).hostname or "").lower()


def clean_api(api: str) -> str:
    """去掉探测时加的 ac 参数和多余的 ?/,保证写进配置的是干净接口。"""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    p = urlsplit(api)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "ac"]
    path = p.path
    if path.endswith("/") and not path.endswith("//"):
        pass
    if not path.endswith("/"):
        path += "/"
    return urlunsplit((p.scheme, p.netloc, path, urlencode(q), ""))


def main() -> None:
    rows = json.loads(RANK.read_text(encoding="utf-8")) if RANK.exists() else []
    picked: dict[str, dict] = {}
    for r in rows:
        api = r.get("api", "")
        h = host(api)
        brand = next((v for k, v in BRAND.items() if k in h), None)
        if not brand or not r.get("ok"):
            continue
        # 同品牌只留延迟最低的一条
        if brand[1] in picked and picked[brand[1]]["ms"] <= r["ms"]:
            continue
        picked[brand[1]] = {"ms": r["ms"], "api": clean_api(api),
                            "total": r.get("total", 0),
                            "play": r.get("play_kind", ""), "emoji": brand[0]}

    prev = json.loads(MY.read_text(encoding="utf-8")) if MY.exists() else {}
    olds = {s.get("key"): s for s in prev.get("sites", [])}
    old_host = {host(s.get("api", "")): s.get("key") for s in prev.get("sites", [])
                if str(s.get("api", "")).startswith("http")}

    direct = []
    for name, p in picked.items():
        nm = f"{p['emoji']}{name}·{p['ms']}ms"
        direct.append({
            # 沿用旧 key,观看历史不会因为这次重排而丢
            "key": old_host.get(host(p["api"])) or key_of(p["api"]),
            "name": nm,
            "type": 1 if "json" in p["api"] else 0,
            "api": p["api"],
            "searchable": 1,
            "quickSearch": 1,
            "filter": 1,
            "_ms": p["ms"],
        })
    # 探测出的 type 不准(多数站同时支持 xml/json),统一用 0,兼容性最好
    for s in direct:
        s["type"] = 0

    # 77 用上一份里已有的 key/字段,只换显示名
    for f in FIXED:
        old = olds.get(f["key"], {})
        f["key"] = old.get("key", f["key"])
        f["name"] = f["name"] + f"·{f['ms']}ms"

    csp = [s for s in prev.get("sites", []) if str(s.get("api", "")).startswith("csp_")]

    merged = sorted(direct + [dict(f) for f in FIXED],
                    key=lambda x: x.get("_ms", x.get("ms", 99999)))
    for s in merged:
        s.pop("_ms", None)
        s.pop("ms", None)

    sites = merged + csp
    doc = {
        "sites": sites,
        "parses": prev.get("parses", []),
        "flags": prev.get("flags", []),
        "lives": prev.get("lives", []),
    }
    if prev.get("spider"):
        doc["spider"] = prev["spider"]
    MY.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"直连 {len(direct)} + 弹幕仓 1 + 蜘蛛 {len(csp)} = {len(sites)} 站")
    for s in sites[:len(direct) + 1]:
        print(f"  {s['name']}")
    print(f"→ {MY}")


def key_of(api: str) -> str:
    """和历史记录绑定的稳定 key:由 host+path 派生,改名不丢观看进度。"""
    import hashlib
    from urllib.parse import urlsplit
    p = urlsplit(api)
    seed = (p.netloc + p.path).rstrip("/").lower()
    return "s_" + hashlib.sha1(seed.encode()).hexdigest()[:10]


if __name__ == "__main__":
    main()
