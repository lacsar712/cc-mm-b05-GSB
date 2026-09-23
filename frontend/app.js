const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const historyRows = document.querySelector("#historyRows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const thresholdForm = document.querySelector("#thresholdForm");
const thresholdState = document.querySelector("#thresholdState");

function fmtTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString("zh-CN", { hour12: false });
}

function paint(list) {
  rows.innerHTML = list
    .map(
      (r) =>
        `<tr><td>${r.site}</td><td>${r.ch4_pct}</td><td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td><td>${r.note}</td></tr>`,
    )
    .join("");
}

function paintHistory(list) {
  historyRows.innerHTML = list
    .map(
      (h) =>
        `<tr><td>${h.threshold_pct}</td><td>${fmtTime(h.expires_at)}</td><td>${h.set_by}</td><td class="${h.active ? "temp-on" : ""}">${h.active ? "生效中" : "已失效"}</td></tr>`,
    )
    .join("");
}

function paintCurrent(cur) {
  if (cur.active) {
    thresholdState.textContent = `临时 ${cur.threshold_pct}%（${fmtTime(cur.expires_at)} 失效，${cur.set_by} 设定）`;
    thresholdState.className = "temp-on";
  } else {
    thresholdState.textContent = "1%（百分之一，无生效临时线）";
    thresholdState.className = "temp-off";
  }
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

async function loadThresholds() {
  const [cur, history] = await Promise.all([
    api("/api/thresholds/current"),
    api("/api/thresholds/history"),
  ]);
  paintCurrent(cur);
  paintHistory(history);
}

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  thresholdForm.hidden = role !== "writer";
  connect();
  load();
  loadThresholds();
  // 定时刷新，临时线到点失效后状态与履历自动回到百分之一。
  setInterval(loadThresholds, 2000);
}

async function load() {
  paint(await api("/api/readings"));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送报警：${row.site} ${row.ch4_pct}% ${row.level}`;
    load();
  };
}

document.querySelector("#go").onclick = async () => {
  const data = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      username: document.querySelector("#user").value,
      password: document.querySelector("#pass").value,
    }),
  });
  token = data.access_token;
  role = data.role;
  localStorage.setItem(tokenKey, token);
  localStorage.setItem("methane_role", role);
  showApp();
};

form.onsubmit = async (e) => {
  e.preventDefault();
  try {
    const r = await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value,
        ch4_pct: Number(document.querySelector("#ch4").value),
      }),
    });
    live.textContent = r.level === "报警"
      ? `已上报：${r.site} ${r.ch4_pct}% 判为报警，已推送`
      : `已上报：${r.site} ${r.ch4_pct}% 判为正常，不推送报警`;
    await load();
  } catch (err) {
    live.textContent = err.message;
  }
};

thresholdForm.onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/thresholds", {
      method: "POST",
      body: JSON.stringify({
        threshold_pct: Number(document.querySelector("#tempThreshold").value),
        expires_in_seconds: Number(document.querySelector("#tempSeconds").value),
      }),
    });
    live.textContent = "临时报警线已抬高并写入履历";
    await loadThresholds();
  } catch (err) {
    live.textContent = err.message;
  }
};

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
