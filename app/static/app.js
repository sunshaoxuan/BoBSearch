const PAGE_SIZE = 50;
const state = {
  query: "",
  response: null,
  results: [],
  page: 1,
  relevanceMode: "smart",
  inResultsQuery: "",
  searchController: null,
  searchRequestId: 0,
  isSearching: false,
  activeTab: "search",
  addingTokens: new Set(),
  torrents: [],
  torrentsLoaded: false,
  torrentFiles: {},
  targetSuggestions: {},
  selectedFiles: {},
  torrentActions: new Set(),
  movingHash: null,
};

const $ = (id) => document.getElementById(id);

const fmtSize = (n) => {
  if (!n || n < 0) return "未知大小";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = n;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
};

async function postJson(url, data, options = {}) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
    signal: options.signal,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || body.message || `HTTP ${res.status}`);
  return body;
}

async function getJson(url) {
  const res = await fetch(url);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || body.message || `HTTP ${res.status}`);
  return body;
}

async function health() {
  $("status").textContent = "检查中...";
  const res = await fetch("/api/health");
  const data = await res.json();
  renderHealth(data);
  $("status").textContent = "服务状态已更新。";
}

function renderHealth(data) {
  $("healthPanel").innerHTML = `
    ${healthItem("Jackett", data.jackett.ok, data.jackett.ok ? `${data.jackett.indexers} 个源` : data.jackett.error)}
    ${healthItem("qB", data.qbit.ok, data.qbit.ok ? data.qbit.version : data.qbit.error)}
    ${healthItem("LLM", data.llm.ok, data.llm.ok ? data.llm.model : data.llm.error)}
  `;
}

function healthItem(label, ok, text) {
  return `<div class="health-item ${ok ? "ok" : "bad"}"><span>${escapeHtml(label)}</span><strong>${ok ? "OK" : "FAIL"}</strong><small>${escapeHtml(text || "")}</small></div>`;
}

async function search() {
  if (state.movingHash) return;
  const query = $("query").value.trim();
  if (query.length < 2) {
    $("status").textContent = "请输入至少两个字符。";
    return;
  }
  if (state.searchController) {
    state.searchController.abort();
  }
  const requestId = state.searchRequestId + 1;
  state.searchRequestId = requestId;
  state.searchController = new AbortController();
  state.query = query;
  state.page = 1;
  state.inResultsQuery = "";
  $("inResultsQuery").value = "";
  setTab("search");
  $("lowResults").innerHTML = "";
  $("errors").innerHTML = "";
  $("pagination").innerHTML = "";
  setSearching(true);
  try {
    const data = await postJson("/api/search", {
      query,
      category: $("category").value,
      sort: $("sort").value,
    }, { signal: state.searchController.signal });
    if (requestId !== state.searchRequestId) return;
    state.response = data;
    state.results = data.results || [];
    render();
    $("status").textContent = "完成。";
    setSearching(false);
  } catch (err) {
    if (err.name === "AbortError") return;
    if (requestId !== state.searchRequestId) return;
    $("status").textContent = `搜索失败：${err.message}`;
    setSearching(false);
  } finally {
    if (requestId === state.searchRequestId) {
      state.searchController = null;
    }
  }
}

function setSearching(isSearching) {
  state.isSearching = isSearching;
  $("status").classList.toggle("searching-status", isSearching);
  $("searchBtn").disabled = isSearching;
  $("searchBtn").textContent = isSearching ? "搜索中" : "搜索";
  if (isSearching) {
    $("status").innerHTML = statusLoading();
    $("results").innerHTML = loadingPanel();
    $("pagination").innerHTML = "";
  }
}

function statusLoading() {
  return `
    <span class="status-pulse" aria-hidden="true"><i></i><i></i><i></i></span>
    <span>搜索进行中：正在查询源站、合并重复资源并整理版本信息</span>
  `;
}

function loadingPanel() {
  return `
    <div class="loading-panel glass">
      <div class="loading-orbit" aria-hidden="true">
        <span></span>
      </div>
      <div class="loading-copy">
        <h2>正在搜索资源</h2>
        <p>正在并发查询 Jackett、去重评分，并调用 LLM 整理版本信息。</p>
        <div class="loading-steps">
          <span style="--step:0">查询 Jackett</span>
          <span style="--step:1">去重评分</span>
          <span style="--step:2">LLM 整理</span>
        </div>
        <div class="loading-bar"><span></span></div>
        <small>可以直接输入新关键词并回车，新搜索会接管当前请求。</small>
      </div>
    </div>
  `;
}

