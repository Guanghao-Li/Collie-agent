const API_KEY_STORAGE = "collie_memory_dashboard_api_key";

const state = {
  selectedMemoryId: "",
  selectedIds: new Set(),
  filters: {
    status: "active",
    kind: "",
    query: "",
    limit: 50,
    offset: 0,
  },
  apiKey: "",
  lastStats: null,
  currentItems: [],
  loadingCount: 0,
};

function byId(id) {
  return document.getElementById(id);
}

function setText(id, value) {
  const element = byId(id);
  if (element) {
    element.textContent = value == null || value === "" ? "-" : String(value);
  }
}

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) {
    element.className = className;
  }
  if (text !== undefined) {
    element.textContent = text == null ? "" : String(text);
  }
  return element;
}

function clearNode(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

function shortId(id) {
  const text = String(id || "");
  return text.length > 10 ? `${text.slice(0, 8)}...` : text;
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : String(value);
}

function formatJson(value) {
  if (value === null || value === undefined || value === "") {
    return "";
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch (error) {
    return String(value);
  }
}

function showMessage(message, type = "success") {
  const banner = byId("messageBanner");
  banner.textContent = message;
  banner.className = `message ${type}`;
  banner.hidden = false;
  window.clearTimeout(showMessage.timeoutId);
  showMessage.timeoutId = window.setTimeout(() => {
    banner.hidden = true;
  }, type === "error" ? 8000 : 3600);
}

function setLoading(isLoading) {
  state.loadingCount += isLoading ? 1 : -1;
  state.loadingCount = Math.max(0, state.loadingCount);
  document.querySelectorAll("button").forEach((button) => {
    button.disabled = state.loadingCount > 0;
  });
}

function updateAuthState() {
  state.apiKey = localStorage.getItem(API_KEY_STORAGE) || "";
  setText("authState", state.apiKey ? "API key saved" : "No API key");
}

async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const hasBody = options.body !== undefined && options.body !== null;
  if (hasBody && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (state.apiKey) {
    headers.set("Authorization", `Bearer ${state.apiKey}`);
  }

  const response = await fetch(path, { ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const error = payload && payload.error ? payload.error : {};
    const message = error.message || response.statusText || "Request failed";
    const code = error.code || `http_${response.status}`;
    throw new Error(`${code}: ${message}`);
  }
  return payload;
}

async function runTask(task, successMessage = "") {
  setLoading(true);
  try {
    const result = await task();
    if (successMessage) {
      showMessage(successMessage);
    }
    return result;
  } catch (error) {
    showMessage(error.message || String(error), "error");
    return null;
  } finally {
    setLoading(false);
  }
}

function paramsFromObject(values) {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      params.set(key, String(value));
    }
  });
  return params;
}

async function loadHealth() {
  const health = await runTask(() => apiFetch("/health"));
  if (!health) {
    setText("serviceStatus", "Service unavailable");
    return;
  }
  setText("serviceStatus", `${health.service || "memory"} online`);
  setText("vectorState", health.vector_enabled ? "enabled" : "disabled");
  setText("adminState", health.admin_enabled ? "enabled" : "disabled");
}

async function loadStats() {
  const stats = await runTask(() => apiFetch("/memory/stats"));
  if (!stats) {
    return;
  }
  state.lastStats = stats;
  setText("statActive", stats.active);
  setText("statPending", stats.pending_candidates);
  setText("statReview", stats.requires_review);
  setText("statDeleted", stats.deleted);
}

function readFilters() {
  state.filters.status = byId("statusFilter").value || "active";
  state.filters.kind = byId("kindFilter").value.trim();
  state.filters.query = byId("queryFilter").value.trim();
  state.filters.limit = Math.max(1, Math.min(500, Number(byId("limitFilter").value) || 50));
}

function memoryListStatuses() {
  if (state.filters.status !== "all") {
    return [state.filters.status];
  }
  return ["active", "deleted", "superseded", "pending", "lowered_confidence"];
}

async function fetchMemoryPage(status) {
  const params = paramsFromObject({
    status,
    kind: state.filters.kind,
    query: state.filters.query,
    limit: state.filters.limit,
    offset: state.filters.status === "all" ? 0 : state.filters.offset,
  });
  return apiFetch(`/memory?${params.toString()}`);
}

