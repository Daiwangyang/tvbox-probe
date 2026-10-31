/**
 * 配置中转 —— 两层作用,和公开/私有没有关系:
 *
 *   1. 破缓存。jsDelivr 对分支引用缓存约 12 小时,jsDelivr 之外没法控制。
 *      这里自己设 max-age,把最旧版本压在 60 秒内,探测频率才有意义。
 *   2. 挡白嫖。地址带 sig,别人拿到地址也用不了(除非把 sig 传出去)。
 *
 * 仓库是公开的,所以不需要 PAT 了,直连 raw。
 *
 * 部署:
 *   npx wrangler secret put SIGN     ← 自定一串密码
 *   npx wrangler deploy
 *
 * 用:https://<你的worker>.workers.dev/my.json?sig=<SIGN>
 */

const REPO = "Daiwangyang/tvbox-probe";
const FILE = "my.json";
const BRANCH = "main";

// 60 秒。短到探测轮次跟得上,长到不至于每个请求都回源
const MAX_AGE = 60;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,OPTIONS",
};

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });

    const url = new URL(req.url);
    const sig = url.searchParams.get("sig");
    if (env.SIGN && sig !== env.SIGN) {
      return new Response("bad sig", { status: 401, headers: CORS });
    }

    const path = url.pathname === "/" ? `/${FILE}` : url.pathname;
    // raw.githubusercontent.com 国内常抖,抖动时退到 jsDelivr 兜底
    const targets = [
      `https://raw.githubusercontent.com/${REPO}/${BRANCH}${path}`,
      `https://cdn.jsdelivr.net/gh/${REPO}@${BRANCH}${path}`,
    ];

    let body = null;
    let lastStatus = 502;
    for (const t of targets) {
      try {
        const res = await fetch(t, {
          headers: { "User-Agent": "tvbox-probe-worker", Accept: "*/*" },
          cf: { cacheTtl: MAX_AGE, cacheEverything: true },
        });
        if (res.ok) {
          body = await res.arrayBuffer();
          break;
        }
        lastStatus = res.status;
      } catch {
        lastStatus = 502;
      }
    }

    if (!body) {
      // 两路都挂:吐一条 60 秒的 502,让客户端快点重试而不是死等
      return new Response(`upstream ${lastStatus}`, {
        status: lastStatus,
        headers: { ...CORS, "Cache-Control": "public, max-age=60" },
      });
    }

    return new Response(body, {
      status: 200,
      headers: {
        ...CORS,
        "Content-Type": path.endsWith(".json")
          ? "application/json; charset=utf-8"
          : "text/plain; charset=utf-8",
        "Cache-Control": `public, max-age=${MAX_AGE}`,
      },
    });
  },
};
