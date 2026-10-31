const state = {
  properties: [],
  tasks: [],
  settings: {},
  currentTask: null,
};

const STATUS = {
  active: "已匹配",
  needs_review: "待确认",
  received: "已接收",
  script_ready: "草稿待审核",
  exported: "已导出",
  processing: "处理中",
  review: "待审片",
  completed: "已完成",
  failed: "失败",
};

const VIEW_META = {
  dashboard: ["OVERVIEW", "工作台"],
  properties: ["LISTING DATABASE", "房源库"],
  tasks: ["VIDEO PIPELINE", "视频任务"],
  settings: ["LOCAL CONFIGURATION", "系统设置"],
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function statusPill(status) {
  return `<span class="status-pill ${escapeHtml(status)}">${escapeHtml(STATUS[status] || status)}</span>`;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

let toastTimer;
function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("show"), 3600);
}

async function api(path, options = {}) {
  const config = { ...options };
  if (config.body && !(config.body instanceof FormData)) {
    config.headers = { "Content-Type": "application/json", ...(config.headers || {}) };
  }
  const response = await fetch(path, config);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    const detail = payload?.detail;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || `请求失败 (${response.status})`));
  }
  return payload;
}

function busy(form, isBusy, busyText = "处理中…") {
  const button = form.querySelector('button[type="submit"]');
  if (!button) return;
  if (isBusy) {
    button.dataset.originalText = button.textContent;
    button.textContent = busyText;
  } else if (button.dataset.originalText) {
    button.textContent = button.dataset.originalText;
  }
  button.disabled = isBusy;
}

function showView(name) {
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  $("#page-eyebrow").textContent = VIEW_META[name][0];
  $("#page-title").textContent = VIEW_META[name][1];
  if (name === "tasks") loadTasks();
  if (name === "properties") loadProperties();
  if (name === "dashboard") loadDashboard();
}

async function loadDashboard() {
  const data = await api("/api/dashboard");
  $("#stat-properties").textContent = data.property_count;
  $("#stat-review").textContent = data.review_count;
  $("#stat-tasks").textContent = data.task_count;
  $("#stat-completed").textContent = data.completed_count;
  $("#nav-property-count").textContent = data.property_count;
  $("#recent-tasks").innerHTML = data.recent_tasks.length ? data.recent_tasks.map((task) => `
    <div class="task-row" data-task="${task.id}" role="button" tabindex="0">
      <span class="number">${escapeHtml(task.task_no)}</span>
      <div><strong>${escapeHtml(task.property_label || "未命名房源")}</strong><small>${escapeHtml(task.original_filename)}</small></div>
      <span>${escapeHtml(task.aspect_ratio)} · ${task.target_duration} 秒</span>
      ${statusPill(task.status)}
    </div>`).join("") : `<div class="empty"><strong>还没有视频任务</strong>导入房源并上传第一段实拍视频，即可开始。</div>`;
}

async function loadProperties() {
  const search = encodeURIComponent($("#property-search").value.trim());
  const status = encodeURIComponent($("#property-status").value);
  const data = await api(`/api/properties?limit=1000&search=${search}&status=${status}`);
  state.properties = data.items;
  $("#property-total").textContent = `共 ${data.total} 条${data.total > data.items.length ? `，当前显示 ${data.items.length} 条` : ""}`;
  $("#property-rows").innerHTML = data.items.length ? data.items.map((item) => `
    <tr>
      <td><strong>${escapeHtml([item.region, item.building, item.room].filter(Boolean).join(" · ") || "未完整房源")}</strong><small class="mono">ID ${escapeHtml(item.id.slice(0, 8))}</small></td>
      <td><strong>${escapeHtml(item.area || "面积待补充")}</strong><small>${escapeHtml(item.partition || "格局待补充")}</small></td>
      <td>${escapeHtml(item.rent || "—")}<small>口径需确认</small></td>
      <td>${escapeHtml(item.source_sheet)}<small>第 ${item.source_row} 行</small></td>
      <td>${item.is_new_marked ? '<span class="mark new">新增</span>' : ""}${item.is_special_marked ? '<span class="mark special">特别</span>' : ""}${!item.is_new_marked && !item.is_special_marked ? "—" : ""}</td>
      <td>${statusPill(item.status)}</td>
    </tr>`).join("") : `<tr><td colspan="6"><div class="empty"><strong>没有匹配的房源</strong>调整搜索条件，或导入房源 Excel。</div></td></tr>`;
  fillPropertyOptions();
}

