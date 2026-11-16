/**
 * 配置中转 + 77dpw(66大片网) 适配
 *
 * 原有职责：把 my.json 从 GitHub 镜像中转出来，带 sig 防盗链 + 60s 缓存。
 *
 * 新增职责：把 www.77dpw.vip 的片源转成 TVBox 标准 CMS 接口。
 *   背景：该站 api.php 已关闭(返回 closed)、站内搜索有图形验证码，只能
 *         (a) 抓分类页 HTML 拿列表  (b) 走 index.php/ajax/suggest 做搜索
 *         (c) 抓详情页 HTML 拿剧集
 *   播放：播放页里的 player_aaaa.url 是 base64+URL编码的「平台原始链接」
 *         (腾讯/B站/爱奇艺/芒果)，必须现场走 qlplayer 两步解析成 m3u8。
 *         而 m3u8 清单 30 秒就 404（分片是持久 CDN 地址），所以 /77play
 *         每次请求都重新解析一次，永远给最新清单。
 *
 * 注意：影视仓播放器不支持弹幕渲染，弹幕数据只对网页播放器有意义。
 *
 * 部署：
 *   npx wrangler secret put SIGN
 *   npx wrangler deploy
 *
 * 用：https://<你的域名>/my.json?sig=<SIGN>
 *     https://<你的域名>/77?sig=<SIGN>   (TVBox 站点接口)
 */

const REPO = "Daiwangyang/tvbox-probe";
const FILE = "my.json";
const BRANCH = "main";

// 60 秒。和上游镜像的 max-age 对齐,不叠出自己的缓存层
const MAX_AGE = 60;

// 按实测速度排序。ghproxy.net 不稳,挪到 raw 之后
const MIRRORS = [
  (p) => `https://gh-proxy.com/https://raw.githubusercontent.com/${REPO}/${BRANCH}${p}`,
  (p) => `https://ghfast.top/https://raw.githubusercontent.com/${REPO}/${BRANCH}${p}`,
  (p) => `https://raw.githubusercontent.com/${REPO}/${BRANCH}${p}`,
  (p) => `https://ghproxy.net/https://raw.githubusercontent.com/${REPO}/${BRANCH}${p}`,
  (p) => `https://cdn.jsdelivr.net/gh/${REPO}@${BRANCH}${p}`,
];

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,OPTIONS",
};

// ===================== 77dpw 适配层 =====================

const DP = "https://www.77dpw.vip";
const QL = "https://svip.qlplayer.cyou/";
const UA77 =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";
const H77 = {
  "User-Agent": UA77,
  Accept: "text/html,application/xhtml+xml,*/*;q=0.8",
  "Accept-Language": "zh-CN,zh;q=0.9",
};

// 站点只有这 5 个分类
const CLASSES = [
  ["1", "电影"],
  ["2", "动漫"],
  ["3", "剧集"],
  ["4", "短剧"],
  ["5", "综艺"],
];

const CLASS_NAME = { "1": "电影", "2": "动漫", "3": "剧集", "4": "短剧", "5": "综艺" };

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&#\d+;/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function stripTags(s) {
  return String(s || "").replace(/<[^>]+>/g, "");
}

