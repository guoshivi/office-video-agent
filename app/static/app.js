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
const LEVEL_TAG = { blocking: "必须改", warn: "看一眼", ok: "OK", unknown: "待检" };

// 机器能说清"哪里不对"，但说不出"你该动哪里"。
// 这张表补的是后半句——对不懂这套系统的人来说，后者才是他真正需要的。
const CHECK_FIX = {
  script: "口播是空的，先写文案再出片",
  video: "还没有成片，先点「提交真实出片」",
  media: "读不出画面参数，人工打开看一眼能不能正常播放",
  audio: "成片没有声音，配音没进去，重新出片",
  aspect: "横版视频发到竖版平台会被压扁，重出竖版",
  duration: "太短观众看不清，太长完播率会掉，15–60 秒最稳",
  area: "把口播里的面积改成房源库那个数，再重新出片",
  rent: "租金和房源库对不上，确认哪个是对的再改文案",
  identity: "文案里出现了别的楼栋，八成是视频套错了房源",
  compliance: "去掉微信、手机号、「加我」这类说法，会被限流",
  hygiene: "口播里念出了内部提醒语，删掉那一句再重新出片",
  duplicate: "这套房源最近发过，确认是不是要重发",
};

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

/* ------------------------------------------------------------------ 开工体检 */

// 按钮上的字不用后端那句长描述，太长会撑破卡片；
// action 到短动词的映射就放在前端，人话归人话。
const READY_SHORT = {
  start_xiaohongshu: "点我启动",
  start_douyin: "点我打开",
  login_xiaohongshu: "点我扫码",
  enable_xiaohongshu: "点我打开",
  enable_douyin: "点我打开",
  open_settings: "去填地址",
};

function readyChip(item) {
  const state = item.ok ? "ok" : item.optional ? "off" : "todo";
  const tail = item.ok ? "在线" : (READY_SHORT[item.action] || "未就绪");
  const clickable = Boolean(item.action);
  return `<button type="button" class="ready-chip ${state}${clickable ? " can-fix" : ""}"
    data-action="${escapeHtml(item.action)}" title="${escapeHtml(item.detail)}"
    ${clickable ? "" : "disabled"}>
    ${escapeHtml(item.label)}<span>${escapeHtml(tail)}</span></button>`;
}

function renderReady(report) {
  const bar = $("#ready-bar");
  if (!bar) return;
  bar.classList.remove("is-loading");
  bar.dataset.state = !report.can_render ? "down" : report.blocking ? "gap" : "ok";
  $("#ready-headline").textContent = report.headline;
  const problems = report.items.filter((item) => !item.ok && !item.optional);
  $("#ready-detail").textContent = problems.length
    ? problems.map((item) => item.detail).join("；")
    : `上次检查 ${report.checked_at || "刚刚"}`;
  $("#ready-chips").innerHTML = report.items.map(readyChip).join("");
}

async function loadReady(force = false) {
  try {
    const report = await api(`/api/ready${force ? "?refresh=1" : ""}`);
    renderReady(report);
    return report;
  } catch (error) {
    const bar = $("#ready-bar");
    bar.classList.remove("is-loading");
    bar.dataset.state = "down";
    $("#ready-headline").textContent = "检查没跑起来";
    $("#ready-detail").textContent = error.message;
    return null;
  }
}