function render() {
  if (!state.response) return;
  const filtered = filteredResults();
  const visible = filtered.visible;
  const low = filtered.low;
  const totalPages = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
  state.page = Math.min(Math.max(1, state.page), totalPages);
  const start = (state.page - 1) * PAGE_SIZE;
  const pageItems = visible.slice(start, start + PAGE_SIZE);

  renderSummary(filtered, start, pageItems.length, totalPages);
  renderErrors();
  $("results").innerHTML = pageItems.map(card).join("") || emptyState("没有符合当前过滤条件的结果。");
  renderLowResults(low);
  renderPagination(visible.length, totalPages);
}

function filteredResults() {
  const inQuery = normalize(state.inResultsQuery);
  const mode = state.relevanceMode;
  const sorted = sortResults(state.results.slice());
  const matched = sorted.filter((result) => !inQuery || searchableText(result).includes(inQuery));
  if (mode === "strict") {
    return { visible: matched.filter((r) => r.relevance_level !== "low"), low: [] };
  }
  if (mode === "loose") {
    return { visible: matched, low: [] };
  }
  return {
    visible: matched.filter((r) => r.relevance_level !== "low"),
    low: matched.filter((r) => r.relevance_level === "low"),
  };
}

function sortResults(results) {
  const sort = $("sort").value;
  const relevanceRank = { high: 2, medium: 1, low: 0 };
  const base = (r) => [relevanceRank[r.relevance_level] ?? 0, r.relevance_score || 0];
  if (sort === "size") return results.sort((a, b) => compareTuple(base(b), base(a)) || (b.size || -1) - (a.size || -1));
  if (sort === "date") return results.sort((a, b) => compareTuple(base(b), base(a)) || String(b.publish_date || "").localeCompare(String(a.publish_date || "")));
  if (sort === "sources") return results.sort((a, b) => compareTuple(base(b), base(a)) || (b.sources || []).length - (a.sources || []).length);
  return results.sort((a, b) => compareTuple(base(b), base(a)) || (b.seeders || -1) - (a.seeders || -1));
}

function compareTuple(a, b) {
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    const diff = (a[i] || 0) - (b[i] || 0);
    if (diff) return diff;
  }
  return 0;
}

function renderSummary(filtered, start, pageCount, totalPages) {
  const data = state.response;
  const summary = data.relevance_summary || { high: 0, medium: 0, low: 0 };
  const failed = (data.indexers || []).filter((x) => x.status === "error" || x.status === "timeout").length;
  $("summary").innerHTML = `
    <div class="metric"><span>原始</span><strong>${data.total_raw}</strong></div>
    <div class="metric"><span>去重</span><strong>${data.total_deduped}</strong></div>
    <div class="metric"><span>高/中相关</span><strong>${summary.high + summary.medium}</strong></div>
    <div class="metric"><span>低相关</span><strong>${summary.low}</strong></div>
    <div class="metric"><span>失败源</span><strong>${failed}</strong></div>
  `;
  $("viewMeta").textContent = `${filtered.visible.length ? start + 1 : 0}-${start + pageCount} / ${filtered.visible.length}，第 ${state.page}/${totalPages} 页${data.llm_error ? "；LLM 整理失败，已显示基础结果" : ""}`;
}

function renderErrors() {
  const bad = (state.response.indexers || []).filter((x) => x.status === "error" || x.status === "timeout");
  if (!bad.length) {
    $("errors").innerHTML = "";
    return;
  }
  $("errors").innerHTML = `<details><summary>${bad.length} 个源查询失败或超时</summary>${bad.map((x) => `<div>${escapeHtml(x.id)}: ${escapeHtml(x.error || x.status)}</div>`).join("")}</details>`;
}

function renderLowResults(low) {
  if (state.relevanceMode !== "smart" || !low.length) {
    $("lowResults").innerHTML = "";
    return;
  }
  $("lowResults").innerHTML = `
    <details class="low-panel">
      <summary>${low.length} 条低相关结果，可能是中文 Query 被源站忽略后返回的默认列表</summary>
      <div class="low-grid">${low.slice(0, 80).map(card).join("")}</div>
    </details>
  `;
}

