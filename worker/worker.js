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
    const su = `${WSY_SITE}/index.php/ajax/suggest?mid=1&wd=` + encodeURIComponent(wd);
    const r = await getText(su, `${WSY_SITE}/`, 9000);
    let items = [];
    if (r.status === 200) {
      try {
        const j = JSON.parse(r.text);
        items = (j.list || [])
          .filter(function (x) { return x && x.id && x.name; })
          .slice(0, 20);
      } catch (e) {
        /* 忽略 */
      }
    }
    if (!items.length) {
      return wsyJson({ code: 1, msg: "ok", page: 1, pagecount: 1,
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
    items.sort(function (a, b) { return score(b.name) - score(a.name); });
    const d = await getText(
      `${WSY}?ac=detail&ids=` + items.map(function (x) { return x.id; }).join(","),
      null, 16000);
    if (d.status === 200) {
      try {
        const j = JSON.parse(d.text);
        if (j && j.list) {
          j.class = WSY_CLASS;
          j.total = j.list.length;
          return wsyJson(j);
        }
      } catch (e) {
        /* 落到下面的空结果 */
      }
    }
    return wsyJson({ code: 1, msg: "ok", page: 1, pagecount: 1,
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
