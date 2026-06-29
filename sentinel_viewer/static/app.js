const state = {
  summary: null,
  scenes: [],
  selectedId: null,
  layerById: new Map(),
  geoLayer: null,
  user: null,
  downloadProducts: [],
  downloadPollId: null,
  downloadArea: null,
  downloadAreaLayer: null,
  aoiArea: null,
  aoiAreaLayer: null,
  areaSelectionMode: "filter",
  areaSelecting: false,
  areaStartLatLng: null,
  areaDraftLayer: null,
  users: [],
  trashItems: [],
};

const els = {
  loginScreen: document.getElementById("loginScreen"),
  loginForm: document.getElementById("loginForm"),
  loginEmail: document.getElementById("loginEmail"),
  loginPassword: document.getElementById("loginPassword"),
  loginError: document.getElementById("loginError"),
  userEmail: document.getElementById("userEmail"),
  userManageButton: document.getElementById("userManageButton"),
  trashButton: document.getElementById("trashButton"),
  trashScreen: document.getElementById("trashScreen"),
  closeTrashButton: document.getElementById("closeTrashButton"),
  trashRefreshButton: document.getElementById("trashRefreshButton"),
  trashEmptyButton: document.getElementById("trashEmptyButton"),
  trashNote: document.getElementById("trashNote"),
  trashList: document.getElementById("trashList"),
  userScreen: document.getElementById("userScreen"),
  closeUserButton: document.getElementById("closeUserButton"),
  userCreateForm: document.getElementById("userCreateForm"),
  newUserEmail: document.getElementById("newUserEmail"),
  newUserRole: document.getElementById("newUserRole"),
  newUserPassword: document.getElementById("newUserPassword"),
  userError: document.getElementById("userError"),
  userRefreshButton: document.getElementById("userRefreshButton"),
  userList: document.getElementById("userList"),
  logoutButton: document.getElementById("logoutButton"),
  downloadButton: document.getElementById("downloadButton"),
  downloadScreen: document.getElementById("downloadScreen"),
  closeDownloadButton: document.getElementById("closeDownloadButton"),
  downloadSearchForm: document.getElementById("downloadSearchForm"),
  downloadFromInput: document.getElementById("downloadFromInput"),
  downloadToInput: document.getElementById("downloadToInput"),
  downloadTopInput: document.getElementById("downloadTopInput"),
  downloadAreaLabel: document.getElementById("downloadAreaLabel"),
  downloadSelectAreaButton: document.getElementById("downloadSelectAreaButton"),
  downloadClearAreaButton: document.getElementById("downloadClearAreaButton"),
  hideDownloadedCheckbox: document.getElementById("hideDownloadedCheckbox"),
  hideExcludedCheckbox: document.getElementById("hideExcludedCheckbox"),
  downloadRefreshButton: document.getElementById("downloadRefreshButton"),
  downloadSearchButton: document.getElementById("downloadSearchButton"),
  downloadStartButton: document.getElementById("downloadStartButton"),
  downloadStatus: document.getElementById("downloadStatus"),
  downloadProductCount: document.getElementById("downloadProductCount"),
  downloadProductList: document.getElementById("downloadProductList"),
  downloadLogs: document.getElementById("downloadLogs"),
  sceneCount: document.getElementById("sceneCount"),
  totalSize: document.getElementById("totalSize"),
  errorCount: document.getElementById("errorCount"),
  excludedCount: document.getElementById("excludedCount"),
  dateRange: document.getElementById("dateRange"),
  filteredCount: document.getElementById("filteredCount"),
  sceneList: document.getElementById("sceneList"),
  detailPanel: document.getElementById("detailPanel"),
  queryInput: document.getElementById("queryInput"),
  familySelect: document.getElementById("familySelect"),
  missionSelect: document.getElementById("missionSelect"),
  directionSelect: document.getElementById("directionSelect"),
  dateFromInput: document.getElementById("dateFromInput"),
  dateToInput: document.getElementById("dateToInput"),
  aoiAreaLabel: document.getElementById("aoiAreaLabel"),
  aoiSelectButton: document.getElementById("aoiSelectButton"),
  aoiClearButton: document.getElementById("aoiClearButton"),
  csvLink: document.getElementById("csvLink"),
  rescanButton: document.getElementById("rescanButton"),
  fitButton: document.getElementById("fitButton"),
  selectAreaButton: document.getElementById("selectAreaButton"),
  areaSelectionHint: document.getElementById("areaSelectionHint"),
  areaSelectionHintText: document.getElementById("areaSelectionHintText"),
  cancelAreaSelectionButton: document.getElementById("cancelAreaSelectionButton"),
  toggleListButton: document.getElementById("toggleListButton"),
  clearSelectionButton: document.getElementById("clearSelectionButton"),
};

const map = L.map("map", {
  zoomControl: false,
  preferCanvas: true,
}).setView([33.35, 126.55], 7);

L.control.zoom({ position: "bottomright" }).addTo(map);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

