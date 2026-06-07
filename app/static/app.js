const PAGE_SIZE = 50;
const DOWNLOAD_REFRESH_MS = 15000;
const FILE_COMPLETE_THRESHOLD = 1.0;
const ACTIVE_TAB_STORAGE_KEY = "bobsearch:activeTab:v1";
const state = {
  query: "",
  response: null,
  results: [],
  currentHistoryId: null,
  currentHistoryItem: null,
  searchHistory: [],
  historyLoaded: false,
  latestRestoreAttempted: false,
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
  openTorrentFiles: new Set(),
  movingHash: null,
  manualAdding: false,
  torrentsRefreshing: false,
  torrentRefreshTimer: null,
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

function fmtProgress(progress) {
  const value = Number(progress || 0);
  if (value >= 1) return "100%";
  return `${Math.floor(Math.max(0, value) * 1000) / 10}%`;
}

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

async function deleteJson(url) {
  const res = await fetch(url, { method: "DELETE" });
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
    ${healthItem("搜索工具", data.jackett.ok, data.jackett.ok ? `${data.jackett.indexers} 个源` : userFacingServiceText(data.jackett.error))}
    ${healthItem("下载工具", data.qbit.ok, data.qbit.ok ? data.qbit.version : userFacingServiceText(data.qbit.error))}
    ${healthItem("主模型", data.llm.ok, data.llm.ok ? data.llm.model : userFacingServiceText(data.llm.error))}
    ${healthItem("备用模型", data.llm_fallback?.configured ? data.llm_fallback.ok : true, data.llm_fallback?.configured ? (data.llm_fallback.ok ? data.llm_fallback.model : userFacingServiceText(data.llm_fallback.error)) : "未配置")}
  `;
}

function userFacingServiceText(value) {
  return String(value || "")
    .replaceAll("Jackett", "搜索工具")
    .replaceAll("qBittorrent", "下载工具")
    .replaceAll("qB", "下载工具")
    .replaceAll("LLM", "大模型");
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
  setTab("search", { restoreHistory: false });
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
    state.currentHistoryId = data.history_id || null;
    state.currentHistoryItem = {
      id: data.history_id || "",
      query,
      category: $("category").value,
      sort: $("sort").value,
      total_deduped: data.total_deduped || 0,
      result_count: (data.results || []).length,
    };
    render();
    await loadSearchHistory();
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
        <p>正在并发查询搜索工具、去重评分，并调用大模型整理版本信息。</p>
        <div class="loading-steps">
          <span style="--step:0">查询搜索工具</span>
          <span style="--step:1">去重评分</span>
          <span style="--step:2">大模型整理</span>
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
  $("viewMeta").textContent = `${filtered.visible.length ? start + 1 : 0}-${start + pageCount} / ${filtered.visible.length}，第 ${state.page}/${totalPages} 页${data.llm_error ? "；大模型整理失败，已显示基础结果" : ""}`;
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
    const data = await postJson("/api/qbit/add", { query: state.query, token, history_id: state.currentHistoryId });
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
  if (state.torrentsRefreshing && !force) return;
  state.torrentsRefreshing = true;
  $("torrentStatus").textContent = "正在读取下载工具清单...";
  try {
    const data = await getJson("/api/qbit/torrents");
    state.torrents = data.torrents || [];
    state.torrentsLoaded = true;
    renderTorrents();
    renderDownloadSummary();
    $("torrentStatus").textContent = state.torrents.length ? `已读取 ${state.torrents.length} 个下载任务。` : "下载工具暂无任务。";
  } catch (err) {
    $("torrentStatus").textContent = `读取失败：${err.message}`;
  } finally {
    state.torrentsRefreshing = false;
  }
}

function setManualAddBusy(isBusy, text = "") {
  state.manualAdding = isBusy;
  const elements = [$("addMagnetBtn"), $("uploadTorrentBtn"), $("torrentFileInput"), $("manualMagnet")];
  for (const el of elements) {
    if (el) el.disabled = isBusy;
  }
  if (text) $("manualAddStatus").textContent = text;
}

async function addManualMagnet() {
  if (state.manualAdding || state.movingHash) return;
  const magnet = $("manualMagnet").value.trim();
  if (!magnet) {
    $("manualAddStatus").textContent = "请先粘贴 magnet 链接。";
    return;
  }
  setManualAddBusy(true, "正在添加磁力任务...");
  try {
    const data = await postJson("/api/qbit/add-magnet", { magnet_uri: magnet });
    $("manualAddStatus").textContent = data.message || "已添加磁力任务。";
    $("manualMagnet").value = "";
    await loadTorrents(true);
  } catch (err) {
    $("manualAddStatus").textContent = `添加失败：${err.message}`;
  } finally {
    setManualAddBusy(false);
  }
}

async function uploadTorrentFile() {
  if (state.manualAdding || state.movingHash) return;
  const input = $("torrentFileInput");
  const file = input?.files?.[0];
  if (!file) {
    $("manualAddStatus").textContent = "请先选择 .torrent 文件。";
    return;
  }
  const form = new FormData();
  form.append("file", file);
  setManualAddBusy(true, `正在上传种子：${file.name}`);
  try {
    const res = await fetch("/api/qbit/add-torrent", { method: "POST", body: form });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || body.message || `HTTP ${res.status}`);
    $("manualAddStatus").textContent = body.message || "已添加种子任务。";
    input.value = "";
    await loadTorrents(true);
  } catch (err) {
    $("manualAddStatus").textContent = `上传失败：${err.message}`;
  } finally {
    setManualAddBusy(false);
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
  $("downloadMeta").textContent = state.torrentsLoaded ? "显示下载工具全部任务，已完成任务可移动入库。" : "进入下载管理后读取下载工具清单。";
}

function setTab(tab, options = {}) {
  state.activeTab = tab;
  writeActiveTab(tab);
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tab);
  });
  $("searchTab").classList.toggle("active", tab === "search");
  $("downloadsTab").classList.toggle("active", tab === "downloads");
  $("searchSideCard").classList.toggle("hidden", tab !== "search");
  $("downloadsSideCard").classList.toggle("hidden", tab !== "downloads");
  if (tab === "downloads") {
    loadTorrents();
    startTorrentAutoRefresh();
  } else {
    stopTorrentAutoRefresh();
    if (options.restoreHistory !== false) {
      restoreLatestSearchIfNeeded();
    }
  }
}

function writeActiveTab(tab) {
  try {
    window.localStorage.setItem(ACTIVE_TAB_STORAGE_KEY, tab);
  } catch {
    // UI preference persistence is best effort.
  }
}

function readActiveTab() {
  try {
    return window.localStorage.getItem(ACTIVE_TAB_STORAGE_KEY) === "downloads" ? "downloads" : "search";
  } catch {
    return "search";
  }
}

async function loadSearchHistory() {
  try {
    const data = await getJson("/api/search/history");
    state.searchHistory = data.items || [];
    state.historyLoaded = true;
    renderSearchHistorySelect();
  } catch (err) {
    state.historyLoaded = false;
    renderSearchHistorySelect(`历史读取失败：${err.message}`);
  }
}

function renderSearchHistorySelect(errorText = "") {
  const select = $("historySelect");
  if (!select) return;
  if (errorText) {
    select.innerHTML = `<option value="">${escapeHtml(errorText)}</option>`;
    updateHistoryActions();
    return;
  }
  if (!state.searchHistory.length) {
    select.innerHTML = `<option value="">历史搜索：暂无记录</option>`;
    updateHistoryActions();
    return;
  }
  select.innerHTML = [
    `<option value="">历史搜索：选择一条已保存结果</option>`,
    ...state.searchHistory.map((item) => `<option value="${escapeAttr(item.id)}" ${item.id === state.currentHistoryId ? "selected" : ""}>${escapeHtml(historyLabel(item))}</option>`),
  ].join("");
  updateHistoryActions();
}

function historyLabel(item) {
  const count = item.result_count ?? item.total_deduped ?? 0;
  const time = item.updated_at ? formatHistoryTime(item.updated_at) : "";
  return `${item.query} · ${categoryLabel(item.category)} · ${sortLabel(item.sort)} · ${count} 条${time ? ` · ${time}` : ""}`;
}

function categoryLabel(category) {
  return { all: "全部", movies: "电影", tv: "剧集", anime: "动画" }[category] || category || "全部";
}

function sortLabel(sort) {
  return { seeders: "种子数", sources: "来源数", date: "发布时间", size: "大小" }[sort] || sort || "种子数";
}

function formatHistoryTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function restoreLatestSearchIfNeeded() {
  if (state.response || state.latestRestoreAttempted || state.isSearching || state.activeTab !== "search") return;
  state.latestRestoreAttempted = true;
  await loadLatestSearchHistory();
}

async function loadLatestSearchHistory() {
  try {
    const data = await getJson("/api/search/history/latest");
    if (data.response) {
      applyHistoryResponse(data.item, data.response);
      $("status").textContent = "已恢复上一次搜索结果。";
    }
  } catch (err) {
    $("status").textContent = `恢复历史搜索失败：${err.message}`;
  }
}

async function loadHistoryItem(historyId) {
  if (!historyId || state.movingHash || state.isSearching) return;
  try {
    $("status").textContent = "正在载入历史搜索结果...";
    const data = await getJson(`/api/search/history/${encodeURIComponent(historyId)}`);
    applyHistoryResponse(data.item, data.response);
    $("status").textContent = "已载入历史搜索结果。";
  } catch (err) {
    $("status").textContent = `载入历史搜索失败：${err.message}`;
  }
}

function applyHistoryResponse(item, response) {
  if (!response) return;
  state.response = response;
  state.results = response.results || [];
  state.query = response.query || "";
  state.currentHistoryId = response.history_id || item?.id || null;
  state.currentHistoryItem = item || null;
  state.page = 1;
  state.inResultsQuery = "";
  $("query").value = state.query;
  $("inResultsQuery").value = "";
  if (item?.category && $("category").querySelector(`option[value="${CSS.escape(item.category)}"]`)) {
    $("category").value = item.category;
  }
  if (item?.sort && $("sort").querySelector(`option[value="${CSS.escape(item.sort)}"]`)) {
    $("sort").value = item.sort;
  }
  renderSearchHistorySelect();
  render();
}

function updateHistoryActions() {
  const deleteBtn = $("historyDeleteBtn");
  const clearBtn = $("historyClearBtn");
  if (deleteBtn) deleteBtn.disabled = !state.currentHistoryId || state.isSearching || state.movingHash;
  if (clearBtn) clearBtn.disabled = !state.searchHistory.length || state.isSearching || state.movingHash;
}

function clearSearchView(message) {
  state.response = null;
  state.results = [];
  state.currentHistoryId = null;
  state.currentHistoryItem = null;
  state.page = 1;
  state.inResultsQuery = "";
  state.latestRestoreAttempted = true;
  $("inResultsQuery").value = "";
  $("status").textContent = message;
  renderSearchHistorySelect();
  render();
}

async function deleteCurrentHistory() {
  if (!state.currentHistoryId || state.isSearching || state.movingHash) return;
  const item = state.currentHistoryItem || state.searchHistory.find((entry) => entry.id === state.currentHistoryId);
  if (!confirm(`删除这条历史搜索？\n${item?.query || state.query || ""}`)) return;
  try {
    await deleteJson(`/api/search/history/${encodeURIComponent(state.currentHistoryId)}`);
    await loadSearchHistory();
    clearSearchView("已删除当前历史搜索。");
  } catch (err) {
    $("status").textContent = `删除历史搜索失败：${err.message}`;
  }
}

async function clearSearchHistory() {
  if (!state.searchHistory.length || state.isSearching || state.movingHash) return;
  if (!confirm(`清空全部 ${state.searchHistory.length} 条历史搜索？`)) return;
  try {
    const data = await deleteJson("/api/search/history");
    await loadSearchHistory();
    clearSearchView(data.message || "已清空历史搜索。");
  } catch (err) {
    $("status").textContent = `清空历史搜索失败：${err.message}`;
  }
}

function startTorrentAutoRefresh() {
  if (state.torrentRefreshTimer) return;
  state.torrentRefreshTimer = window.setInterval(() => {
    if (state.activeTab === "downloads" && !state.movingHash) {
      loadTorrents();
    }
  }, DOWNLOAD_REFRESH_MS);
}

function stopTorrentAutoRefresh() {
  if (!state.torrentRefreshTimer) return;
  window.clearInterval(state.torrentRefreshTimer);
  state.torrentRefreshTimer = null;
}

function renderTorrents() {
  $("torrentList").innerHTML = state.torrents.map(torrentCard).join("") || emptyState("下载工具暂无下载任务。");
  restoreOpenTorrentFiles();
}

function restoreOpenTorrentFiles() {
  const activeHashes = new Set(state.torrents.map((torrent) => torrent.hash));
  for (const hash of Array.from(state.openTorrentFiles)) {
    if (!activeHashes.has(hash)) {
      state.openTorrentFiles.delete(hash);
      delete state.torrentFiles[hash];
      delete state.targetSuggestions[hash];
      delete state.selectedFiles[hash];
      continue;
    }
    const holder = $(`files-${hash}`);
    const toggle = $(`toggle-files-${hash}`);
    if (!holder) continue;
    holder.dataset.open = "1";
    if (toggle) {
      toggle.innerHTML = icon("chevronUp");
      toggle.title = "收起文件";
      toggle.setAttribute("aria-label", "收起文件");
      toggle.setAttribute("aria-expanded", "true");
    }
    if (state.torrentFiles[hash] && state.targetSuggestions[hash]) {
      renderTorrentFiles(hash);
      refreshTargetState(hash);
    } else {
      holder.innerHTML = `<div class="mini-status">文件面板已展开，正在等待下次读取。</div>`;
    }
  }
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

function torrentStateFlags(torrent) {
  const raw = String(torrent?.state || "").toLowerCase();
  const runningPrefixes = ["downloading", "forceddl", "forcedmeta", "forcedup", "checking", "checkingresume", "moving", "queued", "allocating", "metadl", "stalled", "uploading"];
  const stoppedPrefixes = ["paused", "stopped", "error", "missingfiles"];
  const canStart = stoppedPrefixes.some((prefix) => raw.startsWith(prefix));
  const canStop = runningPrefixes.some((prefix) => raw.startsWith(prefix));
  return { raw, canStart, canStop };
}

function torrentActionMeta(torrent) {
  const flags = torrentStateFlags(torrent);
  return {
    start: {
      disabled: !flags.canStart,
      title: flags.canStart ? "开始" : "任务已在运行或不可开始",
    },
    stop: {
      disabled: !flags.canStop,
      title: flags.canStop ? "停止" : "任务已停止或当前不可停止",
    },
  };
}

function torrentCard(torrent) {
  const complete = Boolean(torrent.is_complete);
  const progressValue = Number(torrent.progress || 0);
  const progressPercent = Math.min(100, Math.max(0, progressValue * 100));
  const progressText = fmtProgress(progressValue);
  const moving = state.movingHash === torrent.hash;
  const actionMeta = torrentActionMeta(torrent);
  return `
    <article class="torrent-card ${moving ? "moving" : ""}" data-hash="${escapeAttr(torrent.hash)}">
      <div class="torrent-head">
        <div class="torrent-main">
          <h3>${collapsibleText(torrent.name, "torrent-title")}</h3>
          <div class="meta">
            <span class="pill ${complete ? "strong" : ""}">${complete ? "已完成" : "下载中"} ${progressText}</span>
            <span>${fmtSize(torrent.completed)} / ${fmtSize(torrent.size)}</span>
            <span>${escapeHtml(torrent.state || "unknown")}</span>
            <span>${escapeHtml(torrent.category || "未分类")}</span>
            <span>↓ ${fmtSize(torrent.download_speed || 0)}/s</span>
            <span>↑ ${fmtSize(torrent.upload_speed || 0)}/s</span>
          </div>
        </div>
        <div class="torrent-actions">
          <div class="torrent-control-row">
            <button class="ghost torrent-action icon-btn" title="${escapeAttr(actionMeta.start.title)}" aria-label="${escapeAttr(actionMeta.start.title)}" ${actionMeta.start.disabled ? "disabled" : ""} onclick="controlTorrent('${escapeAttr(torrent.hash)}', 'start', this)">${icon("play")}</button>
            <button class="ghost torrent-action icon-btn" title="${escapeAttr(actionMeta.stop.title)}" aria-label="${escapeAttr(actionMeta.stop.title)}" ${actionMeta.stop.disabled ? "disabled" : ""} onclick="controlTorrent('${escapeAttr(torrent.hash)}', 'stop', this)">${icon("stop")}</button>
            <button class="ghost danger torrent-action icon-btn" title="删除任务和文件" aria-label="删除任务和文件" onclick="controlTorrent('${escapeAttr(torrent.hash)}', 'delete-with-files', this)">${icon("trash")}</button>
          </div>
          <button id="toggle-files-${escapeAttr(torrent.hash)}" class="ghost torrent-action icon-btn torrent-expand-btn" title="展开文件" aria-label="展开文件" aria-expanded="false" onclick="toggleTorrentFiles('${escapeAttr(torrent.hash)}')">${icon("chevronDown")}</button>
        </div>
      </div>
      <div class="progress"><span style="width:${progressPercent}%"></span></div>
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
  const torrent = state.torrents.find((item) => item.hash === hash);
  const actionMeta = torrentActionMeta(torrent || {});
  if ((action === "start" || action === "stop") && actionMeta[action]?.disabled) {
    $("torrentStatus").textContent = actionMeta[action].title;
    return;
  }
  const actionText = {
    start: "开始",
    stop: "停止",
    "delete-with-files": "删除任务和文件",
  }[action] || action;
  const key = `${hash}:${action}`;
  if (state.torrentActions.has(key)) return;
  if (action === "delete-with-files") {
    const ok = confirm(`将从下载工具删除任务，并连同已下载文件一起删除：\n${torrent?.name || hash}\n\n这个操作不能撤销。继续吗？`);
    if (!ok) return;
  }
  state.torrentActions.add(key);
  setTorrentActionBusy(button, true, actionText);
  $("torrentStatus").textContent = `正在${actionText} 下载任务...`;
  try {
    const data = await postJson(`/api/qbit/torrents/${encodeURIComponent(hash)}/${action}`, {});
    $("torrentStatus").textContent = data.message || `已${actionText}。`;
    delete state.torrentFiles[hash];
    delete state.targetSuggestions[hash];
    delete state.selectedFiles[hash];
    state.openTorrentFiles.delete(hash);
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
    state.openTorrentFiles.delete(hash);
    if (toggle) {
      toggle.innerHTML = icon("chevronDown");
      toggle.title = "展开文件";
      toggle.setAttribute("aria-label", "展开文件");
      toggle.setAttribute("aria-expanded", "false");
    }
    return;
  }
  holder.dataset.open = "1";
  state.openTorrentFiles.add(hash);
  if (toggle) {
    toggle.innerHTML = icon("chevronUp");
    toggle.title = "收起文件";
    toggle.setAttribute("aria-label", "收起文件");
    toggle.setAttribute("aria-expanded", "true");
  }
  holder.innerHTML = state.torrentFiles[hash]
    ? fileLoadingPanel("正在检查目标状态", "检查 Jellyfin 目标目录是否已经存在。", ["刷新状态"])
    : fileLoadingPanel("正在读取文件列表", "连接下载工具，展开任务内文件和目录结构。", ["读取下载工具", "生成文件树", "准备勾选"]);
  try {
    if (!state.torrentFiles[hash]) {
      const data = await getJson(`/api/qbit/torrents/${encodeURIComponent(hash)}/files`);
      state.torrentFiles[hash] = data.files || [];
    }
    if (state.targetSuggestions[hash]) {
      await refreshTargetState(hash);
    } else {
      await ensureTargetSuggestions(hash, holder);
    }
    state.selectedFiles[hash] = state.selectedFiles[hash] || new Set();
    renderTorrentFiles(hash);
  } catch (err) {
    holder.innerHTML = `<div class="mini-status">读取文件失败：${escapeHtml(err.message)}</div>`;
  }
}

function targetCacheKey(hash) {
  return `bobsearch:targetSuggestions:v4:${hash}`;
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
  holder.innerHTML = fileLoadingPanel("正在计算目标目录", "读取任务名和文件列表，交给大模型判断电影、电视剧或软件并生成目录。", ["整理文件名", "查询资料", "大模型命名"]);
  const torrent = state.torrents.find((item) => item.hash === hash);
  const data = await postJson("/api/jellyfin/targets", {
    query: torrent?.name || "",
    file_names: allFilePaths(state.torrentFiles[hash] || []),
  });
  state.targetSuggestions[hash] = data.targets || [];
  writeTargetSuggestionsCache(hash, state.targetSuggestions[hash]);
}

async function refreshTargetState(hash) {
  const targets = state.targetSuggestions[hash];
  if (!targets || !targets.length) return;
  const data = await postJson("/api/jellyfin/targets/refresh", { targets });
  state.targetSuggestions[hash] = data.targets || [];
  writeTargetSuggestionsCache(hash, state.targetSuggestions[hash]);
  const holder = $(`files-${hash}`);
  if (holder?.dataset.open === "1") {
    renderTorrentFiles(hash);
  }
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
  const files = state.torrentFiles[hash] || [];
  const targets = state.targetSuggestions[hash] || [];
  const firstTarget = targets[0];
  const canMove = canMoveSelected(hash, firstTarget);
  holder.innerHTML = `
    <div class="file-tools">
      <select id="targetChoice-${escapeAttr(hash)}" class="target-choice" onchange="updateTargetReason('${escapeAttr(hash)}')">
        ${targets.map(targetOption).join("")}
      </select>
      <button class="ghost" onclick="selectAllFiles('${escapeAttr(hash)}')">全选</button>
      <button class="ghost" onclick="clearSelectedFiles('${escapeAttr(hash)}')">清空</button>
      <button class="ghost" onclick="clearTargetSuggestions('${escapeAttr(hash)}')">清除目录名</button>
      <button id="moveBtn-${escapeAttr(hash)}" ${canMove ? "" : "disabled"} onclick="moveSelected('${escapeAttr(hash)}')">移动勾选并清理</button>
    </div>
    <p id="targetReason-${escapeAttr(hash)}" class="target-reason">${targetReasonText(targets[0])}</p>
    <p class="section-note">${targets.length ? "目标目录已按现有目录和命名规则自动生成，优先选择最高匹配项。" : "没有可用目标目录候选。"}</p>
    ${selectedMoveNote(hash)}
    <div class="file-tree">${files.map((node) => fileNode(hash, node, 0)).join("") || emptyState("没有文件信息。")}</div>
  `;
}

function fileNodeMap(nodes, map = new Map()) {
  for (const node of nodes || []) {
    map.set(node.path, node);
    fileNodeMap(node.children || [], map);
  }
  return map;
}

function selectedNodesComplete(hash) {
  const selected = Array.from(state.selectedFiles[hash] || []);
  if (!selected.length) return false;
  const nodeMap = fileNodeMap(state.torrentFiles[hash] || []);
  return selected.every((path) => {
    const node = nodeMap.get(path);
    return node && Number(node.progress || 0) >= FILE_COMPLETE_THRESHOLD;
  });
}

function canMoveSelected(hash, target) {
  return Boolean(target) && !target.disabled && selectedNodesComplete(hash);
}

function selectedMoveNote(hash) {
  const selected = Array.from(state.selectedFiles[hash] || []);
  if (!selected.length) return `<p class="section-note">请勾选已完成的文件或文件夹后再移动。</p>`;
  if (!selectedNodesComplete(hash)) return `<p class="section-note">只有勾选项全部下载完成后，才能移动勾选并清理。</p>`;
  return `<p class="section-note">勾选项已完成，可直接移动勾选并清理，不需要等待整任务全部完成。</p>`;
}

function targetOption(target) {
  const disabled = target.disabled ? " · 不可移动" : "";
  const stateLabel = target.existing ? "现存" : "新建";
  const label = `${target.category}/${target.target_folder || target.folder} · ${stateLabel} · ${Math.round((target.score || 0) * 100)}%${disabled}`;
  const value = `${target.category}\t${target.folder}`;
  return `<option value="${escapeAttr(value)}">${escapeHtml(label)}</option>`;
}

function episodeLabel(season, episodes) {
  if (!episodes || !episodes.length) return "";
  if (episodes.length > 1) return `S${String(season).padStart(2, "0")}E${String(episodes[0]).padStart(2, "0")}-E${String(episodes[episodes.length - 1]).padStart(2, "0")}`;
  return `S${String(season).padStart(2, "0")}E${String(episodes[0]).padStart(2, "0")}`;
}

function targetReasonText(target) {
  if (!target) return "";
  const reason = target.reason ? `：${target.reason}` : "";
  const episode = target.media_type === "series" && target.season_number && (target.episode_numbers || []).length
    ? `；季集：${episodeLabel(target.season_number, target.episode_numbers)}`
    : "";
  const rename = target.rename_plan?.preview ? `；重命名预览：${target.rename_plan.preview}` : "";
  const disabled = target.disabled ? "；无法识别季集号，不能自动移动" : "";
  return `匹配说明${reason}${episode}${rename}${disabled}`;
}

function targetRootLabel(category) {
  return category === "software" ? "软件目录" : "Jellyfin";
}

function targetDisplayPath(category, folder) {
  return category === "software" ? `${targetRootLabel(category)}/${folder}` : `${targetRootLabel(category)}/${category}/${folder}`;
}

function updateTargetReason(hash) {
  const choice = $(`targetChoice-${hash}`);
  const reason = $(`targetReason-${hash}`);
  if (!choice || !reason) return;
  const target = (state.targetSuggestions[hash] || [])[choice.selectedIndex];
  reason.textContent = targetReasonText(target);
  const torrent = state.torrents.find((item) => item.hash === hash);
  const moveBtn = $(`moveBtn-${hash}`);
  if (moveBtn) {
    moveBtn.disabled = !canMoveSelected(hash, target);
  }
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
        <span>${fmtProgress(node.progress)}</span>
      </label>
    </div>
    ${children}
  `;
}

function toggleSelectedFile(hash, path, checked) {
  state.selectedFiles[hash] = state.selectedFiles[hash] || new Set();
  if (checked) state.selectedFiles[hash].add(path);
  else state.selectedFiles[hash].delete(path);
  renderTorrentFiles(hash);
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
  const target = selectedTarget(hash);
  const [targetCategory, targetFolder] = targetChoice.split("\t");
  if (!selected.length) {
    $("torrentStatus").textContent = "请先勾选要移动的文件或文件夹。";
    return;
  }
  if (!targetCategory || !targetFolder) {
    $("torrentStatus").textContent = "没有可用的目标目录候选。";
    return;
  }
  if (!selectedNodesComplete(hash)) {
    $("torrentStatus").textContent = "只有勾选项全部下载完成后，才能移动勾选并清理。";
    return;
  }
  if (target?.disabled) {
    $("torrentStatus").textContent = "当前电视剧目标无法识别季集号，不能自动移动。";
    return;
  }
  const ok = confirm(`将移动 ${selected.length} 个勾选项到 ${targetDisplayPath(targetCategory, targetFolder)}。全部成功后会删除下载任务，并删除未勾选的剩余文件。继续吗？`);
  if (!ok) return;
  setMoveBusy(hash, true);
  try {
    const data = await postJson(`/api/qbit/torrents/${encodeURIComponent(hash)}/move-selected`, {
      selected_paths: selected,
      target_category: targetCategory,
      target_folder: targetFolder,
      rename_plan: target?.rename_plan || null,
    });
    $("torrentStatus").textContent = data.message;
    delete state.torrentFiles[hash];
    delete state.selectedFiles[hash];
    delete state.targetSuggestions[hash];
    state.openTorrentFiles.delete(hash);
    state.torrents = state.torrents.filter((torrent) => torrent.hash !== hash);
    renderTorrents();
    renderDownloadSummary();
    await loadTorrents(true);
  } catch (err) {
    $("torrentStatus").textContent = `移动失败，下载任务和剩余文件已保留：${err.message}`;
  } finally {
    setMoveBusy(hash, false);
  }
}

function selectedTarget(hash) {
  const choice = $(`targetChoice-${hash}`);
  if (!choice) return null;
  return (state.targetSuggestions[hash] || [])[choice.selectedIndex] || null;
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
$("addMagnetBtn").addEventListener("click", addManualMagnet);
$("uploadTorrentBtn").addEventListener("click", uploadTorrentFile);
$("torrentFileInput").addEventListener("change", (event) => {
  const file = event.target.files?.[0];
  $("manualAddStatus").textContent = file ? `已选择：${file.name}` : "";
});
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
$("historySelect").addEventListener("change", (event) => {
  loadHistoryItem(event.target.value);
});
$("historyDeleteBtn").addEventListener("click", deleteCurrentHistory);
$("historyClearBtn").addEventListener("click", clearSearchHistory);
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

async function init() {
  health();
  renderDownloadSummary();
  await loadSearchHistory();
  const initialTab = readActiveTab();
  setTab(initialTab);
  if (initialTab === "search") {
    restoreLatestSearchIfNeeded();
  }
}

init();
