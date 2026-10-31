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

先说结论：**投影仪上填镜像地址，不要填 `workers.dev`。**

`workers.dev` 这个域名国内基本不可达（实测直连、走代理、DoH 都拿不到，20 秒超时），而 GitHub raw 本身也是 15 秒不响应。能用的只有第三方反代。实测：

| 通道 | 结果 |
| --- | --- |
| `raw.githubusercontent.com` | 15s 超时 |
| `workers.dev` | 不通 |
| `cdn.jsdelivr.net` | 通，但发 `s-maxage=43200`，purge 后 4 分钟仍是旧文件 |
| `gh-proxy.com` | 通，0.7–2.9s，`max-age=60`，**内容实时** |
| `ghfast.top` | 通，1.4s，可作备用 |

所以要填的是：

```
https://gh-proxy.com/https://raw.githubusercontent.com/Daiwangyang/tvbox-probe/main/my.json
```

`gh-proxy.com` 挂了就换 `ghfast.top` 或 `ghproxy.net`，把域名替换掉即可，路径不变。

### 那 Worker 还要不要

要，但只当加 `sig` 的壳。它按顺序试五家上游（反代优先，raw 和 jsDelivr 兜底），每 8 秒换下一家，自己发 `max-age=60`。缺点是 `workers.dev` 国内不通，所以只有在你另配了自定义域名、或客户端能走代理时才有用。

```bash
cd worker
npx wrangler secret put SIGN
npx wrangler deploy
# → https://<你的worker>.workers.dev/my.json?sig=<SIGN>
```

不套 Worker 就等于不设防：仓库公开，谁看到地址都能用。想挡人的话，把上面那条镜像地址里的 `Daiwangyang` 换成别人猜不到的 fork 也行。

## 缓存这一关别绕开

成品要从 GitHub 拉到投影仪，中间隔着一层 CDN。jsDelivr 对分支引用缓存 12 小时，**每 30 分钟探一轮、中间 11 个半小时拿的还是旧文件**，探测频率白做。

`purge.py` 每轮提交后主动推 `purge.jsdelivr.net` 清缓存（见 workflow 最后一步）。实测 purge 接口虽然返回 `finished`，边缘节点仍会滞后几分钟——这也是最终选反代而不是 jsDelivr 的原因：反代发的是 `max-age=60`，天然对齐轮次。

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
- 直播源(`lives`)没做
- 依赖第三方反代，它们单点、说关就关；挂了要手动换域名
- 公开仓库 Actions 不限时长；转回私有就只剩 2000 分钟/月，得把 cron 退回 `0 */4`

片源均为未授权，脚本只汇总接口 URL，不托管内容。