function renderPagination(total, totalPages) {
  if (!state.response) {
    $("pagination").innerHTML = "";
    return;
  }
  $("pagination").innerHTML = `
    <button class="ghost" ${state.page <= 1 ? "disabled" : ""} onclick="setPage(${state.page - 1})">上一页</button>
    <span>${state.page} / ${totalPages} · 每页 ${PAGE_SIZE} · ${total} 条</span>
    <button class="ghost" ${state.page >= totalPages ? "disabled" : ""} onclick="setPage(${state.page + 1})">下一页</button>
  `;
}

function setPage(page) {
  state.page = page;
  render();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function collapsibleText(value, className = "") {
  const text = String(value ?? "");
  return `
    <button type="button" class="collapsible-text ${escapeAttr(className)}" title="${escapeAttr(text)}" aria-expanded="false">
      <span>${escapeHtml(text)}</span>
      <small class="collapse-hint">查看更多</small>
    </button>
  `;
}

function card(result) {
  const tags = [...(result.tags || []), ...(result.quality_flags || []).map((x) => `! ${x}`)];
  const levelLabel = { high: "高相关", medium: "中相关", low: "低相关" }[result.relevance_level] || "低相关";
  return `
    <article class="card ${escapeAttr(result.relevance_level)}">
      <div class="card-layout">
        <div class="card-main">
          <div class="card-head">
            <h2>${collapsibleText(result.normalized_name || result.title, "result-title")}</h2>
            <span class="score">${levelLabel} · ${Math.round((result.relevance_score || 0) * 100)}%</span>
          </div>
          <div class="meta">
            <span>${escapeHtml(String(result.peers ?? "未知"))} peers</span>
            <span>${escapeHtml(result.category || "未分类")}</span>
          </div>
          ${tags.length ? `<div class="tags">${tags.map((tag) => `<span class="pill ${tag.startsWith("!") ? "flag" : ""}">${escapeHtml(tag)}</span>`).join("")}</div>` : ""}
          ${(result.relevance_reasons || []).length ? `<div class="reasons">${result.relevance_reasons.map((x) => `<span>${escapeHtml(x)}</span>`).join("")}</div>` : ""}
          ${result.recommendation ? `<p class="note">${escapeHtml(result.recommendation)}</p>` : ""}
          ${result.group_note ? `<p class="note">${escapeHtml(result.group_note)}</p>` : ""}
          <div class="sources">${escapeHtml(result.title)}${result.details ? ` · <a class="link" target="_blank" href="${escapeAttr(result.details)}">详情页</a>` : ""}${result.info_hash ? ` · ${escapeHtml(result.info_hash)}` : ""}</div>
          <div class="trackers">${escapeHtml((result.trackers || []).join(" / "))}</div>
          ${result.magnet_uri ? `<details class="magnet"><summary>显示磁力链接</summary><code>${escapeHtml(result.magnet_uri)}</code></details>` : ""}
          <div class="actions">
            <button class="qbit-add-btn" onclick="addToQbit('${escapeAttr(result.token)}', this)">添加到下载</button>
          </div>
        </div>
        ${resourceGrid(result)}
      </div>
    </article>
  `;
}

function resourceGrid(result) {
  const resolution = detectResolution(result) || "未知";
  const sourceCount = (result.sources || []).length;
  const trackerText = (result.trackers || []).slice(0, 2).join(" / ") || "未知";
  return `
    <aside class="resource-grid" aria-label="资源关键指标">
      ${resourceMetric("分辨率", resolution)}
      ${resourceMetric("下载大小", fmtSize(result.size))}
      ${resourceMetric("Seeders", String(result.seeders ?? "未知"))}
      ${resourceMetric("来源", `${sourceCount || "未知"} 个`, trackerText)}
    </aside>
  `;
}

function resourceMetric(label, value, sub = "") {
  return `
    <div class="resource-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      ${sub ? `<small>${escapeHtml(sub)}</small>` : ""}
    </div>
  `;
}

function detectResolution(result) {
  const text = [
    result.title,
    result.normalized_name,
    ...(result.tags || []),
    ...(result.sources || []).map((source) => source.title),
  ].join(" ");
  const match = text.match(/\b(4320p|2160p|1080p|720p|480p|4k|8k)\b/i);
  return match ? match[1].toUpperCase().replace("P", "p") : "";
}

function searchableText(result) {
  const sourceText = (result.sources || []).map((source) => [
    source.title,
    source.tracker,
    source.category,
    domain(source.details),
  ].join(" ")).join(" ");
  return normalize([
    result.title,
    result.normalized_name,
    result.category,
    result.info_hash,
    (result.trackers || []).join(" "),
    (result.tags || []).join(" "),
    (result.quality_flags || []).join(" "),
    sourceText,
  ].filter(Boolean).join(" "));
}

async function addToQbit(token, button) {
  if (state.movingHash) return;
  if (!confirm("请确认你有权下载该资源。是否添加到下载？")) return;
  if (state.addingTokens.has(token)) return;
  state.addingTokens.add(token);
  setQbitAdding(button, true);
  setStatusBusy("正在添加到下载，创建下载任务...");
  try {
    const data = await postJson("/api/qbit/add", { query: state.query, token });
    setStatusDone(data.message);
    $("status").textContent = data.message;
  } catch (err) {
    setStatusDone(`添加失败：${err.message}`, true);
  } finally {
    state.addingTokens.delete(token);
    setQbitAdding(button, false);
  }
}

function setQbitAdding(button, isAdding) {
  if (!button) return;
  button.disabled = isAdding;
  button.classList.toggle("qbit-adding", isAdding);
  button.innerHTML = isAdding ? `<span class="button-spinner" aria-hidden="true"></span><span>添加中</span>` : "添加到下载";
}

function setStatusBusy(text) {
  $("status").classList.add("searching-status");
  $("status").innerHTML = `
    <span class="status-pulse" aria-hidden="true"><i></i><i></i><i></i></span>
    <span>${escapeHtml(text)}</span>
  `;
}

function setStatusDone(text, isError = false) {
  $("status").classList.remove("searching-status");
  $("status").classList.toggle("status-error", isError);
  $("status").textContent = text;
  if (!isError) {
    window.setTimeout(() => $("status").classList.remove("status-error"), 1200);
  }
}

async function loadTorrents(force = false) {
  if (state.movingHash && !force) return;
  $("torrentStatus").textContent = "正在读取 qB 下载清单...";
  try {
    const data = await getJson("/api/qbit/torrents");
    state.torrents = data.torrents || [];
    state.torrentsLoaded = true;
    renderTorrents();
    renderDownloadSummary();
    $("torrentStatus").textContent = state.torrents.length ? `已读取 ${state.torrents.length} 个 qB 任务。` : "qB 暂无任务。";
  } catch (err) {
    $("torrentStatus").textContent = `读取失败：${err.message}`;
  }
}

function renderDownloadSummary() {
  const complete = state.torrents.filter((torrent) => torrent.is_complete).length;
  const active = state.torrents.filter((torrent) => !torrent.is_complete).length;
  const totalSize = state.torrents.reduce((sum, torrent) => sum + (torrent.size || 0), 0);
  $("downloadSummary").innerHTML = `
    <div class="metric"><span>任务</span><strong>${state.torrents.length}</strong></div>
    <div class="metric"><span>已完成</span><strong>${complete}</strong></div>
    <div class="metric"><span>下载中</span><strong>${active}</strong></div>
    <div class="metric"><span>总大小</span><strong>${fmtSize(totalSize)}</strong></div>
  `;
  $("downloadMeta").textContent = state.torrentsLoaded ? "显示 qB 全部任务，已完成任务可移动入库。" : "进入下载管理后读取 qB 清单。";
}

function setTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tab);
  });
  $("searchTab").classList.toggle("active", tab === "search");
  $("downloadsTab").classList.toggle("active", tab === "downloads");
  $("searchSideCard").classList.toggle("hidden", tab !== "search");
  $("downloadsSideCard").classList.toggle("hidden", tab !== "downloads");
  if (tab === "downloads") {
    loadTorrents();
  }
}