async function loadMemories() {
  readFilters();
  const result = await runTask(async () => {
    const pages = await Promise.all(memoryListStatuses().map(fetchMemoryPage));
    const combined = pages.flatMap((page) => page.items || []);
    if (state.filters.status === "all") {
      return {
        items: combined.slice(state.filters.offset, state.filters.offset + state.filters.limit),
        has_more: combined.length > state.filters.offset + state.filters.limit,
      };
    }
    return pages[0] || { items: [], has_more: false };
  });
  if (!result) {
    return;
  }
  state.currentItems = result.items || [];
  renderMemoryList(state.currentItems);
  setText("pageState", `offset ${state.filters.offset}`);
  byId("nextPageButton").dataset.hasMore = result.has_more ? "true" : "false";
}

function renderMemoryList(items) {
  const list = byId("memoryList");
  clearNode(list);
  if (!items.length) {
    list.appendChild(createElement("div", "empty", "No memories match the current filters."));
    return;
  }

  items.forEach((item) => {
    const row = createElement("div", "memory-row");
    if (item.id === state.selectedMemoryId) {
      row.classList.add("selected");
    }

    const checkbox = createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selectedIds.has(item.id);
    checkbox.addEventListener("click", (event) => {
      event.stopPropagation();
      if (checkbox.checked) {
        state.selectedIds.add(item.id);
      } else {
        state.selectedIds.delete(item.id);
      }
    });
    row.appendChild(checkbox);

    row.appendChild(createElement("div", "memory-cell", shortId(item.id)));
    row.appendChild(createElement("div", "memory-cell", item.kind || item.type || "-"));

    const summaryButton = createElement("button", "memory-cell memory-summary", item.summary || item.text || "-");
    summaryButton.type = "button";
    summaryButton.title = item.summary || item.text || "";
    summaryButton.addEventListener("click", (event) => {
      event.stopPropagation();
      loadDetail(item.id);
    });
    row.appendChild(summaryButton);

    row.appendChild(createElement("span", "status-pill", item.status || "-"));
    row.appendChild(createElement("div", "memory-cell", formatNumber(item.importance)));
    row.appendChild(createElement("div", "memory-cell", formatNumber(item.confidence)));
    row.appendChild(createElement("div", "memory-cell", item.reinforcement || 0));
    row.appendChild(createElement("div", "memory-cell", item.source_ref || "-"));
    row.appendChild(createElement("div", "memory-cell", item.updated_at || "-"));
    row.addEventListener("click", () => loadDetail(item.id));
    list.appendChild(row);
  });
}

async function loadDetail(memoryId) {
  if (!memoryId) {
    return;
  }
  const detail = await runTask(() => apiFetch(`/memory/${encodeURIComponent(memoryId)}`));
  if (!detail) {
    return;
  }
  state.selectedMemoryId = detail.id;
  renderMemoryList(state.currentItems);
  renderDetail(detail);
}

function renderDetail(detail) {
  const summary = byId("detailSummary");
  clearNode(summary);
  const fields = [
    ["ID", detail.id],
    ["Kind", detail.kind || detail.type],
    ["Status", detail.status],
    ["Source", detail.source_ref],
    ["Happened", detail.happened_at],
    ["Created", detail.created_at],
    ["Updated", detail.updated_at],
    ["Replacement history", formatJson(detail.replacements || [])],
  ];
  fields.forEach(([label, value]) => {
    summary.appendChild(createElement("dt", "", label));
    summary.appendChild(createElement("dd", "", value || "-"));
  });

  byId("summaryInput").value = detail.summary || detail.text || "";
  byId("bodyInput").value = detail.body || detail.text || "";
  byId("typeInput").value = detail.kind || detail.type || "";
  byId("statusInput").value = detail.status || "";
  byId("importanceInput").value = detail.importance ?? "";
  byId("confidenceInput").value = detail.confidence ?? "";
  byId("tagsInput").value = Array.isArray(detail.tags) ? detail.tags.join(", ") : "";
  byId("metadataInput").value = formatJson(detail.metadata || {});
}

