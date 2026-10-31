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
import tempfile
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


def pick_episode(api: str) -> str | None:
    """从一个站里抽一条能播的 m3u8 / mp4 地址。"""
    try:
        raw, _, _ = probe.fetch(
            probe.qs(api, {"ac": "videolist", "wd": probe.KEYWORDS[0]}), limit=6000
        )
        m = re.search(r'"vod_id"\s*:\s*(\d+)', probe.decode(raw))
        if not m:
            return None
        raw2, _, _ = probe.fetch(
            probe.qs(api, {"ac": "detail", "ids": m.group(1)}), limit=12000
        )
        txt = probe.decode(raw2)
        for u in re.findall(r'"url"\s*:\s*"([^"]+)"', txt):
            u = u.replace("\\/", "/")
            if u.endswith((".m3u8", ".mp4")) or ".m3u8?" in u or ".mp4?" in u:
                return u
        return None
    except Exception:
        return None


def play_ok(url: str) -> dict:
    """用 ffmpeg 解 SECONDS 秒,统计出了多少帧。"""
    ff = shutil.which("ffmpeg")
    if not ff:
        return {"ok": False, "frames": 0, "reason": "无 ffmpeg"}
    with tempfile.TemporaryDirectory() as td:
        cmd = [
            ff, "-hide_banner", "-loglevel", "error",
            "-user_agent", probe.UA,
            "-rw_timeout", "12000000",
            "-i", url,
            "-t", str(SECONDS),
            "-f", "null", "-",
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=SECONDS + 25, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return {"ok": False, "frames": 0, "reason": "超时"}
        # 统计解出的帧数:用 -f null 时 ffmpeg 不报帧数,改成再跑一次统计
        cmd2 = [
            ff, "-hide_banner", "-user_agent", probe.UA,
            "-rw_timeout", "12000000", "-i", url,
            "-t", str(SECONDS), "-f", "null", "-",
        ]
        try:
            p2 = subprocess.run(cmd2, capture_output=True, text=True,
                                timeout=SECONDS + 25, encoding="utf-8", errors="replace")
            m = re.search(r"frame=\s*(\d+)", p2.stderr)
            frames = int(m.group(1)) if m else 0
        except Exception:
            frames = 0

    if p.returncode != 0 and frames == 0:
        err = (p.stderr or "").strip().splitlines()
        return {"ok": False, "frames": 0, "reason": (err[-1][:70] if err else "解码失败")}
    return {"ok": frames >= SECONDS * 10, "frames": frames, "reason": ""}


def main() -> int:
    if not shutil.which("ffmpeg"):
        print("没装 ffmpeg,跳过播放测试")
        return 1
    if not (ROOT / "my.json").exists():
        print("先跑 probe.py")
        return 1

    sites = json.loads((ROOT / "my.json").read_text(encoding="utf-8"))["sites"]
    cands = sites[:MAX_CANDIDATES]
    print(f"对前 {len(cands)} 个站做播放测试({SECONDS}s/个,并发 {WORKERS})")

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
    (ROOT / "my.json").write_text(
        json.dumps({"sites": ordered, "parses": [], "flags": [], "lives": []},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"\n能播 {got}/{len(results)},已按流畅度重排 my.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
