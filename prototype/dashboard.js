"use strict";

const byId = (id) => document.getElementById(id);
const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const kst = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});
const price = (cents) => usd.format(cents / 100);
const time = (second) => kst.format(new Date(second * 1000));
const metrics = document.querySelectorAll(".metric strong");
const captions = document.querySelectorAll(".metric p:last-child");
const instrument = document.querySelector(".instrument");
const marketTime = document.querySelector("#market > .caption");
const plot = document.querySelector(".chart");
const canvas = document.createElement("canvas");
canvas.id = "runtime-chart";
canvas.setAttribute("role", "img");
canvas.setAttribute("aria-label", "TQQQ 최근 120초 시세");
const status = byId("feed-status");
const toggle = byId("toggle");
const stateValues = document.querySelectorAll("#system dd");
const watchValues = document.querySelectorAll(".stack section:first-child dd");
const activity = document.querySelector(".activity");
let paused = false;
let updating = false;

function draw(points) {
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(canvas.clientWidth * ratio));
  canvas.height = Math.round(canvas.clientHeight * ratio);
  const ctx = canvas.getContext("2d");
  const { width, height } = canvas;
  const inset = height * 0.1;
  ctx.strokeStyle = "#2b3541";
  for (const fraction of [0.1, 0.3, 0.5, 0.7, 0.9]) {
    ctx.beginPath();
    ctx.moveTo(0, height * fraction);
    ctx.lineTo(width, height * fraction);
    ctx.stroke();
  }
  if (!points.length) return;

  const values = points.map(({ price }) => price);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = Math.max(1, points.at(-1).second - points[0].second);
  ctx.strokeStyle = "#75e2ba";
  ctx.lineWidth = 2 * ratio;
  let previous = null;
  ctx.beginPath();
  for (const point of points) {
    const x = 2 + (width - 4) * (point.second - points[0].second) / span;
    const fraction = high === low ? 0.5 : (high - point.price) / (high - low);
    const y = inset + (height - 2 * inset) * fraction;
    if (previous === null || point.second !== previous + 1) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
    previous = point.second;
  }
  ctx.stroke();
  canvas.setAttribute("aria-label", `TQQQ 최근 시세. 최저 ${price(low)}, 최고 ${price(high)}`);
}

function render(data) {
  const { account, latest, quotes, fills, chart, starting_cash: initial } = data;
  const equity = latest?.equity ?? initial;
  const pnl = equity - initial;
  const stale = !latest || Date.now() / 1000 - latest.second > 5;
  status.textContent = latest
    ? `${stale ? "수신 지연 · " : ""}마지막 수신 ${time(latest.second)} KST`
    : "시세 수신 대기";
  metrics[0].textContent = price(equity);
  metrics[1].textContent = (pnl > 0 ? "+" : "") + price(pnl);
  metrics[1].className = `num ${pnl < 0 ? "down" : "up"}`;
  captions[1].textContent = `${(100 * pnl / initial).toFixed(2)}% · 초기 자금 대비`;
  captions[1].className = metrics[1].className;
  metrics[2].textContent = account.symbol ? `${account.symbol} ${account.quantity}주` : "현금";
  captions[2].textContent = account.entry ? `평균 진입가 ${price(account.entry)}` : "보유 종목 없음";
  metrics[3].textContent = price(account.cash);
  instrument.querySelector("strong").textContent = quotes.TQQQ ? price(quotes.TQQQ) : "—";
  for (const [index, symbol] of ["TQQQ", "SQQQ", "QQQ"].entries()) {
    watchValues[index].textContent = quotes[symbol] ? price(quotes[symbol]) : "—";
  }
  draw(chart);
  marketTime.textContent = chart.length
    ? `최근 ${chart.length}개 샘플 · ${time(chart[0].second)} ~ ${time(chart.at(-1).second)} KST`
    : "시세 수신 대기";
  stateValues[1].textContent = stale ? "수신 대기 또는 지연" : "저장 중";
  stateValues[1].className = stale ? "down" : "up";
  stateValues[2].textContent = fills[0]?.notification ?? "체결 대기";
  paused = Boolean(account.paused);
  toggle.textContent = paused ? "모의 주문 재개" : "모의 주문 일시정지";
  toggle.disabled = updating;
  const table = document.querySelector("tbody");
  table.replaceChildren();
  for (const fill of fills) {
    const row = table.insertRow();
    for (const value of [time(fill.second), fill.symbol, fill.side === "BUY" ? "매수" : "매도",
                         `${fill.quantity}주`, price(fill.price), fill.reason]) {
      row.insertCell().textContent = value;
    }
  }
  if (!fills.length) {
    const cell = table.insertRow().insertCell();
    cell.colSpan = 6;
    cell.textContent = "아직 모의 체결이 없습니다.";
  }
  for (const [index, value] of [
    latest ? `${latest.signal} · ${latest.reason}` : "판단 대기",
    paused ? "일시정지 · 기존 포지션 유지" : "모의 주문 활성",
  ].entries()) {
    activity.children[index].textContent = value;
  }
}

async function refresh() {
  try {
    const response = await fetch("/api/state");
    if (!response.ok) throw new Error("State unavailable");
    render(await response.json());
  } catch {
    status.textContent = "서버 연결 끊김 · 화면에 마지막 데이터 표시";
    toggle.disabled = true;
  }
}

if (location.protocol === "http:") {
  byId("controls").removeAttribute("hidden");
  byId("mode").textContent = "모의매매 · 합성 시세";
  document.querySelector(".heading .caption").textContent = "로컬 데모 계좌";
  document.querySelector(".metric:nth-child(2) p").textContent = "누적 손익";
  document.querySelector(".session").textContent = "합성 시세";
  instrument.lastElementChild.remove();
  plot.replaceWith(canvas);
  stateValues[0].textContent = "데모 소스";
  stateValues[0].parentElement.querySelector("dt").textContent = "시세 입력";
  document.querySelector(".lower section:last-child h2").textContent = "최근 전략 판단";
  document.querySelector(".lower section:last-child .section-top .caption").remove();
  activity.lastElementChild.remove();
  const reasonHeading = document.createElement("th");
  reasonHeading.scope = "col";
  reasonHeading.textContent = "판단 근거";
  document.querySelector("thead tr").append(reasonHeading);
  byId("footer").textContent = "3Q 개발 빌드 · 로컬 모의매매";
  render({
    account: { cash: 0, paused: false }, latest: null, quotes: {}, fills: [],
    chart: [], starting_cash: 1,
  });
  for (const metric of metrics) metric.textContent = "—";
  captions[1].textContent = "계좌 조회 대기";
  toggle.disabled = true;
  toggle.addEventListener("click", async () => {
    updating = true;
    toggle.disabled = true;
    try {
      const response = await fetch(paused ? "/api/resume" : "/api/pause", { method: "POST" });
      if (!response.ok) throw new Error("Control unavailable");
      await refresh();
    } catch {
      status.textContent = "모의 주문 상태를 변경하지 못했습니다.";
    } finally {
      updating = false;
    }
  });
  async function poll() {
    if (!updating) await refresh();
    setTimeout(poll, 1000);
  }
  poll();
}