async function saveMemory(event) {
  event.preventDefault();
  if (!state.selectedMemoryId) {
    showMessage("Select a memory first.", "error");
    return;
  }

  let metadata = {};
  const metadataText = byId("metadataInput").value.trim();
  if (metadataText) {
    try {
      metadata = JSON.parse(metadataText);
    } catch (error) {
      showMessage("metadata JSON is invalid; no request was sent.", "error");
      return;
    }
  }

  const payload = {
    summary: byId("summaryInput").value.trim(),
    body: byId("bodyInput").value,
    type: byId("typeInput").value.trim(),
    status: byId("statusInput").value.trim(),
    importance: Number(byId("importanceInput").value),
    confidence: Number(byId("confidenceInput").value),
    tags: byId("tagsInput").value.split(",").map((tag) => tag.trim()).filter(Boolean),
    metadata,
  };

  await runTask(
    () => apiFetch(`/memory/${encodeURIComponent(state.selectedMemoryId)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
    "Memory saved."
  );
  await loadDetail(state.selectedMemoryId);
  await loadMemories();
  await loadStats();
}

async function deleteMemory() {
  if (!state.selectedMemoryId) {
    showMessage("Select a memory first.", "error");
    return;
  }
  const reason = byId("deleteReasonInput").value.trim();
  const confirmed = window.confirm(`Delete memory ${state.selectedMemoryId}?`);
  if (!confirmed) {
    return;
  }
  const result = await runTask(
    () => apiFetch(`/memory/${encodeURIComponent(state.selectedMemoryId)}`, {
      method: "DELETE",
      body: JSON.stringify({ reason }),
    }),
    "Memory deleted."
  );
  if (result) {
    state.selectedMemoryId = "";
    await loadMemories();
    await loadStats();
  }
}

async function batchDeleteSelected() {
  const ids = Array.from(state.selectedIds);
  if (!ids.length) {
    showMessage("Select at least one memory.", "error");
    return;
  }
  const confirmed = window.confirm(`Delete ${ids.length} selected memories?`);
  if (!confirmed) {
    return;
  }
  const reason = byId("deleteReasonInput").value.trim();
  const result = await runTask(() => apiFetch("/memory/batch-delete", {
    method: "POST",
    body: JSON.stringify({ ids, reason }),
  }));
  if (!result) {
    return;
  }
  state.selectedIds.clear();
  showMessage(`Deleted ${result.affected_ids.length}; missing ${result.missing_ids.length}.`);
  await loadMemories();
  await loadStats();
}

function renderSimilar(items) {
  const container = byId("similarResults");
  clearNode(container);
  if (!items.length) {
    container.appendChild(createElement("div", "empty", "No similar memories found."));
    return;
  }
  items.forEach((item) => {
    const row = createElement("button", "similar-item");
    row.type = "button";
    row.appendChild(createElement("div", "memory-summary", item.summary || item.text || item.id));
    row.appendChild(createElement("div", "memory-cell", `${item.kind || item.type || "-"} score ${formatNumber(item.score)}`));
    row.addEventListener("click", () => loadDetail(item.id));
    container.appendChild(row);
  });
}

async function findSimilar(useSelected = false) {
  const payload = {
    limit: Math.max(1, Math.min(50, Number(byId("similarLimitInput").value) || 10)),
  };
  if (useSelected && state.selectedMemoryId) {
    payload.id = state.selectedMemoryId;
  } else {
    payload.text = byId("similarTextInput").value.trim();
  }
  if (!payload.id && !payload.text) {
    showMessage("Select a memory or enter text for similar search.", "error");
    return;
  }
  const result = await runTask(() => apiFetch("/memory/find-similar", {
    method: "POST",
    body: JSON.stringify(payload),
  }));
  if (result) {
    renderSimilar(result.items || []);
  }
}

function renderRecords(records) {
  const container = byId("recallRecords");
  clearNode(container);
  if (!records.length) {
    container.appendChild(createElement("div", "empty", "No records returned."));
    return;
  }
  records.forEach((record) => {
    const details = createElement("details", "record-item");
    const summary = createElement(
      "summary",
      "",
      `${record.kind || "-"} ${shortId(record.id)} score ${formatNumber(record.score)}`
    );
    details.appendChild(summary);
    details.appendChild(createElement("div", "", record.summary || ""));
    const pre = createElement("pre", "output-block", formatJson(record));
    details.appendChild(pre);
    container.appendChild(details);
  });
}

async function recallMemory() {
  const payload = {
    query: byId("recallQueryInput").value.trim(),
    intent: byId("recallIntentInput").value,
    memory_kind: byId("recallKindInput").value.trim() || null,
    limit: Math.max(1, Math.min(50, Number(byId("recallLimitInput").value) || 8)),
  };
  if (!payload.query) {
    showMessage("Recall query is required.", "error");
    return;
  }
  const result = await runTask(() => apiFetch("/memory/recall", {
    method: "POST",
    body: JSON.stringify(payload),
  }));
  if (!result) {
    return;
  }
  byId("recallTextBlock").textContent = result.text_block || result.content || "";
  renderRecords(result.records || []);
}

async function memorizePending() {
  const payload = {
    summary: byId("memorizeSummaryInput").value.trim(),
    memory_kind: byId("memorizeKindInput").value.trim() || "preference",
    importance: Number(byId("memorizeImportanceInput").value) || 0.7,
    confidence: Number(byId("memorizeConfidenceInput").value) || 0.8,
    source_ref: byId("memorizeSourceInput").value.trim() || "manual:dashboard",
  };
  if (!payload.summary) {
    showMessage("Summary is required.", "error");
    return;
  }
  const result = await runTask(() => apiFetch("/memory/memorize", {
    method: "POST",
    body: JSON.stringify(payload),
  }), "Pending memory accepted.");
  if (result) {
    byId("memorizeResult").textContent = formatJson(result);
    await loadStats();
  }
}

async function runOptimizer() {
  const result = await runTask(() => apiFetch("/memory/optimize", {
    method: "POST",
    body: JSON.stringify({ force: true }),
  }), "Optimizer finished.");
  if (result) {
    byId("optimizerResult").textContent = formatJson(result);
    await loadStats();
    await loadMemories();
    await loadOptimizerState();
  }
}

async function loadOptimizerState() {
  const result = await runTask(() => apiFetch("/memory/optimizer/state"));
  if (result) {
    byId("optimizerState").textContent = formatJson(result);
  }
}

function renderEvents(events) {
  const container = byId("eventTimeline");
  clearNode(container);
  if (!events.length) {
    container.appendChild(createElement("div", "empty", "No events found."));
    return;
  }
  events.forEach((event) => {
    const item = createElement("div", "event-item");
    item.appendChild(createElement("strong", "", event.happened_at || event.updated_at || "-"));
    item.appendChild(createElement("div", "", event.summary || event.body || event.text || "-"));
    item.appendChild(createElement("div", "memory-cell", event.source_ref || event.id || ""));
    container.appendChild(item);
  });
}

async function loadEvents() {
  const params = paramsFromObject({
    start: byId("eventStartInput").value,
    end: byId("eventEndInput").value,
    limit: Math.max(1, Math.min(500, Number(byId("eventLimitInput").value) || 100)),
  });
  const result = await runTask(() => apiFetch(`/memory/events?${params.toString()}`));
  if (result) {
    renderEvents(result.events || []);
  }
}

function saveApiKey() {
  const input = byId("apiKeyInput");
  const value = input.value.trim();
  if (!value) {
    showMessage("Enter an API key first.", "error");
    return;
  }
  localStorage.setItem(API_KEY_STORAGE, value);
  input.value = "";
  updateAuthState();
  showMessage("API key saved locally.");
  loadStats();
  loadMemories();
  loadOptimizerState();
}

function clearApiKey() {
  localStorage.removeItem(API_KEY_STORAGE);
  byId("apiKeyInput").value = "";
  updateAuthState();
  showMessage("API key cleared.");
}

function bindEvents() {
  byId("saveApiKeyButton").addEventListener("click", saveApiKey);
  byId("clearApiKeyButton").addEventListener("click", clearApiKey);
  byId("refreshButton").addEventListener("click", async () => {
    await loadHealth();
    await loadStats();
    await loadMemories();
    await loadOptimizerState();
  });
  byId("batchDeleteButton").addEventListener("click", batchDeleteSelected);
  byId("editForm").addEventListener("submit", saveMemory);
  byId("deleteMemoryButton").addEventListener("click", deleteMemory);
  byId("findSimilarCurrentButton").addEventListener("click", () => findSimilar(true));
  byId("findSimilarTextButton").addEventListener("click", () => findSimilar(false));
  byId("recallButton").addEventListener("click", recallMemory);
  byId("memorizeButton").addEventListener("click", memorizePending);
  byId("optimizeButton").addEventListener("click", runOptimizer);
  byId("loadEventsButton").addEventListener("click", loadEvents);
  byId("previousPageButton").addEventListener("click", () => {
    state.filters.offset = Math.max(0, state.filters.offset - state.filters.limit);
    loadMemories();
  });
  byId("nextPageButton").addEventListener("click", () => {
    state.filters.offset += state.filters.limit;
    loadMemories();
  });
  ["statusFilter", "kindFilter", "queryFilter", "limitFilter"].forEach((id) => {
    byId(id).addEventListener("change", () => {
      state.filters.offset = 0;
      loadMemories();
    });
  });
  byId("queryFilter").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      state.filters.offset = 0;
      loadMemories();
    }
  });
}

async function bootDashboard() {
  updateAuthState();
  bindEvents();
  await loadHealth();
  await loadStats();
  await loadMemories();
  await loadOptimizerState();
}

document.addEventListener("DOMContentLoaded", bootDashboard);