async function runReadyAction(button) {
  const action = button.dataset.action;
  if (!action) return;

  if (action === "open_settings") { showView("settings"); return; }
  if (action === "install_render_deps") {
    // 装组件要一分钟上下，按钮上得一直有反馈，否则会被当成卡死
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "正在装…";
    try {
      const result = await api("/api/ready/start", {
        method: "POST",
        body: JSON.stringify({ target: "render_deps" }),
      });
      toast(result.message || "已处理", !result.ok);
      if (result.ready) renderReady(result.ready);
      else await loadReady(true);
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
    return;
  }
  if (action === "login_xiaohongshu") {
    showView("publish");
    await loadPublish();
    await requestQrcode("xiaohongshu");
    return;
  }

  const platform = action.includes("douyin") ? "douyin" : "xiaohongshu";
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  try {
    const result = action.startsWith("enable_")
      ? await api("/api/ready/toggle", {
        method: "POST",
        body: JSON.stringify({ platform, enabled: true }),
      })
      : await api("/api/ready/start", {
        method: "POST",
        body: JSON.stringify({ target: platform }),
      });
    toast(result.message || "已处理");
    if (result.ready) renderReady(result.ready);
    else await loadReady(true);
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
    button.textContent = original;
  }
}

/* ------------------------------------------------------------------ 工作台 */

async function loadDashboard() {
  // 体检要两三秒，不能挡住下面的数字卡片，所以不 await
  loadReady();
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
        <span>${escapeHtml(task.aspect_ratio)} · ${Number(task.target_duration) > 0 ? task.target_duration + " 秒" : "根据原素材"}</span>
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
        <td>${escapeHtml(task.aspect_ratio)} · ${Number(task.target_duration) > 0 ? task.target_duration + " 秒" : "根据原素材"}</td>
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
    $("#stage-verdict").innerHTML = `<span class="dot"></span><div><strong>等待选择</strong><small>检查结论会显示在这里</small></div>`;
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
  const summary = precheck.summary || "还没做检查";
  // 结论只说"是什么"没用，要接一句"所以现在该干嘛"
  const nextStep = precheck.level === "blocking"
    ? "改完点右上角「重新检查」再看一次"
    : precheck.level === "warn" ? "这些不影响发，觉得没问题就可以通过" : "";
  banner.innerHTML = `<span class="dot"></span><div><strong>${escapeHtml(LEVEL_LABEL[precheck.level] || "待检")}</strong>
    <small>${escapeHtml(summary)}${nextStep ? ` · ${escapeHtml(nextStep)}` : ""}</small></div>`;

  const ordered = [...precheck.blocking, ...precheck.warn, ...precheck.ok.slice(0, 4)];
  $("#stage-checks").innerHTML = ordered.length
    ? ordered.map((check) => {
      const fix = check.level === "ok" ? "" : CHECK_FIX[check.id];
      return `
      <div class="check-item ${escapeHtml(check.level)}">
        <span class="bulb"></span>
        <div><span class="label">${escapeHtml(check.label)}</span> · <span class="detail">${escapeHtml(check.detail)}</span>
          ${fix ? `<small class="fix">怎么办：${escapeHtml(fix)}</small>` : ""}</div>
        <span class="tag-lv">${escapeHtml(LEVEL_TAG[check.level] || check.level)}</span>
      </div>`;
    }).join("")
    : `<div class="empty" style="padding:18px"><strong>还没有检查结果</strong>点右上角「重新检查」</div>`;

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

function cleanPlatformMessage(message) {
  // 平台返回的提示是给机器/命令行看的，常带 ❌✅ 和换行；卡片上只留一句人话
  const flat = (message || "").replace(/\s+/g, " ").trim().replace(/^[❌✅⚠]+\s*/, "");
  return flat.length > 88 ? `${flat.slice(0, 88)}…` : flat;
}

function platformCard(key, info) {
  const label = key === "xiaohongshu" ? "小红书" : "抖音";
  const avatar = key === "xiaohongshu" ? "小红" : "抖音";
  const avatarClass = key === "xiaohongshu" ? "xhs" : "dy";
  const online = info.reachable && info.logged_in;
  // 「没打开开关」和「打开了但连不上」是两件事：
  // 副标题永远说运行状态（服务没启动 / 未登录 / 已登录），
  // 右上角的灯才说开关。混在一起会让人以为"服务挂了"，白排查一轮。
  const stateText = !info.reachable ? "服务没启动" : info.logged_in ? "已登录" : "未登录";
  const pill = !info.enabled
    ? { text: "未启用", cls: "neutral" }
    : online ? { text: "就绪", cls: "active" }
      : info.reachable ? { text: "待登录", cls: "needs_review" }
        : { text: "不可用", cls: "failed" };
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
        <span class="status-pill ${pill.cls}" style="margin-left:auto">${pill.text}</span>
      </div>
      <div class="platform-rows">
        <div class="platform-row"><span>今日额度</span><span>${quota.used_today ?? 0} / ${quota.daily_limit ?? "—"}</span></div>
        <div class="platform-row"><span>最小间隔</span><span>${escapeHtml(String(state.settings.publish_min_interval_minutes || "—"))} 分钟</span></div>
        ${hint}
        <div class="platform-row"><span>说明</span><span class="note">${escapeHtml(cleanPlatformMessage(info.message))}</span></div>
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
  // 老库里没有这些键时，下拉框会显示空白；给一个和代码一致的默认值，
  // 免得看起来像"配置丢了"
  [["render_engine", "local"], ["local_audio_mode", "mix"], ["render_flower", "1"],
   ["render_flower_style", "auto"], ["render_flower_density", "normal"],
   ["render_bgm", "1"], ["render_bgm_volume", "0.14"],
   ["render_subtitle_size", "medium"], ["render_subtitle_bottom", "middle"],
   ["render_subtitle_highlight", "1"], ["render_highlight_source", "ai"],
   ["render_kenburns", "1"], ["tts_provider", "auto"], ["tts_cluster", "volcano_tts"],
   ["tts_emotion_scale", "4"]].forEach(([name, value]) => {
    if (form.elements[name] && !form.elements[name].value) form.elements[name].value = value;
  });
  if (form.elements.default_duration) {
    // 默认时长设为 0 表示「默认根据原素材」：选完视频会自动开启跟原片模式
    $("#task-form").elements.target_duration.value = Number(settings.default_duration) > 0 ? settings.default_duration : 45;
    $("#task-form").elements.aspect_ratio.value = settings.default_aspect_ratio;
    $("#task-form").elements.voice.value = settings.default_voice;
  }
  $("#ffmpeg-status").textContent = health.ffmpeg_available ? "FFmpeg 已就绪（随程序自带）" : "FFmpeg 不见了；出片会失败，重装一份完整包";
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
  // 配音用哪家：豆包 / 免费兜底，一眼能看出来，不用翻到设置里找
  const ttsState = health.tts_state || { provider: "edge", label: "edge-tts", ready: true, detail: "" };
  const ttsBox = $("#tts-state");
  if (ttsBox) {
    ttsBox.textContent = `${ttsState.detail || ""}`.trim() || ttsState.label;
    $("#tts-pill").textContent = ttsState.provider === "volcano"
      ? (ttsState.ready ? "豆包语音" : "待补音色")
      : "免费配音";
    $("#tts-pill").className = `status-pill ${ttsState.provider === "volcano" && ttsState.ready ? "active" : "neutral"}`;
  }
  renderFeishu(feishu);
  renderBitable(bitable);
  loadSemanticStatus().catch(() => {});
  api("/api/copy/status")
    .then((copy) => {
      const hint = $("#copy-model-hint");
      if (hint) hint.textContent = copy.ready ? `${copy.label} · ${copy.model} · ` : "未配置 AI，走规则版 · ";
      const button = $("#btn-task-versions");
      if (button) button.title = copy.ready ? "" : "走规则套写；去系统设置里选服务商并填 API Key 就能用大模型润色";
    })
    .catch(() => {});
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
  resetQuick();
  openDialog("quick-dialog");
  setTimeout(() => $("#quick-query")?.focus(), 150);
}

function openAdvancedTask() {
  closeDialog("quick-dialog");
  fillPropertyOptions(state.propertyOptions);
  resetDurationField();
  // 人群下拉要在弹窗打开时就有选项，不能等点了「出三版」才填
  ensureAudienceOptions().catch(() => {});
  resetRenderOptions();
  openDialog("task-dialog");
}

// 每次新开一张单，观感项都退回「跟设置」，免得上一单的选择被悄悄带过来
function resetRenderOptions() {
  ["subtitle_size", "subtitle_bottom", "tts_rate", "flower"].forEach((name) => {
    const field = $(`#task-form [name="${name}"]`);
    if (field) field.value = "";
  });
  const box = $("#task-render-options");
  if (box) box.open = false;
  renderOptionHints();
}

// 「跟设置」到底是哪个值，写在折叠条上，省得每次回去翻设置页
function renderOptionHints(selector = "#render-defaults-hint") {
  const hint = $(selector);
  if (!hint) return;
  const settings = state.settings || {};
  const size = { small: "小", medium: "中", large: "大" }[settings.render_subtitle_size] || "中";
  const where = { high: "靠上", middle: "居中", low: "靠下" }[settings.render_subtitle_bottom] || "居中";
  const rate = (settings.render_tts_rate || "").trim() || "默认";
  const flower = String(settings.render_flower ?? "1") === "1" ? "开" : "关";
  hint.textContent = `当前：${size}字 · ${where} · 语速${rate} · 花字${flower}`;
}

function renderDetailHints() {
  renderOptionHints("#detail-defaults-hint");
}

/* ------------------------------------------------------------------ 快速出片 */

// 三步里任何一步没完成，按钮就不亮——比让人点了报错友好。
const quick = { clip: null, upload: null, chosen: null, query: "", options: {}, timer: null };

function resetQuick() {
  quick.clip = null;
  quick.upload = null;
  quick.chosen = null;
  quick.query = "";
  clearTimeout(quick.timer);
  $("#quick-video").value = "";
  $("#quick-file-name").textContent = "";
  $("#quick-query").value = "";
  $("#quick-matches").innerHTML = "";
  $("#quick-progress").classList.add("hidden");
  $("#quick-progress-bar").style.width = "0%";
  $("#quick-step-video").classList.remove("done");
  $("#quick-step-property").classList.remove("done");
  $("#quick-submit").disabled = true;
  $("#quick-summary").textContent = "选好视频、确认房源，就可以开始。";
  // 打开时就把设置里的默认选择显示出来，让人看见"确实有这个开关"
  $("#quick-audio-mode").value = state.settings.local_audio_mode || "mix";
}

function humanSize(bytes) {
  const mb = Number(bytes || 0) / 1024 / 1024;
  if (mb >= 1) return `${mb.toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function quickSummary() {
  const parts = [];
  if (quick.clip) parts.push(`视频已就绪（${humanSize(quick.clip.size)}）`);
  if (quick.chosen) parts.push(`房源：<strong>${escapeHtml(quick.chosen.label)}</strong>`);
  $("#quick-summary").innerHTML = parts.length === 2
    ? `${parts[0]}，${parts[1]}。点右下角「开始出片」，出完会进审核台等你确认。`
    : "选好视频、确认房源，就可以开始。";
  $("#quick-submit").disabled = !(quick.clip && quick.chosen);
}

/** 上传视频。用 XHR 是为了拿到上传进度——2 GB 的片子没有进度条会让人以为卡死。 */
function uploadQuickClip(file) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("video", file);
    form.append("channel", "desktop");

    const bar = $("#quick-progress-bar");
    $("#quick-progress").classList.remove("hidden");
    bar.style.width = "0%";

    const request = new XMLHttpRequest();
    request.open("POST", "/api/channel/clips");
    request.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      bar.style.width = `${Math.round((event.loaded / event.total) * 100)}%`;
    });
    request.addEventListener("load", () => {
      let payload = null;
      try { payload = JSON.parse(request.responseText); } catch { /* 下面统一报错 */ }
      if (request.status >= 400 || !payload) {
        reject(new Error(payload?.detail || `上传失败（${request.status}）`));
        return;
      }
      bar.style.width = "100%";
      resolve(payload);
    });
    request.addEventListener("error", () => reject(new Error("上传中断，检查网络后重试")));
    request.send(form);
  });
}

async function pickQuickVideo(file) {
  if (!file) return;
  if (!/\.(mp4|mov|m4v|avi|mkv)$/i.test(file.name)) {
    toast("只支持 MP4 / MOV / M4V / AVI / MKV", true);
    return;
  }
  $("#quick-file-name").textContent = `正在上传：${file.name}`;
  $("#quick-step-video").classList.add("done");
  if (quick.clip) {
    // 换视频时把上一段撤掉，避免占着 inbox 不放
    api(`/api/channel/clips/${quick.clip.id}`, { method: "DELETE" }).catch(() => {});
    quick.clip = null;
    quickSummary();
  }
  try {
    const result = await uploadQuickClip(file);
    quick.clip = result.clip;
    $("#quick-file-name").textContent = `已上传：${file.name}`;
    $("#quick-progress").classList.add("hidden");
    quickSummary();
    $("#quick-query").focus();
  } catch (error) {
    $("#quick-step-video").classList.remove("done");
    $("#quick-file-name").textContent = "";
    $("#quick-progress").classList.add("hidden");
    toast(error.message, true);
  }
}

function renderQuickMatches(result) {
  const box = $("#quick-matches");
  const step = $("#quick-step-property");
  quick.options = {};

  if (result.status === "matched") {
    quick.chosen = result.property;
    quick.options[result.property.property_id] = result.property.label;
    step.classList.add("done");
    box.innerHTML = `<button type="button" class="quick-match chosen" data-pick="${escapeHtml(result.property.property_id)}">
      就是这套：${escapeHtml(result.property.label)}<small>已选中</small></button>`;
    quickSummary();
    return;
  }

  if (result.status === "ambiguous") {
    quick.chosen = null;
    step.classList.remove("done");
    result.candidates.forEach((item) => { quick.options[item.property_id] = item.label; });
    box.innerHTML = `<div class="quick-empty">找到 ${result.candidates.length} 套相近的，点一下选对的那套：</div>`
      + result.candidates.map((item) => `<button type="button" class="quick-match" data-pick="${escapeHtml(item.property_id)}">
        ${escapeHtml(item.label)}<small>${item.status === "needs_review" ? "待确认" : ""}</small></button>`).join("");
    quickSummary();
    return;
  }

  quick.chosen = null;
  step.classList.remove("done");
  box.innerHTML = `<div class="quick-empty">${escapeHtml(result.hint || "没找到这套房源。打「区 + 楼栋 + 房号」再试，比如 F区 F14 202-2")}</div>`;
  quickSummary();
}

async function queryQuickProperty(text) {
  const keyword = (text || "").trim();
  if (keyword.length < 2) {
    $("#quick-matches").innerHTML = "";
    quick.chosen = null;
    $("#quick-step-property").classList.remove("done");
    quickSummary();
    return;
  }
  try {
    const result = await api(`/api/channel/resolve?q=${encodeURIComponent(keyword)}`);
    // 打字快的时候旧请求可能后到，用当前输入值挡一下
    if ($("#quick-query").value.trim() !== keyword) return;
    renderQuickMatches(result);
  } catch (error) {
    $("#quick-matches").innerHTML = `<div class="quick-empty">${escapeHtml(error.message)}</div>`;
  }
}

function chooseQuickCandidate(propertyId) {
  const node = $(`#quick-matches [data-pick="${propertyId}"]`);
  if (!node) return;
  quick.chosen = { property_id: propertyId, label: quick.options[propertyId] || node.textContent.trim() };
  $("#quick-step-property").classList.add("done");
  $$("#quick-matches .quick-match").forEach((item) => item.classList.toggle("chosen", item === node));
  quickSummary();
}

async function submitQuick() {
  if (!quick.clip || !quick.chosen) return;
  const button = $("#quick-submit");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "正在建任务…";
  try {
    const bound = await api(`/api/channel/clips/${quick.clip.id}/bind`, {
      method: "POST",
      body: JSON.stringify({ property_id: quick.chosen.property_id }),
    });
    if (bound.status !== "bound") {
      renderQuickMatches(bound);
      toast("房源没定下来，再选一次", true);
      return;
    }
    const task = bound.task;
    // 快速出片也要能选原声：先把选择落到任务上，再提交出片
    const audioMode = $("#quick-audio-mode")?.value || "";
    const effective = task.audio_mode || state.settings.local_audio_mode || "mix";
    if (audioMode && audioMode !== effective) {
      await api(`/api/tasks/${task.id}/audio`, {
        method: "PUT", body: JSON.stringify({ audio_mode: audioMode }),
      });
    }
    button.textContent = "正在提交出片…";
    await api(`/api/tasks/${task.id}/render`, { method: "POST" });
    closeDialog("quick-dialog");
    toast(`${task.task_no} 已开始出片，好了会进审核台`);
    showView("tasks");
    loadTasks();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.textContent = original;
    button.disabled = !(quick.clip && quick.chosen);
  }
}

/* ------------------------------------------------------------------ 任务对话框 */

function applyTaskDetail(task, initial = true) {
  state.currentTask = task;
  if (initial) {
    $("#detail-number").textContent = task.task_no;
    $("#detail-title").textContent = task.property_label || "视频任务";
    $("#detail-script").value = task.script;
    // 任务没单独选过就显示设置里的默认值，避免下拉空白
    $("#detail-audio-mode").value = task.audio_mode || state.settings.local_audio_mode || "mix";
    // 观感四项反过来：没单独选过就显示「跟设置」，不假装是某个具体值
    const renderFields = {
      "#detail-subtitle-size": task.subtitle_size,
      "#detail-subtitle-bottom": task.subtitle_bottom,
      "#detail-tts-rate": task.tts_rate,
      "#detail-flower": task.flower,
    };
    Object.entries(renderFields).forEach(([selector, value]) => {
      const field = $(selector);
      if (field) field.value = value || "";
    });
    renderDetailHints();
    $("#detail-warnings").innerHTML = task.warnings.map((warning) => `<div class="warning-item">${escapeHtml(warning)}</div>`).join("");
    // 换一个任务，上一套三版就不该还挂在这儿
    const versions = $("#detail-versions");
    if (versions) versions.innerHTML = "";
    ensureAudienceOptions().catch(() => {});
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

// ---------------------------------------------------------------- 口播文案

function copyDurationValue() {
  const raw = Number(durationInput().value);
  if (Number.isFinite(raw) && raw > 0) return Math.round(raw);
  // 跟原片模式下用素材真实长度，还没选视频就按 45 秒估
  if (sourceSeconds) return Math.round(clampDuration(sourceSeconds));
  return 45;
}

function shotCountForDuration(seconds) {
  // 一镜约 7.5 秒：45 秒 6 镜，90 秒 8 镜。少于 4 镜讲不清一套房，多于 8 镜口播会赶。
  return Math.max(4, Math.min(8, Math.round(Number(seconds || 45) / 7.5)));
}

// 三版开头：同一套房源，参数反差 / 痛点召唤 / 稀缺时效各出一条，
// 点哪版就把整篇口播换掉。人群决定「说给谁听」，换人群开头会跟着变。
// 新建任务弹窗和任务详情共用一个接口，只是往不同的框里填。
let copyLibraryReady = false;

async function ensureAudienceOptions() {
  const selects = [$("#detail-audience"), $("#task-audience")].filter(Boolean);
  if (copyLibraryReady || !selects.length) return;
  const data = await api("/api/copy/hooks");
  const options = (data.audiences || [])
    .map((item) => `<option value="${escapeHtml(item.key)}">${escapeHtml(item.label)}</option>`)
    .join("");
  const tip = (data.tiers || []).map((tier) => `${tier.label}：${tier.when || ""}`).join("\n");
  selects.forEach((select) => {
    select.innerHTML = options;
    select.title = tip;
  });
  copyLibraryReady = true;
}

function versionTitlesHtml(titles) {
  const group = (label, list) => {
    if (!list || !list.length) return "";
    const pills = list.map((text) => `<span>${escapeHtml(text)}</span>`).join("");
    return `<div class="version-titles"><em>${label}</em><div class="version-pills">${pills}</div></div>`;
  };
  return group("抖音标题", titles.douyin) + group("小红书标题", titles.xiaohongshu);
}

function renderCopyVersions(data, box, target, pickedNote, options = {}) {
  if (!box) return;
  const cards = (data.versions || []).map((version, index) => `
    <button type="button" class="version-card" data-index="${index}" title="${escapeHtml(version.script)}">
      <span class="version-tag">${escapeHtml(version.tier_label)}</span>
      <strong>${escapeHtml(version.hook)}</strong>
    </button>`).join("");
  // 新建任务里地方小，标题留给任务详情页看，这里只给开头
  box.innerHTML = cards + (options.titles === false ? "" : versionTitlesHtml(data.titles || {}));
  box.classList.remove("hidden");
  box.querySelectorAll(".version-card").forEach((node) => {
    node.addEventListener("click", () => {
      const version = data.versions[Number(node.dataset.index)];
      if (!version || !target) return;
      target.value = version.script;
      box.querySelectorAll(".version-card").forEach((item) => item.classList.remove("active"));
      node.classList.add("active");
      toast(`已换成「${version.tier_label}」这版，${pickedNote}`);
    });
  });
}

async function loadCopyVersions() {
  const task = state.currentTask;
  if (!task || !task.property_id) {
    toast("这条任务没关联房源，钩子写不出来", true);
    return;
  }
  const button = $("#btn-copy-versions");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "在想…";
  try {
    await ensureAudienceOptions();
    const data = await api("/api/copy/versions", {
      method: "POST",
      body: JSON.stringify({
        property_id: task.property_id,
        audience: $("#detail-audience")?.value || "any",
        count: 3,
      }),
    });
    renderCopyVersions(data, $("#detail-versions"), $("#detail-script"), "点保存草稿才生效");
    toast(`出了 ${data.versions.length} 版，点一条就用它`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

// 新建任务时还没有任务记录，但房源已经选好了——出三版只需要房源，
// 所以这里能直接给，不用先建草稿再进详情页。
async function loadTaskVersions() {
  const propertyId = $("#task-property")?.value;
  if (!propertyId) {
    toast("先选关联房源，才知道写哪一套", true);
    return;
  }
  const button = $("#btn-task-versions");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "在想…";
  try {
    await ensureAudienceOptions();
    const data = await api("/api/copy/versions", {
      method: "POST",
      body: JSON.stringify({
        property_id: propertyId,
        audience: $("#task-audience")?.value || "any",
        count: 3,
        // 镜头数跟目标时长走：一镜约 7.5 秒，45 秒给 6 个镜头
        shot_count: shotCountForDuration(copyDurationValue()),
      }),
    });
    renderCopyVersions(data, $("#task-versions"), $("#task-script"), "不满意可以换人群再出一次", { titles: false });
    toast(`出了 ${data.versions.length} 版，点一条填进文案框`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

async function polishCopy() {
  const text = $("#task-script").value.trim();
  if (text.length < 10) {
    toast("先写几句，或点「出三版」挑一个再润色", true);
    return;
  }
  const button = $("#btn-polish-copy");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "润色中…";
  try {
    const result = await api("/api/copy/polish", {
      method: "POST",
      body: JSON.stringify({ property_id: $("#task-property").value || "", script: text }),
    });
    $("#task-script").value = result.script || text;
    toast("润色完成，可继续手改");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

$("#btn-task-versions")?.addEventListener("click", loadTaskVersions);
$("#btn-polish-copy")?.addEventListener("click", polishCopy);
// 点开折叠区时把它拉进视野：新建弹窗里它在文案框下面，不滚一下
// 用户会以为点了没反应
["#task-render-options", "#detail-render-options"].forEach((selector) => {
  const box = $(selector);
  box?.addEventListener("toggle", () => {
    if (box.open) box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
});
// 换房源后上一套的开头就作废了，留着会被误点
$("#task-property")?.addEventListener("change", () => {
  const box = $("#task-versions");
  if (box) box.innerHTML = "";
});
$("#btn-copy-versions")?.addEventListener("click", loadCopyVersions);

$("#task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  busy(form, true, "正在上传…");
  try {
    const payload = new FormData(form);
    // 「跟原片」模式开启 → 提交 0，后端按素材真实长度出片
    if (durationButton().classList.contains("on")) {
      payload.set("target_duration", "0");
    }
    const task = await api("/api/tasks", { method: "POST", body: payload });
    closeDialog("task-dialog");
    form.reset();
    resetDurationField();
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

$("#tts-preview").addEventListener("click", async () => {
  const form = $("#settings-form");
  const button = $("#tts-preview");
  const label = $("#tts-preview-state");
  button.disabled = true;
  label.textContent = "先生成一句…";
  try {
    // 先落库，试听用的就是刚填进去的凭据，不用先点保存再回来
    const values = Object.fromEntries(new FormData(form).entries());
    values.default_duration = Number(values.default_duration || 45);
    state.settings = await api("/api/settings", { method: "PUT", body: JSON.stringify(values) });
    const response = await fetch("/api/tts/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: "230平的办公室，整整一面落地窗，白天不用开灯。",
        voice: form.elements.default_voice ? form.elements.default_voice.value : "female_warm",
      }),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || "试听失败，检查 AppID / Token 和音色 ID");
    }
    const provider = response.headers.get("X-TTS-Provider") || "";
    const blob = await response.blob();
    const audio = new Audio(URL.createObjectURL(blob));
    const name = provider === "volcano" ? "豆包语音" : "免费配音";
    label.textContent = name + "，播放中…";
    audio.onended = () => { label.textContent = name + "（试听结束）"; };
    await audio.play();
  } catch (error) {
    label.textContent = error.message;
  } finally { button.disabled = false; }
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
    // 先把这条任务的原声选择落库，再出片：出片读的是任务上的值
    const audioMode = $("#detail-audio-mode").value;
    const effective = state.currentTask.audio_mode || state.settings.local_audio_mode || "mix";
    if (audioMode !== effective) {
      await api(`/api/tasks/${state.currentTask.id}/audio`, {
        method: "PUT", body: JSON.stringify({ audio_mode: audioMode }),
      });
    }
    // 画面与字幕同理：改了才写库，没改不动，免得把「跟设置」写成死值
    const options = {
      subtitle_size: $("#detail-subtitle-size")?.value || "",
      subtitle_bottom: $("#detail-subtitle-bottom")?.value || "",
      tts_rate: $("#detail-tts-rate")?.value || "",
      flower: $("#detail-flower")?.value || "",
    };
    const before = state.currentTask;
    const changed = ["subtitle_size", "subtitle_bottom", "tts_rate", "flower"]
      .some((key) => options[key] !== (before[key] || ""));
    if (changed) {
      await api(`/api/tasks/${state.currentTask.id}/render-options`, {
        method: "PUT", body: JSON.stringify(options),
      });
    }
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
  button.textContent = "检查中…";
  try {
    await api(`/api/review/${taskId}/precheck`, { method: "POST" });
    await selectReview(taskId);
    toast("检查完了，结论已更新");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "重新检查"; }
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
/* ------------------------------------------------------------------ 原片时长 */

/* 浏览器自己就能读文件时长，不必等后端 ffprobe（那台机器上本来也没装）。
   MOV/AVI 偶有读不出的情况（duration 是 Infinity 或直接 error），当没读到处理。 */
function probeVideoDuration(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const probe = document.createElement("video");
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      URL.revokeObjectURL(url);
      resolve(value);
    };
    probe.preload = "metadata";
    probe.onloadedmetadata = () => done(Number.isFinite(probe.duration) ? probe.duration : 0);
    probe.onerror = () => done(0);
    // 元数据迟迟不来时别让按钮一直转圈
    setTimeout(() => done(0), 8000);
    probe.src = url;
  });
}

const durationInput = () => $("#target-duration");
const durationButton = () => $("#use-source-duration");

let sourceSeconds = 0;      // 当前视频的真实长度
let autoFilledSeconds = 0;  // 上一次由「跟原片」填进去的值，用来判断用户有没有手动改过

function renderDurationButton(seconds) {
  sourceSeconds = seconds || 0;
  const button = durationButton();
  if (!sourceSeconds) {
    button.disabled = true;
    button.classList.remove("on");
    button.textContent = "跟原片";
    button.title = "读不出这段视频的长度，请手动填";
    return;
  }
  button.disabled = false;
  button.textContent = `跟原片（${Math.round(sourceSeconds)} 秒）`;
  button.title = `按原视频的 ${Math.round(sourceSeconds)} 秒出片`;
}

function clampDuration(seconds) {
  return Math.min(180, Math.max(10, Math.round(seconds)));
}

// 每次打开表单都是干净的：清掉上一次的视频和时长状态
function resetDurationField() {
  $("#video-input").value = "";
  $("#video-file-name").textContent = "MP4、MOV、M4V、AVI、MKV，单个不超过 2 GB";
  autoFilledSeconds = 0;
  renderDurationButton(0);
  durationButton().classList.remove("on");
}

function applySourceDuration() {
  if (!sourceSeconds) return;
  // 「跟原片」是模式开关：开启后提交 target_duration=0，
  // 出片时由后端按素材的真实长度定，不受 180 秒上限截断
  const follow = !durationButton().classList.contains("on");
  if (follow) {
    durationInput().value = Math.min(180, Math.max(10, Math.round(sourceSeconds)));
    durationInput().placeholder = "根据原素材";
    durationButton().classList.add("on");
    autoFilledSeconds = Number(durationInput().value);
    toast(`已开启「根据原素材」：按原片 ${Math.round(sourceSeconds)} 秒出片`);
  } else {
    durationButton().classList.remove("on");
    autoFilledSeconds = 0;
    toast("已取消根据原素材，恢复手动时长");
  }
}

$("#use-source-duration").addEventListener("click", applySourceDuration);

// 手动改过就不再算「跟原片」，按钮恢复未选中
$("#target-duration").addEventListener("input", () => {
  if (Number(durationInput().value) !== autoFilledSeconds) {
    durationButton().classList.remove("on");
    autoFilledSeconds = 0;
  }
});

$("#video-input").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  $("#video-file-name").textContent = file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB` : "未选择视频";
  if (!file) {
    renderDurationButton(0);
    return;
  }
  const wasAuto = autoFilledSeconds;
  const seconds = await probeVideoDuration(file);
  renderDurationButton(seconds);

  // 设置里默认时长 = 0 → 新任务默认「根据原素材」，选完视频自动开启
  const defaultFollowSource = Number(state.settings.default_duration) === 0;
  if (seconds && (defaultFollowSource || (wasAuto && Number(durationInput().value) === wasAuto))) {
    durationInput().value = clampDuration(seconds);
    autoFilledSeconds = clampDuration(seconds);
    durationButton().classList.add("on");
    if (defaultFollowSource) {
      toast(`默认根据原素材出片：这段视频 ${Math.round(seconds)} 秒`);
    }
    return;
  }
  // 目标比素材还长，说明做不到。直接按素材长度来，别让人设了 45 却出 25 秒。
  if (seconds && Number(durationInput().value) > Math.round(seconds)) {
    const target = clampDuration(seconds);
    durationInput().value = target;
    autoFilledSeconds = target;
    durationButton().classList.add("on");
    toast(
      target === Math.round(seconds)
        ? `这段视频只有 ${Math.round(seconds)} 秒，目标时长已按原片改为 ${target} 秒`
        : `这段视频只有 ${Math.round(seconds)} 秒，不足 ${target} 秒下限，出片会比目标短`,
    );
  }
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

/* ------------------------------------------------------------------ 拖拽导入 */

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
  // 快速出片对话框里拖进来的应该是视频，不是房源表；两者都别抢错
  if (event.target.closest?.("#quick-drop")) {
    pickQuickVideo(file);
    return;
  }
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

$("#ready-refresh").addEventListener("click", async () => {
  const button = $("#ready-refresh");
  button.disabled = true;
  button.textContent = "检查中…";
  await loadReady(true);
  button.disabled = false;
  button.textContent = "重新检查";
});

$("#ready-chips").addEventListener("click", (event) => {
  const chip = event.target.closest(".ready-chip.can-fix");
  if (chip) runReadyAction(chip);
});

/* ------------------------------------------------------------------ 快速出片事件 */

$("#quick-video").addEventListener("change", (event) => {
  pickQuickVideo(event.target.files?.[0]);
});

$("#quick-drop").addEventListener("dragover", (event) => {
  event.preventDefault();
  $("#quick-drop").classList.add("dragover");
});
$("#quick-drop").addEventListener("dragleave", () => {
  $("#quick-drop").classList.remove("dragover");
});

$("#quick-query").addEventListener("input", (event) => {
  clearTimeout(quick.timer);
  const value = event.target.value;
  // 打字时不要每敲一下就查一次库，停手 320ms 再查
  quick.timer = setTimeout(() => queryQuickProperty(value), 320);
});

$("#quick-matches").addEventListener("click", (event) => {
  const pick = event.target.closest("[data-pick]");
  if (pick) chooseQuickCandidate(pick.dataset.pick);
});

$("#quick-advanced").addEventListener("click", () => openAdvancedTask());

$("#quick-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submitQuick();
});

Promise.all([loadDashboard(), loadProperties(), loadTasks(), loadSettings()])
  .catch((error) => toast(`初始化失败：${error.message}`, true));
