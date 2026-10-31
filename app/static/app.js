/* ==========================================================================
   房源视频工作台 · 前端
   --------------------------------------------------------------------------
   无构建链、无框架、无外部依赖：一个中介朋友要在一台可能断网的 Windows
   机器上长期跑，任何 CDN 或打包步骤都是负债。

   组织方式按"页面"切段，每段自包含，靠 state 和 refresh 函数通信。
   ========================================================================== */

const state = {
  properties: [],
  propertyOptions: [],
  tasks: [],
  settings: {},
  currentTask: null,
  review: { items: [], current: null, detail: null },
  publish: null,
  palette: { items: [], index: 0, open: false },
};

const STATUS = {
  active: "已匹配",
  needs_review: "待确认",
  received: "已接收",
  script_ready: "草稿待审",
  exported: "已导出",
  processing: "处理中",
  review: "待审片",
  completed: "已完成",
  failed: "失败",
  pending: "待审",
  approved: "已通过",
  rejected: "已驳回",
  published: "已发布",
  skipped: "已跳过",
  publishing: "发布中",
  uncertain: "待确认结果",
};

const VIEW_META = {
  dashboard: ["OVERVIEW", "工作台"],
  review: ["REVIEW DESK", "审核台"],
  properties: ["LISTING DATABASE", "房源库"],
  tasks: ["VIDEO PIPELINE", "视频任务"],
  publish: ["DISTRIBUTION", "发布"],
  settings: ["LOCAL CONFIGURATION", "设置"],
};

const LEVEL_LABEL = { blocking: "必须处理", warn: "建议看看", ok: "正常", unknown: "待检" };

// 任务来源在数据库里是英文标识，界面上不该原样吐给使用者看
const SOURCE_LABEL = { desktop: "本机上传", webui: "网页", feishu: "飞书", im: "消息", cli: "命令行" };

const $ = (selector, scope = document) => scope.querySelector(selector);
const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];

/* ------------------------------------------------------------------ 工具 */

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
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

let toastTimer;
function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("show"), 3800);
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