function renderTorrents() {
  $("torrentList").innerHTML = state.torrents.map(torrentCard).join("") || emptyState("qB 暂无下载任务。");
}

function icon(name) {
  const paths = {
    play: '<polygon points="9 7 17 12 9 17 9 7"></polygon>',
    stop: '<rect x="8" y="8" width="8" height="8" rx="1.5"></rect>',
    trash: '<path d="M9 6h6"></path><path d="M10 6l.5-2h3l.5 2"></path><path d="M7 8h10"></path><path d="M9 8l.5 11h5L15 8"></path>',
    chevronDown: '<path d="m7 10 5 5 5-5"></path>',
    chevronUp: '<path d="m7 14 5-5 5 5"></path>',
  };
  return `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">${paths[name] || ""}</svg>`;
}

function torrentCard(torrent) {
  const complete = Boolean(torrent.is_complete);
  const progress = Math.round((torrent.progress || 0) * 1000) / 10;
  const moving = state.movingHash === torrent.hash;
  return `
    <article class="torrent-card ${moving ? "moving" : ""}" data-hash="${escapeAttr(torrent.hash)}">
      <div class="torrent-head">
        <div class="torrent-main">
          <h3>${collapsibleText(torrent.name, "torrent-title")}</h3>
          <div class="meta">
            <span class="pill ${complete ? "strong" : ""}">${complete ? "已完成" : "下载中"} ${progress}%</span>
            <span>${fmtSize(torrent.completed)} / ${fmtSize(torrent.size)}</span>
            <span>${escapeHtml(torrent.state || "unknown")}</span>
            <span>${escapeHtml(torrent.category || "未分类")}</span>
            <span>↓ ${fmtSize(torrent.download_speed || 0)}/s</span>
            <span>↑ ${fmtSize(torrent.upload_speed || 0)}/s</span>
          </div>
        </div>
        <div class="torrent-actions">
          <div class="torrent-control-row">
            <button class="ghost torrent-action icon-btn" title="开始" aria-label="开始" onclick="controlTorrent('${escapeAttr(torrent.hash)}', 'start', this)">${icon("play")}</button>
            <button class="ghost torrent-action icon-btn" title="停止" aria-label="停止" onclick="controlTorrent('${escapeAttr(torrent.hash)}', 'stop', this)">${icon("stop")}</button>
            <button class="ghost danger torrent-action icon-btn" title="删除任务和文件" aria-label="删除任务和文件" onclick="controlTorrent('${escapeAttr(torrent.hash)}', 'delete-with-files', this)">${icon("trash")}</button>
          </div>
          <button id="toggle-files-${escapeAttr(torrent.hash)}" class="ghost torrent-action icon-btn torrent-expand-btn" title="展开文件" aria-label="展开文件" aria-expanded="false" onclick="toggleTorrentFiles('${escapeAttr(torrent.hash)}')">${icon("chevronDown")}</button>
        </div>
      </div>
      <div class="progress"><span style="width:${Math.min(100, Math.max(0, progress))}%"></span></div>
      <div id="files-${escapeAttr(torrent.hash)}" class="torrent-files"></div>
      ${moving ? torrentMoveOverlay() : ""}
    </article>
  `;
}

