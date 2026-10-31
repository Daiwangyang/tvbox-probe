#!/usr/bin/env python3
"""播放流畅度实测 —— 拉一小段流看能不能出画面。

不看接口响应快不快(那是另一回事),而是真的让 ffmpeg 去解码:
能出帧 = 能播;缓冲打不满、codec 不对、403 = 不能播。

只对已经通过延迟初筛的站跑,否则一轮要几十分钟。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe  # noqa: E402

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "play.json"
SECONDS = 6              # 实际拉流秒数
WORKERS = 4              # ffmpeg 吃 CPU,并发别开大
MAX_CANDIDATES = 14      # 只测初筛后的前 N 个


def _urls_from_play_url(txt: str) -> list[str]:
    """从 vod_play_url 里抠地址。

    格式是 `集名$地址$$$集名$地址`,地址一圈 JSON 转义(`https:\\/\\/`),
    直接按 "url":"..." 找是找不到的——那套字段只出现在少数自建接口里。
    """
    out: list[str] = []
    for m in re.finditer(r'"vod_play_url"\s*:\s*"((?:[^"\\]|\\.)*)"', txt):
        seg = m.group(1)
        for part in seg.split("$$$"):
            for piece in part.split("#"):
                _, _, u = piece.partition("$")
                u = u.replace("\\/", "/").strip()
                if u.startswith(("http://", "https://")):
                    out.append(u)
    return out


def pick_episode(api: str) -> str | None:
    """从一个站里抽一条能播的 m3u8 / mp4 地址。"""
    try:
        for kw in probe.KEYWORDS:
            raw, _, _ = probe.fetch(
                probe.qs(api, {"ac": "videolist", "wd": kw}), limit=6000
            )
            txt = probe.decode(raw)
            # 优先挑 m3u8 的记录,它们播放更稳,也便于计数
            for m in re.finditer(r'"vod_id"\s*:\s*(\d+)', txt):
                raw2, _, _ = probe.fetch(
                    probe.qs(api, {"ac": "detail", "ids": m.group(1)}), limit=12000
                )
                body = probe.decode(raw2)
                cands = [u for u in _urls_from_play_url(body)
                         if u.endswith((".m3u8", ".mp4")) or ".m3u8?" in u]
                if cands:
                    m3u8 = [u for u in cands if ".m3u8" in u]
                    return (m3u8 or cands)[0]
        return None
    except Exception:
        return None


def play_ok(url: str) -> dict:
    """用 ffmpeg 解 SECONDS 秒,统计出了多少帧。"""
    ff = shutil.which("ffmpeg")
    if not ff:
        return {"ok": False, "frames": 0, "reason": "无 ffmpeg"}
    # 只跑一遍:ffmpeg 的进度是用 \r 原地刷新的,要 -progress 才拿得到干净的帧数
    cmd = [
        ff, "-hide_banner", "-loglevel", "error",
        "-user_agent", probe.UA,
        "-rw_timeout", "12000000",
        "-i", url,
        "-t", str(SECONDS),
        "-progress", "pipe:1",
        "-f", "null", "-",
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=SECONDS + 25, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "frames": 0, "reason": "超时"}

    # -progress 每行 key=value,末尾一定有一条最终值
    frames = 0
    for m in re.finditer(r"^frame=(\d+)$", p.stdout or "", re.M):
        frames = max(frames, int(m.group(1)))

    if frames == 0:
        err = (p.stderr or "").strip().splitlines()
        return {"ok": False, "frames": 0, "reason": (err[-1][:70] if err else "解码失败")}
    # 6 秒 24fps 满帧约 144,取 10fps 作合格线:能连出画面就算通
    return {"ok": frames >= SECONDS * 10, "frames": frames, "reason": ""}


def main() -> int:
    if not shutil.which("ffmpeg"):
        print("没装 ffmpeg,跳过播放测试")
        return 1
    if not (ROOT / "my.json").exists():
        print("先跑 probe.py")
        return 1

    sites = json.loads((ROOT / "my.json").read_text(encoding="utf-8"))["sites"]
    # csp 站 api 不是 URL,拉不到地址,只测 CMS 站
    url_sites = [s for s in sites if s["type"] in (0, 1)]
    cands = url_sites[:MAX_CANDIDATES]
    print(f"对 {len(cands)} 个 CMS 站做播放测试({SECONDS}s/个,并发 {WORKERS})")

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {}
        for s in cands:
            ep = pick_episode(s["api"])
            if not ep:
                results[s["key"]] = {"ok": False, "frames": 0, "reason": "抽不到播放地址"}
                continue
            futs[pool.submit(play_ok, ep)] = s
        for fut in as_completed(futs):
            s = futs[fut]
            r = fut.result()
            results[s["key"]] = r
            mark = "OK " if r["ok"] else "x  "
            print(f"  {mark}{r['frames']}帧 {s['name'][:18]} {r['reason']}")

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    # 按能不能播重排:能播的按帧数(约等于流畅度)降序,不能播的沉底
    def rank(s: dict) -> tuple:
        r = results.get(s["key"], {})
        return (0 if r.get("ok") else 1, -r.get("frames", 0))

    ordered = sorted(sites, key=rank)
    got = sum(1 for r in results.values() if r.get("ok"))
    # 重写前把 spider 等全局字段原样带上,csp 站全靠它
    doc = json.loads((ROOT / "my.json").read_text(encoding="utf-8"))
    doc["sites"] = ordered
    (ROOT / "my.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"\n能播 {got}/{len(results)},已按流畅度重排 my.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
