# tvbox-probe

给影视仓用的源筛选器。公开仓库，每 30 分钟自跑一次，输出一份固定地址的配置。

解决的问题：**换配置丢历史、每次要一个个试、配置要跳小程序看广告。**

## 它怎么工作

```
种子源(65条) → 展开 400 个候选 → 真实搜索测延迟 → 剔除引流和死站
  → 校验 jar 有没有对应类 → 按延迟和流畅度排序 → my.json
```

`my.json` 的地址永远不变。投影仪填一次，之后不动。

## 三个关键设计

**历史不会丢**

影视仓的历史绑在站点 `key` 上。网上配置用 `p1/p2` 这类序号，换一份就全乱。
这里的 `key` 由域名和路径哈希算出，不带序号，跨轮次实测零漂移。
只要站还在名单里，不管你看到第几集都还在。

**广告配置会被剔掉**

配置拉回来不是 JSON、而是“关注公众号 / 跳小程序 / 扫码”的授权页时，
整条源直接扔掉。这类和站点跳解析页、视频中插广告都不是一回事。

**jar 类校验**

`csp_Xxx` 站的 api 不是 URL，要靠 jar 里的 `com/github/catvod/spider/Xxx` 类跑。
脚本会读主 jar 的 dex 类表，没有对应类的站直接跳过——留着也是白屏。

## 稳定性

- **基线优先** `sites.json` 里的站测不通也降级保留，不马上删
- **连续淘汰** 连续 2 轮不通才真删，防抖
- **延迟阈值** 超 1500ms 不进成品
- **失败放行** jar 下载失败时按“可能有”处理，不误杀
- 全挂也不怕，上一次的 `my.json` 还在

## 访问

投影仪上填：

```
https://cf.ddngo.space/my.json?sig=Q_wVNi5yQy7UK7Gc
```

填一次，之后不动。域名是自己的，不依赖第三方反代。

`sig` 是密码，漏出去就换一串：

```bash
cd worker
npx wrangler secret put SIGN
npx wrangler deploy
```

### 为什么不直接用 workers.dev / raw / jsDelivr

都实测过：

| 通道 | 结果 |
| --- | --- |
| `raw.githubusercontent.com` | 15s 超时 |
| `*.workers.dev` | 不通（直连、代理、DoH 全 20s 超时）|
| `cdn.jsdelivr.net` | 通，但分支引用发 `s-maxage=43200`，调 purge 后 4 分钟仍吐旧文件 |
| `cf.ddngo.space` | 通，1.2–2.1s，**60 秒过期后确实回源** |
| `gh-proxy.com` / `ghfast.top` | 通，作 Worker 的回源上游 |

绑自己域名走的是 Cloudflare 的另一条入口，绕过了失效的 workers.dev。

### 回源顺序

Worker 按下面顺序试，每家 8 秒超时，挂了自动下一家：

1. `gh-proxy.com`
2. `ghfast.top`
3. `ghproxy.net`
4. `raw.githubusercontent.com`
5. `cdn.jsdelivr.net`

命中的结果进 Cache API 存 60 秒，再发出去。

## 本地跑

```bash
python fetch_seeds.py     # 拉种子(自动走镜像)
python serve.py           # 面板,浏览器开 127.0.0.1:5678
python probe.py           # 或者直接用命令行
python playtest.py        # 播放实测(需要 ffmpeg)
```

`serve.py` 是本地视觉面板：延迟表格、一键重测、局域网地址。适合想在电脑上盯着看的时候。

## 产物

| 文件 | 说明 |
| --- | --- |
| `my.json` | 成品，填到影视仓「配置地址」 |
| `report.md` | 报告，含被剔除的引流源 |
| `sites.json` | 手工基线，优先保留 |
| `play.json` | 播放实测结果 |
| `quarantine.json` | 淘汰计数 |
| `purge.py` | 推 jsDelivr 清缓存，workflow 自动调 |

## 参数

`probe.py` 顶部：`TIMEOUT`、`WORKERS`、`MAX_MS`、`DROP_AFTER`、`KEYWORDS`。
`PROBE_PROXY` 环境变量只用于拉配置——**测站点固定直连**，因为电视上没有代理。

## 已知局限

- 引流判定是启发式，会漏；有些站是首次要求、之后放行，单轮判不准
- jar 只能校验类是否存在，类在但站死了看不出来
- 播放实测只覆盖前 14 个 CMS 站（`MAX_CANDIDATES`），csp 站测不了——它们的地址要 spider 跑出来
- 403/404 的站会被判不可播沉底，但不删；片源本身抽风时可能误判一轮
- 直播源(`lives`)没做
- 回源仍靠第三方反代，但它们只是 Worker 的上游，任一挂掉会自动跳到下一家，多个全挂才影响更新
- Cloudflare 免费版在国内不保证速度，实测 1.2–2.1s 尚可，恶化时把投影仪换回 `gh-proxy.com` 直连地址
- 公开仓库 Actions 不限时长；转回私有就只剩 2000 分钟/月，得把 cron 退回 `0 */4`

片源均为未授权，脚本只汇总接口 URL，不托管内容。
