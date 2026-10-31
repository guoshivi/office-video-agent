const state = {
  properties: [],
  propertyOptions: [],
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

function renderProperties(items, total, emptyTitle = "没有匹配的房源") {
  state.properties = items;
  $("#property-total").textContent = `共 ${total} 条${total > items.length ? `，当前显示 ${items.length} 条` : ""}`;
  $("#property-rows").innerHTML = items.length ? items.map((item) => `
    <tr>
      <td><strong>${escapeHtml([item.region, item.building, item.room].filter(Boolean).join(" · ") || "未完整房源")}</strong><small class="mono">ID ${escapeHtml(item.id.slice(0, 8))}${item.score !== undefined ? ` · 相似度 ${item.score}` : ""}</small></td>
      <td><strong>${escapeHtml(item.area || "面积待补充")}</strong><small>${escapeHtml(item.partition || "格局待补充")}</small></td>
      <td>${escapeHtml(item.rent || "—")}<small>口径需确认</small></td>
      <td>${escapeHtml(item.source_sheet)}<small>第 ${item.source_row} 行</small></td>
      <td>${item.is_new_marked ? '<span class="mark new">新增</span>' : ""}${item.is_special_marked ? '<span class="mark special">特别</span>' : ""}${!item.is_new_marked && !item.is_special_marked ? "—" : ""}</td>
      <td><div class="cell-actions">${statusPill(item.status)}${item.status === "needs_review" ? `<button class="text-button confirm-property" data-confirm-property="${item.id}">确认房源</button>` : ""}</div></td>
    </tr>`).join("") : `<tr><td colspan="6"><div class="empty"><strong>${escapeHtml(emptyTitle)}</strong>调整搜索条件，或导入房源 Excel。</div></td></tr>`;
  fillPropertyOptions();
}

async function loadProperties() {
  const search = encodeURIComponent($("#property-search").value.trim());
  const status = encodeURIComponent($("#property-status").value);
  const data = await api(`/api/properties?limit=1000&search=${search}&status=${status}`);
  renderProperties(data.items, data.total);
}

async function runPropertySearch() {
  const query = $("#property-search").value.trim();
  if ($("#search-mode").value !== "semantic" || query.length < 2) {
    await loadProperties();
    return;
  }
  const data = await api(`/api/semantic/search?q=${encodeURIComponent(query)}&limit=100`);
  renderProperties(data.items, data.total, "没有语义匹配的房源");
}

async function loadSemanticStatus() {
  const semantic = await api("/api/semantic/status");
  state.semantic = semantic;
  const pill = $("#semantic-pill");
  const button = $("#semantic-reindex");
  if (!semantic.configured) {
    $("#semantic-status").textContent = "待配置本地嵌入服务地址与模型名";
    pill.textContent = "待配置";
    pill.className = "status-pill neutral";
    button.disabled = true;
  } else {
    $("#semantic-status").textContent = `${semantic.model} · 已索引 ${semantic.indexed_count}/${semantic.property_count} 条`;
    pill.textContent = semantic.index_complete ? "索引完整" : "索引待更新";
    pill.className = `status-pill ${semantic.index_complete ? "active" : "needs_review"}`;
    button.disabled = false;
  }
}

function fillPropertyOptions(items = state.propertyOptions.length ? state.propertyOptions : state.properties) {
  const select = $("#task-property");
  const previous = select.value;
  select.innerHTML = items.length
    ? '<option value="">请选择要绑定的房源</option>' + items.map((item) => `<option value="${item.id}">${escapeHtml([item.region, item.building, item.room, item.area].filter(Boolean).join(" · "))}${item.status === "needs_review" ? "（待确认）" : ""}</option>`).join("")
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
      <td>${statusPill(task.status)}${task.status === "processing" ? `<small>${escapeHtml(task.stage || "处理中")} · ${task.progress}%</small>` : task.output_video_path ? "<small>成片已就绪</small>" : ""}</td>
      <td>${formatDate(task.created_at)}</td>
      <td><button class="text-button" data-task="${task.id}">查看</button></td>
    </tr>`).join("") : `<tr><td colspan="7"><div class="empty"><strong>还没有视频任务</strong>新建任务后，可在这里审核口播并导出。</div></td></tr>`;
}

async function loadSettings() {
  const [settings, health, dataInfo, workbench] = await Promise.all([
    api("/api/settings"), api("/api/health"), api("/api/system/data-info"),
    api("/api/workbench/status"), loadSemanticStatus(),
  ]);
  state.settings = settings;
  const form = $("#settings-form");
  Object.entries(settings).forEach(([key, value]) => {
    if (form.elements[key]) form.elements[key].value = value;
  });
  if (form.elements.default_duration) {
    $("#task-form").elements.target_duration.value = settings.default_duration;
    $("#task-form").elements.aspect_ratio.value = settings.default_aspect_ratio;
    $("#task-form").elements.voice.value = settings.default_voice;
  }
  $("#ffmpeg-status").textContent = health.ffmpeg_available ? "FFmpeg 已加入系统环境变量" : "FFmpeg 未配置；基础任务管理可用，视频处理前需安装";
  $("#ffmpeg-pill").textContent = health.ffmpeg_available ? "可用" : "待配置";
  $("#ffmpeg-pill").className = `status-pill ${health.ffmpeg_available ? "active" : "needs_review"}`;
  $("#workbench-status").textContent = workbench.online
    ? `${workbench.url}${workbench.worker?.running_job ? " · 正在执行任务" : " · 在线空闲"}`
    : health.workbench_configured
      ? `已配置但当前无法连接：${workbench.error || "未知错误"}`
      : "尚未填写工作台 API 地址";
  $("#workbench-pill").textContent = workbench.online ? "在线" : health.workbench_configured ? "离线" : "待配置";
  $("#workbench-pill").className = `status-pill ${workbench.online ? "active" : health.workbench_configured ? "failed" : "needs_review"}`;
  $("#data-directory").textContent = dataInfo.data_directory;
}

function openDialog(id) {
  const dialog = $(`#${id}`);
  if (!dialog.open) dialog.showModal();
}

function closeDialog(id) {
  const dialog = $(`#${id}`);
  if (id === "detail-dialog") {
    state.currentTask = null;
    $("#detail-video").pause();
    $("#detail-video").removeAttribute("src");
    $("#detail-video").load();
  }
  dialog.close();
}

async function openNewTask() {
  const data = await api("/api/properties?limit=1000");
  state.propertyOptions = data.items;
  if (!state.propertyOptions.length) {
    toast("请先导入房源，再创建视频任务", true);
    openDialog("import-dialog");
    return;
  }
  fillPropertyOptions(state.propertyOptions);
  openDialog("task-dialog");
}

function applyTaskDetail(task, initial = true) {
  state.currentTask = task;
  if (initial) {
    $("#detail-number").textContent = task.task_no;
    $("#detail-title").textContent = task.property_label || "视频任务";
    $("#detail-script").value = task.script;
    $("#detail-warnings").innerHTML = task.warnings.map((warning) => `<div class="warning-item">${escapeHtml(warning)}</div>`).join("");
    $("#export-result").classList.add("hidden");
    $("#export-task").textContent = task.workbench_package_path ? "重新导出任务包" : "导出任务包";
  }
  const videoSource = task.output_video_path ? `/api/tasks/${task.id}/output` : `/api/tasks/${task.id}/video`;
  if ($("#detail-video").getAttribute("src") !== videoSource) $("#detail-video").src = videoSource;
  $("#render-task").disabled = task.status === "processing";
  $("#render-task").textContent = task.status === "processing" ? "正在出片…" : task.status === "completed" ? "重新出片" : "提交真实出片";
  const renderStatus = $("#render-status");
  renderStatus.className = `render-state ${task.status}`;
  renderStatus.textContent = task.status === "processing"
    ? `${task.stage || "处理中"} · ${task.progress}%`
    : task.status === "completed"
      ? "真实成片已生成，可预览或下载。"
      : task.status === "failed"
        ? `出片失败：${task.error_message || "请查看工作台状态"}`
        : "尚未提交真实出片。将占用视频工作台的 GPU 任务队列。";
  const output = $("#detail-output");
  output.classList.toggle("hidden", !task.output_video_path);
  output.href = task.output_video_path ? `/api/tasks/${task.id}/output` : "#";
}

async function openTask(taskId) {
  const task = await api(`/api/tasks/${taskId}`);
  applyTaskDetail(task);
  openDialog("detail-dialog");
}

async function watchTask(taskId) {
  for (let attempt = 0; attempt < 900; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 4000));
    if (!state.currentTask || state.currentTask.id !== taskId) return;
    const task = await api(`/api/tasks/${taskId}`);
    applyTaskDetail(task, false);
    if (["completed", "failed"].includes(task.status)) {
      await Promise.all([loadTasks(), loadDashboard()]);
      toast(task.status === "completed" ? "成片已生成" : "出片失败，请查看原因", task.status === "failed");
      return;
    }
  }
}