function torrentMoveOverlay() {
  return `
    <div class="torrent-move-overlay" aria-live="polite">
      <div class="file-loading-orbit" aria-hidden="true"><span></span></div>
      <div>
        <strong>正在移动并清理</strong>
        <p>正在停止任务、移动勾选内容，并在成功后清理下载任务和剩余文件。</p>
      </div>
    </div>
  `;
}

async function controlTorrent(hash, action, button) {
  if (state.movingHash) return;
  const actionText = {
    start: "开始",
    stop: "停止",
    "delete-with-files": "删除任务和文件",
  }[action] || action;
  const key = `${hash}:${action}`;
  if (state.torrentActions.has(key)) return;
  if (action === "delete-with-files") {
    const torrent = state.torrents.find((item) => item.hash === hash);
    const ok = confirm(`将从 qB 删除任务，并连同已下载文件一起删除：\n${torrent?.name || hash}\n\n这个操作不能撤销。继续吗？`);
    if (!ok) return;
  }
  state.torrentActions.add(key);
  setTorrentActionBusy(button, true, actionText);
  $("torrentStatus").textContent = `正在${actionText} qB 任务...`;
  try {
    const data = await postJson(`/api/qbit/torrents/${encodeURIComponent(hash)}/${action}`, {});
    $("torrentStatus").textContent = data.message || `已${actionText}。`;
    delete state.torrentFiles[hash];
    delete state.targetSuggestions[hash];
    delete state.selectedFiles[hash];
    await loadTorrents();
  } catch (err) {
    $("torrentStatus").textContent = `${actionText}失败：${err.message}`;
  } finally {
    state.torrentActions.delete(key);
    setTorrentActionBusy(button, false, actionText);
  }
}

