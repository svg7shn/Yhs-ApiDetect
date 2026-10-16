/* Authenticity console ( /authenticity ) — three-step, one-click run.
 *
 * 1 连接配置(base_url + api_key)→ 2 模型选择(读取中转站自己的模型列表并勾选)
 * → 3 测试参数 → 开始真实性检测。
 *
 * Secrets stay here: base_url + api_key live only in this page's memory and are
 * sent once per probe run — nothing is written to localStorage and the server
 * never persists them. The server only ever stores the targets, because
 * cross-family rejection and the mandatory-source rule are decisions a client
 * must not be able to skip.
 */
(function () {
  "use strict";

  var S = window.__AUTH_SOURCES__ || { sources: [], family_labels: {}, wire_labels: {}, verdict_labels: {} };
  var SOURCE_BY_ID = {};
  (S.sources || []).forEach(function (s) { SOURCE_BY_ID[s.id] = s; });
  var FAMILY_ORDER = ["claude", "gpt", "gemini"];

  // 记录列表一页显示多少条;历史多了以后靠「显示更多」翻页,不把页面撑长。
  var RECORD_PAGE = 10;

  // 延迟单位自适应:>=1000ms 收敛成秒(1 位小数),不足 1s 保留毫秒。
  function fmtMs(value) {
    if (value == null || value === "") return null;
    var n = Number(value);
    if (!isFinite(n)) return null;
    return n >= 1000 ? (n / 1000).toFixed(1) + " s" : Math.round(n) + " ms";
  }

  // 只在当前页面内存中保存本次填写的 base_url / api_key:不写 localStorage,刷新即清空。
  var local = [];
  var configs = [];
  var pollTimer = null;
  var activeTaskId = null;
  var lastRecordCount = -1;
  var recordRequestId = 0;
  var lastReport = null;
  var lastRecords = [];
  var recordLimit = RECORD_PAGE;
  var recordIndex = {};
  var modalRecord = null;

  var catalog = null;        // { wire_family, models: [] } — the last "刷新模型" result
  var selectedModels = {};   // { modelName: true }
  var sourceByFamily = {};   // { claude: "anthropic_direct", ... }
  var skippedOpen = false;   // 非对话 / 未识别族模型是否展开

  // ---- helpers -----------------------------------------------------------

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function norm(url) { return String(url || "").trim().replace(/\/+$/, ""); }
  function keyOf(url) { return norm(url).toLowerCase(); }

  function localFor(baseUrl) {
    for (var i = 0; i < local.length; i += 1) {
      if (keyOf(local[i].base_url) === keyOf(baseUrl)) return local[i];
    }
    return null;
  }

  function configFor(baseUrl) {
    for (var i = 0; i < configs.length; i += 1) {
      if (keyOf(configs[i].base_url) === keyOf(baseUrl)) return configs[i];
    }
    return null;
  }

  function $(id) { return document.getElementById(id); }

  function api(path, options) {
    return fetch(path, options).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (payload) {
        return { status: res.status, ok: res.ok, payload: payload };
      });
    });
  }

  function send(path, body, method) {
    return api(path, {
      method: method || "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  function errorText(result) {
    if (result && result.payload && result.payload.error) return result.payload.error.message || "请求失败";
    return "请求失败(HTTP " + (result ? result.status : "?") + ")";
  }

  function hours() { return $("auth-hours") ? $("auth-hours").value : "168"; }

  function familyLabel(family) { return (S.family_labels || {})[family] || family || ""; }

  function wireLabel(wire) { return (S.wire_labels || {})[wire] || wire || "-"; }

  function familyOfModel(model) {
    var name = String(model || "").toLowerCase();
    if (!name) return null;
    if (name.indexOf("claude") === 0 || name.indexOf("anthropic") === 0) return "claude";
    if (/^(gpt-|o1|o3|o4|chatgpt)/.test(name)) return "gpt";
    if (name.indexOf("gemini") === 0 || name.indexOf("models/gemini") === 0) return "gemini";
    return null;
  }

  // Models that cannot answer a chat probe must never become a target.
  function isChatLike(model) {
    return !/(embed|embedding|whisper|tts|audio|speech|transcri|dall|dalle|image|moderation|rerank|ocr|clip|voice|video|guard|sora|veo|stable-diffusion)/i
      .test(String(model || ""));
  }

  function verdictBadge(verdict) {
    var cls = verdict === "authentic" ? "ok" : verdict === "suspect" ? "warn" : verdict === "fail" ? "fail" : "error";
    var label = (S.verdict_labels || {})[verdict] || verdict;
    return '<span class="auth-badge auth-badge-' + cls + '">' + esc(label) + "</span>";
  }

  function rate(value) {
    return value == null ? "-" : (Math.round(value * 1000) / 10) + "%";
  }

  function setNotice(message) {
    var box = $("auth-models-error");
    if (!box) return;
    if (!message) { box.hidden = true; box.textContent = ""; return; }
    box.textContent = message;
    box.hidden = false;
  }

  function setTaskNotice(message) {
    var box = $("auth-task-errors");
    if (!box) return;
    if (!message) { box.hidden = true; box.textContent = ""; return; }
    box.textContent = message;
    box.hidden = false;
  }

  // ---- connection --------------------------------------------------------

  function currentBaseUrl() { return norm($("auth-base-url") ? $("auth-base-url").value : ""); }

  function currentKey() {
    var typed = $("auth-api-key") ? $("auth-api-key").value.trim() : "";
    if (typed) return typed;
    var entry = localFor(currentBaseUrl());
    return entry ? (entry.api_key || "") : "";
  }

  function rememberLocal(baseUrl, apiKey) {
    var entry = localFor(baseUrl);
    if (entry) entry.api_key = apiKey;
    else local.push({ base_url: baseUrl, api_key: apiKey });
    renderBaseUrlOptions();
  }

  function renderBaseUrlOptions() {
    var list = $("auth-base-url-list");
    if (!list) return;
    list.innerHTML = local.map(function (entry) {
      return '<option value="' + esc(entry.base_url) + '"></option>';
    }).join("");
  }

  function restoreLast() {
    if (!local.length) return;
    var last = local[local.length - 1];
    if (!$("auth-base-url").value) $("auth-base-url").value = last.base_url || "";
    if (!$("auth-api-key").value) $("auth-api-key").value = last.api_key || "";
    var cfg = configFor(last.base_url);
    if (!cfg) return;
    (cfg.targets || []).forEach(function (target) {
      var family = target.family || familyOfModel(target.model);
      if (family && target.claimed_source) sourceByFamily[family] = target.claimed_source;
    });
  }

  // ---- model selection ---------------------------------------------------

  function defaultSourceFor(family) {
    var hit = (S.sources || []).filter(function (s) { return s.family === family; })[0];
    return hit ? hit.id : "";
  }

  function sourceOptionsFor(family, selected) {
    return (S.sources || []).filter(function (s) { return s.family === family; }).map(function (s) {
      return '<option value="' + esc(s.id) + '"' + (s.id === selected ? " selected" : "") + ">" + esc(s.label) + "</option>";
    }).join("");
  }

  function classifiedGroups() {
    var out = { claude: [], gpt: [], gemini: [], skipped: [] };
    if (!catalog) return out;
    (catalog.models || []).forEach(function (name) {
      var family = familyOfModel(name);
      if (!family || !isChatLike(name)) { out.skipped.push(name); return; }
      out[family].push(name);
    });
    return out;
  }

  function selectedArray() {
    var groups = classifiedGroups();
    var picked = [];
    FAMILY_ORDER.forEach(function (family) {
      groups[family].forEach(function (name) { if (selectedModels[name]) picked.push(name); });
    });
    return picked;
  }

  function collectTargets() {
    var groups = classifiedGroups();
    var targets = [];
    FAMILY_ORDER.forEach(function (family) {
      var source = sourceByFamily[family] || defaultSourceFor(family);
      if (!source) return;
      groups[family].forEach(function (name) {
        if (selectedModels[name]) targets.push({ model: name, claimed_source: source });
      });
    });
    if (!targets.length && !catalog) {
      // 没有刷新模型也能直接沿用服务端上一次保存的检测目标。
      var cfg = configFor(currentBaseUrl());
      if (cfg) {
        targets = (cfg.targets || []).filter(function (t) { return t.model; }).map(function (t) {
          return { model: t.model, claimed_source: t.claimed_source };
        });
      }
    }
    return targets;
  }

  function updateDurationHint(count) {
    var hint = $("auth-duration-hint");
    if (!hint) return;
    if (!count) { hint.textContent = "先在「模型选择」里勾选模型,这里会估算整轮耗时。"; return; }
    var low = Math.max(1, Math.round((count * 30) / 60));
    var high = Math.max(low, Math.round((count * 75) / 60));
    hint.textContent = count + " 个模型 ≈ " + low + "~" + high + " 分钟(按 30~75 秒 / 模型估算)";
  }

  function renderModels() {
    var host = $("auth-models");
    if (!host) return;
    var groups = classifiedGroups();
    var picked = selectedArray();

    if (!catalog) {
      var cfg = configFor(currentBaseUrl());
      var saved = cfg ? (cfg.targets || []).filter(function (t) { return t.model; }).length : 0;
      var note = $("auth-models-note");
      if (note) {
        note.innerHTML = "填好上面的地址与 Key,点「刷新模型」读取中转站的 <b>/models</b> 列表,再勾选要检测的模型。" +
          "每个模型按「模型 + 声称来源」逐项检测,同族模型共用一个声称来源。";
      }
      host.innerHTML = '<p class="auth-note-inline">' + (saved
        ? "该地址已保存过 " + saved + " 个检测目标,可以直接点下方「开始真实性检测」沿用;也可以点右上「刷新模型」重新勾选。"
        : "还没有读取模型列表。填好地址与 API Key 后点右上「刷新模型」。") + "</p>";
      updateDurationHint(picked.length);
      return;
    }

    var testable = FAMILY_ORDER.reduce(function (sum, family) { return sum + groups[family].length; }, 0);
    var html = '<div class="auth-models-tools">' +
      '<span class="auth-models-count">已选 <b>' + picked.length + "</b> / " + testable + " 个可测模型</span>" +
      '<button class="btn auth-btn" type="button" id="auth-models-all">全选可测模型</button>' +
      '<button class="btn auth-btn" type="button" id="auth-models-none">清空选择</button>' +
      "</div>";

    FAMILY_ORDER.forEach(function (family) {
      var names = groups[family];
      if (!names.length) return;
      var source = sourceByFamily[family] || defaultSourceFor(family);
      var onCount = names.filter(function (name) { return selectedModels[name]; }).length;
      html += '<div class="auth-model-group">' +
        '<div class="auth-model-group-head">' +
          '<span class="auth-group-name">' + esc(familyLabel(family)) + "</span>" +
          '<span class="auth-group-count">' + names.length + "</span>" +
          '<label class="auth-group-source">声称来源' +
            '<select data-source-family="' + esc(family) + '">' + sourceOptionsFor(family, source) + "</select>" +
          "</label>" +
          '<button class="auth-group-toggle" type="button" data-group-toggle="' + esc(family) + '">' +
            (onCount === names.length ? "取消本族" : "全选本族") +
          "</button>" +
        "</div>" +
        '<div class="auth-chips">' + names.map(function (name) {
          return '<button class="auth-model-chip' + (selectedModels[name] ? " is-on" : "") +
            '" type="button" data-model="' + esc(name) + '">' + esc(name) + "</button>";
        }).join("") + "</div>" +
      "</div>";
    });

    if (groups.skipped.length) {
      html += '<button class="auth-skip-models" type="button" id="auth-skip-toggle">' +
        '<span aria-hidden="true">' + (skippedOpen ? "▾" : "▸") + "</span> 非对话 / 未识别族模型 " +
        groups.skipped.length + " 个(向量 / 图像 / 语音等,或无法从名称识别 Claude · OpenAI · Gemini 族,默认不测)" +
        "</button>";
      if (skippedOpen) {
        html += '<div class="auth-chips">' + groups.skipped.map(function (name) {
          return '<span class="auth-model-chip is-off">' + esc(name) + "</span>";
        }).join("") + "</div>";
      }
    }

    host.innerHTML = html;

    var noteNode = $("auth-models-note");
    if (noteNode) {
      noteNode.innerHTML = "已连接接口:<b>/models</b>(" + esc(wireLabel(catalog.wire_family)) + "),共 <b>" +
        (catalog.models || []).length + "</b> 个模型 —— 其中 <b>" + testable + "</b> 个可测," +
        groups.skipped.length + " 个非对话 / 未识别族模型已自动排除。";
    }
    updateDurationHint(picked.length);
  }

  function loadModels() {
    var baseUrl = currentBaseUrl();
    var apiKey = currentKey();
    if (!/^https?:\/\//.test(baseUrl)) { setNotice("站点地址必须以 http:// 或 https:// 开头。"); return; }
    if (apiKey.length < 8) { setNotice("API Key 看起来不完整(至少 8 位)。"); return; }

    var btn = $("auth-load-models");
    setNotice("");
    setTaskNotice("");
    if (btn) { btn.disabled = true; btn.textContent = "读取中…"; }

    send("/api/authenticity/models", { base_url: baseUrl, api_key: apiKey }).then(function (res) {
      if (btn) { btn.disabled = false; btn.textContent = "刷新模型"; }
      var payload = res.payload || {};
      if (!payload.ok) {
        var detail = (payload.attempts || []).map(function (attempt) {
          return (attempt.wire_family || "?") + " " + (attempt.transport_error || ("HTTP " + attempt.status)) +
            "(" + (attempt.count || 0) + ")";
        }).join(" · ");
        catalog = null;
        renderModels();
        setNotice("读取模型列表失败:" + (detail || errorText(res)));
        return;
      }

      catalog = { wire_family: payload.wire_family, models: payload.models || [] };
      selectedModels = {};
      skippedOpen = false;

      var cfg = configFor(baseUrl);
      var restored = false;
      if (cfg) {
        var savedModels = {};
        (cfg.targets || []).forEach(function (target) {
          var family = target.family || familyOfModel(target.model);
          if (family && target.claimed_source) sourceByFamily[family] = target.claimed_source;
          if (target.model) savedModels[target.model] = true;
        });
        (catalog.models || []).forEach(function (name) {
          if (savedModels[name]) { selectedModels[name] = true; restored = true; }
        });
      }
      if (!restored) {
        var groups = classifiedGroups();
        FAMILY_ORDER.forEach(function (family) {
          groups[family].forEach(function (name) { selectedModels[name] = true; });
        });
      }
      renderModels();
    });
  }

  // ---- configuration and report range -------------------------------------

  var RANGE_LABELS = { "24": "最近 24 小时", "168": "最近 7 天", "720": "最近 30 天", "0": "全部范围" };


  function refreshConfigs() {
    return api("/api/authenticity/config").then(function (res) {
      configs = (res.payload && res.payload.configs) || [];
    });
  }

  // ---- running ------------------------------------------------------------

  function showTask(on) {
    var node = $("auth-task");
    if (node) node.hidden = !on;
  }

  function refreshCompletedRecords(taskId, attempts) {
    if (activeTaskId !== taskId) return;
    loadRecords().catch(function () {
      if (activeTaskId !== taskId) return;
      if (attempts > 1) {
        pollTimer = window.setTimeout(function () { refreshCompletedRecords(taskId, attempts - 1); }, 1500);
      } else {
        setTaskNotice("检测已结束，但读取最新记录失败，请刷新页面重试。");
      }
    });
  }

  function startPolling(taskId) {
    if (pollTimer) window.clearTimeout(pollTimer);
    activeTaskId = taskId;
    lastRecordCount = -1;
    var failures = 0;
    showTask(true);
    function tick() {
      api("/api/authenticity/task_status?task_id=" + encodeURIComponent(taskId)).then(function (res) {
        if (activeTaskId !== taskId) return;
        var task = res.payload && res.payload.task;
        if (!res.ok || !task) {
          throw new Error(errorText(res));
        }
        failures = 0;
        renderTask(task);
        var recordCount = (task.record_ids || []).length;
        if (recordCount !== lastRecordCount) {
          lastRecordCount = recordCount;
          if (recordCount) loadRecords().catch(function () { /* retry next tick */ lastRecordCount = -1; });
        }
        if (task.status === "done" || task.status === "failed") {
          pollTimer = null;
          // Retry transient storage/network lag before asking the user to refresh.
          refreshCompletedRecords(taskId, 3);
          return;
        }
        pollTimer = window.setTimeout(tick, 1500);
      }).catch(function (err) {
        if (activeTaskId !== taskId) return;
        failures += 1;
        setTaskNotice("进度同步暂时失败，正在重试（" + failures + "）：" + err.message);
        pollTimer = window.setTimeout(tick, Math.min(1500 * failures, 6000));
      });
    }
    tick();
  }

  function renderTask(task) {
    var running = task.status === "running" || task.status === "queued";
    var label = task.status === "running" ? "检测中" : task.status === "queued" ? "排队中" : task.status === "done" ? "已完成" : "失败";
    var progressText = task.finished + " / " + task.total;

    if (running || task.status === "done" || task.status === "failed") showTask(true);

    var chip = $("auth-run-chip");
    if (chip) {
      chip.hidden = !running;
      chip.textContent = label + " · " + progressText;
    }

    var current = $("auth-task-current");
    if (current) {
      if (running) current.textContent = task.current || (label + " · " + progressText);
      else if (task.status === "done") current.textContent = "上一轮已完成 · " + progressText;
      else current.textContent = task.error ? "失败 · " + task.error : "空闲";
    }

    var pct = task.total ? Math.round((task.finished / task.total) * 100) : 0;
    var bar = $("auth-progress-bar");
    var wrap = $("auth-progress");
    var currentInFlight = running && Boolean(task.current);
    if (wrap) {
      wrap.classList.toggle("is-active", currentInFlight);
      wrap.setAttribute("aria-valuenow", String(pct));
      wrap.setAttribute("aria-valuetext", currentInFlight
        ? "已完成 " + progressText + "，当前模型检测中"
        : "已完成 " + progressText);
    }
    if (bar) bar.style.width = pct + "%";
    var progressLabel = $("auth-progress-text");
    if (progressLabel) progressLabel.textContent = currentInFlight
      ? "已完成 " + progressText + " · 正在采集当前模型的检测信号（此阶段不估算百分比）"
      : "已完成 " + progressText;

    var lines = [];
    (task.config_errors || []).forEach(function (item) {
      lines.push("配置错误 · " + (item.target || "") + " · " + (item.message || ""));
    });
    (task.target_failures || []).forEach(function (item) {
      lines.push("已跳过 · " + (item.target || "该模型") + " · " + (item.message || item.code || ""));
    });
    if (task.error) lines.push("任务失败 · " + task.error);
    setTaskNotice(lines.join("\n"));
  }

  function probeOptions() {
    var node = $("auth-timeout");
    var value = node ? parseInt(node.value, 10) : NaN;
    if (isNaN(value)) value = 45;
    if (value < 5) value = 5;
    if (value > 300) value = 300;
    return { timeout: value };
  }

  function startRun() {
    var baseUrl = currentBaseUrl();
    var apiKey = currentKey();
    if (!/^https?:\/\//.test(baseUrl)) { setTaskNotice("站点地址必须以 http:// 或 https:// 开头。"); return; }
    if (apiKey.length < 8) { setTaskNotice("API Key 看起来不完整(至少 8 位)。"); return; }

    var targets = collectTargets();
    if (!targets.length) {
      setTaskNotice(catalog
        ? "还没有勾选任何可测模型,请在「模型选择」里至少选一个。"
        : "还没有可用的检测目标:请在「模型选择」里点「刷新模型」并勾选模型。");
      return;
    }

    rememberLocal(baseUrl, apiKey);
    var cfg = configFor(baseUrl);
    var body = {
      base_url: baseUrl,
      channel_name: cfg ? cfg.channel_name : baseUrl,
      enabled: cfg ? Boolean(cfg.enabled) : true,
      targets: targets,
      available_models: catalog ? catalog.models : null,
      version: cfg ? cfg.version : null
    };
    var path = cfg ? "/api/authenticity/config/" + encodeURIComponent(cfg.channel_id) : "/api/authenticity/config";

    var btn = $("auth-run-all");
    setTaskNotice("");
    if (btn) { btn.disabled = true; btn.textContent = "启动中…"; }

    send(path, body, cfg ? "PUT" : "POST").then(function (res) {
      if (!res.ok) {
        if (btn) { btn.disabled = false; btn.textContent = "开始真实性检测"; }
        setTaskNotice("保存检测目标失败:" + errorText(res));
        return null;
      }
      var saved = res.payload.config;
      return send("/api/authenticity/test/" + encodeURIComponent(saved.channel_id), {
        api_key: apiKey,
        options: probeOptions()
      }).then(function (res2) {
        if (btn) { btn.disabled = false; btn.textContent = "开始真实性检测"; }
        if (!res2.ok) { setTaskNotice(errorText(res2)); return null; }
        showTask(true);
        startPolling(res2.payload.task_id);
        return refreshConfigs();
      });
    });
  }

  // ---- report & records ---------------------------------------------------

  function renderReport(report) {
    lastReport = report || null;
    var host = $("auth-report");
    if (!host) return;
    if (!report || !report.by_family || !report.by_family.length) {
      host.innerHTML = '<div class="auth-empty">该范围内还没有检测记录。填好地址与 Key、勾选模型后点「开始真实性检测」。</div>';
      return;
    }
    var head = "<tr><th>协议族</th><th>总请求</th><th>已完成</th><th>真实</th><th>疑似异常</th><th>判定伪造</th><th>传输错误</th><th>真实率</th><th>疑似率</th><th>能力准确率</th><th>平均延迟</th></tr>";
    var rows = report.by_family.map(function (b) {
      return "<tr>" +
        "<td>" + esc(b.family_label || b.family) + "</td>" +
        "<td>" + b.total + "</td>" +
        "<td>" + b.completed + "</td>" +
        "<td>" + b.authentic + "</td>" +
        "<td>" + b.suspect + "</td>" +
        "<td>" + b.fail + "</td>" +
        "<td>" + b.error + "</td>" +
        "<td>" + rate(b.authentic_rate) + "</td>" +
        "<td>" + rate(b.suspect_rate) + "</td>" +
        "<td>" + rate(b.capability_accuracy_avg) + "</td>" +
        "<td>" + (b.latency_avg_ms == null ? "-" : fmtMs(b.latency_avg_ms)) + "</td>" +
      "</tr>";
    }).join("");
    var t = report.totals || {};
    rows += '<tr class="auth-muted"><td>合计</td><td>' + t.total + "</td><td>" + t.completed + "</td><td>" + t.authentic +
      "</td><td>" + t.suspect + "</td><td>" + t.fail + "</td><td>" + t.error + "</td><td>" + rate(t.authentic_rate) +
      "</td><td>" + rate(t.suspect_rate) + "</td><td colspan=\"2\"></td></tr>";
    host.innerHTML = '<div class="auth-table-scroll"><table class="auth-table">' + head + rows + "</table></div>";
  }

  var SIGNAL_LABELS = {
    capability: "能力题",
    identity: "身份自报",
    error_injection: "错误注入",
    thinking_signature: "thinking 签名",
    model_echo: "模型回显",
    parameter_validation: "参数校验",
    cache: "缓存行为"
  };

  function signalLabel(key) {
    return SIGNAL_LABELS[key] || key || "";
  }

  var SIGNAL_STATUS_LABELS = { pass: "通过", fail: "未通过", warn: "存疑", skip: "不适用", unavailable: "无法判断", error: "出错" };
  function signalStatusLabel(status) {
    return SIGNAL_STATUS_LABELS[status] || status || "未知";
  }

  function fmtTime(value) {
    if (!value) return "";
    var d = new Date(value);
    if (isNaN(d.getTime())) return String(value);
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
      " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  // A transmission-error record has no score or latency; printing the bare
  // labels ("score · 置信 0.9 · ms") reads as corrupt data.
  function recordMeta(rec) {
    var parts = [];
    if (rec.score != null && rec.score !== "") parts.push("score " + rec.score);
    if (rec.confidence != null && rec.confidence !== "") parts.push("置信 " + rec.confidence);
    if (rec.latency_ms != null && rec.latency_ms !== "") parts.push(fmtMs(rec.latency_ms));
    return parts.join(" · ");
  }

  function renderRecords(records) {
    var target = $("auth-records");
    if (!target) return;
    lastRecords = records || [];
    // Index every rendered record so 「详情」 can resolve it.
    records.forEach(function (rec) { recordIndex[String(rec.record_id)] = rec; });
    if (!records.length) {
      target.innerHTML = '<div class="auth-empty">该范围内没有检测记录。</div>';
      return;
    }
    target.innerHTML = records.map(function (rec) {
      var meta = recordMeta(rec);
      var id = esc(rec.record_id || "");
      return '<div class="auth-record" data-delete-url="/api/authenticity/records/' + encodeURIComponent(rec.record_id) + '">' +
        '<div class="auth-record-main">' +
          '<div class="auth-record-title">' + verdictBadge(rec.verdict) +
            "<span>" + esc(rec.channel_name || rec.base_url) + "</span>" +
            "<span>" + esc(rec.model) + "</span>" +
          "</div>" +
          '<div class="auth-record-sub">' +
            "<span>" + esc(rec.source_label || rec.source) + "</span>" +
            (meta ? "<span>" + esc(meta) + "</span>" : "") +
            "<span>" + esc(fmtTime(rec.time)) + "</span>" +
          "</div>" +
        "</div>" +
        '<div class="auth-record-actions">' +
          '<button class="btn" type="button" data-record-act="detail" data-record-id="' + id + '">详情</button>' +
          '<button class="btn" type="button" data-record-act="json" data-record-id="' + id + '">JSON</button>' +
          '<button class="btn" type="button" data-record-act="md" data-record-id="' + id + '">MD</button>' +
        "</div>" +
      "</div>";
    }).join("");
    if (records.length >= recordLimit) {
      target.innerHTML += '<button class="btn auth-btn auth-records-more" type="button" id="auth-records-more">显示更多记录</button>';
    }
  }

  // ---- result modal -------------------------------------------------------

  function findRecord(recordId) {
    return recordIndex[String(recordId)] || null;
  }

  function scoreTone(rec) {
    return rec.verdict === "authentic" ? "ok" : rec.verdict === "suspect" ? "warn" : rec.verdict === "fail" ? "fail" : "";
  }

  function kpi(label, value, tone) {
    return '<div class="auth-kpi"><p class="auth-kpi-label">' + esc(label) + "</p>" +
      '<p class="auth-kpi-value' + (tone ? " is-" + tone : "") + '">' + esc(value) + "</p></div>";
  }

  function modalBody(rec) {
    var signals = (rec.signals || []).map(function (sig) {
      return '<div class="auth-signal">' +
        "<span>" + esc(signalLabel(sig.signal)) + "</span>" +
        '<span class="auth-badge auth-badge-' + (sig.status === "pass" ? "ok" : sig.status === "fail" ? "fail" : sig.status === "warn" ? "warn" : "off") + '">' + esc(signalStatusLabel(sig.status)) + "</span>" +
        "<span>" + esc(sig.detail || "") + "</span>" +
      "</div>";
    }).join("");
    return (rec.error_msg ? '<div class="auth-alert">传输错误 · ' + esc(rec.error_msg) + "</div>" : "") +
      '<div class="auth-kpi-grid">' +
        kpi("真实性评分", rec.score == null ? "-" : rec.score + " / 100", scoreTone(rec)) +
        kpi("置信度", rec.confidence == null ? "-" : String(rec.confidence), "") +
        kpi("能力准确率", rec.capability_accuracy == null ? "-" : rate(rec.capability_accuracy), "") +
        kpi("协议族", rec.wire_family || "-", "") +
        kpi("平均延迟", rec.latency_ms == null ? "-" : fmtMs(rec.latency_ms), "") +
        kpi("采集信号", (rec.signals || []).length + " 项", "") +
      "</div>" +
      (rec.summary ? '<p class="auth-modal-section-title">判定摘要</p><p class="auth-panel-note">' + esc(rec.summary) + "</p>" : "") +
      '<p class="auth-modal-section-title">信号明细</p><div class="auth-signal-list">' +
        (signals || '<div class="auth-empty">没有采集到信号。</div>') + "</div>" +
      '<p class="auth-modal-section-title">探针轨迹(已脱敏,不含鉴权头)</p>' +
      '<div class="auth-trace">' + esc(JSON.stringify(rec.probe_trace || [], null, 2)) + "</div>";
  }

  function openRecord(recordId) {
    var rec = findRecord(recordId);
    if (!rec) return;
    modalRecord = rec;
    $("auth-modal-verdict").innerHTML = verdictBadge(rec.verdict);
    $("auth-modal-title").textContent = (rec.channel_name || rec.base_url) + " · " + rec.model;
    $("auth-modal-sub").textContent = "声称来源 " + (rec.source_label || rec.source) +
      " · 线协议 " + (rec.wire_family || "-") + " · " + fmtTime(rec.time);
    $("auth-modal-body").innerHTML = modalBody(rec);
    var modal = $("auth-result-modal");
    modal.hidden = false;
    document.body.style.overflow = "hidden";
    var closeBtn = modal.querySelector(".auth-modal-close");
    if (closeBtn) closeBtn.focus();
  }

  function closeModal() {
    var modal = $("auth-result-modal");
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    modalRecord = null;
    document.body.style.overflow = "";
  }

  // ---- exports ------------------------------------------------------------

  var FENCE = String.fromCharCode(96).repeat(3);

  function safeName(value) {
    var text = String(value == null ? "" : value).replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "");
    return text.slice(0, 64) || "export";
  }

  function recordBase(rec) {
    return safeName((rec.model || "model") + "-" + (rec.channel_id || rec.channel_name || "channel") + "-" + (rec.record_id || ""));
  }

  function downloadText(filename, text, mime) {
    try {
      var blob = new Blob([text], { type: mime || "text/plain;charset=utf-8" });
      var url = URL.createObjectURL(blob);
      var link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
    } catch (err) {
      window.alert("导出失败:" + err);
    }
  }

  function signalLines(rec) {
    return (rec.signals || []).map(function (sig) {
      return "- 「" + signalLabel(sig.signal) + "」" + (sig.status || "") + " — " + (sig.detail || "");
    }).join("\n");
  }

  function recordMarkdown(rec) {
    var meta = [
      "- 渠道地址:" + (rec.base_url || "-"),
      "- 模型:" + (rec.model || "-"),
      "- 声称来源:" + (rec.source_label || rec.source || "-"),
      "- 线协议:" + (rec.wire_family || "-"),
      "- 判定:" + (rec.verdict_label || rec.verdict || "-"),
      "- 真实性评分:" + (rec.score == null ? "-" : rec.score + " / 100"),
      "- 置信度:" + (rec.confidence == null ? "-" : rec.confidence),
      "- 能力准确率:" + (rec.capability_accuracy == null ? "-" : rate(rec.capability_accuracy)),
      "- 平均延迟:" + (rec.latency_ms == null ? "-" : fmtMs(rec.latency_ms)),
      "- 检测时间:" + (rec.time || "-"),
      "- 记录 ID:" + (rec.record_id || "-")
    ];
    if (rec.error_msg) meta.push("- 传输错误:" + rec.error_msg);
    return [
      "# 真实性检测报告 · " + (rec.channel_name || rec.base_url || ""),
      "",
      meta.join("\n"),
      "",
      "## 判定摘要",
      "",
      rec.summary || "(无)",
      "",
      "## 信号明细",
      "",
      signalLines(rec) || "(无)",
      "",
      "## 探针轨迹(已脱敏,不含鉴权头)",
      "",
      FENCE + "json",
      JSON.stringify(rec.probe_trace || [], null, 2),
      FENCE,
      ""
    ].join("\n");
  }

  function reportTable() {
    var report = lastReport || {};
    var head = "| 协议族 | 总请求 | 已完成 | 真实 | 疑似异常 | 判定伪造 | 传输错误 | 真实率 | 疑似率 | 能力准确率 | 平均延迟 |";
    var sep = "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |";
    var rows = (report.by_family || []).map(function (b) {
      return "| " + [b.family_label || b.family, b.total, b.completed, b.authentic, b.suspect, b.fail, b.error,
        rate(b.authentic_rate), rate(b.suspect_rate), rate(b.capability_accuracy_avg),
        b.latency_avg_ms == null ? "-" : fmtMs(b.latency_avg_ms)].join(" | ") + " |";
    });
    var t = report.totals || {};
    if (t.total != null) {
      rows.push("| 合计 | " + [t.total, t.completed, t.authentic, t.suspect, t.fail, t.error,
        rate(t.authentic_rate), rate(t.suspect_rate), "-", "-"].join(" | ") + " |");
    }
    return [head, sep].concat(rows).join("\n");
  }

  function reportMarkdown() {
    var select = $("auth-hours");
    var rangeText = select ? select.options[select.selectedIndex].text : (RANGE_LABELS[hours()] || hours() + " 小时");
    var lines = [
      "# Yhs-ApiDetect · 渠道真实性检测报告",
      "",
      "- 时间范围:" + rangeText,
      "- 记录数:" + lastRecords.length,
      "- 导出时间:" + new Date().toLocaleString(),
      "",
      "## 协议族统计",
      "",
      reportTable(),
      "",
      "## 检测记录",
      ""
    ];
    if (!lastRecords.length) lines.push("(无记录)");
    lastRecords.forEach(function (rec, index) {
      lines.push("### " + (index + 1) + ". " + (rec.channel_name || rec.base_url) + " · " + rec.model);
      lines.push("");
      lines.push("- 判定:" + (rec.verdict_label || rec.verdict) + " · 评分 " + (rec.score == null ? "-" : rec.score) +
        " · 置信 " + (rec.confidence == null ? "-" : rec.confidence) +
        " · 延迟 " + (rec.latency_ms == null ? "-" : fmtMs(rec.latency_ms)));
      lines.push("- 声称来源:" + (rec.source_label || rec.source) + " · 线协议 " + (rec.wire_family || "-"));
      lines.push("- 时间:" + (rec.time || "-") + " · 记录 ID " + (rec.record_id || "-"));
      lines.push("");
      lines.push(signalLines(rec) || "(无信号)");
      lines.push("");
      lines.push("## 探针轨迹(已脱敏)");
      lines.push("");
      lines.push(FENCE + "json");
      lines.push(JSON.stringify(rec.probe_trace || [], null, 2));
      lines.push(FENCE);
      lines.push("");
    });
    return lines.join("\n");
  }

  function exportRecord(rec, kind) {
    if (!rec) return;
    if (kind === "json") {
      downloadText(recordBase(rec) + ".json", JSON.stringify(rec, null, 2), "application/json;charset=utf-8");
    } else {
      downloadText(recordBase(rec) + ".md", recordMarkdown(rec), "text/markdown;charset=utf-8");
    }
  }

  function exportReport(kind) {
    var stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    if (kind === "json") {
      downloadText("authenticity-report-" + stamp + ".json",
        JSON.stringify({ exported_at: new Date().toISOString(), hours: hours(), report: lastReport, records: lastRecords }, null, 2),
        "application/json;charset=utf-8");
    } else {
      downloadText("authenticity-report-" + stamp + ".md", reportMarkdown(), "text/markdown;charset=utf-8");
    }
  }

  function loadReport() {
    return api("/api/authenticity/report?hours=" + hours()).then(function (res) {
      if (res.payload && res.payload.report) renderReport(res.payload.report);
    });
  }

  function loadRecords() {
    var requestId = ++recordRequestId;
    return api("/api/authenticity/records?hours=" + hours() + "&limit=" + recordLimit + "&_=" + Date.now()).then(function (res) {
      if (!res.ok || !res.payload || !Array.isArray(res.payload.records)) {
        if (requestId !== recordRequestId) return;
        throw new Error(errorText(res));
      }
      if (requestId !== recordRequestId) return;
      if (res.payload.report) renderReport(res.payload.report);
      renderRecords(res.payload.records);
    });
  }

  // ---- wiring -------------------------------------------------------------

  document.addEventListener('record-deleted', function (event) {
    if (event.detail.url.indexOf('/api/authenticity/records/') !== 0) return;
    recordIndex = {};
    lastRecords = lastRecords.filter(function (rec) { return '/api/authenticity/records/' + encodeURIComponent(rec.record_id) !== event.detail.url; });
    renderRecords(lastRecords);
    loadRecords().catch(function () {});
  });

  function init() {
    if (!$("auth-models")) return;

    $("auth-load-models").addEventListener("click", loadModels);
    $("auth-run-all").addEventListener("click", startRun);

    $("auth-models").addEventListener("click", function (event) {
      var chip = event.target.closest("button[data-model]");
      if (chip) {
        var name = chip.getAttribute("data-model");
        if (selectedModels[name]) delete selectedModels[name];
        else selectedModels[name] = true;
        renderModels();
        return;
      }
      var groupBtn = event.target.closest("button[data-group-toggle]");
      if (groupBtn) {
        var family = groupBtn.getAttribute("data-group-toggle");
        var names = classifiedGroups()[family] || [];
        var allOn = names.length > 0 && names.every(function (n) { return selectedModels[n]; });
        names.forEach(function (n) { if (allOn) delete selectedModels[n]; else selectedModels[n] = true; });
        renderModels();
        return;
      }
      if (event.target.closest("#auth-models-all")) {
        var groups = classifiedGroups();
        FAMILY_ORDER.forEach(function (fam) {
          groups[fam].forEach(function (n) { selectedModels[n] = true; });
        });
        renderModels();
        return;
      }
      if (event.target.closest("#auth-models-none")) {
        selectedModels = {};
        renderModels();
        return;
      }
      if (event.target.closest("#auth-skip-toggle")) {
        skippedOpen = !skippedOpen;
        renderModels();
      }
    });

    $("auth-models").addEventListener("change", function (event) {
      var select = event.target.closest("select[data-source-family]");
      if (!select) return;
      sourceByFamily[select.getAttribute("data-source-family")] = select.value;
    });

    $("auth-hours").addEventListener("change", function () {
      $("auth-export").setAttribute("href", "/api/authenticity/report/export?hours=" + hours());
      recordLimit = RECORD_PAGE;
      loadReport();
      loadRecords();
    });
    $("auth-export").setAttribute("href", "/api/authenticity/report/export?hours=" + hours());
    $("auth-export-json").addEventListener("click", function () { exportReport("json"); });
    $("auth-export-md").addEventListener("click", function () { exportReport("md"); });
    $("auth-modal-json").addEventListener("click", function () { exportRecord(modalRecord, "json"); });
    $("auth-modal-md").addEventListener("click", function () { exportRecord(modalRecord, "md"); });

    Array.prototype.forEach.call(document.querySelectorAll("[data-modal-close]"), function (node) {
      node.addEventListener("click", closeModal);
    });

    document.addEventListener("click", function (event) {
      if (event.target.closest && event.target.closest("#auth-records-more")) {
        recordLimit += RECORD_PAGE * 2;
        loadRecords();
        return;
      }
      var btn = event.target.closest ? event.target.closest("[data-record-act]") : null;
      if (!btn) return;
      var recordId = btn.getAttribute("data-record-id");
      var act = btn.getAttribute("data-record-act");
      if (act === "detail") openRecord(recordId);
      else exportRecord(findRecord(recordId), act);
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeModal();
    });

    renderBaseUrlOptions();
    renderModels();
    refreshConfigs().then(function () {
      restoreLast();
      renderModels();
      loadReport();
      loadRecords();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
