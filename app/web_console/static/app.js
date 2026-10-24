/* Maple-FunASR Web 控制台前端（无框架，纯 fetch + DOM） */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const state = {
    uploadId: null,
    logCursor: 0,
    running: false,
    lastResultCount: -1,
    devicesLoaded: false,
    polling: false,
  };

  // ─────────── 工具 ───────────

  async function api(url, options = {}) {
    const res = await fetch(url, {
      headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
      ...options,
    });
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = null;
    }
    if (!res.ok) {
      const message = (data && (data.message || data.hint)) || `请求失败（${res.status}）`;
      throw new Error(message);
    }
    return data;
  }

  function setConn(ok, text) {
    const dot = $("conn-dot");
    dot.classList.toggle("online", ok === true);
    dot.classList.toggle("offline", ok === false);
    $("conn-text").textContent = text;
  }

  function fmtTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function fmtElapsed(seconds) {
    const s = Math.max(0, Math.floor(seconds || 0));
    const mm = String(Math.floor(s / 60)).padStart(2, "0");
    const ss = String(s % 60).padStart(2, "0");
    return `${mm}:${ss}`;
  }

  function setBar(el, percent) {
    const p = Number(percent);
    const bar = $(el);
    if (!isFinite(p) || p < 0) {
      bar.style.width = "0%";
      bar.className = "";
      return;
    }
    bar.style.width = `${Math.min(100, Math.max(0, p))}%`;
    bar.className = p >= 90 ? "crit" : p >= 70 ? "high" : "";
  }

  function toast(message, kind = "info") {
    const div = document.createElement("div");
    div.className = `toast ${kind}`;
    div.textContent = message;
    document.body.appendChild(div);
    setTimeout(() => div.remove(), 4200);
  }

  // ─────────── 监控 ───────────

  async function pollMetrics() {
    try {
      const data = await api("/api/metrics");
      for (const key of ["cpu", "ram", "gpu"]) {
        const metric = data[key] || {};
        const percent = metric.percent;
        $(`${key}-pct`).textContent = percent >= 0 ? `${Math.round(percent)}%` : "--";
        setBar(`${key}-bar`, percent);
        $(`${key}-detail`).textContent = metric.detail || "";
        $(`${key}-bar`).parentElement.title = metric.name || "";
      }
      let source = `数据源：${data.source || "未知"}`;
      if (data.notes && data.notes.length) source += `（${data.notes.join("；")}）`;
      $("metrics-source").textContent = source;
    } catch (e) {
      $("metrics-source").textContent = "监控数据不可用";
    }
  }

  // ─────────── 任务状态 ───────────

  const STATE_LABEL = {
    idle: "空闲",
    running: "运行中",
    done: "已完成",
    failed: "已失败",
    cancelled: "已取消",
  };

  async function pollState() {
    const data = await api("/api/state");
    const running = data.state === "running";
    const wasRunning = state.running;
    state.running = running;

    $("job-state").textContent = STATE_LABEL[data.state] || data.state;
    $("job-bar").style.width = `${data.progress || 0}%`;
    $("job-bar").className = data.state === "failed" ? "crit" : "";
    $("job-stage").textContent = `${data.progress || 0}% · ${data.stage || "等待开始"}`;
    $("job-elapsed").textContent = data.started_at
      ? `用时 ${fmtElapsed(data.elapsed)}`
      : "";

    $("start-btn").disabled = running || !state.uploadId;
    $("cancel-btn").disabled = !running;

    if (wasRunning && !running) {
      if (data.state === "done") {
        toast(`转写完成，共 ${data.sentences} 条语句`, "ok");
      } else if (data.state === "failed") {
        toast(data.message || "转写失败", "bad");
      }
      refreshResult();
    }

    if (data.downloads) renderDownloads(data.downloads);
    return data;
  }

  // ─────────── 日志 ───────────

  async function pollLogs() {
    const data = await api(`/api/logs?since=${state.logCursor}`);
    if (!data.logs || data.logs.length === 0) {
      state.logCursor = data.next || state.logCursor;
      return;
    }
    const box = $("log");
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
    for (const entry of data.logs) {
      box.textContent += `[${fmtTime(entry.ts)}] ${entry.text}\n`;
    }
    state.logCursor = data.next;
    $("log-count").textContent = `${data.next} 条`;
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  // ─────────── 结果 ───────────

  async function refreshResult() {
    try {
      const data = await api("/api/sentences");
      renderResult(data.sentences || []);
    } catch (e) {
      /* 忽略 */
    }
  }

  function renderResult(sentences) {
    if (sentences.length === state.lastResultCount) return;
    state.lastResultCount = sentences.length;

    $("result-count").textContent = `${sentences.length} 条`;
    const body = $("result-body");
    body.innerHTML = "";

    if (!sentences.length) {
      body.innerHTML = '<tr><td colspan="3" class="empty">暂无结果</td></tr>';
    } else {
      const frag = document.createDocumentFragment();
      for (const s of sentences) {
        const tr = document.createElement("tr");
        const start = (s.start_ms || 0) / 1000;
        const end = (s.end_ms || 0) / 1000;
        tr.innerHTML = `
          <td class="spk">说话人 ${s.spk ?? "?"}</td>
          <td class="time">${start.toFixed(1)}s – ${end.toFixed(1)}s</td>
          <td></td>`;
        tr.lastElementChild.textContent = s.text || "";
        frag.appendChild(tr);
      }
      body.appendChild(frag);
    }

    for (const link of document.querySelectorAll("#exports a")) {
      link.style.pointerEvents = sentences.length ? "auto" : "none";
      link.style.opacity = sentences.length ? "1" : ".45";
    }
  }

  // ─────────── 设备 / 模型 ───────────

  async function loadDevices() {
    try {
      const data = await api("/api/devices");
      const select = $("device-select");
      select.innerHTML = '<option value="auto">自动（推荐）</option>';
      for (const d of data.devices || []) {
        if (d.value === "cpu") continue;
        const opt = document.createElement("option");
        opt.value = d.value;
        opt.textContent = d.label;
        select.appendChild(opt);
      }
      const cpu = document.createElement("option");
      cpu.value = "cpu";
      cpu.textContent = "CPU（软件解码）";
      select.appendChild(cpu);
      state.devicesLoaded = true;
    } catch (e) {
      /* 保留默认项 */
    }
  }

  let modelCache = {};

  async function loadModels() {
    const hub = $("hub-select").value;
    try {
      const data = await api(`/api/models?hub=${hub}`);
      modelCache = {};
      const list = $("model-list");
      list.innerHTML = "";
      for (const b of data.bundles) {
        modelCache[b.key] = b;
        list.appendChild(renderModel(b));
      }
      const select = $("model-select");
      const current = select.value;
      select.innerHTML = "";
      for (const b of data.bundles) {
        const opt = document.createElement("option");
        opt.value = b.name;
        opt.textContent = b.name;
        select.appendChild(opt);
      }
      if (current) select.value = current;
    } catch (e) {
      toast(`模型列表加载失败：${e.message}`, "bad");
    }
  }

  function renderModel(bundle) {
    const item = document.createElement("div");
    item.className = "model-item";
    item.dataset.key = bundle.key;

    const status = bundle.installed
      ? '<span class="badge ok">已安装</span>'
      : bundle.repos_done > 0
        ? `<span class="badge busy">部分安装 ${bundle.repos_done}/${bundle.repos_total}</span>`
        : '<span class="badge">未安装</span>';

    item.innerHTML = `
      <div class="model-head">
        <span class="model-name">${bundle.name}</span>
        ${status}
      </div>
      <div class="model-desc">${bundle.description || ""}</div>
      <div class="model-foot">
        <span class="model-meta">${bundle.language || ""} · ${bundle.approx_size}${
          bundle.installed ? ` · 磁盘 ${bundle.disk_size}` : ""}</span>
        <button class="btn small" data-action="download">下载</button>
      </div>
      <div class="bar" data-role="bar" style="display:none"><i></i></div>
      <div class="model-meta" data-role="status" style="margin-top:6px"></div>`;

    const btn = item.querySelector('[data-action="download"]');
    btn.disabled = bundle.installed;
    if (bundle.installed) btn.textContent = "已下载";
    btn.addEventListener("click", () => downloadModel(bundle.key, btn));
    return item;
  }

  function renderDownloads(downloads) {
    for (const [key, dl] of Object.entries(downloads)) {
      const item = document.querySelector(`.model-item[data-key="${key}"]`);
      if (!item) continue;
      const bar = item.querySelector('[data-role="bar"]');
      const inner = bar.querySelector("i");
      const status = item.querySelector('[data-role="status"]');
      const btn = item.querySelector('[data-action="download"]');
      const active = dl.state === "running" || dl.state === "queued";

      bar.style.display = active ? "block" : "none";
      btn.disabled = active;
      if (active) {
        const percent = dl.percent >= 0 ? dl.percent : 0;
        inner.style.width = `${percent}%`;
        status.textContent = `${percent}%　${dl.done || ""} / ${dl.total || ""}${
          dl.speed ? `　${dl.speed}/s` : ""}${dl.file ? `　${dl.file}` : ""}`;
      } else if (dl.state === "done") {
        status.textContent = "下载完成";
        btn.disabled = true;
        btn.textContent = "已下载";
      } else if (dl.state === "failed") {
        status.textContent = dl.message || "下载失败";
        btn.disabled = false;
        btn.textContent = "重试";
      } else if (dl.state === "cancelled") {
        status.textContent = "已取消";
        btn.disabled = false;
        btn.textContent = "继续";
      }
    }
  }

  async function downloadModel(key, btn) {
    btn.disabled = true;
    btn.textContent = "准备中…";
    try {
      const hub = $("hub-select").value;
      await api("/api/models/download", {
        method: "POST",
        body: JSON.stringify({ key, hub }),
      });
      toast("已开始下载模型", "ok");
    } catch (e) {
      toast(e.message, "bad");
      btn.disabled = false;
      btn.textContent = "重试";
    }
  }

  // ─────────── 上传 / 任务控制 ───────────

  function bindUpload() {
    const zone = $("dropzone");
    const input = $("file-input");

    input.addEventListener("change", () => {
      if (input.files && input.files[0]) uploadFile(input.files[0]);
    });

    ["dragenter", "dragover"].forEach((evt) =>
      zone.addEventListener(evt, (e) => {
        e.preventDefault();
        zone.classList.add("drag");
      }));

    ["dragleave", "drop"].forEach((evt) =>
      zone.addEventListener(evt, (e) => {
        e.preventDefault();
        zone.classList.remove("drag");
      }));

    zone.addEventListener("drop", (e) => {
      const file = e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) uploadFile(file);
    });
  }

  function uploadFile(file) {
    const title = $("dz-title");
    title.textContent = `上传中… ${file.name}`;

    const form = new FormData();
    form.append("file", file);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) {
        const pct = Math.round((e.loaded / e.total) * 100);
        title.textContent = `上传中 ${pct}% · ${file.name}`;
      }
    });
    xhr.addEventListener("load", () => {
      let data = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch (e) {
        data = null;
      }
      if (xhr.status === 200 && data && data.ok) {
        state.uploadId = data.id;
        title.textContent = `${data.name}（${(data.size / 1024 / 1024).toFixed(1)} MB）`;
        $("start-btn").disabled = state.running;
        toast("文件已上传", "ok");
      } else {
        title.textContent = "点击选择或拖拽音视频文件";
        toast((data && data.message) || "上传失败", "bad");
      }
    });
    xhr.addEventListener("error", () => {
      title.textContent = "点击选择或拖拽音视频文件";
      toast("上传失败：网络错误", "bad");
    });
    xhr.send(form);
  }

  async function startJob() {
    if (!state.uploadId) return;
    const payload = {
      upload_id: state.uploadId,
      model: $("model-select").value,
      device: $("device-select").value,
      max_speakers: Number($("max-spk").value || 0),
      vad_seg: Number($("vad-seg").value || 30),
      batch_size: Number($("batch-size").value || 1),
    };
    $("start-btn").disabled = true;
    try {
      await api("/api/start", { method: "POST", body: JSON.stringify(payload) });
      $("log").textContent = "";
      state.logCursor = 0;
      state.lastResultCount = -1;
      renderResult([]);
      toast("任务已开始", "ok");
    } catch (e) {
      toast(e.message, "bad");
      $("start-btn").disabled = false;
    }
  }

  async function cancelJob() {
    try {
      await api("/api/cancel", { method: "POST", body: "{}" });
      toast("已请求取消", "info");
    } catch (e) {
      toast(e.message, "bad");
    }
  }

  // ─────────── 轮询调度 ───────────

  async function tick() {
    if (state.polling) return;
    state.polling = true;
    try {
      await pollState();
      await Promise.all([pollLogs(), pollMetrics()]);
      setConn(true, "已连接");
    } catch (e) {
      setConn(false, "连接中断");
    } finally {
      state.polling = false;
      setTimeout(tick, 1200);
    }
  }

  // ─────────── 初始化 ───────────

  document.addEventListener("DOMContentLoaded", () => {
    bindUpload();
    $("start-btn").addEventListener("click", startJob);
    $("cancel-btn").addEventListener("click", cancelJob);
    $("hub-select").addEventListener("change", loadModels);

    loadDevices();
    loadModels();
    refreshResult();
    tick();
  });
})();
