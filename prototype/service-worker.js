"use strict";

// Financial data and authenticated responses are always fetched from the server.
self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() => new Response(
    "3Q 서버에 연결할 수 없습니다. 맥과 HTTPS 터널 연결을 확인해 주세요.",
    { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } },
  )));
});