/** 带超时的文本抓取。失败返回 {status:0}，不抛错。 */
async function getText(url, referer, timeoutMs) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs || 12000);
  try {
    const h = Object.assign({}, H77);
    if (referer) h.Referer = referer;
    const r = await fetch(url, {
      headers: h,
      signal: ctl.signal,
      redirect: "follow",
    });
    return { status: r.status, text: await r.text() };
  } catch (e) {
    return { status: 0, text: "" };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * 从列表/分类/搜索页解析影片卡片。
 * 卡片形态:
 *   <a href="/voddetail/142838.html" title="名字" class="module-poster-item ...">
 *     ...<div class="module-item-note">备注</div>...<img data-original="海报" ...
 * 用 split 而不是回溯正则,避免在大 HTML 上把 CPU 打爆。
 */
function parseCards(html) {
  const out = [];
  const seen = {};
  const parts = html.split('<a href="/voddetail/');
  for (let i = 1; i < parts.length; i++) {
    const seg = parts[i];
    const m = seg.match(/^(\d+)\.html"\s+title="([^"]*)"/);
    if (!m) continue;
    const id = m[1];
    if (seen[id]) continue;
    seen[id] = 1;
    const chunk = seg.slice(0, 1600);
    const pic = (chunk.match(/data-original="([^"]+)"/) || [])[1] || "";
    const note = (chunk.match(/module-item-note">([^<]*)</) || [])[1] || "";
    out.push({ id: id, name: esc(m[2]), pic: esc(pic), note: esc(note) });
  }
  return out;
}

/** 从播放页里抽出 player_aaaa 这个 JSON。 */
function extractPlayer(html) {
  const i = html.indexOf("player_aaaa=");
  if (i < 0) return null;
  const j = html.indexOf("{", i);
  if (j < 0) return null;
  let depth = 0;
  for (let k = j; k < html.length; k++) {
    const c = html[k];
    if (c === "{") depth++;
    else if (c === "}") {
      depth--;
      if (depth === 0) {
        try {
          return JSON.parse(html.slice(j, k + 1));
        } catch (e) {
          return null;
        }
      }
    }
  }
  return null;
}

/** 抓平台原始链接（腾讯/B站…）。播放页里是 base64 + URL 编码双层。 */
async function platformUrl(vodId, sid, nid) {
  const pageUrl = `${DP}/vodplay/${vodId}-${sid}-${nid}.html`;
  const r = await getText(pageUrl, `${DP}/voddetail/${vodId}.html`);
  if (r.status !== 200) return { err: `播放页 HTTP ${r.status}` };
  if (r.text.indexOf("系统安全验证") >= 0) return { err: "播放页被验证码拦截" };
  const d = extractPlayer(r.text);
  if (!d || !d.url) return { err: "播放页无 player 数据" };
  let real;
  try {
    real = decodeURIComponent(atob(d.url));
  } catch (e) {
    return { err: "播放地址解码失败" };
  }
  if (!/^https?:\/\//i.test(real)) return { err: "解码结果不是 URL" };
  return { url: real, from: d.from || "", referer: pageUrl };
}

/** 走 qlplayer 两步：先取 token，再 resolve.php 换 m3u8。 */
async function resolveQL(videoUrl) {
  const ref = QL + "?" + new URLSearchParams({ url: videoUrl }).toString();
  let r = await getText(ref, "", 15000);
  if (r.status !== 200) return null;
  const i = r.text.indexOf("apiToken");
  if (i < 0) return null;
  const tok = r.text.slice(i).split('"')[1];
  if (!tok) return null;

  const ru = QL + "api/resolve.php?" + new URLSearchParams({ token: tok }).toString();
  r = await getText(ru, ref, 15000);
  if (r.status !== 200) return null;
  try {
    const j = JSON.parse(r.text);
    if (j && j.code === 200 && j.url) return String(j.url);
  } catch (e) {
    /* 非 JSON 就当失败 */
  }
  return null;
}

/** m3u8 里的相对路径补成绝对，避免播放器解析不到。 */
function normalizeM3u8(text, base) {
  const lines = text.split(/\r?\n/);
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const t = line.trim();
    if (!t) {
      out.push(line);
      continue;
    }
    if (t.charAt(0) === "#") {
      out.push(
        line.replace(/URI="([^"]+)"/g, function (_, u) {
          try {
            return 'URI="' + new URL(u, base).toString() + '"';
          } catch (e) {
            return 'URI="' + u + '"';
          }
        })
      );
    } else if (/^https?:\/\//i.test(t)) {
      out.push(t);
    } else {
      try {
        out.push(new URL(t, base).toString());
      } catch (e) {
        out.push(t);
      }
    }
  }
  return out.join("\n");
}

/** 生成一集的播放地址（指向本 worker 的 /77play）。 */
function epUrl(origin, vodId, sid, nid) {
  return `${origin}/77play?id=${vodId}&s=${sid}&e=${nid}`;
}

/** 详情页 -> {name, pic, desc, year, groups:{sid:[{nid,label}]}} */
function parseDetail(html) {
  const name = esc(stripTags((html.match(/<h1[^>]*>([\s\S]*?)<\/h1>/) || [])[1] || ""));
  const pic = esc((html.match(/<img[^>]+data-original="([^"]+)"/) || [])[1] || "");
  const desc = esc(
    stripTags(
      (html.match(/module-info-introduction-content[^>]*>([\s\S]*?)<\/div>/) || [])[1] || ""
    )
  ).slice(0, 800);
  const ym = html.match(/href="\/vodshow\/\d+-+\-+(\d{4})\.html"/);
  const year = ym ? ym[1] : "";

  const groups = {};
  const re =
    /<a[^>]*class="[^"]*module-play-list-link[^"]*"[^>]*href="\/vodplay\/(\d+)-(\d+)-(\d+)\.html"[^>]*>([\s\S]*?)<\/a>/g;
  let m;
  while ((m = re.exec(html))) {
    const sid = m[2];
    const nid = parseInt(m[3], 10);
    const label = esc(stripTags(m[4])) || String(nid);
    if (!groups[sid]) groups[sid] = [];
    groups[sid].push({ nid: nid, label: label });
  }
  return { name: name, pic: pic, desc: desc, year: year, groups: groups };
}

/** 拼 TVBox 的 vod_play_from / vod_play_url。 */
function buildPlay(origin, vodId, groups) {
  const sids = Object.keys(groups).sort(function (a, b) {
    return Number(a) - Number(b);
  });
  const froms = [];
  const urls = [];
  for (let i = 0; i < sids.length; i++) {
    const sid = sids[i];
    const eps = groups[sid].slice().sort(function (a, b) {
      return a.nid - b.nid;
    });
    if (!eps.length) continue;
    froms.push(`线路${sid}`);
    urls.push(
      eps
        .map(function (e) {
          return e.label + "$" + epUrl(origin, vodId, sid, e.nid);
        })
        .join("#")
    );
  }
  return { from: froms.join("$$$"), url: urls.join("$$$") };
}

/** 详情接口：?ids=1,2,3 */
async function detailList(ids, origin) {
  const out = [];
  for (let i = 0; i < ids.length; i++) {
    const id = ids[i];
    const r = await getText(`${DP}/voddetail/${id}.html`, `${DP}/`);
    if (r.status !== 200) continue;
    const d = parseDetail(r.text);
    const play = buildPlay(origin, id, d.groups);
    out.push({
      vod_id: id,
      vod_name: d.name || `影片${id}`,
      vod_pic: d.pic,
      vod_remarks: "",
      vod_year: d.year,
      vod_area: "",
      vod_lang: "",
      vod_actor: "",
      vod_director: "",
      vod_content: d.desc,
      vod_play_from: play.from,
      vod_play_url: play.url,
    });
  }
  return out;
}

function wrapList(items) {
  return items.map(function (c) {
    return {
      vod_id: c.id,
      vod_name: c.name,
      vod_pic: c.pic,
      vod_remarks: c.note,
    };
  });
}

/** CMS 列表/搜索/详情统一出口。 */
async function handleCms(url) {
  const origin = url.origin;
  const ac = url.searchParams.get("ac") || "list";
  const wd = (url.searchParams.get("wd") || "").trim();
  const ids = (url.searchParams.get("ids") || "").trim();
  const t = url.searchParams.get("t") || "";
  const pg = Math.max(1, parseInt(url.searchParams.get("pg") || "1", 10) || 1);

  const classes = CLASSES.map(function (c) {
    return { type_id: c[0], type_name: c[1] };
  });

  // 详情
  if (ids && (ac === "detail" || ac === "videolist")) {
    const list = await detailList(ids.split(",").slice(0, 8), origin);
    return { list: list, class: classes, page: 1, pagecount: 1, limit: list.length, total: list.length };
  }

  // 搜索：走 suggest（站内搜索有图形验证码，这个接口没有）
  if (wd) {
    const su = `${DP}/index.php/ajax/suggest?mid=1&wd=` + encodeURIComponent(wd);
    const r = await getText(su, `${DP}/`);
    let items = [];
    let total = 0;
    if (r.status === 200) {
      try {
        const j = JSON.parse(r.text);
        if (j && j.list && j.list.length) {
          items = j.list.map(function (x) {
            return { id: String(x.id), name: esc(x.name), pic: esc(x.pic), note: "" };
          });
          total = j.total || items.length;
        }
      } catch (e) {
        /* 忽略 */
      }
    }
    return {
      list: wrapList(items),
      class: classes,
      page: 1,
      pagecount: 1,
      limit: items.length,
      total: total,
    };
  }

  // 分类列表
  const tid = CLASS_NAME[t] ? t : "1";
  const listUrl = pg <= 1 ? `${DP}/vodtype/${tid}.html` : `${DP}/vodtype/${tid}-${pg}.html`;
  const r = await getText(listUrl, `${DP}/`);
  let items = [];
  if (r.status === 200) items = parseCards(r.text);

  return {
    list: wrapList(items),
    class: classes,
    page: pg,
    pagecount: 999,
    limit: items.length,
    total: 99999,
  };
}

/** 播放代理：现场解析 -> 拉最新 m3u8 内容 -> 回给播放器。 */
async function handlePlay(url) {
  const id = url.searchParams.get("id");
  const sid = url.searchParams.get("s");
  const nid = url.searchParams.get("e");
  if (!id || !sid || !nid) {
    return new Response("bad params", { status: 400, headers: CORS });
  }

  // 解析一次要 5~7 秒(qlplayer 的 resolve 慢)，同一集短时间内重复请求直接复用。
  // 分片令牌有效期约 20 小时，缓存 120 秒是安全的。
  const cache = caches.default;
  const ck = new Request(`https://c77.internal/p/${id}-${sid}-${nid}`);
  const hit = await cache.match(ck);
  if (hit) return hit;

  const p = await platformUrl(id, sid, nid);
  if (p.err) {
    return new Response(p.err, { status: 502, headers: CORS });
  }

  let m3u8Url = p.url;
  const isPlatform = /v\.qq\.com|bilibili\.com|iqiyi\.com|mgtv\.com|youku\.com/i.test(p.url);
  if (isPlatform) {
    const resolved = await resolveQL(p.url);
    if (!resolved) {
      return new Response("qlplayer 解析失败", { status: 502, headers: CORS });
    }
    m3u8Url = resolved;
  }

  const m = await getText(m3u8Url, p.referer, 15000);
  if (m.status !== 200) {
    return new Response("m3u8 HTTP " + m.status, { status: 502, headers: CORS });
  }
  const body = m.text.trimStart().indexOf("#EXTM3U") === 0
    ? normalizeM3u8(m.text, m3u8Url)
    : m.text;

  const res = new Response(body, {
    status: 200,
    headers: Object.assign({}, CORS, {
      "Content-Type": "application/vnd.apple.mpegurl",
      "Cache-Control": "public, max-age=120",
    }),
  });
  const toCache = res.clone();
  toCache.headers.set("Cache-Control", "public, max-age=120");
  await cache.put(ck, toCache);
  return res;
}

// ===================== 无损云(wsyzy) 适配层 =====================
// api.wsyzy.net 本身就是标准 MacCMS JSON 接口,列表/详情/播放地址都是直连 m3u8,
// 所以只补两个缺口:
//   1. 官方把搜索关了(ac=videolist&wd= 返回"暂不支持搜索")-> 改走 wsyzy.cc 的 suggest
//   2. 分类表里混着空父类(电影/电视剧/综艺/动漫)+ 一批成人分区 -> 重写 class 字段
const WSY = "https://api.wsyzy.net/api.php/provide/vod/";
const WSY_SITE = "https://wsyzy.cc";

// 只保留有内容的叶子分类;1/2/3/4 是空父类(t=1 实测返回 0 条),
// 55~61/73 是成人/擦边分区,直接丢掉
const WSY_KEEP = [
  [6, "动作片"], [7, "喜剧片"], [8, "爱情片"], [9, "科幻片"], [10, "恐怖片"],
  [11, "剧情片"], [12, "战争片"], [13, "国产剧"], [14, "欧美剧"], [15, "韩剧"],
  [16, "日剧"], [17, "港剧"], [18, "台剧"], [19, "泰剧"], [20, "纪录片"],
  [23, "海外剧"], [25, "大陆综艺"], [26, "日韩综艺"], [27, "港台综艺"],
  [28, "欧美综艺"], [29, "国产动漫"], [30, "日韩动漫"], [31, "欧美动漫"],
  [39, "动画片"], [44, "港台动漫"], [45, "海外动漫"], [47, "演唱会"],
  [48, "体育赛事"], [49, "篮球"], [50, "足球"], [52, "斯诺克"],
  [53, "影视解说"], [54, "爽文短剧"], [62, "4K电影"], [63, "有声动漫"],
  [64, "女频恋爱"], [65, "反转爽剧"], [66, "古装仙侠"], [67, "年代穿越"],
  [68, "脑洞悬疑"], [69, "现代都市"], [70, "邵氏电影"], [71, "Netflix电影"],
  [72, "Netflix自制剧"], [75, "科普学习"], [76, "漫剧"],
];
const WSY_CLASS = WSY_KEEP.map(function (c) {
  return { type_id: c[0], type_name: c[1] };
});

function wsyJson(obj) {
  return new Response(JSON.stringify(obj), {
    status: 200,
    headers: Object.assign({}, CORS, {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    }),
  });
}

async function handleWsy(url) {
  const ac = url.searchParams.get("ac") || "list";
  const wd = (url.searchParams.get("wd") || "").trim();
  const ids = (url.searchParams.get("ids") || "").trim();

  // ---- 搜索:官方接口不支持,拆成 suggest 拿 id + detail 拿详情 ----
  if (wd) {
    // 站点的 suggest 时快时慢(实测 2~6 秒),同一个词条 10 分钟内复用结果
    const cache = caches.default;
    const ck = new Request(
      `https://wsy-search.internal/s/` +
      new URLSearchParams({ wd: wd }).toString());
    const hit = await cache.match(ck);
    if (hit) return hit;

    const finish = async function (doc) {
      const res = wsyJson(doc);
      const copy = res.clone();
      copy.headers.set("Cache-Control", "public, max-age=600");
      await cache.put(ck, copy);
      return res;
    };

    const su = `${WSY_SITE}/index.php/ajax/suggest?mid=1&wd=` +
      encodeURIComponent(wd) + "&limit=60";
    const r = await getText(su, `${WSY_SITE}/`, 12000);
    let items = [];
    if (r.status === 200) {
      try {
        const j = JSON.parse(r.text);
        // 默认只给 20 条,正片常被"XX之XXX"这类短剧挤出前 20(实测搜"狂飙"
        // 精确命中在第 30 位之后),所以要 limit=60 再自己排。
        items = (j.list || []).filter(function (x) { return x && x.id && x.name; });
      } catch (e) {
        /* 忽略 */
      }
    }
    if (!items.length) {
      return finish({ code: 1, msg: "ok", page: 1, pagecount: 1,
                      limit: 0, total: 0, list: [], class: WSY_CLASS });
    }
    // suggest 是按站点索引顺序出的,精确命中的正片常排在第 4~6 位。
    // 按"完全相等 > 前缀 > 包含"重排,免得搜"庆余年"第一个结果是《庆余年之帝王业》这类短剧。
    const score = function (n) {
      if (n === wd) return 4;
      if (n.indexOf(wd) === 0) return 3;
      if (n.indexOf(wd) >= 0) return 2;
      return 1;
    };
    items.sort(function (a, b) {
      const d = score(b.name) - score(a.name);
      // 同级里短的更接近原名:"庆余年第二季" 该排在 "庆余年之风起沧州" 前面
      return d !== 0 ? d : a.name.length - b.name.length;
    });
    // 排序后精确/前缀命中一定在最前面,所以取 20 条既能保证正片在列,
    // 又只发 1 次 detail(上游单次上限正好 20),搜索耗时从 4s 降到 2s
    items = items.slice(0, 20);

    // 上游两个坑:
    //   1. detail 每次最多认 20 个 ids,传 60 个只回前 20 条 —— 必须分批拉
    //   2. detail 不认传进去的 ids 顺序,是它自己的排序 —— 拿到后要自己重排
    const order = {};
    items.forEach(function (x, i) { order[String(x.id)] = i; });
    const chunks = [];
    for (let i = 0; i < items.length; i += 20) {
      chunks.push(items.slice(i, i + 20).map(function (x) { return x.id; }));
    }
    const got = await Promise.all(chunks.map(function (c) {
      return getText(`${WSY}?ac=detail&ids=` + c.join(","), null, 16000);
    }));
    const merged = [];
    const seen = {};
    for (const d of got) {
      if (d.status !== 200) continue;
      try {
        const j = JSON.parse(d.text);
        (j.list || []).forEach(function (v) {
          const k = String(v.vod_id);
          if (seen[k]) return;
          seen[k] = 1;
          merged.push(v);
        });
      } catch (e) {
        /* 单批失败不影响其余 */
      }
    }
    if (merged.length) {
      merged.sort(function (a, b) {
        const ia = order[String(a.vod_id)];
        const ib = order[String(b.vod_id)];
        return (ia === undefined ? 999 : ia) - (ib === undefined ? 999 : ib);
      });
      return finish({ code: 1, msg: "ok", page: 1, pagecount: 1,
                      limit: merged.length, total: merged.length,
                      list: merged, class: WSY_CLASS });
    }
    return finish({ code: 1, msg: "ok", page: 1, pagecount: 1,
                    limit: 0, total: 0, list: [], class: WSY_CLASS });
  }

  // ---- 详情 / 分类列表:原样透传,只改写 class ----
  const passthrough = ["ac", "ids", "t", "pg", "h", "from"];
  const qs = new URLSearchParams();
  passthrough.forEach(function (k) {
    const v = url.searchParams.get(k);
    if (v !== null) qs.set(k, v);
  });
  if (!qs.get("ac")) qs.set("ac", ac);

  const r = await getText(`${WSY}?` + qs.toString(), null, 22000);
  if (r.status !== 200) {
    return wsyJson({ code: 0, msg: "upstream " + r.status, page: 1,
                     pagecount: 0, limit: 0, total: 0, list: [], class: WSY_CLASS });
  }
  try {
    const j = JSON.parse(r.text);
    j.class = WSY_CLASS;
    return wsyJson(j);
  } catch (e) {
    return wsyJson({ code: 0, msg: "bad json", page: 1, pagecount: 0,
                     limit: 0, total: 0, list: [], class: WSY_CLASS });
  }
}

// ===================== 抖音直播 =====================
// 思路:live.douyin.com 的分类页 SSR HTML 里就写着在播房间的 web_rid,
// 拿 web_rid 调 webcast/room/web/enter 能拿到 stream_url 里的 HLS 地址,
// 这些 CDN 地址本身不需要 referer/cookie,任何播放器都能直接拉。
// 坑:enter 接口不带 cookie 会返回非 JSON(拿不到房间),必须先访问首页取 ttwid。
const DY_HOST = "https://live.douyin.com";
const DY_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";
const DY_BASE = {
  aid: "6383", app_name: "douyin_web", live_id: "1",
  device_platform: "web", language: "zh-CN", enter_from: "web_live",
  cookie_enabled: "true", screen_width: "1920", screen_height: "1080",
  browser_language: "zh-CN", browser_platform: "Win32",
  browser_name: "Chrome", browser_version: "126.0.0.0",
};
// ===================== Y5影院 适配层 =====================
// 站点:www.dec4wvi.com(同一套源码轮换的别名还有 ivdy.com、y2s52n7.com)
// 前台是 Next.js,真接口在 /mw-movie/anonymous/... 下,全部要签名:
//   t        = 毫秒时间戳
//   sign     = sha1( md5( 按键排序的 query 串 + "&key=<KEY>&t=<t>" ) )
//   deviceId = 任意 UUID(服务端不校验内容,只当幂等键)
// Worker 没有内置 md5(crypto.subtle 只给 SHA-1/256/512),所以下面自带一份。
//
// 播放:/v2/video/episode/url 按「剧 id + 集 nid」现签现发一条 ppvod CDN 直链,
// 带 auth_key(有时效),所以 /y5pl 每次请求都重新换一次,绝不做长缓存。
// 实测:清单 200、分片 200 + video/mp2t、ffmpeg 播 6 秒无错、1080p 无水印无台标。
//
// !! 重要:这套代码目前在 Cloudflare 上跑不通,不要写进 my.json !!
// 上游 CDN 是「按请求方 IP 签发地址」的:国内 IP 拿到的是 ppvod011 + auth_key
// (与 IP 无关),境外 IP 一律发到 kqgfbs + whip=<发起方IP> + sign,而 sign 包含
// whip,改不了;试过三种绕法全失败:
//   1. 改 whip 为客户端 IP      → 403 sign not match
//   2. 透传 X-Forwarded-For     → 上游不采信,whip 仍是 Cloudflare 出口 IP
//   3. worker 自己拉清单再转发  → Cloudflare 出口 IP 每次请求都在变
//                                 (实测 104.23.172.34 / .35 交替),签发时的 IP
//                                 和拉取时的 IP 对不上,照样 403
// 结论:必须有一台国内服务器做这层中转(1 核 1G 足够)。签名和接口逻辑是好的,
// 搬到国内机器上就能用,所以代码保留,只是不挂路由、不进配置。

const Y5 = "https://www.dec4wvi.com";
const Y5_MW = "/mw-movie";
const Y5_KEY = "cb808529bae6b6be45ecfab29a4889bc";
const Y5_DEV = "3f2a91d7-6c48-4b0e-9a51-7d2c8134b6ef";
const Y5_PAGE = 20;

// 站点只有这 5 个大类(/anonymous/get/filer/type 实测返回的就是它们)
const Y5_CLASS = [
  { type_id: 1, type_name: "电影" },
  { type_id: 2, type_name: "电视剧" },
  { type_id: 3, type_name: "综艺" },
  { type_id: 4, type_name: "动漫" },
  { type_id: 88, type_name: "短剧" },
];

/** MD5 → 小写 hex。Worker 环境无内置实现,按 RFC 1321 手写。 */
function y5md5(input) {
  const bytes = new TextEncoder().encode(input);
  const ml = bytes.length;
  const nBlocks = ((ml + 8) >> 6) + 1;
  const w = new Int32Array(nBlocks * 16);
  for (let i = 0; i < ml; i++) w[i >> 2] |= bytes[i] << ((i & 3) << 3);
  w[ml >> 2] |= 0x80 << ((ml & 3) << 3);
  w[nBlocks * 16 - 2] = (ml * 8) | 0;
  w[nBlocks * 16 - 1] = Math.floor(ml / 536870912);

  let a0 = 0x67452301, b0 = 0xefcdab89 | 0, c0 = 0x98badcfe | 0, d0 = 0x10325476;
  const K = new Int32Array(64);
  for (let i = 0; i < 64; i++) K[i] = Math.floor(Math.abs(Math.sin(i + 1)) * 4294967296) | 0;
  const S = [7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
    5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
    4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
    6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21];
  for (let blk = 0; blk < nBlocks; blk++) {
    const o = blk * 16;
    let A = a0, B = b0, C = c0, D = d0;
    for (let i = 0; i < 64; i++) {
      let F, g;
      if (i < 16) { F = (B & C) | (~B & D); g = i; }
      else if (i < 32) { F = (D & B) | (~D & C); g = (5 * i + 1) & 15; }
      else if (i < 48) { F = B ^ C ^ D; g = (3 * i + 5) & 15; }
      else { F = C ^ (B | ~D); g = (7 * i) & 15; }
      const tmp = D;
      D = C; C = B;
      const sum = (A + F + K[i] + w[o + g]) | 0;
      B = (B + ((sum << S[i]) | (sum >>> (32 - S[i])))) | 0;
      A = tmp;
    }
    a0 = (a0 + A) | 0; b0 = (b0 + B) | 0; c0 = (c0 + C) | 0; d0 = (d0 + D) | 0;
  }
  const out = [];
  [a0, b0, c0, d0].forEach(function (v) {
    for (let i = 0; i < 4; i++) {
      out.push(((v >>> (i * 8)) & 0xff).toString(16).padStart(2, "0"));
    }
  });
  return out.join("");
}

async function y5sha1(s) {
  const buf = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(s));
  return Array.prototype.map.call(new Uint8Array(buf), function (x) {
    return x.toString(16).padStart(2, "0");
  }).join("");
}

/**
 * 调 Y5 后端。params 里空值会剔掉(签名要对"实际发出的参数"算,
 * 带上空串会和站点自己的签名口径不一致)。
 */
async function y5Call(path, params, ip) {
  const p = {};
  Object.keys(params || {}).sort().forEach(function (k) {
    const v = params[k];
    if (v === null || v === undefined || v === "") return;
    p[k] = String(v);
  });
  const ks = Object.keys(p);
  const t = Date.now();
  const plain = ks.map(function (k) { return k + "=" + p[k]; }).join("&");
  const h = (plain ? plain + "&" : "") + "key=" + Y5_KEY + "&t=" + t;
  const sign = await y5sha1(y5md5(h));
  const qs = ks.map(function (k) {
    return k + "=" + encodeURIComponent(p[k]);
  }).join("&");

  const ctl = new AbortController();
  const timer = setTimeout(function () { ctl.abort(); }, 12000);
  try {
    const r = await fetch(`${Y5}${Y5_MW}${path}` + (qs ? "?" + qs : ""), {
      headers: {
        "User-Agent": UA77,
        Accept: "application/json",
        sign: sign,
        t: String(t),
        deviceId: Y5_DEV,
        Referer: Y5 + "/",
        // 上游 CDN 按「请求方 IP」签发地址(境外 IP 会被发到绑 IP 的线路),
        // 把播放器的真实公网 IP 透传过去,争取让它按这个 IP 签发
        ...(ip ? { "X-Forwarded-For": ip, "x-original-forwarded-for": ip } : {}),
      },
      signal: ctl.signal,
    });
    if (!r.ok) return null;
    return await r.json();
  } catch (e) {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

function y5Json(obj) {
  return new Response(JSON.stringify(obj), {
    status: 200,
    headers: Object.assign({}, CORS, {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    }),
  });
}

/** 单条记录 → 苹果CMS 的 vod_* 字段。 */
function y5Item(v) {
  return {
    vod_id: v.vodId,
    vod_name: v.vodName || "",
    vod_pic: v.vodPic || "",
    vod_remarks: v.vodRemarks || "",
    vod_year: v.vodYear ? String(v.vodYear) : "",
    vod_area: v.vodArea || "",
    vod_lang: v.vodLang || "",
    vod_actor: v.vodActor || "",
    vod_director: v.vodDirector || "",
    vod_content: stripTags(String(v.vodContent || v.vodBlurb || "")),
    vod_score: v.vodScore ? String(v.vodScore) : "",
    type_id: v.typeId1 || v.typeId || 0,
    type_name: v.typeName || "",
  };
}

/** 一集的播放地址:指向本 worker,点开那一刻才去换新鲜的 auth_key。 */
function y5Ep(origin, id, nid) {
  return `${origin}/y5pl?id=${id}&nid=${nid}`;
}

async function handleY5(url) {
  const ac = url.searchParams.get("ac") || "list";
  const wd = (url.searchParams.get("wd") || "").trim();
  const ids = (url.searchParams.get("ids") || "").trim();
  const tid = (url.searchParams.get("t") || "").trim();
  let pg = parseInt(url.searchParams.get("pg") || "1", 10);
  if (!(pg > 0)) pg = 1;

  // ---- 详情:顺带把剧集表拼成 vod_play_url ----
  if (ac === "detail" && ids) {
    const list = [];
    for (const raw of ids.split(",").slice(0, 10)) {
      const id = raw.trim();
      if (!/^\d+$/.test(id)) continue;
      const j = await y5Call("/anonymous/video/detail", { id: id });
      const d = j && j.data;
      if (!d) continue;
      const it = y5Item(d);
      const eps = (d.episodeList || []).slice().sort(function (a, b) {
        return (a.sort || 0) - (b.sort || 0);
      });
      it.vod_play_from = "y5";
      it.vod_play_url = eps.map(function (e) {
        return (e.name || "正片") + "$" + y5Ep(url.origin, d.vodId, e.nid);
      }).join("#");
      if (!it.vod_remarks && eps.length) it.vod_remarks = eps.length + "集";
      list.push(it);
    }
    return y5Json({ list: list });
  }

  // ---- 搜索 ----
  if (wd) {
    const j = await y5Call("/anonymous/video/searchByWordPageable", {
      keyword: wd, pageNum: pg, pageSize: Y5_PAGE,
    });
    const d = (j && j.data) || {};
    const list = (d.list || []).map(y5Item);
    const total = d.totalCount || list.length;
    return y5Json({
      page: pg,
      pagecount: Math.max(1, Math.ceil(total / Y5_PAGE)),
      limit: Y5_PAGE,
      total: total,
      list: list,
    });
  }

  // ---- 分类列表(首页也走这条) ----
  const params = { pageNum: pg, pageSize: Y5_PAGE };
  if (/^\d+$/.test(tid)) {
    // 1/2/3/4/88 是大类,按 type1 查;其余当作具体 typeId
    if (["1", "2", "3", "4", "88"].indexOf(tid) >= 0) params.type1 = tid;
    else params.type = tid;
  } else {
    // 不带分类时上游返回空数组,默认落到「电影」,否则首页一片空白
    params.type1 = "1";
  }
  const j = await y5Call("/anonymous/video/list", params);
  const d = (j && j.data) || {};
  const list = (d.list || []).map(y5Item);
  const total = d.totalCount || list.length;
  return y5Json({
    class: Y5_CLASS,
    page: pg,
    pagecount: Math.max(1, Math.ceil(total / Y5_PAGE)),
    limit: Y5_PAGE,
    total: total,
    list: list,
  });
}

/**
 * 单集播放:现签现取一条直链,再把清单里的相对分片补成绝对地址。
 * 不做 ts 代理 —— 分片走 CDN 直连,worker 只出这一份几百 KB 的清单。
 */
async function handleY5Play(url, req) {
  const id = (url.searchParams.get("id") || "").trim();
  const nid = (url.searchParams.get("nid") || "").trim();
  if (!/^\d+$/.test(id) || !/^\d+$/.test(nid)) {
    return new Response("bad id", { status: 400, headers: CORS });
  }
  const ip = (req && (req.headers.get("cf-connecting-ip") ||
                      req.headers.get("x-forwarded-for") || "")) || "";
  const j = await y5Call("/anonymous/v2/video/episode/url", {
    clientType: 1, id: id, nid: nid,
  }, ip.split(",")[0].trim());
  const list = ((j && j.data && j.data.list) || []).filter(function (x) {
    return x && x.url && /^https?:/i.test(x.url);
  });
  if (!list.length) {
    return new Response("no source", { status: 404, headers: CORS });
  }
  // 同一条片子会给 1080/720/480 好几档,按分辨率取最高
  list.sort(function (a, b) { return (b.resolution || 0) - (a.resolution || 0); });
  const pick = list[0];

  // 注意 referer 一定不能带:ppvod 的 CDN 看到 referer 就把请求 302 到
  // kqgfbs 那条线,并在 URL 里写死「发起方 IP」(whip=...)。worker 的出口 IP
  // 是 Cloudflare 的,播放器一换 IP 立刻 403。不带 referer 拿到的
  // auth_key 直链才是跟 IP 无关的那一档。
  const r = await getText(pick.url, null, 8000);
  if (r.status !== 200 || !r.text) {
    // 清单拉不到就 302 给原始地址,让播放器自己去跟
    return new Response(null, {
      status: 302,
      headers: Object.assign({}, CORS, {
        Location: pick.url,
        "Cache-Control": "no-store",
      }),
    });
  }
  return new Response(normalizeM3u8(r.text, pick.url), {
    status: 200,
    headers: Object.assign({}, CORS, {
      "Content-Type": "application/vnd.apple.mpegurl",
      "Cache-Control": "no-store",
    }),
  });
}

// 1 热门 2 游戏 3 娱乐 4 才艺 5 生活 (编号不对只是少抓,不会报错)
const DY_CATS = ["1", "2", "3"];

let dyCk = { val: "", exp: 0 };

async function dyCookie() {
  const now = Date.now();
  if (dyCk.val && now < dyCk.exp) return dyCk.val;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 12000);
  try {
    const r = await fetch(DY_HOST + "/", {
      headers: { "User-Agent": DY_UA, Accept: "text/html,application/xhtml+xml" },
      signal: ctl.signal,
    });
    await r.text();  // 必须把 body 读掉,cookie 才算真发下来
    const list = typeof r.headers.getSetCookie === "function"
      ? r.headers.getSetCookie()
      : [r.headers.get("set-cookie") || ""];
    const parts = [];
    for (const c of list) {
      const m = String(c || "").match(/(ttwid|UIFID_TEMP|msToken)=([^;,\s]+)/);
      if (m) parts.push(m[1] + "=" + m[2]);
    }
    dyCk = { val: parts.join("; "), exp: now + 25 * 60 * 1000 };
  } catch (e) {
    /* 拿不到就空 cookie 硬试 */
  } finally {
    clearTimeout(timer);
  }
  return dyCk.val;
}

async function dyFetch(url, cookie, accept, timeoutMs) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs || 10000);
  try {
    const h = { "User-Agent": DY_UA, Referer: DY_HOST + "/", Accept: accept };
    if (cookie) h.Cookie = cookie;
    const r = await fetch(url, { headers: h, signal: ctl.signal, redirect: "follow" });
    return { status: r.status, text: r.status === 200 ? await r.text() : "" };
  } catch (e) {
    return { status: 0, text: "" };
  } finally {
    clearTimeout(timer);
  }
}

/** 分类页里挖在播房间的 web_rid。 */
async function dyRids(cookie) {
  const pages = await Promise.all(DY_CATS.map(function (c) {
    return dyFetch(`${DY_HOST}/category/${c}`, cookie, "text/html", 12000);
  }));
  const out = [];
  const seen = {};
  for (const p of pages) {
    if (p.status !== 200) continue;
    const re = /web_rid\\?"\s*:\s*\\?"(\d{5,})/g;
    let m;
    while ((m = re.exec(p.text)) !== null) {
      const rid = m[1];
      if (seen[rid]) continue;
      seen[rid] = 1;
      out.push(rid);
    }
  }
  return out;
}

/** 单个房间:只要还在播(status=2)且有 HLS 就返回一条直播。 */
async function dyRoom(rid, cookie) {
  const q = new URLSearchParams(DY_BASE);
  q.set("web_rid", rid);
  const r = await dyFetch(`${DY_HOST}/webcast/room/web/enter/?` + q.toString(),
                          cookie, "application/json", 9000);
  if (r.status !== 200) return null;
  let j;
  try { j = JSON.parse(r.text); } catch (e) { return null; }
  const room = (((j.data || {}).data) || [])[0];
  if (!room || room.status !== 2) return null;
  const map = (room.stream_url || {}).hls_pull_url_map || {};
  const pick = map.FULL_HD1 || map.HD1 || map.SD1 || map.SD2 ||
    (Object.keys(map).length ? map[Object.keys(map)[0]] : "");
  if (!pick) return null;
  return {
    nick: ((room.owner || {}).nickname || "") || ("房间" + rid),
    title: String(room.title || ""),
    url: String(pick),
  };
}

async function handleDyLive(url) {
  const cache = caches.default;
  const manual = (url.searchParams.get("rids") || "").trim();
  const ck = new Request("https://dy-live.internal/m3u/" + (manual || "hot"));
  const hit = await cache.match(ck);

  const cookie = await dyCookie();
  let rids = manual
    ? manual.split(",").map(function (s) { return s.trim(); }).filter(Boolean)
    : await dyRids(cookie);

  if (!rids.length) {
    // 一个都没抓到,宁可回上次的缓存,也别给播放器一个空列表
    if (hit) return hit;
    return new Response("#EXTM3U\n", {
      status: 200,
      headers: { "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8" },
    });
  }
  rids = rids.slice(0, 26);

  const rooms = (await Promise.all(rids.map(function (rid) {
    return dyRoom(rid, cookie);
  }))).filter(Boolean);

  const lines = ["#EXTM3U"];
  for (const r of rooms) {
    const name = r.title ? `${r.nick} · ${r.title}` : r.nick;
    lines.push(`#EXTINF:-1 tvg-name="${r.nick}" group-title="抖音直播",${name}`);
    lines.push(r.url);
  }

  const res = new Response(lines.join("\n") + "\n", {
    status: 200,
    headers: Object.assign({}, CORS, {
      "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
      // 地址里带 CDN 过期参数,不能长缓存;3 分钟够用且不至于每次都重抓
      "Cache-Control": "public, max-age=180",
    }),
  });
  const copy = res.clone();
  copy.headers.set("Cache-Control", "public, max-age=180");
  await cache.put(ck, copy);
  return res;
}

// ===================== CCTV5 兜底直播 =====================
// 配置里原来那条 CCTV5 来自 fanmingming 的 ipv6.m3u,里面是
// http://[2409:8087:...]:6610/... 这种 IPv6 字面地址的移动内网源,
// 没有 IPv6 或不是移动宽带的网络就是一片空白。这里出一条 IPv4 直连的,
// 实测 1080p 可解(CCTV5 体育,含 NBA 每周转播场次)。
const CCTV_LINES = [
  ["CCTV5·IPv4直连", "http://gmxw.7766.org:808/hls/93/index.m3u8"],
];

function handleCctv() {
  const out = ["#EXTM3U"];
  for (const [name, u] of CCTV_LINES) {
    out.push(
      `#EXTINF:-1 tvg-name="CCTV5" tvg-logo="https://live.fanmingming.cn/tv/CCTV5.png" group-title="体育",${name}`
    );
    out.push(u);
  }
  return new Response(out.join("\n") + "\n", {
    headers: Object.assign({}, CORS, {
      "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
      "Cache-Control": "public, max-age=300",
    }),
  });
}

// ===================== 主入口 =====================

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });

    const url = new URL(req.url);
    const path = url.pathname;

    // 77dpw 相关接口不校验 sig：
    //   1. /77play 的地址要写进配置，TVBox 播放时不会带额外参数
    //   2. /77 的 api 也要写进 my.json，而 my.json 在公开仓库里，
    //      写 sig 进去等于把 sig 公开，所以这里只靠「地址不公开」保护
    if (path === "/77play") return handlePlay(url);

    // 无损云适配层,同样免 sig(api 地址要写进公开的 my.json)
    if (path === "/wsy") return handleWsy(url);

    // Y5影院适配层:api 地址要写进 my.json,所以同样免 sig
    if (path === "/y5") return handleY5(url);
    if (path === "/y5pl") return handleY5Play(url, req);

    // 抖音直播:每次现抓现生成 m3u,地址写进 lives
    if (path === "/dylive" || path === "/dylive.m3u") return handleDyLive(url);

    // CCTV5 兜底线路(IPv4),给没有 IPv6 的网络用
    if (path === "/cctv" || path === "/cctv.m3u") return handleCctv();

    if (path === "/77") {
      const doc = await handleCms(url);
      return new Response(JSON.stringify(doc), {
        status: 200,
        headers: Object.assign({}, CORS, {
          "Content-Type": "application/json; charset=utf-8",
          "Cache-Control": "no-store",
        }),
      });
    }

    const sig = url.searchParams.get("sig");
    if (env.SIGN && sig !== env.SIGN) {
      return new Response("bad sig", {
        status: 401,
        headers: Object.assign({}, CORS, { "Cache-Control": "no-store" }),
      });
    }

    // 以下是原有的 my.json 中转
    const filePath = path === "/" ? `/${FILE}` : path;

    // 带 sig 的响应缓存本身分不开、也不该被别的请求复用,所以用 Cache API
    // 自己存一份。不带 sig 时 Cache-Control 头只是给浏览器看的,边缘不一定会缓存。
    // key 里带 KV 版本号:改了缓存策略就把它加一,免得被旧条目按旧 TTL 卡住。
    const KV = "v8";
    const cache = caches.default;
    const cacheKey = new Request(new URL(`/${KV}${filePath}`, url.origin).toString());
    const hit = await cache.match(cacheKey);
    if (hit) return hit;

    let body = null;
    let lastStatus = 502;
    for (const mk of MIRRORS) {
      // 一家给 8 秒,五家全试最多 40 秒,不会把客户端吊死
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), 8000);
      try {
        // 关掉上游自带的 cacheEverything:它在边缘另存一份并按上游头算 TTL
        // (实测 4 小时),会和我们的 60 秒打架。只走上面那层 Cache API。
        const res = await fetch(mk(filePath), {
          headers: { "User-Agent": "tvbox-probe-worker", Accept: "*/*" },
          signal: ctl.signal,
          cf: { cacheEverything: false },
        });
        if (res.ok) {
          body = await res.arrayBuffer();
          clearTimeout(timer);
          break;
        }
        lastStatus = res.status;
      } catch (e) {
        lastStatus = 504;
      } finally {
        clearTimeout(timer);
      }
    }

    if (!body) {
      // 全挂:吐一条短缓存的错误,让客户端快点重试而不是死等
      return new Response(`upstream ${lastStatus}`, {
        status: lastStatus,
        headers: Object.assign({}, CORS, {
          "Cache-Control": `public, max-age=${MAX_AGE}`,
        }),
      });
    }

    const out = new Response(body, {
      status: 200,
      headers: Object.assign({}, CORS, {
        "Content-Type": filePath.endsWith(".json")
          ? "application/json; charset=utf-8"
          : "text/plain; charset=utf-8",
        "Cache-Control": `public, max-age=${MAX_AGE}`,
      }),
    });
    // clone 才能既存又发。存之前显式压一遍 Cache-Control:
    // 不压的话 Cache API 会沿用上游镜像的头(可能 4 小时),轮次就跟不上了。
    // 注意 out.headers 是 Headers 实例,用 {...} 展开会得到空对象,必须 clone 后 set。
    const forCache = out.clone();
    forCache.headers.set("Cache-Control", `public, max-age=${MAX_AGE}`);
    await cache.put(cacheKey, forCache);
    return out;
  },
};