$("#import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  busy(form, true, "正在导入…");
  try {
    const result = await api("/api/properties/import", { method: "POST", body: new FormData(form) });
    $("#import-result").innerHTML = `<strong>导入完成</strong><br>读取 ${result.total_rows} 行；新增 ${result.inserted_rows}，更新 ${result.updated_rows}，跳过 ${result.skipped_rows}；待确认 ${result.review_rows}。`;
    $("#import-result").classList.remove("hidden");
    await Promise.all([loadProperties(), loadDashboard(), loadSemanticStatus()]);
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
    await loadSettings();
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

$("#render-task").addEventListener("click", async () => {
  if (!state.currentTask) return;
  const button = $("#render-task");
  button.disabled = true;
  button.textContent = "正在提交…";
  try {
    const updated = await api(`/api/tasks/${state.currentTask.id}/script`, {
      method: "PUT", body: JSON.stringify({ script: $("#detail-script").value }),
    });
    applyTaskDetail(updated, false);
    const result = await api(`/api/tasks/${state.currentTask.id}/render`, { method: "POST" });
    applyTaskDetail(result.task, false);
    toast("已提交视频工作台");
    watchTask(result.task.id).catch((error) => toast(error.message, true));
    await loadTasks();
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
  }
});

$$(".nav-item").forEach((item) => item.addEventListener("click", () => showView(item.dataset.view)));
$$('[data-go]').forEach((item) => item.addEventListener("click", () => showView(item.dataset.go)));
$$('[data-close]').forEach((item) => item.addEventListener("click", () => closeDialog(item.dataset.close)));
["#top-import", "#property-import"].forEach((selector) => $(selector).addEventListener("click", () => openDialog("import-dialog")));
["#top-new-task", "#hero-new-task", "#task-new"].forEach((selector) => $(selector).addEventListener("click", () => openNewTask().catch((error) => toast(error.message, true))));
$("#property-refresh").addEventListener("click", () => runPropertySearch().then(() => toast("房源列表已刷新")).catch((error) => toast(error.message, true)));
$("#property-status").addEventListener("change", () => runPropertySearch().catch((error) => toast(error.message, true)));
$("#search-mode").addEventListener("change", () => runPropertySearch().catch((error) => toast(error.message, true)));
let searchTimer;
$("#property-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => runPropertySearch().catch((error) => toast(error.message, true)), 350);
});
$("#semantic-reindex").addEventListener("click", async () => {
  const button = $("#semantic-reindex");
  button.disabled = true;
  button.textContent = "正在建立索引…";
  try {
    const result = await api("/api/semantic/reindex", { method: "POST" });
    await loadSemanticStatus();
    toast(`向量索引完成，共 ${result.indexed_count} 条`);
  } catch (error) { toast(error.message, true); }
  finally { button.textContent = "建立 / 更新索引"; button.disabled = false; }
});
$("#excel-drop input").addEventListener("change", (event) => {
  $("#excel-file-name").textContent = event.target.files[0]?.name || "";
  $("#import-result").classList.add("hidden");
});
$("#video-input").addEventListener("change", (event) => {
  const file = event.target.files[0];
  $("#video-file-name").textContent = file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB` : "未选择视频";
});
document.addEventListener("click", async (event) => {
  const confirmButton = event.target.closest("[data-confirm-property]");
  if (confirmButton) {
    confirmButton.disabled = true;
    try {
      const result = await api(`/api/properties/${confirmButton.dataset.confirmProperty}/confirm`, { method: "POST" });
      await Promise.all([runPropertySearch(), loadDashboard()]);
      toast(result.duplicate_count > 1 ? "已确认该行，注意同组仍有其他重复房源" : "房源已确认");
    } catch (error) { toast(error.message, true); confirmButton.disabled = false; }
    return;
  }
  const target = event.target.closest("[data-task]");
  if (target) openTask(target.dataset.task).catch((error) => toast(error.message, true));
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.matches(".task-row")) openTask(event.target.dataset.task).catch((error) => toast(error.message, true));
});

Promise.all([loadDashboard(), loadProperties(), loadTasks(), loadSettings()])
  .catch((error) => toast(`初始化失败：${error.message}`, true));
