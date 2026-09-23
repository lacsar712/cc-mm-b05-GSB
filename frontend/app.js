const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const raiseForm = document.querySelector("#raiseForm");
const thresholdState = document.querySelector("#thresholdState");
const raiseRows = document.querySelector("#raiseRows");

function fmtTime(iso) {
  return new Date(iso).toLocaleString();
}

function paint(list) {
  rows.innerHTML = list
    .map(
      (r) =>
        `<tr><td>${r.site}</td><td>${r.ch4_pct}</td><td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td><td>${r.note}</td></tr>`,
    )
    .join("");
}

function paintThresholds(data) {
  if (data.active_raise) {
    const left = Math.max(0, Math.round((new Date(data.active_raise.expires_at) - new Date(data.server_time)) / 1000));
    thresholdState.innerHTML = `当前报警线 <strong class="temp">${data.current}%</strong>（临时，约 ${left} 秒后失效，自动回到 ${data.default}%）`;
  } else {
    thresholdState.innerHTML = `当前报警线 <strong>${data.current}%</strong>（标准线）`;
  }
  raiseRows.innerHTML = data.history
    .map(
      (r) =>
        `<tr><td class="temp">${r.threshold}</td><td>${fmtTime(r.created_at)}</td><td>${fmtTime(r.expires_at)}</td><td>${r.created_by}</td></tr>`,
    )
    .join("");
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

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  // 旁观账号只读：抬线表单仅检查员可见，履历对所有人只读展示
  raiseForm.hidden = role !== "writer";
  connect();
  load();
  loadThresholds();
  // 轮询当前线，临时线失效后状态自动回到 1%
  setInterval(loadThresholds, 5000);
}

async function load() {
  paint(await api("/api/readings"));
}

async function loadThresholds() {
  try {
    paintThresholds(await api("/api/thresholds"));
  } catch (err) {
    thresholdState.textContent = err.message;
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送报警：${row.site} ${row.level}`;
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
    live.textContent = r.level === "报警" ? "已上报：报警" : `已上报：正常（按当前线 ${r.threshold}% 判定，不推送报警）`;
    load();
  } catch (err) {
    live.textContent = err.message;
  }
};

raiseForm.onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/thresholds", {
      method: "POST",
      body: JSON.stringify({
        threshold: Number(document.querySelector("#raiseValue").value),
        valid_seconds: Number(document.querySelector("#raiseSeconds").value),
      }),
    });
    await loadThresholds();
  } catch (err) {
    thresholdState.textContent = err.message;
  }
};

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
