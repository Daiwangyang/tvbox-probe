/**
 * 私有仓库代理 —— 让 jsDelivr 拒绝的私有仓库也能被投影仪读到。
 *
 * 原理:Worker 拿着 PAT 去 GitHub 取 raw,再吐出来。Worker 地址本身是公网可访问的,
 * 但源码在私有仓库里,别人拿不到、也没法改。
 *
 * 部署:
 *   1. 建一个 GitHub PAT(classic,fine-grained 也可,只要有 Contents: Read)
 *   2. npx wrangler secret put GH_TOKEN      ← 粘 PAT
 *   3. npx wrangler secret put SIGN          ← 随便一串密码,防止别人乱刷额度
 *   4. npx wrangler deploy
 *
 * 用:https://<你的worker>.workers.dev/my.json?sig=<SIGN>
 */

const REPO = "Daiwangyang/tvbox-probe";
const FILE = "my.json";
const BRANCH = "main";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,OPTIONS",
};

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });

    const url = new URL(req.url);
    const sig = url.searchParams.get("sig");
    // 有设 SIGN 就必须对上;没设就是全开
    if (env.SIGN && sig !== env.SIGN) {
      return new Response("bad sig", { status: 401, headers: CORS });
    }

    const path = url.pathname === "/" ? `/${FILE}` : url.pathname;
    const target = `https://raw.githubusercontent.com/${REPO}/${BRANCH}${path}`;

    const res = await fetch(target, {
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        "User-Agent": "tvbox-probe-worker",
        Accept: "*/*",
      },
      cf: { cacheTtl: 1800, cacheEverything: true },
    });

    if (!res.ok) {
      return new Response(`upstream ${res.status}`, { status: res.status, headers: CORS });
    }

    const body = await res.arrayBuffer();
    return new Response(body, {
      status: 200,
      headers: {
        ...CORS,
        "Content-Type": path.endsWith(".json")
          ? "application/json; charset=utf-8"
          : "text/plain; charset=utf-8",
        // 缓存半小时,和探测频率对齐
        "Cache-Control": "public, max-age=1800",
      },
    });
  },
};
