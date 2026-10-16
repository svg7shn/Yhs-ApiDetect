/* 生图检测页 — /image 的前端逻辑。
   后端口径(Python 重写版):
     POST /api/image/models       {base_url, api_key, timeout}                     -> {models:[{name,kind,size}], models_error}
     POST /api/image/run          {base_url, api_key, models[], manual_models[], phases[], timeout, retry} -> {task:{task_id}}
     GET  /api/image/task_status?task_id=...                                      -> {task:{status,total,finished,current,results[],passed,failed,error}}
     GET  /api/image/evidence/{task_id}/{path}                                     -> 证据文件
     GET  /api/image/history?limit=50                                              -> {runs:[...]}
   API Key 只留在这块内存里:不上 localStorage、不拼进任何上报。 */

(function () {
  "use strict";

  var boot = window.__IMAGE_BOOT__ || {};
  var PHASES = Array.isArray(boot.phases) ? boot.phases : [];
  var DEFAULT_BASE = typeof boot.defaultBaseUrl === "string" ? boot.defaultBaseUrl : "";
  var DEFAULT_TIMEOUT = Number(boot.defaultTimeout) || 180;
  var POLL_MS = 1000;
  var BASE_STORE_KEY = "Yhs-ApiDetect.image.base";

  var el = {
    base: document.getElementById("img-base"),
    key: document.getElementById("img-key"),
    keyToggle: document.getElementById("img-key-toggle"),
    modelsLoad: document.getElementById("img-models-load"),
    modelsManual: document.getElementById("img-models-manual"),
    modelsBox: document.getElementById("img-models"),
    modelsHint: document.getElementById("img-models-hint"),
    manual: document.getElementById("img-manual"),
    phases: document.getElementById("img-phases"),
    phasesAll: document.getElementById("img-phases-all"),
    phasesNone: document.getElementById("img-phases-none"),
    timeout: document.getElementById("img-timeout"),
    retry: document.getElementById("img-retry"),
    run: document.getElementById("img-run"),
    reset: document.getElementById("img-reset"),
    alert: document.getElementById("img-alert"),
    plan: document.getElementById("img-plan"),
    task: document.getElementById("img-task"),
    taskId: document.getElementById("img-task-id"),
    taskCurrent: document.getElementById("img-task-current"),
    progress: document.getElementById("img-progress"),
    progressBar: document.getElementById("img-progress-bar"),
    progressText: document.getElementById("img-progress-text"),
    runEmpty: document.getElementById("img-run-empty"),
    stateBadge: document.getElementById("img-state-badge"),
    results: document.getElementById("img-results"),
    resultsEmpty: document.getElementById("img-results-empty"),
    countBadge: document.getElementById("img-count-badge"),
    expand: document.getElementById("img-expand"),
    historyWrap: document.getElementById("img-history-wrap"),
    historyBody: document.getElementById("img-history-body"),
    historyEmpty: document.getElementById("img-history-empty"),
    historyRefresh: document.getElementById("img-history-refresh"),
    stats: {
      models: document.querySelector('[data-stat="models"]'),
      passed: document.querySelector('[data-stat="passed"]'),
      failed: document.querySelector('[data-stat="failed"]')
    }
  };

  var state = {
    taskId: null,
    rendered: 0,
    timer: null,
    running: false,
    expanded: true,
    stalledAt: 0,
    stalledNotified: false
  };

  /* ── 小工具 ─────────────────────────────────────────── */

  function fmtTime(iso) {
    if (!iso) { return "—"; }
    var d = new Date(iso);
    if (isNaN(d.getTime())) { return String(iso); }
    var pad = function (n) { return n < 10 ? "0" + n : String(n); };
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
      " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function evidenceUrl(taskId, rel) {
    if (!taskId || !rel) { return ""; }
    var encoded = String(rel).split("/").map(encodeURIComponent).join("/");
    return "/api/image/evidence/" + encodeURIComponent(taskId) + "/" + encoded;
  }

  function setAlert(message, soft) {
    if (!message) {
      el.alert.hidden = true;
      el.alert.textContent = "";
      el.alert.className = "img-alert";
      return;
    }
    el.alert.textContent = message;
    el.alert.className = soft ? "img-alert is-soft" : "img-alert";
    el.alert.hidden = false;
  }

  function setBadge(node, text, variant) {
    if (!node) { return; }
    node.textContent = text;
    node.className = "img-badge" + (variant ? " " + variant : "");
  }

  async function requestJson(path, options) {
    var res;
    try {
      res = await fetch(path, options || {});
    } catch (err) {
      var netErr = new Error("请求发送失败:" + (err && err.message ? err.message : "网络错误"));
      netErr.status = 0;
      throw netErr;
    }
    var data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok || !data || data.ok === false) {
      var detail = data && data.error ? (data.error.message || data.error.code) : "";
      var httpErr = new Error(detail || ("接口返回 HTTP " + res.status));
      httpErr.status = res.status;
      httpErr.payload = data;
      throw httpErr;
    }
    return data;
  }

  function postJson(path, body) {
    return requestJson(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
  }

  /* ── 表单读写 ───────────────────────────────────────── */

  function selectedPhases() {
    var boxes = el.phases ? el.phases.querySelectorAll("input[type=checkbox]") : [];
    var out = [];
    for (var i = 0; i < boxes.length; i += 1) {
      if (boxes[i].checked) { out.push(boxes[i].value); }
    }
    return out;
  }

  function selectedModels() {
    if (!el.modelsBox || el.modelsBox.hidden) { return []; }
    var boxes = el.modelsBox.querySelectorAll("input[type=checkbox]");
    var out = [];
    for (var i = 0; i < boxes.length; i += 1) {
      if (!boxes[i].checked) { continue; }
      out.push({
        name: boxes[i].value,
        kind: boxes[i].getAttribute("data-kind") || "images",
        size: boxes[i].getAttribute("data-size") || "1024x1024"
      });
    }
    return out;
  }

  function manualModels() {
    if (!el.manual || el.manual.hidden) { return []; }
    return el.manual.value.split("\n").map(function (line) {
      return line.trim();
    }).filter(function (line) { return line.length > 0; });
  }

  function readForm() {
    return {
      base_url: (el.base.value || "").trim(),
      api_key: (el.key.value || "").trim(),
      models: selectedModels(),
      manual_models: manualModels(),
      phases: selectedPhases(),
      timeout: Number(el.timeout.value) || DEFAULT_TIMEOUT,
      retry: Number(el.retry.value) || 0
    };
  }

  function rememberBase() {
    try {
      var v = (el.base.value || "").trim();
      if (v) { window.localStorage.setItem(BASE_STORE_KEY, v); }
      else { window.localStorage.removeItem(BASE_STORE_KEY); }
    } catch (err) { /* 隐私模式下忽略 */ }
  }

  function restoreBase() {
    try {
      var saved = window.localStorage.getItem(BASE_STORE_KEY);
      if (saved) { el.base.value = saved; }
    } catch (err) { /* 忽略 */ }
    if (!el.base.value && DEFAULT_BASE) { el.base.placeholder = DEFAULT_BASE; }
  }

  /* ── 概览数字 ───────────────────────────────────────── */

  function setStats(models, passed, failed) {
    if (el.stats.models) { el.stats.models.textContent = String(models); }
    if (el.stats.passed) { el.stats.passed.textContent = String(passed); }
    if (el.stats.failed) { el.stats.failed.textContent = String(failed); }
  }

  /* ── 本次将跑哪些项(右栏的排期预览) ─────────────────── */

  function phaseNamesFor(kind, picked) {
    var out = [];
    picked.forEach(function (p) {
      if (!p || !p.name) { return; }
      if (kind === "chat") {
        if (p.name === "chat_generation") { out.push(p.name); }
        return;
      }
      if (p.name.indexOf("chat_") === 0) { return; }
      out.push(p.name);
    });
    return out;
  }

  function planForModel(kind, picked) {
    var names = phaseNamesFor(kind, picked);
    if (kind === "chat") { return names; }
    var hasGen = names.some(function (n) { return n.indexOf("generation_") === 0; });
    var edits = names.filter(function (n) { return n.indexOf("edit_") === 0; });
    // 只勾改图时,后端会先自动跑一次 transparent 生成当原图,所以多出一项。
    if (!hasGen && edits.length) { return names.concat(["generation_transparent(自动原图)"]); }
    return names;
  }

  function renderPlan() {
    if (!el.plan) { return; }
    var allPhases = Array.isArray(boot.phases) ? boot.phases : [];
    var chosen = selectedPhases();
    var picked = chosen.length
      ? allPhases.filter(function (p) { return chosen.indexOf(p.name) >= 0; })
      : allPhases;

    var models = selectedModels();
    var manual = manualModels();
    var rows = models.length
      ? models.map(function (m) { return { name: m.name, kind: m.kind || "images" }; })
      : manual.map(function (n) { return { name: n, kind: "images" }; });

    el.plan.textContent = "";
    if (!rows.length) {
      var tip = document.createElement("p");
      tip.style.margin = "0";
      tip.textContent = "还没勾选模型:点「开始检测」会自动拉取该站全部具备图片能力的模型,那时才知道要跑多少项。";
      el.plan.appendChild(tip);
      return;
    }

    var total = 0;
    var items = [];
    rows.forEach(function (row) {
      var list = planForModel(row.kind, picked);
      total += list.length;
      items.push(row.name + "（" + row.kind + "）" + list.length + " 项");
    });

    var head = document.createElement("p");
    head.style.margin = "0";
    var strong = document.createElement("b");
    strong.textContent = String(total);
    head.appendChild(document.createTextNode("本次将跑 "));
    head.appendChild(strong);
    head.appendChild(document.createTextNode(" 项（" + rows.length + " 个模型 × 已勾测试项）"));
    el.plan.appendChild(head);

    var ul = document.createElement("ul");
    ul.className = "img-plan-list";
    items.forEach(function (text) {
      var li = document.createElement("li");
      li.textContent = text;
      ul.appendChild(li);
    });
    el.plan.appendChild(ul);
  }

  /* ── 模型列表 ───────────────────────────────────────── */

  function renderModels(models) {
    if (!el.modelsBox) { return; }
    el.modelsBox.textContent = "";
    if (!models.length) {
      el.modelsBox.hidden = true;
      return;
    }
    models.forEach(function (model) {
      var label = document.createElement("label");
      label.className = "img-model";
      var box = document.createElement("input");
      box.type = "checkbox";
      box.value = model.name || "";
      box.setAttribute("data-kind", model.kind || "images");
      box.setAttribute("data-size", model.size || "");
      var name = document.createElement("span");
      name.className = "img-model-name";
      name.textContent = model.name || "(未命名)";
      var meta = document.createElement("span");
      meta.className = "img-model-meta";
      meta.textContent = (model.kind || "images") + " · " + (model.size || "—");
      label.appendChild(box);
      label.appendChild(name);
      label.appendChild(meta);
      el.modelsBox.appendChild(label);
    });
    el.modelsBox.hidden = false;
  }

  async function loadModels() {
    var base = (el.base.value || "").trim();
    var key = (el.key.value || "").trim();
    if (!key) {
      setAlert("先填 API Key,再读模型列表。", true);
      el.key.focus();
      return;
    }
    rememberBase();
    el.modelsLoad.disabled = true;
    var oldText = el.modelsLoad.textContent;
    el.modelsLoad.textContent = "读取中…";
    setAlert("");
    try {
      var data = await postJson("/api/image/models", {
        base_url: base,
        api_key: key,
        timeout: Number(el.timeout.value) || DEFAULT_TIMEOUT
      });
      renderModels(Array.isArray(data.models) ? data.models : []);
      if (data.models_error) {
        setAlert("没读到模型列表:" + data.models_error + "。可以点「手填模型名」自己写。", true);
        if (el.modelsHint) {
          el.modelsHint.textContent = "读取失败,改用手填模型名,或勾选后可重试。";
        }
      } else {
        var count = (data.models || []).length;
        setAlert(count ? "读到 " + count + " 个具备图片能力的模型,勾选要检测的;不勾选则检测全部。" : "该站没有返回具备图片能力的模型,请手填模型名。", true);
        if (el.modelsHint) {
          el.modelsHint.textContent = "勾选要检测的模型;不勾选 = 自动检测该站全部具备图片能力的模型。";
        }
      }
    } catch (err) {
      setAlert("读取模型列表失败:" + err.message, false);
    } finally {
      el.modelsLoad.disabled = false;
      el.modelsLoad.textContent = oldText;
    }
  }

  /* ── 结果渲染 ───────────────────────────────────────── */

  // 逐项明细:默认就展开,失败项整行标红,"未执行"的占位项用灰点。
  function buildCheckList(checks) {
    var wrap = document.createElement("div");
    wrap.className = "img-checks-wrap";
    var title = document.createElement("p");
    title.className = "img-checks-title";
    title.textContent = "逐项检查";
    wrap.appendChild(title);
    (checks || []).forEach(function (check) {
      var det = check.detail || "";
      var skipped = !check.passed && det.indexOf("未执行") === 0;
      var row = document.createElement("div");
      row.className = "img-checkrow " + (check.passed ? "is-ok" : (skipped ? "is-skip" : "is-fail"));
      var mark = document.createElement("span");
      mark.className = "img-mark";
      mark.textContent = check.passed ? "✓" : (skipped ? "–" : "✗");
      var name = document.createElement("span");
      name.className = "img-ck-name";
      name.textContent = check.name || "检查";
      var dd = document.createElement("span");
      dd.className = "img-ck-det";
      dd.textContent = det || (check.passed ? "通过" : "未通过");
      row.appendChild(mark);
      row.appendChild(name);
      row.appendChild(dd);
      wrap.appendChild(row);
    });
    return wrap;
  }

  // 左侧缩略图:直接用证据接口取后端落盘的那张图;拿不到就写清楚原因。
  function buildThumb(taskId, result) {
    var box = document.createElement("a");
    box.className = "img-thumb";
    box.target = "_blank";
    box.rel = "noopener";
    box.title = "点开原图";
    if (!result.image_file) {
      box.removeAttribute("target");
      box.className = "img-thumb img-thumb-none";
      box.textContent = result.passed ? "这一项没有产出图" : "没有拿到图";
      return box;
    }
    box.href = evidenceUrl(taskId, result.image_file);
    var img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "产出图 " + (result.phase_label || result.phase || "");
    img.src = box.href;
    img.addEventListener("error", function () {
      if (img.parentNode) { img.parentNode.removeChild(img); }
      box.removeAttribute("href");
      box.className = "img-thumb img-thumb-none";
      box.textContent = "产出图没写出来";
    });
    box.appendChild(img);
    return box;
  }

  function buildWhy(result) {
    var wrap = document.createElement("div");
    wrap.className = "img-why";
    var box = document.createElement("p");
    box.className = "img-why-box" + (result.passed ? " is-ok" : "");
    box.textContent = result.reason || (result.passed ? "全部检查通过" : "未通过");
    wrap.appendChild(box);
    var checks = result.checks || [];
    var ok = 0;
    checks.forEach(function (c) { if (c.passed) { ok += 1; } });
    var sub = document.createElement("p");
    sub.className = "img-why-sub";
    sub.textContent = "共 " + checks.length + " 项检查 · " + ok + " 项通过 · " + (checks.length - ok) + " 项未通过";
    wrap.appendChild(sub);
    return wrap;
  }

  function buildEvidence(taskId, result) {
    var wrap = document.createElement("div");
    wrap.className = "img-evidence";
    var items = [
      ["请求", result.request_file],
      ["响应", result.response_file],
      ["图片", result.image_file]
    ];
    var shown = 0;
    items.forEach(function (item) {
      if (!item[1]) { return; }
      var a = document.createElement("a");
      a.href = evidenceUrl(taskId, item[1]);
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = "下载" + item[0];
      wrap.appendChild(a);
      shown += 1;
    });
    if (!shown) {
      var none = document.createElement("span");
      none.className = "img-evidence-none";
      none.textContent = "这一项没有留下证据文件。";
      wrap.appendChild(none);
    }
    return wrap;
  }

  function buildResult(taskId, result, expanded) {
    var card = document.createElement("article");
    card.className = "img-result";
    card.setAttribute("data-passed", result.passed ? "1" : "0");

    var head = document.createElement("header");
    head.className = "img-result-head";
    var title = document.createElement("div");
    title.className = "img-result-title";
    var model = document.createElement("span");
    model.className = "img-result-model";
    model.textContent = result.model || "(未命名模型)";
    var phase = document.createElement("span");
    phase.className = "img-result-phase";
    phase.textContent = result.phase_label || result.phase || "";
    title.appendChild(model);
    title.appendChild(phase);
    head.appendChild(title);

    var badge = document.createElement("span");
    badge.className = "img-badge " + (result.passed ? "" : "img-badge-fail");
    badge.textContent = result.passed ? "通过" : "未通过";
    head.appendChild(badge);
    card.appendChild(head);

    var reason = document.createElement("p");
    reason.className = "img-result-reason";
    reason.textContent = result.reason || (result.passed ? "全部检查通过" : "未通过");
    card.appendChild(reason);

    var body = document.createElement("div");
    body.className = "img-result-body";
    body.appendChild(buildThumb(taskId, result));
    body.appendChild(buildWhy(result));
    card.appendChild(body);

    var list = buildCheckList(result.checks);
    list.hidden = !expanded;
    card.appendChild(list);

    card.appendChild(buildEvidence(taskId, result));
    return card;
  }

  function applyExpand() {
    var wraps = el.results.querySelectorAll(".img-checks-wrap");
    for (var i = 0; i < wraps.length; i += 1) {
      wraps[i].hidden = !state.expanded;
    }
  }

  function renderNewResults(task) {
    var results = Array.isArray(task.results) ? task.results : [];
    while (state.rendered < results.length) {
      var card = buildResult(task.task_id, results[state.rendered], state.expanded);
      el.results.appendChild(card);
      state.rendered += 1;
    }
    if (results.length) {
      el.resultsEmpty.hidden = true;
    }
    el.countBadge.textContent = results.length + " 项";
    setStats(
      countModels(results),
      task.passed || 0,
      task.failed || 0
    );
  }

  function countModels(results) {
    var seen = {};
    var n = 0;
    results.forEach(function (item) {
      var name = item.model || "";
      if (!seen[name]) { seen[name] = true; n += 1; }
    });
    return n;
  }

  /* ── 进度 ───────────────────────────────────────────── */

  function setProgress(task) {
    var total = Number(task.total) || 0;
    var finished = Number(task.finished) || 0;
    var ratio = total > 0 ? Math.min(100, Math.round((finished / total) * 100)) : 0;
    el.progressBar.style.width = ratio + "%";
    el.progress.setAttribute("aria-valuenow", String(ratio));
    el.progressText.textContent = finished + " / " + (total || "?") + " 项";
    if (task.status === "running" || task.status === "queued") {
      el.progress.classList.add("is-active");
    } else {
      el.progress.classList.remove("is-active");
    }
  }

  function setTaskChrome(task) {
    el.runEmpty.hidden = true;
    el.task.hidden = false;
    el.taskId.textContent = task.task_id || "—";
    el.taskCurrent.textContent = task.current ||
      (task.status === "done" ? "检测完成" : (task.status === "failed" ? "检测中断" : "准备中…"));
    var labels = { queued: ["排队中", "img-badge-run"], running: ["检测中", "img-badge-run"], done: ["已完成", ""], failed: ["失败", "img-badge-fail"] };
    var info = labels[task.status] || ["空闲", "img-badge-muted"];
    setBadge(el.stateBadge, info[0], info[1]);
    setProgress(task);
  }

  /* ── 轮询 ───────────────────────────────────────────── */

  // 后端卡死时服务端不会报错、状态一直是 running,前端会无限期停在"准备中"。
  // 这里按"长时间一项结果都没有"兜底提示,把用户引到服务窗口日志。
  var STALL_HINT_MS = 60000;

  function watchStall(task) {
    if ((task.finished || 0) > 0) {
      state.stalledAt = 0;
      return;
    }
    if (!state.stalledAt) { state.stalledAt = Date.now(); }
    if (state.stalledNotified) { return; }
    var waited = Math.round((Date.now() - state.stalledAt) / 1000);
    if (waited * 1000 >= STALL_HINT_MS) {
      state.stalledNotified = true;
      setAlert("已经等了 " + waited + " 秒还是没有出任何结果:后端可能出错了。请查看启动服务的那个窗口里的报错;" +
        "最常见的原因是证据目录不可写(环境变量 Yhs-ApiDetect_IMAGE_DIR)。", false);
    }
  }

  function stopPolling() {
    if (state.timer) {
      window.clearTimeout(state.timer);
      state.timer = null;
    }
  }

  async function poll() {
    if (!state.taskId) { return; }
    try {
      var data = await requestJson("/api/image/task_status?task_id=" + encodeURIComponent(state.taskId));
      var task = data.task || {};
      setTaskChrome(task);
      renderNewResults(task);
      watchStall(task);
      if (task.status === "done" || task.status === "failed") {
        state.running = false;
        el.run.disabled = false;
        el.run.textContent = "开始检测";
        if (task.status === "failed") {
          setAlert("检测中断:" + (task.error || "未知原因"), false);
        } else if ((task.failed || 0) > 0) {
          setAlert("检测完成:" + (task.passed || 0) + " 项通过," + (task.failed || 0) + " 项未通过。", true);
        } else {
          setAlert("检测完成:全部 " + (task.finished || 0) + " 项通过。", true);
        }
        loadHistory();
        return;
      }
    } catch (err) {
      if (err.status === 404) {
        state.running = false;
        el.run.disabled = false;
        el.run.textContent = "开始检测";
        setAlert("这次任务的状态已经过期(服务重启或超过保留时长),结果请重跑一次。", false);
        return;
      }
      setAlert("读取进度失败:" + err.message + "(会自动重试)", true);
    }
    state.timer = window.setTimeout(poll, POLL_MS);
  }

  function startPolling(taskId) {
    stopPolling();
    state.taskId = taskId;
    state.rendered = 0;
    state.running = true;
    state.stalledAt = 0;
    state.stalledNotified = false;
    el.results.textContent = "";
    el.resultsEmpty.hidden = false;
    el.countBadge.textContent = "0 项";
    el.run.disabled = true;
    el.run.textContent = "检测中…";
    state.timer = window.setTimeout(poll, 400);
  }

  /* ── 开始检测 ───────────────────────────────────────── */

  async function startRun() {
    if (state.running) { return; }
    var form = readForm();
    if (!form.api_key) {
      setAlert("API Key 不能为空。", false);
      el.key.focus();
      return;
    }
    rememberBase();
    setAlert("");
    el.run.disabled = true;
    el.run.textContent = "正在提交…";
    try {
      var data = await postJson("/api/image/run", form);
      var task = data.task || {};
      if (!task.task_id) { throw new Error("服务没有返回任务 ID"); }
      setTaskChrome(Object.assign({ results: [], finished: 0, passed: 0, failed: 0 }, task));
      startPolling(task.task_id);
    } catch (err) {
      el.run.disabled = false;
      el.run.textContent = "开始检测";
      if (err.status === 409) {
        var existing = err.payload && err.payload.error ? err.payload.error.task_id : "";
        if (existing) {
          setAlert("已有一个检测任务在跑,正在接管它的进度。", true);
          el.task.hidden = false;
          el.runEmpty.hidden = true;
          startPolling(existing);
          return;
        }
      }
      setAlert("启动失败:" + err.message, false);
    }
  }

  /* ── 历史 ───────────────────────────────────────────── */

  function renderHistory(runs) {
    if (!Array.isArray(runs) || !runs.length) {
      el.historyWrap.hidden = true;
      el.historyEmpty.hidden = false;
      el.historyEmpty.textContent = "还没有历史记录。跑完一次检测就会出现在这里。";
      return;
    }
    el.historyBody.textContent = "";
    runs.forEach(function (run) {
      var tr = document.createElement("tr");
      tr.dataset.deleteUrl = "/api/image/history/" + encodeURIComponent(run.task_id);
      var cells = [
        [fmtTime(run.start_time), ""],
        [run.base_url || "—", "img-cell-soft"],
        [run.selection || "—", "img-cell-soft"],
        [(run.passed || 0) + " 通过 / " + (run.failed || 0) + " 未通过", ""],
        [run.status === "done" ? "完成" : (run.status === "failed" ? ("失败:" + (run.error || "")) : (run.status || "—")), "img-cell-soft"]
      ];
      cells.forEach(function (cell) {
        var td = document.createElement("td");
        td.textContent = cell[0];
        if (cell[1]) { td.className = cell[1]; }
        tr.appendChild(td);
      });
      el.historyBody.appendChild(tr);
    });
    el.historyEmpty.hidden = true;
    el.historyWrap.hidden = false;
  }

  document.addEventListener('record-deleted', function (event) { if (event.detail.url.indexOf('/api/image/history/') === 0) loadHistory(); });

  async function loadHistory() {
    try {
      var data = await requestJson("/api/image/history?limit=50");
      renderHistory(data.runs);
    } catch (err) {
      el.historyWrap.hidden = true;
      el.historyEmpty.hidden = false;
      el.historyEmpty.textContent = "读取历史失败:" + err.message;
    }
  }

  /* ── 事件绑定 ───────────────────────────────────────── */

  function bind() {
    el.keyToggle.addEventListener("click", function () {
      var shown = el.key.type === "text";
      el.key.type = shown ? "password" : "text";
      el.keyToggle.textContent = shown ? "显示" : "隐藏";
      el.keyToggle.setAttribute("aria-pressed", shown ? "false" : "true");
    });

    el.modelsLoad.addEventListener("click", loadModels);

    el.modelsManual.addEventListener("click", function () {
      var opened = !el.manual.hidden;
      el.manual.hidden = opened;
      el.modelsManual.setAttribute("aria-pressed", opened ? "false" : "true");
      if (!opened) {
        el.manual.focus();
      } else {
        el.manual.value = "";
      }
    });

    el.phasesAll.addEventListener("click", function () {
      var boxes = el.phases.querySelectorAll("input[type=checkbox]");
      for (var i = 0; i < boxes.length; i += 1) { boxes[i].checked = true; }
    });

    el.phasesNone.addEventListener("click", function () {
      var boxes = el.phases.querySelectorAll("input[type=checkbox]");
      for (var i = 0; i < boxes.length; i += 1) { boxes[i].checked = false; }
    });

    el.run.addEventListener("click", startRun);

    el.reset.addEventListener("click", function () {
      stopPolling();
      state.taskId = null;
      state.rendered = 0;
      state.running = false;
      el.results.textContent = "";
      el.resultsEmpty.hidden = false;
      el.countBadge.textContent = "0 项";
      el.task.hidden = true;
      el.runEmpty.hidden = false;
      el.run.disabled = false;
      el.run.textContent = "开始检测";
      setBadge(el.stateBadge, "空闲", "img-badge-muted");
      setStats(0, 0, 0);
      setAlert("");
    });

    el.expand.addEventListener("click", function () {
      state.expanded = !state.expanded;
      el.expand.setAttribute("aria-pressed", state.expanded ? "true" : "false");
      el.expand.textContent = state.expanded ? "收起逐项明细" : "展开逐项明细";
      applyExpand();
    });

    el.historyRefresh.addEventListener("click", loadHistory);

    el.base.addEventListener("change", rememberBase);

    // 勾选/填写变化时实时重算"本次将跑多少项",让右栏不只是空着。
    if (el.modelsBox) { el.modelsBox.addEventListener("change", renderPlan); }
    if (el.phases) { el.phases.addEventListener("change", renderPlan); }
    if (el.manual) { el.manual.addEventListener("input", renderPlan); }
  }

  function init() {
    restoreBase();
    bind();
    renderPlan();
    setBadge(el.stateBadge, "空闲", "img-badge-muted");
    setStats(0, 0, 0);
    // 恢复中断前正在跑的任务(例如刷新了页面)
    requestJson("/api/image/task_status").then(function (data) {
      if (data.active_task_id) {
        setAlert("有一个检测任务还在跑,正在接管它的进度。", true);
        startPolling(data.active_task_id);
      }
    }).catch(function () { /* 静默:拿不到状态不影响手动开始 */ });
    loadHistory();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