function fillPropertyOptions() {
  const select = $("#task-property");
  const previous = select.value;
  select.innerHTML = state.properties.length
    ? '<option value="">请选择要绑定的房源</option>' + state.properties.map((item) => `<option value="${item.id}">${escapeHtml([item.region, item.building, item.room, item.area].filter(Boolean).join(" · "))}${item.status === "needs_review" ? "（待确认）" : ""}</option>`).join("")
    : '<option value="">请先导入房源</option>';
  select.value = previous;
}

async function loadTasks() {
  const data = await api("/api/tasks?limit=100");
  state.tasks = data.items;
  $("#task-total").textContent = `共 ${data.total} 条`;
  $("#task-rows").innerHTML = data.items.length ? data.items.map((task) => `
    <tr>
      <td><strong class="mono">${escapeHtml(task.task_no)}</strong></td>
      <td><strong>${escapeHtml(task.property_label)}</strong><small>${escapeHtml(task.area || "面积待补充")}</small></td>
      <td>${escapeHtml(task.original_filename)}<small>${escapeHtml(task.source)}</small></td>
      <td>${escapeHtml(task.aspect_ratio)} · ${task.target_duration} 秒</td>
      <td>${statusPill(task.status)}</td>
      <td>${formatDate(task.created_at)}</td>
      <td><button class="text-button" data-task="${task.id}">查看</button></td>
    </tr>`).join("") : `<tr><td colspan="7"><div class="empty"><strong>还没有视频任务</strong>新建任务后，可在这里审核口播并导出。</div></td></tr>`;
}

async function loadSettings() {
  const [settings, health, dataInfo] = await Promise.all([
    api("/api/settings"), api("/api/health"), api("/api/system/data-info"),
  ]);
  state.settings = settings;
  const form = $("#settings-form");
  Object.entries(settings).forEach(([key, value]) => {
    if (form.elements[key]) form.elements[key].value = value;
  });
  if (form.elements.default_duration) $("#task-form").elements.target_duration.value = settings.default_duration;
  $("#ffmpeg-status").textContent = health.ffmpeg_available ? "FFmpeg 已加入系统环境变量" : "FFmpeg 未配置；基础任务管理可用，视频处理前需安装";
  $("#ffmpeg-pill").textContent = health.ffmpeg_available ? "可用" : "待配置";
  $("#ffmpeg-pill").className = `status-pill ${health.ffmpeg_available ? "active" : "needs_review"}`;
  $("#data-directory").textContent = dataInfo.data_directory;
}

function openDialog(id) {
  const dialog = $(`#${id}`);
  if (!dialog.open) dialog.showModal();
}

function closeDialog(id) {
  const dialog = $(`#${id}`);
  if (id === "detail-dialog") {
    $("#detail-video").pause();
    $("#detail-video").removeAttribute("src");
    $("#detail-video").load();
  }
  dialog.close();
}

function openNewTask() {
  if (!state.properties.length) {
    toast("请先导入房源，再创建视频任务", true);
    openDialog("import-dialog");
    return;
  }
  fillPropertyOptions();
  openDialog("task-dialog");
}

async function openTask(taskId) {
  const task = await api(`/api/tasks/${taskId}`);
  state.currentTask = task;
  $("#detail-number").textContent = task.task_no;
  $("#detail-title").textContent = task.property_label || "视频任务";
  $("#detail-script").value = task.script;
  $("#detail-video").src = `/api/tasks/${task.id}/video`;
  $("#detail-warnings").innerHTML = task.warnings.map((warning) => `<div class="warning-item">${escapeHtml(warning)}</div>`).join("");
  $("#export-result").classList.add("hidden");
  $("#export-task").textContent = task.workbench_package_path ? "重新导出任务包" : "导出工作台任务包";
  openDialog("detail-dialog");
}