function colorForFeature(feature) {
  const direction = feature.properties.orbit_direction;
  if (direction === "ASCENDING") return "#147c72";
  if (direction === "DESCENDING") return "#2764b0";
  return "#aa6a00";
}

function styleForFeature(feature) {
  const selected = feature.properties.id === state.selectedId;
  if (selected) {
    return {
      color: "#ffb000",
      weight: 6,
      opacity: 1,
      fillColor: "#ffcf33",
      fillOpacity: 0.14,
      dashArray: "",
    };
  }
  return {
    color: colorForFeature(feature),
    weight: 2,
    opacity: 0.62,
    fillColor: colorForFeature(feature),
    fillOpacity: 0.025,
    dashArray: "",
  };
}

function currentParams() {
  const params = new URLSearchParams();
  if (els.queryInput.value.trim()) params.set("q", els.queryInput.value.trim());
  if (els.familySelect.value) params.set("family", els.familySelect.value);
  if (els.missionSelect.value) params.set("mission", els.missionSelect.value);
  if (els.directionSelect.value) params.set("direction", els.directionSelect.value);
  if (els.dateFromInput.value) params.set("from", els.dateFromInput.value);
  if (els.dateToInput.value) params.set("to", els.dateToInput.value);
  if (state.aoiArea) params.set("bbox", state.aoiArea.join(","));
  return params;
}

function apiUrl(path) {
  const params = currentParams();
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

async function fetchJson(path, options = {}) {
  const response = await fetch(path, options);
  if (response.status === 401) {
    showLogin();
    throw new Error("로그인이 필요합니다");
  }
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) throw new Error(payload.error || `요청 실패: ${response.status}`);
  return payload;
}

function postJson(path, payload) {
  return fetchJson(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  }[char]));
}

function bboxFromBounds(bounds) {
  const west = bounds.getWest();
  const south = bounds.getSouth();
  const east = bounds.getEast();
  const north = bounds.getNorth();
  return [west, south, east, north].map((value) => Number(value.toFixed(6)));
}

function bboxParam() {
  return state.downloadArea ? state.downloadArea.join(",") : "";
}

function compactAreaLabel(area) {
  const [west, south, east, north] = area;
  return `서 ${west.toFixed(3)}, 남 ${south.toFixed(3)}, 동 ${east.toFixed(3)}, 북 ${north.toFixed(3)}`;
}

function areaLabel() {
  if (!state.downloadArea) return "영역: 제주 기본값";
  return `영역: ${compactAreaLabel(state.downloadArea)}`;
}

function updateDownloadAreaLabel() {
  els.downloadAreaLabel.textContent = areaLabel();
}

function updateAoiAreaLabel() {
  els.aoiAreaLabel.textContent = state.aoiArea
    ? `관심지역: ${compactAreaLabel(state.aoiArea)}`
    : "관심지역: 전체";
}

function drawDownloadAreaLayer() {
  if (state.downloadAreaLayer) {
    state.downloadAreaLayer.remove();
    state.downloadAreaLayer = null;
  }
  if (!state.downloadArea) return;
  const [west, south, east, north] = state.downloadArea;
  state.downloadAreaLayer = L.rectangle([[south, west], [north, east]], {
    color: "#d97706",
    weight: 3,
    opacity: 0.95,
    fillColor: "#fbbf24",
    fillOpacity: 0.08,
    dashArray: "8 5",
  }).addTo(map);
}

function setDownloadArea(bounds) {
  state.downloadArea = bboxFromBounds(bounds);
  drawDownloadAreaLayer();
  updateDownloadAreaLabel();
}

function clearDownloadArea() {
  state.downloadArea = null;
  if (state.downloadAreaLayer) {
    state.downloadAreaLayer.remove();
    state.downloadAreaLayer = null;
  }
  updateDownloadAreaLabel();
}

function drawAoiAreaLayer() {
  if (state.aoiAreaLayer) {
    state.aoiAreaLayer.remove();
    state.aoiAreaLayer = null;
  }
  if (!state.aoiArea) return;
  const [west, south, east, north] = state.aoiArea;
  state.aoiAreaLayer = L.rectangle([[south, west], [north, east]], {
    color: "#b45309",
    weight: 3,
    opacity: 0.95,
    fillColor: "#f59e0b",
    fillOpacity: 0.05,
    dashArray: "10 6",
  }).addTo(map);
}

function setAoiArea(bounds) {
  state.aoiArea = bboxFromBounds(bounds);
  drawAoiAreaLayer();
  updateAoiAreaLabel();
}

async function clearAoiArea() {
  state.aoiArea = null;
  if (state.aoiAreaLayer) {
    state.aoiAreaLayer.remove();
    state.aoiAreaLayer = null;
  }
  updateAoiAreaLabel();
  await loadScenes();
}

function showLogin(message = "") {
  document.body.classList.add("auth-locked");
  els.loginScreen.hidden = false;
  els.loginError.textContent = message;
  window.setTimeout(() => els.loginPassword.focus(), 0);
}