function setTorrentActionBusy(button, isBusy, actionText) {
  if (!button) return;
  button.disabled = isBusy;
  button.classList.toggle("qbit-adding", isBusy);
  const iconName = actionText === "开始" ? "play" : actionText === "停止" ? "stop" : "trash";
  button.innerHTML = isBusy ? `<span class="button-spinner dark" aria-hidden="true"></span>` : icon(iconName);
}

async function toggleTorrentFiles(hash) {
  if (state.movingHash) return;
  const holder = $(`files-${hash}`);
  const toggle = $(`toggle-files-${hash}`);
  if (!holder) return;
  if (holder.dataset.open === "1") {
    holder.innerHTML = "";
    holder.dataset.open = "0";
    if (toggle) {
      toggle.innerHTML = icon("chevronDown");
      toggle.title = "展开文件";
      toggle.setAttribute("aria-label", "展开文件");
      toggle.setAttribute("aria-expanded", "false");
    }
    return;
  }
  holder.dataset.open = "1";
  if (toggle) {
    toggle.innerHTML = icon("chevronUp");
    toggle.title = "收起文件";
    toggle.setAttribute("aria-label", "收起文件");
    toggle.setAttribute("aria-expanded", "true");
  }
  holder.innerHTML = fileLoadingPanel("正在读取文件列表", "连接 qBittorrent，展开任务内文件和目录结构。", ["读取 qB", "生成文件树", "准备勾选"]);
  try {
    if (!state.torrentFiles[hash]) {
      const data = await getJson(`/api/qbit/torrents/${encodeURIComponent(hash)}/files`);
      state.torrentFiles[hash] = data.files || [];
    }
    await ensureTargetSuggestions(hash, holder);
    state.selectedFiles[hash] = state.selectedFiles[hash] || new Set();
    renderTorrentFiles(hash);
  } catch (err) {
    holder.innerHTML = `<div class="mini-status">读取文件失败：${escapeHtml(err.message)}</div>`;
  }
}

function targetCacheKey(hash) {
  return `bobsearch:targetSuggestions:v2:${hash}`;
}