$("#import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  busy(form, true, "正在导入…");
  try {
    const result = await api("/api/properties/import", { method: "POST", body: new FormData(form) });
    $("#import-result").innerHTML = `<strong>导入完成</strong><br>读取 ${result.total_rows} 行；新增 ${result.inserted_rows}，更新 ${result.updated_rows}，跳过 ${result.skipped_rows}；待确认 ${result.review_rows}。`;
    $("#import-result").classList.remove("hidden");
    await Promise.all([loadProperties(), loadDashboard()]);
    toast("房源导入完成");
  } catch (error) { toast(error.message, true); }
  finally { busy(form, false); }
});

$("#task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  busy(form, true, "正在上传…");
  try {
    const task = await api("/api/tasks", { method: "POST", body: new FormData(form) });
    closeDialog("task-dialog");
    form.reset();
    await Promise.all([loadTasks(), loadDashboard()]);
    toast("视频已上传，口播草稿已生成");
    await openTask(task.id);
  } catch (error) { toast(error.message, true); }
  finally { busy(form, false); }
});

$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form).entries());
  values.default_duration = Number(values.default_duration);
  busy(form, true, "保存中…");
  try {
    state.settings = await api("/api/settings", { method: "PUT", body: JSON.stringify(values) });
    toast("设置已保存到本机");
  } catch (error) { toast(error.message, true); }
  finally { busy(form, false); }
});

$("#save-script").addEventListener("click", async () => {
  if (!state.currentTask) return;
  const button = $("#save-script");
  button.disabled = true;
  try {
    state.currentTask = await api(`/api/tasks/${state.currentTask.id}/script`, {
      method: "PUT", body: JSON.stringify({ script: $("#detail-script").value }),
    });
    await loadTasks();
    toast("口播草稿已保存");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});

$("#export-task").addEventListener("click", async () => {
  if (!state.currentTask) return;
  const button = $("#export-task");
  button.disabled = true;
  try {
    await api(`/api/tasks/${state.currentTask.id}/script`, {
      method: "PUT", body: JSON.stringify({ script: $("#detail-script").value }),
    });
    const result = await api(`/api/tasks/${state.currentTask.id}/export`, { method: "POST" });
    $("#export-result").innerHTML = `<strong>任务包已导出</strong><br>${escapeHtml(result.package_path)}<br>这只是交接包，不代表视频已渲染。`;
    $("#export-result").classList.remove("hidden");
    await Promise.all([loadTasks(), loadDashboard()]);
    button.textContent = "重新导出任务包";
    toast("工作台任务包已导出");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});

$$(".nav-item").forEach((item) => item.addEventListener("click", () => showView(item.dataset.view)));
$$('[data-go]').forEach((item) => item.addEventListener("click", () => showView(item.dataset.go)));
$$('[data-close]').forEach((item) => item.addEventListener("click", () => closeDialog(item.dataset.close)));
["#top-import", "#property-import"].forEach((selector) => $(selector).addEventListener("click", () => openDialog("import-dialog")));
["#top-new-task", "#hero-new-task", "#task-new"].forEach((selector) => $(selector).addEventListener("click", openNewTask));
$("#property-refresh").addEventListener("click", () => loadProperties().then(() => toast("房源列表已刷新")).catch((error) => toast(error.message, true)));
$("#property-status").addEventListener("change", () => loadProperties().catch((error) => toast(error.message, true)));
let searchTimer;
$("#property-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadProperties().catch((error) => toast(error.message, true)), 300);
});
$("#excel-drop input").addEventListener("change", (event) => {
  $("#excel-file-name").textContent = event.target.files[0]?.name || "";
  $("#import-result").classList.add("hidden");
});
$("#video-input").addEventListener("change", (event) => {
  const file = event.target.files[0];
  $("#video-file-name").textContent = file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB` : "未选择视频";
});
document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-task]");
  if (target) openTask(target.dataset.task).catch((error) => toast(error.message, true));
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.matches(".task-row")) openTask(event.target.dataset.task).catch((error) => toast(error.message, true));
});

Promise.all([loadDashboard(), loadProperties(), loadTasks(), loadSettings()])
  .catch((error) => toast(`初始化失败：${error.message}`, true));