function hideLogin(user) {
  state.user = user;
  els.userEmail.textContent = user.email;
  els.userManageButton.hidden = user.role !== "admin";
  els.downloadButton.hidden = user.role !== "admin";
  els.rescanButton.hidden = user.role !== "admin";
  els.loginPassword.value = "";
  els.loginError.textContent = "";
  els.loginScreen.hidden = true;
  document.body.classList.remove("auth-locked");
}

async function checkSession() {
  const response = await fetch("/api/auth/me");
  if (!response.ok) return null;
  const payload = await response.json();
  return payload.user;
}

async function login(event) {
  event.preventDefault();
  els.loginError.textContent = "";
  const response = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email: els.loginEmail.value,
      password: els.loginPassword.value,
    }),
  });

  if (!response.ok) {
    els.loginError.textContent = "이메일 또는 비밀번호를 확인해 주세요.";
    return;
  }

  const payload = await response.json();
  hideLogin(payload.user);
  await loadInitialData();
}

async function logout() {
  await fetch("/api/auth/logout", { method: "POST" });
  state.user = null;
  els.userManageButton.hidden = true;
  els.downloadButton.hidden = true;
  els.rescanButton.hidden = true;
  closeUserScreen();
  closeDownloadScreen();
  closeTrashScreen();
  showLogin();
}

function formatDateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ko-KR", { hour12: false });
}

function roleLabel(role) {
  if (role === "admin") return "관리자";
  if (role === "user") return "일반 사용자";
  return role || "-";
}

function activeLabel(active) {
  return active ? "활성" : "비활성";
}

function openUserScreen() {
  if (!isAdmin()) return;
  els.userScreen.hidden = false;
  loadUsers();
}

function closeUserScreen() {
  els.userScreen.hidden = true;
  els.userError.textContent = "";
}

