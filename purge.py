"""推 jsDelivr 缓存,让刚提交的 my.json 立刻生效。

jsDelivr 对分支引用默认缓 12 小时,但提供 purge 接口。
探测每 30 分钟跑一轮,每轮提交完就调一次,客户端最多滞后半小时。
"""

import sys
import time
import urllib.request

API = "https://purge.jsdelivr.net"
REPO = "Daiwangyang/tvbox-probe"
BRANCH = "main"
FILES = ("my.json", "sites.json")


def purge(path: str) -> bool:
    url = f"{API}/gh/{REPO}@{BRANCH}/{path}"
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        print(f"  {path} → 失败 {e}")
        return False
    # 接口就算 200,单文件也可能 pending/failed,内容里有状态字段
    if '"status":"finished"' in body or '"status": "finished"' in body:
        print(f"  {path} → ok")
        return True
    print(f"  {path} → 未确认 {body[:120]}")
    return False


def main() -> int:
    ok = 0
    for _ in range(2):  # CDN 边缘节点多,打两次提高覆盖面
        for f in FILES:
            ok += purge(f)
        time.sleep(2)
    print(f"purge 完成 {ok}/{len(FILES) * 2}")
    return 0  # 失败也不该挡住流水线


if __name__ == "__main__":
    sys.exit(main())
