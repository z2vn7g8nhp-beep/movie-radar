/* 电影雷达离线缓存：
   - 网页和数据：先联网取最新的；没网、或者 3 秒还连不上，就用上次的，从主屏打开不会白屏
   - 海报：取过一次就留着，最多 400 张
   - GitHub 接口、日历文件、跨域的 CORS 请求（票根存图用）都不经过缓存 */
const SHELL = "radar-shell-v2", IMG = "radar-img-v1";

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(["./", "./manifest.json", "./icon-180.png", "./icon-192.png"])).catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL && k !== IMG).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

async function trim(cache, max){
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - max; i++) await cache.delete(keys[i]);
}

self.addEventListener("fetch", e => {
  const r = e.request;
  if (r.method !== "GET") return;
  const u = new URL(r.url);
  if (u.origin === location.origin) {
    if (u.pathname.includes("/cal/") || u.pathname.endsWith("/sw.js")) return;
    // 去掉 ?t= 这类防缓存参数，同一个文件只存一份
    const key = u.origin + (r.mode === "navigate" ? u.pathname.replace(/index\.html$/, "") : u.pathname);
    // 国内连 github.io 时好时坏：3 秒还没回来就先用上次存的（不再一直白屏），新的在后台存好，下次打开就是新的
    e.respondWith((async () => {
      const net = fetch(r).then(res => {
        if (res.ok) { const cp = res.clone(); caches.open(SHELL).then(c => c.put(key, cp)); }
        return res;
      });
      const cached = async () => (await caches.match(key)) || (r.mode === "navigate" && await caches.match(u.origin + u.pathname.replace(/[^/]*$/, ""))) || null;
      try {
        const first = await Promise.race([net, new Promise(res => setTimeout(res, 3000, "slow"))]);
        if (first !== "slow") return first;
        const c = await cached();
        if (c) { e.waitUntil(net.catch(() => {})); return c; }
        return await net;
      } catch {
        return (await cached()) || Response.error();
      }
    })());
    return;
  }
  // 海报：只缓存 <img> 发起的普通请求（no-cors），CORS 请求原样放行
  if (r.destination === "image" && r.mode === "no-cors" && /(^|\.)pipi\.cn$|maoyan|meituan|alicdn/.test(u.hostname)) {
    e.respondWith(caches.open(IMG).then(async c => {
      const hit = await c.match(r);
      if (hit) return hit;
      const res = await fetch(r);
      if (res.ok || res.type === "opaque") { c.put(r, res.clone()).then(() => trim(c, 400)).catch(() => {}); }
      return res;
    }));
  }
});
