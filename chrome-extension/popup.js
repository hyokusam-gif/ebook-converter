// 팝업: 단일 저장 / 연속 저장 시작·중지 / 상태 표시
const $ = (id) => document.getElementById(id);

async function send(msg) {
  try {
    return await chrome.runtime.sendMessage(msg);
  } catch (e) {
    return { error: String(e && e.message || e) };
  }
}

// 팝업이 열린 창의 현재 탭 — 백그라운드가 추측하지 않도록 tabId를 직접 넘긴다.
async function currentTabId() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    return tab ? tab.id : undefined;
  } catch {
    return undefined;
  }
}

function phaseText(b) {
  switch (b.phase) {
    case "saving": return "저장 중";
    case "navigating": return "다음화로 이동 중";
    case "waiting-challenge": return "보안 확인 페이지 — 크롬 창에서 직접 통과해 주세요";
    case "retry-reload": return "그림이 안 보여 새로고침 중";
    case "waiting-images": return "그림이 로드되지 않음 — 크롬 창에서 그림이 보이게 해 주세요 (최대 3분)";
    case "loaded": return "잠시 대기";
    default: return "";
  }
}

async function refresh() {
  const s = await send({ type: "status" });
  if (!s || s.error) {
    $("status").textContent = (s && s.error) || "상태를 가져오지 못했습니다.";
    $("status").className = "err";
    return;
  }
  const b = s.batch;
  const running = Boolean(b && b.active);
  $("start").hidden = running;
  $("stop").hidden = !running;
  $("max").disabled = running;
  $("saveOne").disabled = running;
  if (!$("max").dataset.touched && s.max) $("max").value = s.max;

  let text;
  let cls = "";
  if (running) {
    text = `${b.done}/${b.max}화 저장됨 (시작 페이지 포함) · ${phaseText(b)}`;
    if (b.lastTitle) text += `\n${b.lastTitle}`;
    if (b.lastTiming) text += `\n최근 회차: ${b.lastTiming}`;
    cls = (b.phase === "waiting-challenge" || b.phase === "waiting-images") ? "err" : "run";
  } else if (s.last) {
    text = s.last.text;
    cls = s.last.ok ? "" : "err";
  } else {
    text = "대기 중";
  }
  $("status").textContent = text;
  $("status").className = cls;
}

$("saveOne").addEventListener("click", async () => {
  const r = await send({ type: "save-one", tabId: await currentTabId() });
  if (r && r.error) {
    $("status").textContent = r.error;
    $("status").className = "err";
  } else {
    $("status").textContent = "저장 중… (아이콘 배지가 ✓로 바뀌면 완료)";
    $("status").className = "run";
  }
});

$("start").addEventListener("click", async () => {
  const max = parseInt($("max").value, 10) || 20;
  const r = await send({ type: "batch-start", max, tabId: await currentTabId() });
  if (r && r.error) {
    $("status").textContent = r.error;
    $("status").className = "err";
  }
  refresh();
});

$("stop").addEventListener("click", async () => {
  await send({ type: "batch-stop" });
  refresh();
});

function updateRangeHint() {
  const n = parseInt($("max").value, 10) || 0;
  $("rangeHint").textContent = n <= 1
    ? "현재 페이지 한 화만 저장합니다."
    : `현재 페이지를 1화째로 세고, "다음화"를 따라가며 ${n}화까지 저장합니다.`;
}

async function showCurrentTab() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    $("current").textContent = tab && tab.title ? `현재: ${tab.title}` : "현재 페이지를 알 수 없습니다.";
    $("current").title = tab && tab.title ? tab.title : "";
  } catch {
    $("current").textContent = "현재 페이지를 알 수 없습니다.";
  }
}

$("max").addEventListener("input", () => { $("max").dataset.touched = "1"; updateRangeHint(); });
// 단축키로 열었을 때 숫자만 치고 Enter → 바로 연속 저장 시작
$("max").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !$("start").hidden) {
    e.preventDefault();
    $("start").click();
  }
});

showCurrentTab();
refresh().then(() => {
  updateRangeHint();
  if (!$("max").disabled) {
    $("max").focus();
    $("max").select();
  }
});
setInterval(refresh, 1000);