function readTargetSuggestionsCache(hash) {
  try {
    const raw = window.localStorage.getItem(targetCacheKey(hash));
    const parsed = raw ? JSON.parse(raw) : null;
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function writeTargetSuggestionsCache(hash, targets) {
  try {
    window.localStorage.setItem(targetCacheKey(hash), JSON.stringify(targets || []));
  } catch {
    // Cache failure should not block file management.
  }
}

async function ensureTargetSuggestions(hash, holder, force = false) {
  if (!force && state.targetSuggestions[hash]) return;
  if (!force) {
    const cached = readTargetSuggestionsCache(hash);
    if (cached) {
      state.targetSuggestions[hash] = cached;
      return;
    }
  }
  holder.innerHTML = fileLoadingPanel("正在计算 Jellyfin 目标目录", "匹配已有目录，并查询 TMDb/LLM 生成符合规则的文件夹名。", ["匹配已有目录", "查询 TMDb", "LLM 命名"]);
  const torrent = state.torrents.find((item) => item.hash === hash);
  const data = await getJson(`/api/jellyfin/targets?query=${encodeURIComponent(torrent?.name || "")}`);
  state.targetSuggestions[hash] = data.targets || [];
  writeTargetSuggestionsCache(hash, state.targetSuggestions[hash]);
}

function fileLoadingPanel(title, text, steps) {
  return `
    <div class="file-loading glass">
      <div class="file-loading-orbit" aria-hidden="true"><span></span></div>
      <div class="file-loading-copy">
        <h3>${escapeHtml(title)}</h3>
        <p>${escapeHtml(text)}</p>
        <div class="loading-steps compact">
          ${steps.map((step, index) => `<span style="--step:${index}">${escapeHtml(step)}</span>`).join("")}
        </div>
        <div class="loading-bar"><span></span></div>
      </div>
    </div>
  `;
}

function renderTorrentFiles(hash) {
  const holder = $(`files-${hash}`);
  const torrent = state.torrents.find((item) => item.hash === hash);
  const files = state.torrentFiles[hash] || [];
  const targets = state.targetSuggestions[hash] || [];
  const complete = Boolean(torrent && torrent.is_complete);
  holder.innerHTML = `
    <div class="file-tools">
      <select id="targetChoice-${escapeAttr(hash)}" class="target-choice" onchange="updateTargetReason('${escapeAttr(hash)}')">
        ${targets.map(targetOption).join("")}
      </select>
      <button class="ghost" onclick="selectAllFiles('${escapeAttr(hash)}')">全选</button>
      <button class="ghost" onclick="clearSelectedFiles('${escapeAttr(hash)}')">清空</button>
      <button class="ghost" onclick="clearTargetSuggestions('${escapeAttr(hash)}')">清除目录名</button>
      <button ${complete ? "" : "disabled"} onclick="moveSelected('${escapeAttr(hash)}')">移动勾选并清理</button>
    </div>
    <p id="targetReason-${escapeAttr(hash)}" class="target-reason">${targetReasonText(targets[0])}</p>
    <p class="section-note">${targets.length ? "目标目录已按 Jellyfin 现有目录和命名规则自动生成，优先选择最高匹配项。" : "没有可用目标目录候选。"}</p>
    ${complete ? "" : `<p class="section-note">任务尚未完成，只能查看文件，不能移动。</p>`}
    <div class="file-tree">${files.map((node) => fileNode(hash, node, 0)).join("") || emptyState("没有文件信息。")}</div>
  `;
}

function targetOption(target) {
  const label = `${target.category}/${target.folder}${target.existing ? "" : "（新建）"} · ${Math.round((target.score || 0) * 100)}%`;
  const value = `${target.category}\t${target.folder}`;
  return `<option value="${escapeAttr(value)}">${escapeHtml(label)}</option>`;
}

function targetReasonText(target) {
  if (!target) return "";
  const reason = target.reason ? `：${target.reason}` : "";
  return `匹配说明${reason}`;
}

function updateTargetReason(hash) {
  const choice = $(`targetChoice-${hash}`);
  const reason = $(`targetReason-${hash}`);
  if (!choice || !reason) return;
  const target = (state.targetSuggestions[hash] || [])[choice.selectedIndex];
  reason.textContent = targetReasonText(target);
}

function fileNode(hash, node, depth) {
  const selected = state.selectedFiles[hash]?.has(node.path);
  const children = (node.children || []).map((child) => fileNode(hash, child, depth + 1)).join("");
  return `
    <div class="file-node" style="--depth:${depth}">
      <label>
        <input type="checkbox" ${selected ? "checked" : ""} onchange="toggleSelectedFile('${escapeAttr(hash)}', '${escapeAttr(node.path)}', this.checked)">
        <span class="file-name">${collapsibleText(`${node.type === "directory" ? "目录 " : "文件 "}${node.name}`, "file-text")}</span>
        <span>${fmtSize(node.size)}</span>
        <span>${Math.round((node.progress || 0) * 100)}%</span>
      </label>
    </div>
    ${children}
  `;
}

function toggleSelectedFile(hash, path, checked) {
  state.selectedFiles[hash] = state.selectedFiles[hash] || new Set();
  if (checked) state.selectedFiles[hash].add(path);
  else state.selectedFiles[hash].delete(path);
}

function allFilePaths(nodes) {
  const paths = [];
  for (const node of nodes) {
    paths.push(node.path);
    paths.push(...allFilePaths(node.children || []));
  }
  return paths;
}

function selectAllFiles(hash) {
  state.selectedFiles[hash] = new Set(allFilePaths(state.torrentFiles[hash] || []));
  renderTorrentFiles(hash);
}

function clearSelectedFiles(hash) {
  state.selectedFiles[hash] = new Set();
  renderTorrentFiles(hash);
}

async function clearTargetSuggestions(hash) {
  if (state.movingHash) return;
  const holder = $(`files-${hash}`);
  if (!holder) return;
  window.localStorage.removeItem(targetCacheKey(hash));
  delete state.targetSuggestions[hash];
  try {
    await ensureTargetSuggestions(hash, holder, true);
    renderTorrentFiles(hash);
    $("torrentStatus").textContent = "已清除旧目录名并重新生成。";
  } catch (err) {
    holder.innerHTML = `<div class="mini-status">重新生成目录名失败：${escapeHtml(err.message)}</div>`;
  }
}

async function moveSelected(hash) {
  if (state.movingHash) return;
  const selected = Array.from(state.selectedFiles[hash] || []);
  const targetChoice = $(`targetChoice-${hash}`).value;
  const [targetCategory, targetFolder] = targetChoice.split("\t");
  if (!selected.length) {
    $("torrentStatus").textContent = "请先勾选要移动的文件或文件夹。";
    return;
  }
  if (!targetCategory || !targetFolder) {
    $("torrentStatus").textContent = "没有可用的 Jellyfin 目标目录候选。";
    return;
  }
  const ok = confirm(`将移动 ${selected.length} 个勾选项到 Jellyfin/${targetCategory}/${targetFolder}。全部成功后会删除 qB 任务，并删除未勾选的剩余文件。继续吗？`);
  if (!ok) return;
  setMoveBusy(hash, true);
  try {
    const data = await postJson(`/api/qbit/torrents/${encodeURIComponent(hash)}/move-selected`, {
      selected_paths: selected,
      target_category: targetCategory,
      target_folder: targetFolder,
    });
    $("torrentStatus").textContent = data.message;
    delete state.torrentFiles[hash];
    delete state.selectedFiles[hash];
    await loadTorrents(true);
  } catch (err) {
    $("torrentStatus").textContent = `移动失败，qB 任务和剩余文件已保留：${err.message}`;
  } finally {
    setMoveBusy(hash, false);
  }
}

function setMoveBusy(hash, isBusy) {
  state.movingHash = isBusy ? hash : null;
  document.body.classList.toggle("modal-busy", isBusy);
  const card = document.querySelector(`.torrent-card[data-hash="${CSS.escape(hash)}"]`);
  if (isBusy) {
    $("torrentStatus").textContent = "正在暂停任务、移动文件并清理下载任务...";
    if (card) {
      card.classList.add("moving");
      if (!card.querySelector(".torrent-move-overlay")) {
        card.insertAdjacentHTML("beforeend", torrentMoveOverlay());
      }
    }
    showMoveModal();
  } else {
    if (card) {
      card.classList.remove("moving");
      card.querySelector(".torrent-move-overlay")?.remove();
    }
    hideMoveModal();
  }
}

function showMoveModal() {
  if ($("moveModal")) return;
  document.body.insertAdjacentHTML("beforeend", `
    <div id="moveModal" class="move-modal" role="alertdialog" aria-modal="true" aria-labelledby="moveModalTitle">
      <div class="move-modal-panel glass">
        <div class="loading-orbit" aria-hidden="true"><span></span></div>
        <div class="move-modal-copy">
          <h2 id="moveModalTitle">正在移动并清理</h2>
          <p>请保持页面打开。完成前会锁定操作，避免重复移动、删除或刷新任务。</p>
          <div class="loading-steps compact">
            <span style="--step:0">停止下载任务</span>
            <span style="--step:1">移动文件</span>
            <span style="--step:2">清理剩余文件</span>
          </div>
          <div class="loading-bar"><span></span></div>
        </div>
      </div>
    </div>
  `);
}

function hideMoveModal() {
  $("moveModal")?.remove();
  document.body.classList.remove("modal-busy");
}

function emptyState(text) {
  return `<div class="empty">${escapeHtml(text)}</div>`;
}

function domain(url) {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

function normalize(value) {
  return String(value ?? "").toLocaleLowerCase().replace(/\s+/g, " ").trim();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#96;");
}

$("searchBtn").addEventListener("click", search);
$("healthBtn").addEventListener("click", health);
$("refreshTorrentsBtn").addEventListener("click", loadTorrents);
document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => setTab(button.dataset.tab));
});
$("query").addEventListener("keydown", (event) => {
  if (event.key === "Enter") search();
});
$("inResultsQuery").addEventListener("input", (event) => {
  state.inResultsQuery = event.target.value;
  state.page = 1;
  render();
});
$("relevanceMode").addEventListener("change", (event) => {
  state.relevanceMode = event.target.value;
  state.page = 1;
  render();
});
$("sort").addEventListener("change", () => {
  state.page = 1;
  render();
});
document.addEventListener("click", (event) => {
  const trigger = event.target.closest(".collapsible-text");
  if (!trigger) return;
  event.preventDefault();
  event.stopPropagation();
  const expanded = trigger.classList.toggle("expanded");
  trigger.setAttribute("aria-expanded", expanded ? "true" : "false");
  const hint = trigger.querySelector(".collapse-hint");
  if (hint) hint.textContent = expanded ? "收起" : "查看更多";
});
health();
renderDownloadSummary();