/** 数字从旧值滚到新值。单纯为了"数据在动"的手感，不影响任何判断。 */
function animateCount(element, target) {
  if (!element) return;
  const to = Number(target || 0);
  const from = Number(element.dataset.count || 0);
  element.dataset.count = to;
  if (from === to || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    element.textContent = to;
    return;
  }
  const start = performance.now();
  const duration = 620;
  function tick(now) {
    const progress = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    element.textContent = Math.round(from + (to - from) * eased);
    if (progress < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

/* ------------------------------------------------------------------ 视图切换 */

function showView(name) {
  if (!VIEW_META[name]) return;
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  $("#page-eyebrow").textContent = VIEW_META[name][0];
  $("#page-title").textContent = VIEW_META[name][1];
  $("main").scrollTop = 0;

  if (name === "tasks") loadTasks();
  if (name === "properties") loadProperties();
  if (name === "dashboard") loadDashboard();
  if (name === "review") loadReview(true);
  if (name === "publish") loadPublish();
  if (name === "settings") loadSettings();
}

/* ------------------------------------------------------------------ 工作台 */

async function loadDashboard() {
  const [data, review] = await Promise.all([
    api("/api/dashboard"),
    api("/api/review/counts").catch(() => ({ pending: 0 })),
  ]);
  animateCount($("#stat-properties"), data.property_count);
  animateCount($("#stat-review"), review.pending || 0);
  animateCount($("#stat-tasks"), data.task_count);
  animateCount($("#stat-completed"), data.completed_count);
  $("#nav-property-count").textContent = data.property_count;

  const pending = review.pending || 0;
  const badge = $("#nav-review-count");
  badge.textContent = pending;
  badge.classList.toggle("hot", pending > 0);

  $("#recent-tasks").innerHTML = data.recent_tasks.length
    ? data.recent_tasks.map((task) => `
      <div class="task-row" data-task="${task.id}" role="button" tabindex="0">
        <span class="number">${escapeHtml(task.task_no)}</span>
        <div><strong>${escapeHtml(task.property_label || "未命名房源")}</strong><small>${escapeHtml(task.original_filename)}</small></div>
        <span>${escapeHtml(task.aspect_ratio)} · ${task.target_duration} 秒</span>
        ${statusPill(task.status)}
      </div>`).join("")
    : `<div class="empty"><strong>还没有视频任务</strong>导入房源并上传第一段实拍视频，即可开始。</div>`;
}

/* ------------------------------------------------------------------ 房源库 */

function renderProperties(items, total, emptyTitle = "没有匹配的房源") {
  state.properties = items;
  $("#property-total").textContent = `共 ${total} 条${total > items.length ? `，当前显示 ${items.length} 条` : ""}`;
  $("#property-rows").innerHTML = items.length
    ? items.map((item) => `
      <tr>
        <td><strong>${escapeHtml([item.region, item.building, item.room].filter(Boolean).join(" · ") || "未完整房源")}</strong><small class="mono">ID ${escapeHtml(item.id.slice(0, 8))}${item.score !== undefined ? ` · 相似度 ${item.score}` : ""}</small></td>
        <td><strong>${escapeHtml(item.area || "面积待补充")}</strong><small>${escapeHtml(item.partition || "格局待补充")}</small></td>
        <td>${escapeHtml(item.rent || "—")}<small>口径需确认</small></td>
        <td>${escapeHtml(item.source_sheet)}<small>第 ${item.source_row} 行</small></td>
        <td>${item.is_new_marked ? '<span class="mark new">新增</span>' : ""}${item.is_special_marked ? '<span class="mark special">特别</span>' : ""}${!item.is_new_marked && !item.is_special_marked ? "—" : ""}</td>
        <td><div class="cell-actions">${statusPill(item.status)}${item.status === "needs_review" ? `<button class="text-button confirm-property" data-confirm-property="${item.id}">确认房源</button>` : ""}</div></td>
      </tr>`).join("")
    : `<tr><td colspan="6"><div class="empty"><strong>${escapeHtml(emptyTitle)}</strong>调整搜索条件，或导入房源 Excel。</div></td></tr>`;
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

function fillPropertyOptions(items = state.propertyOptions.length ? state.propertyOptions : state.properties) {
  const select = $("#task-property");
  const previous = select.value;
  select.innerHTML = items.length
    ? '<option value="">请选择要绑定的房源</option>' + items.map((item) => `<option value="${item.id}">${escapeHtml([item.region, item.building, item.room, item.area].filter(Boolean).join(" · "))}${item.status === "needs_review" ? "（待确认）" : ""}</option>`).join("")
    : '<option value="">请先导入房源</option>';
  select.value = previous;
}

/* ------------------------------------------------------------------ 视频任务 */

async function loadTasks() {
  const data = await api("/api/tasks?limit=100");
  state.tasks = data.items;
  $("#task-total").textContent = `共 ${data.total} 条`;
  $("#task-rows").innerHTML = data.items.length
    ? data.items.map((task) => `
      <tr>
        <td><strong class="mono">${escapeHtml(task.task_no)}</strong></td>
        <td><strong>${escapeHtml(task.property_label)}</strong><small>${escapeHtml(task.area || "面积待补充")}</small></td>
        <td>${escapeHtml(task.original_filename)}<small>${escapeHtml(SOURCE_LABEL[task.source] || task.source)}</small></td>
        <td>${escapeHtml(task.aspect_ratio)} · ${task.target_duration} 秒</td>
        <td>${statusPill(task.status)}${task.status === "processing" ? `<small>${escapeHtml(task.stage || "处理中")} · ${task.progress}%</small>` : task.output_video_path ? "<small>成片已就绪</small>" : ""}</td>
        <td>${formatDate(task.created_at)}</td>
        <td><button class="text-button" data-task="${task.id}">查看</button></td>
      </tr>`).join("")
    : `<tr><td colspan="7"><div class="empty"><strong>还没有视频任务</strong>新建任务后，可在这里审核口播并出片。</div></td></tr>`;
}

/* ------------------------------------------------------------------ 审核台 */

function ledClass(level) {
  return ["blocking", "warn", "ok"].includes(level) ? level : "unknown";
}

function verdictText(precheck) {
  const level = precheck?.level || "unknown";
  if (level === "blocking") return (precheck.blocking?.[0]?.detail || "有必须处理的问题");
  if (level === "warn") return (precheck.warn?.[0]?.detail || "有几项建议看看");
  if (level === "ok") return "没发现问题";
  return "还没做预检";
}

function renderQueue() {
  const list = $("#queue-list");
  const items = state.review.items || [];
  const counts = items.reduce((acc, item) => {
    acc[item.status] = (acc[item.status] || 0) + 1;
    return acc;
  }, {});
  $("#queue-head-count").textContent = items.length ? `${items.length} 条 · 待审 ${counts.pending || 0}` : "";

  if (!items.length) {
    list.innerHTML = `<div class="empty"><strong>队列是空的</strong>出片完成后会自动进入这里。</div>`;
    return;
  }

  list.innerHTML = items.map((item) => {
    const level = item.precheck?.level || "unknown";
    const area = item.area_raw ? `${item.area_raw}${/平|㎡/.test(item.area_raw) ? "" : "平"}` : "";
    // 用 <video preload="metadata"> 当缩略图：#t=0.1 会让浏览器解码出一帧，
    // 比空白渐变块更容易一眼认出是哪套房。成片不在时才退回纯色块。
    const thumb = item.output_video_path
      ? `<video src="/api/tasks/${item.task_id}/output#t=0.1" preload="metadata" muted playsinline></video>`
      : "";
    return `
      <article class="queue-card ${state.review.current === item.task_id ? "active" : ""}" data-review="${escapeHtml(item.task_id)}">
        <div class="queue-thumb"><span class="led ${ledClass(level)}"></span>${thumb}</div>
        <div class="queue-body">
          <strong>${escapeHtml(item.property_label || item.task_no)}</strong>
          <span class="meta">${escapeHtml(item.task_no)} · ${escapeHtml(area || "面积待补")}</span>
          <span class="verdict ${ledClass(level)}">${escapeHtml(verdictText(item.precheck))}</span>
          ${statusPill(item.status)}
        </div>
      </article>`;
  }).join("");
}

function renderStage(detail) {
  const stage = $("#review-stage");
  if (!detail) {
    $("#stage-title").textContent = "选一条待审的片子";
    $("#stage-sub").textContent = "机器会先比对房源数字，你只需要看标红的";
    $("#stage-player").innerHTML = `<div class="placeholder">左侧选一条，成片会出现在这里</div>`;
    $("#stage-checks").innerHTML = "";
    $("#stage-verdict").className = "verdict-banner";
    $("#stage-verdict").innerHTML = `<span class="dot"></span><div><strong>等待选择</strong><small>预检结论会显示在这里</small></div>`;
    $("#stage-script-wrap").classList.add("hidden");
    stage.dataset.level = "";
    return;
  }

  const precheck = detail.precheck || { level: "unknown", blocking: [], warn: [], ok: [], summary: "" };
  $("#stage-title").textContent = detail.property_label || detail.task_no;
  $("#stage-sub").textContent = `${detail.task_no} · ${formatDate(detail.created_at)} · ${STATUS[detail.status] || detail.status}`;

  const player = $("#stage-player");
  if (detail.output_video_path) {
    player.innerHTML = `<video controls playsinline preload="metadata" src="/api/tasks/${detail.task_id}/output"></video>`;
  } else {
    player.innerHTML = `<div class="placeholder">这条任务还没有成片</div>`;
  }

  const banner = $("#stage-verdict");
  banner.className = `verdict-banner ${ledClass(precheck.level)}`;
  banner.innerHTML = `<span class="dot"></span><div><strong>${escapeHtml(LEVEL_LABEL[precheck.level] || "待检")}</strong><small>${escapeHtml(precheck.summary || "还没做预检")}</small></div>`;

  const ordered = [...precheck.blocking, ...precheck.warn, ...precheck.ok.slice(0, 4)];
  $("#stage-checks").innerHTML = ordered.length
    ? ordered.map((check) => `
      <div class="check-item ${escapeHtml(check.level)}">
        <span class="bulb"></span>
        <div><span class="label">${escapeHtml(check.label)}</span> · <span class="detail">${escapeHtml(check.detail)}</span></div>
        <span class="tag-lv">${escapeHtml(check.level === "ok" ? "OK" : check.level.toUpperCase())}</span>
      </div>`).join("")
    : `<div class="empty" style="padding:18px"><strong>没有预检结果</strong>点右上角「重跑预检」</div>`;

  $("#stage-script-wrap").classList.remove("hidden");
  $("#stage-script").value = detail.script || "";
  stage.dataset.level = precheck.level;
  stage.dataset.taskId = detail.task_id;
  updateReviewActions(precheck.level, detail.status);
}

function updateReviewActions(level, status) {
  const approve = $("#btn-approve");
  const auto = state.settings.publish_auto_after_review === "1";
  approve.textContent = level === "blocking"
    ? "强行通过"
    : auto ? "通过并发布" : "通过";
  approve.classList.toggle("button-lime", level !== "blocking");
  approve.classList.toggle("button-primary", level === "blocking");
  approve.disabled = status === "published";
  $("#btn-reject").disabled = status === "published";
  $("#btn-skip").disabled = status === "published";
}

async function loadReview(refresh) {
  try {
    const query = new URLSearchParams({ limit: "60" });
    const filter = $("#review-filter").value;
    if (filter) query.set("status", filter);
    if (refresh) query.set("refresh", "true");
    const data = await api(`/api/review/items?${query}`);

    // 默认筛"只看待审"时，如果队列空了但存在其它状态，给个提示而不是空白
    state.review.items = data.items;
    renderQueue();

    const stillThere = data.items.some((item) => item.task_id === state.review.current);
    if (!stillThere && data.items.length) {
      selectReview(data.items[0].task_id);
    } else if (state.review.current && stillThere) {
      selectReview(state.review.current);
    } else if (!data.items.length) {
      state.review.current = null;
      renderStage(null);
    }

    const pending = (data.counts || {}).pending || 0;
    const badge = $("#nav-review-count");
    badge.textContent = pending;
    badge.classList.toggle("hot", pending > 0);
  } catch (error) {
    toast(error.message, true);
  }
}

async function selectReview(taskId) {
  state.review.current = taskId;
  $$(".queue-card").forEach((card) => card.classList.toggle("active", card.dataset.review === taskId));
  try {
    const detail = await api(`/api/review/${taskId}`);
    state.review.detail = detail;
    renderStage(detail);
  } catch (error) {
    toast(error.message, true);
  }
}

async function decideReview(action) {
  const taskId = state.review.current;
  if (!taskId) {
    toast("先从左侧选一条", true);
    return;
  }
  const note = $("#stage-note").value.trim();
  if (action === "reject" && !note) {
    toast("驳回要写原因，否则出片的人不知道该改什么", true);
    $("#stage-note").focus();
    return;
  }
  const button = $(`#btn-${action === "approve" ? "approve" : action}`);
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  try {
    const body = { reviewer: "", note, confirm_risky: false };
    let result;
    try {
      result = await api(`/api/review/${taskId}/${action}`, { method: "POST", body: JSON.stringify(body) });
    } catch (error) {
      // 预检有 blocking 时后端会拒绝通过，这里让人显式确认一次再越过
      if (action === "approve" && error.message.includes("confirm_risky")) {
        if (!window.confirm(`${error.message}\n\n确定要跳过这些检查吗？`)) throw error;
        result = await api(`/api/review/${taskId}/approve`, {
          method: "POST",
          body: JSON.stringify({ ...body, confirm_risky: true, note: note || "人工确认后越过预检" }),
        });
      } else {
        throw error;
      }
    }
    toast(result.message || "已处理");
    $("#stage-note").value = "";
    await loadReview(false);
    loadDashboard();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = original;
    if (state.review.detail) updateReviewActions(state.review.detail.precheck?.level, state.review.detail.status);
  }
}

/* ------------------------------------------------------------------ 发布 */

function platformCard(key, info) {
  const label = key === "xiaohongshu" ? "小红书" : "抖音";
  const avatar = key === "xiaohongshu" ? "小红" : "抖音";
  const avatarClass = key === "xiaohongshu" ? "xhs" : "dy";
  const online = info.reachable && info.logged_in;
  const stateText = !info.reachable ? "服务没启动" : info.logged_in ? "已登录" : "未登录";
  const quota = info.quota || {};

  // 抖音的完整命令有四五十个字符，直接铺在卡片里会撑成三行。
  // 卡片上只给关键的调试端口，完整命令放在「复制」按钮里。
  const port = (info.chrome_hint || "").match(/port=(\d+)/)?.[1] || "9222";
  const hint = key === "douyin"
    ? `<div class="platform-row"><span>调试端口</span><span>${escapeHtml(port)}</span></div>`
    : `<div class="platform-row"><span>服务地址</span><code>${escapeHtml(state.settings.publish_xhs_mcp_url || "")}</code></div>`;

  return `
    <article class="platform-card ${online ? "on" : ""}">
      <div class="platform-top">
        <div class="platform-avatar ${avatarClass}">${avatar}</div>
        <div><h4>${label}</h4><small>${escapeHtml(stateText)}</small></div>
        <span class="status-pill ${online ? "active" : info.reachable ? "needs_review" : "failed"}" style="margin-left:auto">${online ? "就绪" : "不可用"}</span>
      </div>
      <div class="platform-rows">
        <div class="platform-row"><span>今日额度</span><span>${quota.used_today ?? 0} / ${quota.daily_limit ?? "—"}</span></div>
        <div class="platform-row"><span>最小间隔</span><span>${escapeHtml(String(state.settings.publish_min_interval_minutes || "—"))} 分钟</span></div>
        ${hint}
        <div class="platform-row"><span>说明</span><span class="note">${escapeHtml((info.message || "").slice(0, 120))}</span></div>
      </div>
      <div class="platform-actions">
        ${key === "xiaohongshu"
          ? `<button class="button button-sm button-primary" data-qr="xiaohongshu">扫码登录</button>`
          : `<button class="button button-sm button-ghost" data-copy="${escapeHtml(info.chrome_hint || "")}">复制启动命令</button>`}
      </div>
      <div id="qr-slot"></div>
    </article>`;
}

async function loadPublish() {
  try {
    const [status, records] = await Promise.all([
      api("/api/publish/status"),
      api("/api/publish/records?limit=40"),
    ]);
    state.publish = status;
    $("#publish-grid").innerHTML =
      platformCard("xiaohongshu", status.xiaohongshu) + platformCard("douyin", status.douyin);

    $("#publish-rows").innerHTML = records.items.length
      ? records.items.map((row) => `
        <tr>
          <td>${formatDate(row.created_at)}</td>
          <td>${escapeHtml(row.platform === "xiaohongshu" ? "小红书" : "抖音")}</td>
          <td><strong>${escapeHtml(row.title || "—")}</strong></td>
          <td>${statusPill(row.status)}</td>
          <td>${row.remote_url ? `<a class="text-button" href="${escapeHtml(row.remote_url)}" target="_blank" rel="noreferrer">打开</a>` : `<small>${escapeHtml((row.message || "").slice(0, 60))}</small>`}</td>
        </tr>`).join("")
      : `<tr><td colspan="5"><div class="empty"><strong>还没有发布记录</strong>审核通过后发布，记录会出现在这里。</div></td></tr>`;
    $("#publish-total").textContent = `共 ${records.items.length} 条`;
  } catch (error) {
    toast(error.message, true);
  }
}

async function requestQrcode(platform) {
  const slot = $("#qr-slot");
  if (!slot) return;
  slot.innerHTML = `<div class="qr-box"><div class="skeleton" style="width:180px;height:180px"></div><small>正在生成二维码…</small></div>`;
  try {
    const result = await api(`/api/publish/login-qrcode?platform=${platform}`, { method: "POST" });
    if (!result.ok) {
      slot.innerHTML = `<div class="qr-box"><small>${escapeHtml(result.message || "没拿到二维码")}</small></div>`;
      return;
    }
    slot.innerHTML = `
      <div class="qr-box">
        <img src="data:image/png;base64,${result.image_base64}" alt="小红书登录二维码">
        <small>用小红书 App 扫码，约 60 秒过期。扫码后再点「刷新」看状态。</small>
      </div>`;
  } catch (error) {
    slot.innerHTML = `<div class="qr-box"><small>${escapeHtml(error.message)}</small></div>`;
  }
}

/* ------------------------------------------------------------------ 设置 */

async function loadSettings() {
  const [settings, health, dataInfo, workbench, feishu, bitable] = await Promise.all([
    api("/api/settings"), api("/api/health"), api("/api/system/data-info"),
    api("/api/workbench/status"), api("/api/feishu/status"), api("/api/review/bitable/status"),
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
  $("#ffmpeg-status").textContent = health.ffmpeg_available ? "FFmpeg 已加入系统环境变量" : "FFmpeg 未配置；基础任务管理可用";
  $("#ffmpeg-pill").textContent = health.ffmpeg_available ? "可用" : "待配置";
  $("#ffmpeg-pill").className = `status-pill ${health.ffmpeg_available ? "active" : "needs_review"}`;
  $("#workbench-status").textContent = workbench.online
    ? `${workbench.url}${workbench.worker?.running_job ? " · 正在执行任务" : " · 在线空闲"}`
    : health.workbench_configured
      ? `已配置但无法连接：${workbench.error || "未知错误"}`
      : "尚未填写工作台 API 地址";
  $("#workbench-pill").textContent = workbench.online ? "在线" : health.workbench_configured ? "离线" : "待配置";
  $("#workbench-pill").className = `status-pill ${workbench.online ? "active" : health.workbench_configured ? "failed" : "needs_review"}`;
  $("#data-directory").textContent = dataInfo.data_directory;
  renderFeishu(feishu);
  renderBitable(bitable);
  loadSemanticStatus().catch(() => {});
}

function renderBitable(bitable) {
  const pill = $("#bitable-pill");
  const button = $("#bitable-push");
  if (!bitable.available) {
    $("#bitable-status").textContent = bitable.hint || "未配置多维表格凭据，审核仅在本机进行";
    pill.textContent = "未接入";
    pill.className = "status-pill neutral";
    button.disabled = true;
    return;
  }
  $("#bitable-status").textContent = bitable.mirror_enabled
    ? `已开启镜像轮询 · 表 ${bitable.table_id}`
    : `已连上表格，但镜像同步没打开（review_bitable_sync=0）`;
  pill.textContent = bitable.mirror_enabled ? "同步中" : "已接入";
  pill.className = `status-pill ${bitable.mirror_enabled ? "active" : "needs_review"}`;
  button.disabled = false;
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

function renderFeishu(feishu) {
  const pill = $("#feishu-pill");
  const label = feishu.connected ? "在线" : feishu.configured ? "未连接" : "待配置";
  $("#feishu-status").textContent = feishu.hint || "读取中";
  pill.textContent = label;
  pill.className = `status-pill ${feishu.connected ? "active" : feishu.configured ? "failed" : "needs_review"}`;
}

/* ------------------------------------------------------------------ 任务对话框 */

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
      toast(task.status === "completed" ? "成片已生成，去审核台看看" : "出片失败，请查看原因", task.status === "failed");
      return;
    }
  }
}

/* ------------------------------------------------------------------ 命令面板 */

function paletteActions() {
  return [
    { kind: "操作", label: "新建视频任务", hint: "上传实拍并生成口播", run: () => openNewTask().catch((e) => toast(e.message, true)) },
    { kind: "操作", label: "导入房源 Excel", hint: "保留原始字段", run: () => openDialog("import-dialog") },
    { kind: "操作", label: "刷新审核队列", hint: "补齐未入队的成片", run: () => { showView("review"); loadReview(true); } },
    { kind: "操作", label: "发布状态", hint: "检查两个平台连通性", run: () => showView("publish") },
    { kind: "操作", label: "系统设置", hint: "工作台、时长、发布限额", run: () => showView("settings") },
  ];
}

function paletteEntries(query) {
  const keyword = query.trim().toLowerCase();
  const actions = paletteActions().filter((item) => !keyword || item.label.toLowerCase().includes(keyword));
  const tasks = state.tasks
    .filter((task) => !keyword || `${task.task_no} ${task.property_label || ""}`.toLowerCase().includes(keyword))
    .slice(0, 8)
    .map((task) => ({
      kind: "任务",
      label: `${task.task_no} · ${task.property_label || "未命名"}`,
      hint: STATUS[task.status] || task.status,
      run: () => openTask(task.id).catch((e) => toast(e.message, true)),
    }));
  const properties = state.properties
    .filter((item) => keyword && `${item.region || ""}${item.building || ""}${item.room || ""}`.toLowerCase().includes(keyword))
    .slice(0, 6)
    .map((item) => ({
      kind: "房源",
      label: [item.region, item.building, item.room].filter(Boolean).join(" "),
      hint: item.area || "",
      run: () => {
        showView("properties");
        $("#property-search").value = [item.building, item.room].filter(Boolean).join(" ");
        runPropertySearch().catch((e) => toast(e.message, true));
      },
    }));
  return [...actions, ...tasks, ...properties].slice(0, 12);
}

function renderPalette() {
  const results = $("#palette-results");
  if (!state.palette.items.length) {
    results.innerHTML = `<div class="empty" style="padding:24px">没有匹配项</div>`;
    return;
  }
  results.innerHTML = state.palette.items.map((item, index) => `
    <div class="palette-item ${index === state.palette.index ? "active" : ""}" data-palette-index="${index}">
      <span class="kind">${escapeHtml(item.kind)}</span>
      <span>${escapeHtml(item.label)}</span>
      <span class="hint">${escapeHtml(item.hint || "")}</span>
    </div>`).join("");
}

function openPalette() {
  state.palette.open = true;
  state.palette.index = 0;
  state.palette.items = paletteEntries("");
  $("#palette").classList.add("open");
  $("#palette-input").value = "";
  renderPalette();
  setTimeout(() => $("#palette-input").focus(), 20);
}

function closePalette() {
  state.palette.open = false;
  $("#palette").classList.remove("open");
}

function runPalette(index = state.palette.index) {
  const item = state.palette.items[index];
  if (!item) return;
  closePalette();
  item.run();
}

/* ------------------------------------------------------------------ 事件绑定 */

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

// 审核台
$("#btn-approve").addEventListener("click", () => decideReview("approve"));
$("#btn-reject").addEventListener("click", () => decideReview("reject"));
$("#btn-skip").addEventListener("click", () => decideReview("skip"));
$("#review-filter").addEventListener("change", () => loadReview(false));
$("#btn-rerun").addEventListener("click", async () => {
  const taskId = state.review.current;
  if (!taskId) return;
  const button = $("#btn-rerun");
  button.disabled = true;
  button.textContent = "预检中…";
  try {
    await api(`/api/review/${taskId}/precheck`, { method: "POST" });
    await selectReview(taskId);
    toast("预检已更新");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "重跑预检"; }
});

// 发布
$("#publish-refresh").addEventListener("click", () => loadPublish());
document.addEventListener("click", async (event) => {
  const qrButton = event.target.closest("[data-qr]");
  if (qrButton) {
    await requestQrcode(qrButton.dataset.qr);
    return;
  }
  const copyButton = event.target.closest("[data-copy]");
  if (copyButton) {
    try {
      await navigator.clipboard.writeText(copyButton.dataset.copy);
      toast("启动命令已复制，在命令行里粘贴执行");
    } catch {
      toast(copyButton.dataset.copy);
    }
    return;
  }
  const reviewCard = event.target.closest("[data-review]");
  if (reviewCard) {
    selectReview(reviewCard.dataset.review);
    return;
  }
  const paletteItem = event.target.closest("[data-palette-index]");
  if (paletteItem) {
    runPalette(Number(paletteItem.dataset.paletteIndex));
    return;
  }
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

$$(".nav-item").forEach((item) => item.addEventListener("click", () => showView(item.dataset.view)));
$$("[data-go]").forEach((item) => item.addEventListener("click", () => showView(item.dataset.go)));
$$("[data-close]").forEach((item) => item.addEventListener("click", () => closeDialog(item.dataset.close)));
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

$("#feishu-restart").addEventListener("click", async () => {
  const button = $("#feishu-restart");
  button.disabled = true;
  button.textContent = "正在重连…";
  try {
    const result = await api("/api/feishu/restart", { method: "POST" });
    renderFeishu(result);
    toast(result.started ? "长连接已发起，正在等待飞书握手" : "未能启动，请检查 .env 中的应用凭据");
  } catch (error) { toast(error.message, true); }
  finally { button.textContent = "重连长连接"; button.disabled = false; }
});

$("#bitable-push").addEventListener("click", async () => {
  const button = $("#bitable-push");
  button.disabled = true;
  button.textContent = "正在推送…";
  try {
    const result = await api("/api/review/bitable/push", { method: "POST" });
    if (!result.available) { toast(result.message || "未配置多维表格"); return; }
    const counts = result.result || {};
    const summary = typeof counts === "object"
      ? `新建 ${counts.created ?? 0} · 更新 ${counts.updated ?? 0} · 跳过 ${counts.skipped ?? 0} · 失败 ${counts.failed ?? 0}`
      : String(counts);
    toast(`队列已推送：${summary}`);
  } catch (error) { toast(error.message, true); }
  finally { button.textContent = "推送当前队列"; button.disabled = false; }
});

$("#excel-drop input").addEventListener("change", (event) => {
  $("#excel-file-name").textContent = event.target.files[0]?.name || "";
  $("#import-result").classList.add("hidden");
});
$("#video-input").addEventListener("change", (event) => {
  const file = event.target.files[0];
  $("#video-file-name").textContent = file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB` : "未选择视频";
});

// 命令面板
$("#open-palette").addEventListener("click", openPalette);
$("#palette").addEventListener("click", (event) => {
  if (event.target.id === "palette") closePalette();
});
$("#palette-input").addEventListener("input", (event) => {
  state.palette.items = paletteEntries(event.target.value);
  state.palette.index = 0;
  renderPalette();
});

document.addEventListener("keydown", (event) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);

  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    state.palette.open ? closePalette() : openPalette();
    return;
  }
  if (state.palette.open) {
    if (event.key === "Escape") { event.preventDefault(); closePalette(); }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      state.palette.index = Math.min(state.palette.index + 1, state.palette.items.length - 1);
      renderPalette();
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      state.palette.index = Math.max(state.palette.index - 1, 0);
      renderPalette();
    }
    if (event.key === "Enter") { event.preventDefault(); runPalette(); }
    return;
  }
  if (typing) return;

  // 审核台的键盘流：J/K 换条，A 通过，R 驳回，G 重跑预检
  if ($("#view-review").classList.contains("active")) {
    const items = state.review.items || [];
    const index = items.findIndex((item) => item.task_id === state.review.current);
    if (event.key.toLowerCase() === "j" && index < items.length - 1) { selectReview(items[index + 1].task_id); }
    if (event.key.toLowerCase() === "k" && index > 0) { selectReview(items[index - 1].task_id); }
    if (event.key.toLowerCase() === "a") decideReview("approve");
    if (event.key.toLowerCase() === "r") { $("#stage-note").focus(); }
    if (event.key.toLowerCase() === "g") $("#btn-rerun").click();
  }
  if (event.key === "Enter" && event.target.matches(".task-row")) {
    openTask(event.target.dataset.task).catch((error) => toast(error.message, true));
  }
});

/* ------------------------------------------------------------------ 视觉细节 */

// 背景光晕跟随鼠标。用 rAF 节流，避免 mousemove 打满主线程。
let glowFrame = null;
document.addEventListener("mousemove", (event) => {
  if (glowFrame) return;
  glowFrame = requestAnimationFrame(() => {
    document.body.style.setProperty("--mx", `${(event.clientX / window.innerWidth) * 100}%`);
    document.body.style.setProperty("--my", `${(event.clientY / window.innerHeight) * 100}%`);
    glowFrame = null;
  });
});

// 整页拖拽导入 Excel：中介最常见的动作是"从文件夹把表拖进来"
let dragDepth = 0;
document.addEventListener("dragenter", (event) => {
  if (!event.dataTransfer?.types.includes("Files")) return;
  dragDepth += 1;
  $("#drop-veil").classList.add("on");
});
document.addEventListener("dragover", (event) => event.preventDefault());
document.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $("#drop-veil").classList.remove("on");
});
document.addEventListener("drop", (event) => {
  event.preventDefault();
  dragDepth = 0;
  $("#drop-veil").classList.remove("on");
  const file = event.dataTransfer?.files?.[0];
  if (!file) return;
  if (!/\.(xlsx|xlsm)$/i.test(file.name)) {
    toast("只支持 .xlsx / .xlsm", true);
    return;
  }
  const input = $("#excel-drop input");
  const transfer = new DataTransfer();
  transfer.items.add(file);
  input.files = transfer.files;
  $("#excel-file-name").textContent = file.name;
  $("#import-result").classList.add("hidden");
  openDialog("import-dialog");
});

// 拖入预览区时给个反馈
document.addEventListener("dragover", (event) => {
  const zone = event.target.closest?.(".drop-zone");
  if (zone) zone.classList.add("dragover");
});
document.addEventListener("dragleave", (event) => {
  const zone = event.target.closest?.(".drop-zone");
  if (zone) zone.classList.remove("dragover");
});

/* ------------------------------------------------------------------ 启动 */

Promise.all([loadDashboard(), loadProperties(), loadTasks(), loadSettings()])
  .catch((error) => toast(`初始化失败：${error.message}`, true));