async function loadUsers() {
  try {
    const payload = await fetchJson("/api/users");
    state.users = payload.users || [];
    renderUsers();
  } catch (error) {
    els.userList.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

async function openTrashScreen() {
  els.trashScreen.hidden = false;
  await loadTrash();
  lucide.createIcons();
}

function closeTrashScreen() {
  els.trashScreen.hidden = true;
  els.trashList.innerHTML = `<div class="empty-state">휴지통 목록을 불러오는 중입니다</div>`;
}

async function loadTrash() {
  try {
    const payload = await fetchJson("/api/trash");
    state.trashItems = payload.items || [];
    els.trashEmptyButton.hidden = !payload.can_empty || !state.trashItems.length;
    els.trashNote.textContent = `휴지통 보관 기간: ${payload.retention_days || "-"}일`;
    renderTrash();
  } catch (error) {
    els.trashList.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function renderTrash() {
  if (!state.trashItems.length) {
    els.trashList.innerHTML = `<div class="empty-state">휴지통이 비어 있습니다</div>`;
    return;
  }
  els.trashList.innerHTML = state.trashItems.map((item) => `
    <article class="trash-card" data-scene-id="${escapeHtml(item.scene_id)}">
      <div>
        <h4>${escapeHtml(item.name)}</h4>
        <p>제외일: ${escapeHtml(formatDateTime(item.excluded_at))}</p>
        <p>삭제 예정일: ${escapeHtml(formatDateTime(item.delete_after))}</p>
        <p>요청자: ${escapeHtml(item.excluded_by || "-")}</p>
      </div>
      <button class="text-button" data-action="restore-trash" type="button">복구</button>
    </article>
  `).join("");
}

async function restoreTrashItem(sceneId) {
  if (!window.confirm("이 이미지를 휴지통에서 복구할까요?")) return;
  const payload = await postJson(`/api/trash/${encodeURIComponent(sceneId)}/restore`, {});
  renderSummary(payload.summary);
  await Promise.all([loadTrash(), loadScenes()]);
}

async function emptyTrash() {
  if (!isAdmin()) return;
  const message = [
    "휴지통의 모든 이미지를 영구 삭제합니다.",
    "이 작업은 되돌릴 수 없습니다.",
    "",
    "계속할까요?",
  ].join("\n");
  if (!window.confirm(message)) return;
  const payload = await postJson("/api/trash/empty", {});
  renderSummary(payload.summary);
  await loadTrash();
}

function renderUsers() {
  if (!state.users.length) {
    els.userList.innerHTML = `<div class="empty-state">등록된 사용자가 없습니다</div>`;
    return;
  }
  els.userList.innerHTML = state.users.map((user) => `
    <article class="user-card" data-email="${escapeHtml(user.email)}">
      <div>
        <h4>${escapeHtml(user.email)}</h4>
        <p>${roleLabel(user.role)} · ${activeLabel(user.active)}</p>
      </div>
      <select data-field="role" aria-label="권한">
        <option value="user"${user.role === "user" ? " selected" : ""}>일반 사용자</option>
        <option value="admin"${user.role === "admin" ? " selected" : ""}>관리자</option>
      </select>
      <select data-field="active" aria-label="상태">
        <option value="true"${user.active ? " selected" : ""}>활성</option>
        <option value="false"${!user.active ? " selected" : ""}>비활성</option>
      </select>
      <div class="user-actions">
        <input data-field="password" type="password" placeholder="새 비밀번호">
        <button class="text-button" data-action="save-user" type="button">저장</button>
      </div>
    </article>
  `).join("");
}

async function createUser(event) {
  event.preventDefault();
  els.userError.textContent = "";
  try {
    await postJson("/api/users", {
      email: els.newUserEmail.value,
      role: els.newUserRole.value,
      password: els.newUserPassword.value,
    });
    els.userCreateForm.reset();
    els.newUserRole.value = "user";
    await loadUsers();
  } catch (error) {
    els.userError.textContent = error.message;
  }
}

async function saveUser(card) {
  const email = card.dataset.email;
  const role = card.querySelector('[data-field="role"]').value;
  const active = card.querySelector('[data-field="active"]').value === "true";
  const passwordInput = card.querySelector('[data-field="password"]');
  const payload = { role, active };
  if (passwordInput.value) payload.password = passwordInput.value;
  await postJson(`/api/users/${encodeURIComponent(email)}/update`, payload);
  await loadUsers();
}

function beginAreaSelection(mode = "filter") {
  closeDownloadScreen();
  state.areaSelectionMode = mode;
  state.areaSelecting = true;
  state.areaStartLatLng = null;
  els.areaSelectionHintText.textContent = mode === "download"
    ? "지도에서 드래그하여 다운로드 검색 영역을 선택하세요."
    : "지도에서 드래그하여 관심지역을 선택하세요.";
  els.areaSelectionHint.hidden = false;
  document.body.classList.add("area-selecting");
  map.dragging.disable();
  map.doubleClickZoom.disable();
}

async function finishAreaSelection(bounds) {
  const mode = state.areaSelectionMode;
  cancelAreaSelection(false);
  if (mode === "download") {
    setDownloadArea(bounds);
    openDownloadScreen().catch(console.error);
    return;
  }
  setAoiArea(bounds);
  await loadScenes();
}

function cancelAreaSelection(removeDraft = true) {
  state.areaSelecting = false;
  state.areaStartLatLng = null;
  state.areaSelectionMode = "filter";
  els.areaSelectionHint.hidden = true;
  document.body.classList.remove("area-selecting");
  map.dragging.enable();
  map.doubleClickZoom.enable();
  if (removeDraft && state.areaDraftLayer) {
    state.areaDraftLayer.remove();
  }
  state.areaDraftLayer = null;
}

function areaMouseDown(event) {
  if (!state.areaSelecting) return;
  state.areaStartLatLng = event.latlng;
  if (state.areaDraftLayer) state.areaDraftLayer.remove();
  state.areaDraftLayer = L.rectangle([event.latlng, event.latlng], {
    color: "#d97706",
    weight: 3,
    opacity: 0.95,
    fillColor: "#fbbf24",
    fillOpacity: 0.12,
    dashArray: "8 5",
  }).addTo(map);
}

function areaMouseMove(event) {
  if (!state.areaSelecting || !state.areaStartLatLng || !state.areaDraftLayer) return;
  state.areaDraftLayer.setBounds(L.latLngBounds(state.areaStartLatLng, event.latlng));
}

function areaMouseUp(event) {
  if (!state.areaSelecting || !state.areaStartLatLng || !state.areaDraftLayer) return;
  const bounds = L.latLngBounds(state.areaStartLatLng, event.latlng);
  const tooSmall = Math.abs(bounds.getEast() - bounds.getWest()) < 0.001
    || Math.abs(bounds.getNorth() - bounds.getSouth()) < 0.001;
  if (tooSmall) {
    cancelAreaSelection();
    return;
  }
  state.areaDraftLayer.remove();
  state.areaDraftLayer = null;
  finishAreaSelection(bounds).catch(console.error);
}

function stateLabel(stateName) {
  if (stateName === "new") return "신규";
  if (stateName === "downloaded") return "다운로드 완료";
  if (stateName === "excluded") return "제외됨";
  return stateName || "알 수 없음";
}

function stateClass(stateName) {
  if (stateName === "new") return "new";
  if (stateName === "downloaded") return "downloaded";
  if (stateName === "excluded") return "excluded";
  return "";
}

function phaseLabel(phase) {
  const labels = {
    idle: "대기",
    queued: "대기열 등록",
    searching: "검색 중",
    downloading: "다운로드 중",
    extracting: "압축 해제 중",
  };
  return labels[phase] || phase || "대기";
}

function directionLabel(direction) {
  if (direction === "ASCENDING") return "상행";
  if (direction === "DESCENDING") return "하행";
  return direction || "알 수 없음";
}

function processingLabel(status) {
  if (status === "ready") return "준비됨";
  if (status === "metadata-missing") return "메타데이터 누락";
  if (status === "zip-issue") return "압축파일 확인 필요";
  return status || "-";
}

function sceneFileStatus(scene) {
  const items = [];
  if (scene.status.safe) items.push("압축 해제됨");
  if (scene.status.zip) items.push("다운로드 가능");
  if (!items.length) return "파일 없음";
  return items.join(" / ");
}

function isAdmin() {
  return state.user && state.user.role === "admin";
}

function percentLabel(done, total) {
  if (!total) return "";
  return `${Math.min(100, Math.round((done / total) * 100))}%`;
}

function monthValue(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  return `${year}-${month}`;
}

function addMonths(date, delta) {
  const next = new Date(date);
  next.setMonth(next.getMonth() + delta);
  return next;
}

function setDefaultDownloadMonths() {
  const now = new Date();
  if (!els.downloadToInput.value) els.downloadToInput.value = monthValue(now);
  if (!els.downloadFromInput.value) els.downloadFromInput.value = monthValue(addMonths(now, -11));
}

function renderDownloadStatus(status) {
  const current = status.current;
  const percent = percentLabel(status.current_bytes, status.total_bytes);
  const searchArea = status.search_bbox
    ? `서 ${Number(status.search_bbox[0]).toFixed(3)}, 남 ${Number(status.search_bbox[1]).toFixed(3)}, 동 ${Number(status.search_bbox[2]).toFixed(3)}, 북 ${Number(status.search_bbox[3]).toFixed(3)}`
    : "제주 기본값";
  const searchPeriod = status.search_month_from && status.search_month_to
    ? `${status.search_month_from} ~ ${status.search_month_to}`
    : "-";
  els.downloadStartButton.disabled = Boolean(status.running);
  els.downloadStatus.innerHTML = `
    <div class="status-row">
      <span>상태</span>
      <strong>${escapeHtml(phaseLabel(status.phase))}</strong>
    </div>
    <div class="status-row">
      <span>선택됨</span>
      <strong>${status.selected || 0}</strong>
    </div>
    <div class="status-row">
      <span>다운로드 완료</span>
      <strong>${status.downloaded || 0}</strong>
    </div>
    <div class="status-row">
      <span>건너뜀</span>
      <strong>${status.skipped || 0}</strong>
    </div>
    <div class="status-row">
      <span>동시 다운로드</span>
      <strong>${status.parallel_downloads || 1}</strong>
    </div>
    <div class="status-row">
      <span>검색 영역</span>
      <strong>${escapeHtml(searchArea)}</strong>
    </div>
    <div class="status-row">
      <span>검색 월</span>
      <strong>${escapeHtml(searchPeriod)}</strong>
    </div>
    ${current ? `
      <div class="status-current">
        <span>${escapeHtml(current.name)}</span>
        <progress value="${status.current_bytes || 0}" max="${status.total_bytes || 1}"></progress>
        <strong>${percent || "작업 중"}</strong>
      </div>
    ` : ""}
    ${status.error ? `<p class="download-error">${escapeHtml(status.error)}</p>` : ""}
  `;
  els.downloadLogs.textContent = status.logs && status.logs.length
    ? status.logs.join("\n")
    : (status.message || "대기 중");
}

function visibleDownloadProducts() {
  return state.downloadProducts.filter((product) => {
    if (product.state === "downloaded" && els.hideDownloadedCheckbox.checked) return false;
    if (product.state === "excluded" && els.hideExcludedCheckbox.checked) return false;
    return true;
  });
}

function renderDownloadProducts(products = null) {
  if (products) state.downloadProducts = products;
  const visibleProducts = visibleDownloadProducts();
  els.downloadProductCount.textContent = `${visibleProducts.length}개 표시 / 전체 ${state.downloadProducts.length}개`;
  if (!visibleProducts.length) {
    els.downloadProductList.innerHTML = `<div class="empty-state">표시할 이미지가 없습니다</div>`;
    return;
  }
  els.downloadProductList.innerHTML = visibleProducts.map((product) => `
    <article class="download-product ${stateClass(product.state)}">
      <div>
        <h4>${escapeHtml(product.name)}</h4>
        <p>${escapeHtml(product.start || "-")} / ${escapeHtml(product.size || "-")}</p>
      </div>
      <span class="download-state ${stateClass(product.state)}">${stateLabel(product.state)}</span>
    </article>
  `).join("");
}

async function refreshDownloadStatus() {
  const status = await fetchJson("/api/download/status");
  renderDownloadStatus(status);
  if (!status.running && state.downloadPollId) {
    window.clearInterval(state.downloadPollId);
    state.downloadPollId = null;
    await loadInitialData();
  }
  return status;
}

function startDownloadPolling() {
  if (state.downloadPollId) return;
  state.downloadPollId = window.setInterval(() => {
    refreshDownloadStatus().catch(console.error);
  }, 2500);
}

async function openDownloadScreen() {
  if (!isAdmin()) return;
  els.downloadScreen.hidden = false;
  document.body.classList.add("download-open");
  setDefaultDownloadMonths();
  updateDownloadAreaLabel();
  await refreshDownloadStatus();
  startDownloadPolling();
  lucide.createIcons();
}

function closeDownloadScreen() {
  els.downloadScreen.hidden = true;
  document.body.classList.remove("download-open");
  if (state.downloadPollId) {
    window.clearInterval(state.downloadPollId);
    state.downloadPollId = null;
  }
  setTimeout(() => map.invalidateSize(), 80);
}

async function searchDownloadProducts(event) {
  event.preventDefault();
  setDefaultDownloadMonths();
  if (els.downloadFromInput.value > els.downloadToInput.value) {
    els.downloadProductList.innerHTML = `<div class="empty-state">시작 월은 종료 월보다 늦을 수 없습니다</div>`;
    return;
  }
  els.downloadProductList.innerHTML = `<div class="empty-state">이미지 검색 중</div>`;
  try {
    const params = new URLSearchParams({
      from: els.downloadFromInput.value,
      to: els.downloadToInput.value,
      top: els.downloadTopInput.value || "20",
    });
    if (state.downloadArea) params.set("bbox", bboxParam());
    const payload = await fetchJson(`/api/download/search?${params.toString()}`);
    renderDownloadProducts(payload.products || []);
  } catch (error) {
    els.downloadProductList.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

async function startDownloadJob() {
  setDefaultDownloadMonths();
  if (els.downloadFromInput.value > els.downloadToInput.value) {
    els.downloadLogs.textContent = "시작 월은 종료 월보다 늦을 수 없습니다";
    return;
  }
  const newCount = state.downloadProducts.filter((product) => product.state === "new").length;
  const message = [
    "현재 검색 결과의 신규 Sentinel-1 이미지를 모두 다운로드 대기열에 추가할까요?",
    "",
    `${newCount || "검색된"}개 신규 이미지가 대상입니다.`,
    "이미지는 한 번에 1개씩 순차적으로 다운로드됩니다.",
    "대용량 파일이므로 시간이 오래 걸리고 디스크 공간을 사용합니다.",
  ].join("\n");
  if (!window.confirm(message)) return;

  try {
    const status = await postJson("/api/download/start", {
      from: els.downloadFromInput.value,
      to: els.downloadToInput.value,
      top: Number(els.downloadTopInput.value || 20),
      bbox: state.downloadArea,
    });
    renderDownloadStatus(status);
    startDownloadPolling();
  } catch (error) {
    els.downloadLogs.textContent = error.message;
  }
}

function optionList(select, values, defaultLabel, formatter = (value) => value) {
  const selected = select.value;
  select.innerHTML = "";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = defaultLabel;
  select.appendChild(empty);

  Object.keys(values).sort().forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = `${formatter(value)} (${values[value]})`;
    select.appendChild(option);
  });
  select.value = selected;
}

function renderSummary(summary) {
  state.summary = summary;
  els.sceneCount.textContent = summary.scene_count;
  els.totalSize.textContent = summary.total_safe_size_label;
  els.errorCount.textContent = summary.error_count;
  els.excludedCount.textContent = summary.excluded_count || 0;
  els.dateRange.textContent = `${summary.date_min} ~ ${summary.date_max}`;
  optionList(els.familySelect, summary.families, "전체 위성군");
  optionList(els.missionSelect, summary.missions, "전체 미션");
  optionList(els.directionSelect, summary.orbit_directions, "전체 궤도", directionLabel);
}

function sceneById(id) {
  return state.scenes.find((scene) => scene.id === id);
}

function chip(text, className = "") {
  return `<span class="chip ${className}">${text}</span>`;
}

function directionClass(direction) {
  if (direction === "ASCENDING") return "asc";
  if (direction === "DESCENDING") return "desc";
  return "";
}

function renderSceneList() {
  els.filteredCount.textContent = state.aoiArea
    ? `관심지역 결과 ${state.scenes.length}개`
    : `${state.scenes.length}개 이미지`;
  els.sceneList.innerHTML = "";

  state.scenes.forEach((scene) => {
    const item = document.createElement("button");
    item.className = `scene-item${scene.id === state.selectedId ? " active" : ""}`;
    item.type = "button";
    item.dataset.sceneId = scene.id;
    item.innerHTML = `
      ${scene.preview.thumbnail_url ? `<img class="scene-thumb" src="${scene.preview.thumbnail_url}" alt="">` : ""}
      <div class="scene-title">${scene.name}</div>
      <div class="scene-meta">
        ${chip(scene.satellite_family)}
        ${chip(scene.mission)}
        ${chip(scene.date)}
        ${chip(directionLabel(scene.orbit_direction), directionClass(scene.orbit_direction))}
        ${chip(`상대궤도 ${scene.relative_orbit || "-"}`)}
        ${chip(scene.safe_size_label)}
        ${scene.status.preview ? chip("미리보기") : ""}
      </div>
    `;
    item.addEventListener("click", () => selectScene(scene.id, true));
    els.sceneList.appendChild(item);
  });
}

function popupHtml(properties) {
  return `
    <div class="popup-title">${properties.name}</div>
    <div class="popup-meta">
      ${properties.family} / ${properties.mission}<br>
      ${properties.date} / ${directionLabel(properties.orbit_direction)}<br>
      상대궤도 ${properties.relative_orbit || "-"} / ${properties.polarization}
    </div>
  `;
}

function renderMap(geojson) {
  state.layerById.clear();
  if (state.geoLayer) state.geoLayer.remove();

  state.geoLayer = L.geoJSON(geojson, {
    style: styleForFeature,
    onEachFeature: (feature, layer) => {
      state.layerById.set(feature.properties.id, layer);
      layer.bindPopup(popupHtml(feature.properties));
      layer.on("click", () => selectScene(feature.properties.id, false));
    },
  }).addTo(map);

  drawAoiAreaLayer();
  fitMapToLayers();
}

function updateMapStyles() {
  if (!state.geoLayer) return;
  state.geoLayer.eachLayer((layer) => {
    const feature = layer.feature;
    const selected = feature.properties.id === state.selectedId;
    layer.setStyle(styleForFeature(feature));
    if (selected) layer.bringToFront();
  });
}

function detailValue(label, value) {
  return `<dt>${label}</dt><dd>${value || "-"}</dd>`;
}

function productLabel(scene) {
  const level = scene.level ? (String(scene.level).startsWith("L") ? scene.level : `L${scene.level}`) : "";
  return [scene.mode, scene.product_type, level].filter(Boolean).join(" ");
}

function renderDetails(scene) {
  if (!scene) {
    els.detailPanel.innerHTML = `<div class="empty-state">지도 영역 또는 이미지를 선택하세요</div>`;
    return;
  }

  els.detailPanel.innerHTML = `
    <div class="detail-content">
      ${scene.preview.quicklook_url ? `
        <figure class="quicklook">
          <img src="${scene.preview.quicklook_url}" alt="${scene.name} 미리보기">
        </figure>
      ` : ""}
      <h2>${scene.name}</h2>
      <div class="scene-meta">
        ${chip(scene.satellite_family)}
        ${chip(scene.mission)}
        ${chip(directionLabel(scene.orbit_direction), directionClass(scene.orbit_direction))}
        ${chip(processingLabel(scene.status.processing))}
      </div>
      <div class="detail-actions">
        ${scene.status.zip || scene.status.safe ? `
          <a class="download-file-button" href="/api/scenes/${scene.id}/download">
            파일 다운로드
          </a>
        ` : `
          <button class="download-file-button disabled" type="button" disabled>
            다운로드 파일 없음
          </button>
        `}
        <button class="danger-button" id="excludeSceneButton" type="button">
          제외하고 휴지통으로 이동
        </button>
      </div>
      <dl class="detail-grid">
        ${detailValue("파일 상태", sceneFileStatus(scene))}
        ${detailValue("날짜", scene.date)}
        ${detailValue("시작", scene.start_time)}
        ${detailValue("종료", scene.stop_time)}
        ${detailValue("제품", productLabel(scene))}
        ${detailValue("편파", scene.polarization)}
        ${detailValue("상대궤도", scene.relative_orbit)}
        ${detailValue("절대궤도", scene.absolute_orbit)}
        ${detailValue("데이터테이크", scene.datatake)}
        ${detailValue("데이터 크기", scene.safe_size_label)}
        ${detailValue("압축파일 크기", scene.zip_size_label)}
        ${isAdmin() ? detailValue("저장 경로", scene.path) : ""}
      </dl>
    </div>
  `;

  document.getElementById("excludeSceneButton")?.addEventListener("click", () => excludeScene(scene));
}

async function excludeScene(scene) {
  const message = [
    "이 이미지를 목록에서 제외하고 관련 파일을 휴지통으로 이동합니다.",
    "휴지통의 파일은 보관기간이 지난 후 실제 삭제됩니다.",
    "",
    scene.name,
    "",
    "계속할까요?",
  ].join("\n");

  if (!window.confirm(message)) return;

  const response = await fetch(`/api/scenes/${scene.id}/exclude`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: "사용자가 뷰어에서 제외" }),
  });

  if (!response.ok) {
    const text = await response.text();
    window.alert(`이미지 제외 처리에 실패했습니다.\n${text}`);
    return;
  }

  const payload = await response.json();
  renderSummary(payload.summary);
  if (payload.delete_after) {
    window.alert(`휴지통으로 이동했습니다.\n실제 삭제 예정일: ${payload.delete_after}`);
  }
  state.selectedId = null;
  renderDetails(null);
  await loadScenes();
}

function selectScene(id, panToLayer) {
  if (state.areaSelecting) return;
  state.selectedId = id;
  const scene = sceneById(id);
  renderDetails(scene);
  renderSceneList();
  updateMapStyles();

  const layer = state.layerById.get(id);
  if (layer) {
    if (panToLayer) map.fitBounds(layer.getBounds(), { padding: [40, 40], maxZoom: 9 });
    layer.openPopup();
  }
}

function clearSelection() {
  state.selectedId = null;
  renderDetails(null);
  renderSceneList();
  updateMapStyles();
  map.closePopup();
}

function fitMapToLayers() {
  if (state.geoLayer && state.geoLayer.getLayers().length) {
    map.fitBounds(state.geoLayer.getBounds(), { padding: [28, 28] });
  } else if (state.aoiAreaLayer) {
    map.fitBounds(state.aoiAreaLayer.getBounds(), { padding: [28, 28] });
  }
}

async function loadScenes() {
  const [scenePayload, footprintPayload] = await Promise.all([
    fetchJson(apiUrl("/api/scenes")),
    fetchJson(apiUrl("/api/footprints")),
  ]);
  state.scenes = scenePayload.scenes;
  els.csvLink.href = apiUrl("/api/export.csv");
  renderSceneList();
  renderMap(footprintPayload);

  if (state.selectedId && !sceneById(state.selectedId)) {
    clearSelection();
  } else if (state.selectedId) {
    renderDetails(sceneById(state.selectedId));
    updateMapStyles();
  }
}

let filterTimer = null;
function scheduleLoad() {
  window.clearTimeout(filterTimer);
  filterTimer = window.setTimeout(loadScenes, 150);
}

async function init() {
  lucide.createIcons();
  document.body.classList.add("auth-locked");
  const user = await checkSession();
  if (!user) {
    showLogin();
    lucide.createIcons();
    return;
  }
  hideLogin(user);
  await loadInitialData();
  lucide.createIcons();
}

async function loadInitialData() {
  const payload = await fetchJson("/api/summary");
  renderSummary(payload.summary);
  await loadScenes();
}

els.loginForm.addEventListener("submit", login);
els.logoutButton.addEventListener("click", logout);
els.trashButton.addEventListener("click", openTrashScreen);
els.closeTrashButton.addEventListener("click", closeTrashScreen);
els.trashRefreshButton.addEventListener("click", loadTrash);
els.trashEmptyButton.addEventListener("click", emptyTrash);
els.trashList.addEventListener("click", async (event) => {
  const button = event.target.closest('[data-action="restore-trash"]');
  if (!button) return;
  const card = button.closest(".trash-card");
  try {
    await restoreTrashItem(card.dataset.sceneId);
  } catch (error) {
    window.alert(`복구에 실패했습니다.\n${error.message}`);
  }
});
els.userManageButton.addEventListener("click", openUserScreen);
els.closeUserButton.addEventListener("click", closeUserScreen);
els.userCreateForm.addEventListener("submit", createUser);
els.userRefreshButton.addEventListener("click", loadUsers);
els.userList.addEventListener("click", async (event) => {
  const button = event.target.closest('[data-action="save-user"]');
  if (!button) return;
  const card = button.closest(".user-card");
  els.userError.textContent = "";
  try {
    await saveUser(card);
  } catch (error) {
    els.userError.textContent = error.message;
  }
});
els.downloadButton.addEventListener("click", openDownloadScreen);
els.closeDownloadButton.addEventListener("click", closeDownloadScreen);
els.downloadSearchForm.addEventListener("submit", searchDownloadProducts);
els.downloadSelectAreaButton.addEventListener("click", () => beginAreaSelection("download"));
els.downloadClearAreaButton.addEventListener("click", clearDownloadArea);
els.hideDownloadedCheckbox.addEventListener("change", () => renderDownloadProducts());
els.hideExcludedCheckbox.addEventListener("change", () => renderDownloadProducts());
els.downloadRefreshButton.addEventListener("click", refreshDownloadStatus);
els.downloadStartButton.addEventListener("click", startDownloadJob);
els.queryInput.addEventListener("input", scheduleLoad);
els.familySelect.addEventListener("change", scheduleLoad);
els.missionSelect.addEventListener("change", scheduleLoad);
els.directionSelect.addEventListener("change", scheduleLoad);
els.dateFromInput.addEventListener("change", scheduleLoad);
els.dateToInput.addEventListener("change", scheduleLoad);
els.aoiSelectButton.addEventListener("click", () => beginAreaSelection("filter"));
els.aoiClearButton.addEventListener("click", () => clearAoiArea().catch(console.error));
els.fitButton.addEventListener("click", fitMapToLayers);
els.selectAreaButton.addEventListener("click", () => beginAreaSelection("filter"));
els.cancelAreaSelectionButton.addEventListener("click", () => cancelAreaSelection());
els.clearSelectionButton.addEventListener("click", clearSelection);
els.toggleListButton.addEventListener("click", () => {
  document.body.classList.toggle("list-collapsed");
  setTimeout(() => map.invalidateSize(), 80);
});

els.rescanButton.addEventListener("click", async () => {
  if (!isAdmin()) return;
  const payload = await fetchJson("/api/rescan");
  renderSummary(payload.summary);
  await loadScenes();
});

map.on("mousedown", areaMouseDown);
map.on("mousemove", areaMouseMove);
map.on("mouseup", areaMouseUp);

init().catch((error) => {
  console.error(error);
  els.sceneList.innerHTML = `<div class="empty-state">${error.message}</div>`;
});
