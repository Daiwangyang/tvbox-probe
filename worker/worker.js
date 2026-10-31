/**
 * 配置中转。为什么非要有这一层:
 *
 *   1. raw.githubusercontent.com 在国内超时(实测 15s 无响应),投影仪直连不了。
 *   2. jsDelivr 能通,但给分支引用发 s-maxage=43200(12 小时),即便调 purge
 *      接口边缘节点也未必马上换(实测 4 分钟仍返回旧文件)。半小时探测就没意义了。
 *   3. 公共反代(gh-proxy 一类)发的是 max-age=60,天然跟得上轮次,
 *      但它们单点、说关就关,所以按顺序多备几家,挂了自动往下走。
 *
 * 仓库是公开的,不需要 PAT,直连各镜像即可。
 *
 * 部署:
 *   npx wrangler secret put SIGN
 *   npx wrangler deploy
 *
 * 用:https://<你的worker>.workers.dev/my.json?sig=<SIGN>
 */

const REPO = "Daiwangyang/tvbox-probe";
const FILE = "my.json";
const BRANCH = "main";

// 60 秒。和上游镜像的 max-age 对齐,不叠出自己的缓存层
const MAX_AGE = 60;

// 按实测速度排序。前两个先试,后面的只是兜底
const MIRRORS = [
  (p) => `https://gh-proxy.com/https://raw.githubusercontent.com/${REPO}/${BRANCH}${p}`,
  (p) => `https://ghfast.top/https://raw.githubusercontent.com/${REPO}/${BRANCH}${p}`,
  (p) => `https://ghproxy.net/https://raw.githubusercontent.com/${REPO}/${BRANCH}${p}`,
  (p) => `https://raw.githubusercontent.com/${REPO}/${BRANCH}${p}`,
  (p) => `https://cdn.jsdelivr.net/gh/${REPO}@${BRANCH}${p}`,
];

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
      return new Response("bad sig", {
        status: 401,
        headers: { ...CORS, "Cache-Control": "no-store" },
      });
    }

    const path = url.pathname === "/" ? `/${FILE}` : url.pathname;

    let body = null;
    let lastStatus = 502;
    for (const mk of MIRRORS) {
      // 一家给 8 秒,五家全试最多 40 秒,不会把客户端吊死
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), 8000);
      try {
        const res = await fetch(mk(path), {
          headers: { "User-Agent": "tvbox-probe-worker", Accept: "*/*" },
          signal: ctl.signal,
          cf: { cacheTtl: MAX_AGE, cacheEverything: true },
        });
        if (res.ok) {
          body = await res.arrayBuffer();
          clearTimeout(timer);
          break;
        }
        lastStatus = res.status;
      } catch {
        lastStatus = 504;
      } finally {
        clearTimeout(timer);
      }
    }

    if (!body) {
      // 全挂:吐一条短缓存的错误,让客户端快点重试而不是死等
      return new Response(`upstream ${lastStatus}`, {
        status: lastStatus,
        headers: { ...CORS, "Cache-Control": `public, max-age=${MAX_AGE}` },
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
