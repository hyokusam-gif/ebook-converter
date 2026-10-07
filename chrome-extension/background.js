// webtoon2pdf 페이지 저장 확장 — 서비스 워커
//
// 단일 저장: 팝업 [이 페이지 저장] 또는 단축키(Alt+Shift+S) → 끝까지 스크롤(지연 로딩 이미지 로드) →
//   탭을 MHTML로 캡처 → 다운로드\webtoon2pdf\<제목>_<시각>.mhtml
// 연속 저장: 팝업 [연속 저장 시작] → 저장 → 다음화 링크/버튼 탐지 → 이동 → 로드 대기 → 저장 … 을
//   최대 N화까지 반복. 파일명은 B<시각>_<번호>_<제목>.mhtml (같은 배치는 접두가 같고 번호순 정렬됨).
//   상태는 chrome.storage.session에 두어 서비스 워커가 중간에 꺼져도 이어간다.
// 확장 단축키는 브라우저가 처리하므로 페이지 스크립트가 Ctrl+S를 막아도 영향이 없다.

const SAVE_DIR = "webtoon2pdf";
const SCROLL_STEP_WAIT_S = 0.12;    // 스크롤 스텝 간격 (지연 로딩은 eager 전환으로 한꺼번에 받으므로 빠르게 훑기만 한다)
const SCROLL_MAX_STEPS = 400;
const IMAGE_WAIT_S = 20;            // 한 라운드에서 이미지 로드를 기다리는 최대 시간
const SCROLL_ROUNDS = 3;            // 대기 라운드 최대 횟수 (늦게 생기는 지연 로딩 이미지 대비)
const SCROLL_TOTAL_S = 60;          // 스크롤+대기 전체 상한
const IMAGE_GRACE_S = 8;            // 첫 라운드 뒤, 뷰어가 뒤늦게 <img>를 넣는지 지켜보는 최대 시간
const IMAGE_QUIET_S = 1.5;          // 이 시간 동안 DOM에 img 추가/src 변경이 없으면 바로 저장
const IMAGES_WAIT_MS = 180000;      // 그림이 안 뜰 때 사용자가 해결할 때까지 기다리는 최대 시간
const IMAGES_POLL_MS = 3000;
const DOWNLOAD_TIMEOUT_MS = 120000;

// 그림이 로드되지 않은 페이지 → 저장하지 않는다 (광고만 담긴 파일 방지)
class NoImagesError extends Error {}

const NAV_SETTLE_MS = 1200;        // 로드 완료/URL 변경 후 이만큼 잠잠하면 이동 완료로 본다
const CLICK_NAV_TIMEOUT_MS = 10000; // 버튼을 눌렀는데 URL이 안 바뀌면 중단
const NAV_WATCHDOG_MS = 60000;      // 이동이 이 시간 안에 안 끝나면 중단
const POLITE_DELAY_MS = 1000;       // 회차 사이 대기 (사이트 부담 완화)
const CHALLENGE_WAIT_MS = 120000;   // 보안 확인을 사용자가 직접 통과할 때까지 기다리는 최대 시간
const CHALLENGE_POLL_MS = 2000;
const DEFAULT_MAX = 20;

// webtoon2pdf/navigator.py NEXT_TEXTS 와 동일
const NEXT_TEXTS = [
  "다음화", "다음 화", "다음편", "다음 편", "다음",
  "next", "next chapter", "next episode", "→", ">", "»", "▶",
];
// webtoon2pdf/browser.py CHALLENGE_TITLES / CHALLENGE_TEXTS 와 동일
const CHALLENGE_TITLES = ["just a moment", "attention required", "access denied", "보안 확인"];
const CHALLENGE_TEXTS = ["checking your browser", "verify you are human", "보안 확인", "봇이 아님"];

let singleBusy = false;
let settleTimer = null;

// ---------- 이벤트 (모두 최상위에 등록: 서비스 워커가 다시 깨어나도 동작) ----------

chrome.commands.onCommand.addListener((command) => {
  if (command === "save-page") saveOneFromActiveTab().catch(logErr);
  else if (command === "batch-toggle") toggleBatch().catch(logErr);
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  handleMessage(msg).then(sendResponse, (e) => sendResponse({ error: String(e && e.message || e) }));
  return true; // 비동기 응답
});

chrome.tabs.onUpdated.addListener((tabId, info) => {
  onTabUpdated(tabId, info).catch(logErr);
});

chrome.tabs.onRemoved.addListener((tabId) => {
  getBatch().then((b) => {
    if (b && b.active && b.tabId === tabId) return finish("탭이 닫혀서 중단", false);
  }).catch(logErr);
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "nav-watchdog") navWatchdog().catch(logErr);
});

function logErr(e) {
  console.error(e);
}

// ---------- 메시지 처리 (팝업) ----------

async function handleMessage(msg) {
  switch (msg && msg.type) {
    case "save-one":
      return saveOneFromActiveTab(msg.tabId);
    case "batch-start": {
      const tab = await activeTab(msg.tabId);
      if (!tab) return { error: "활성 탭을 찾지 못했습니다." };
      const max = Math.max(1, Math.min(999, parseInt(msg.max, 10) || DEFAULT_MAX));
      await chrome.storage.local.set({ max });
      return startBatch(tab, max);
    }
    case "batch-stop":
      await finish("사용자가 중지", false);
      return { ok: true };
    case "status": {
      const { last } = await chrome.storage.session.get("last");
      const { max } = await chrome.storage.local.get("max");
      return { batch: await getBatch(), last: last || null, max: max || DEFAULT_MAX };
    }
    default:
      return { error: "알 수 없는 요청" };
  }
}

async function toggleBatch() {
  const b = await getBatch();
  if (b && b.active) return finish("사용자가 중지", false);
  const tab = await activeTab();
  if (!tab) return;
  const { max } = await chrome.storage.local.get("max");
  return startBatch(tab, max || DEFAULT_MAX);
}

// ---------- 상태 ----------

async function getBatch() {
  const { batch } = await chrome.storage.session.get("batch");
  return batch || null;
}

async function setBatch(batch) {
  await chrome.storage.session.set({ batch });
}

async function updateBatch(patch) {
  const b = await getBatch();
  if (!b) return null;
  Object.assign(b, patch);
  await setBatch(b);
  return b;
}

async function setLast(text, ok) {
  await chrome.storage.session.set({ last: { text, ok, at: Date.now() } });
}

// ---------- 단일 저장 ----------

async function saveOneFromActiveTab(tabId) {
  const tab = await activeTab(tabId);
  if (!tab) return { error: "활성 탭을 찾지 못했습니다." };
  const b = await getBatch();
  if (b && b.active && b.tabId === tab.id) return { error: "이 탭에서 연속 저장이 진행 중입니다." };
  if (!isWebUrl(tab.url)) {
    await badge("!", "#c62828", 3000);
    return { error: "웹 페이지가 아니라 저장할 수 없습니다." };
  }
  if (singleBusy) return { error: "이미 저장 중입니다." };
  singleBusy = true;
  // 팝업이 닫혀도 계속되도록 기다리지 않고 진행
  (async () => {
    try {
      await badge("…", "#757575");
      const filename = `${SAVE_DIR}/${safeName(tab.title)}_${stamp()}.mhtml`;
      const cap = await captureTab(tab, filename);
      await setLast(`저장됨: ${basename(filename)} (${mb(cap.size)} MB, ${cap.timing})`, true);
      await badge("✓", "#2e7d32", 2500);
    } catch (e) {
      if (e instanceof NoImagesError) {
        console.warn("저장 안 함:", e.message);
        await setLast(`이미지가 아직 안 보여 저장하지 않았습니다. 그림이 다 뜬 뒤 다시 눌러 주세요. (${e.message})`, false);
      } else {
        console.error("저장 실패:", e);
        await setLast(`저장 실패: ${e.message || e}`, false);
      }
      await badge("!", "#c62828", 4000);
    } finally {
      singleBusy = false;
    }
  })();
  return { ok: true };
}

// ---------- 연속 저장 ----------

async function startBatch(tab, max) {
  const b = await getBatch();
  if (b && b.active) return { error: "이미 연속 저장이 진행 중입니다." };
  if (!isWebUrl(tab.url)) return { error: "웹 페이지가 아니라 저장할 수 없습니다." };
  await setBatch({
    active: true, tabId: tab.id, batchId: "B" + stamp(), max, done: 0, visited: [],
    phase: "saving", lastTitle: tab.title || "", lastFile: "", navFrom: "", startedAt: Date.now(),
  });
  await setLast(`연속 저장 시작: 이 페이지부터 ${max}화 (현재 페이지 포함)`, true);
  step().catch(async (e) => { console.error(e); await finish(`오류: ${e.message || e}`, false); });
  return { ok: true };
}

async function step() {
  let b = await getBatch();
  if (!b || !b.active) return;
  let tab;
  try { tab = await chrome.tabs.get(b.tabId); } catch { return finish("탭이 닫혀서 중단", false); }
  if (!isWebUrl(tab.url)) return finish("웹 페이지가 아니라서 중단", false);
  const url = stripHash(tab.url);
  if (b.visited.includes(url)) return finish(`완료: ${b.done}화 저장 (이미 저장한 회차로 되돌아옴)`, true);

  await updateBatch({ phase: "saving", lastTitle: tab.title || "" });
  await badge(`${b.done + 1}/${b.max}`, "#1565c0");
  const seq = String(b.done + 1).padStart(3, "0");
  const filename = `${SAVE_DIR}/${b.batchId}_${seq}_${safeName(tab.title)}.mhtml`;
  let cap;
  try {
    cap = await captureTab(tab, filename);
  } catch (e) {
    if (e instanceof NoImagesError) return handleNoImages(b, tab, url, e);
    return finish(`저장 실패(${b.done + 1}번째): ${e.message || e}`, false);
  }

  b = await getBatch();
  if (!b || !b.active) return; // 저장 중에 사용자가 중지
  b.done += 1;
  b.visited.push(url);
  b.lastFile = filename;
  b.lastTiming = cap.timing;
  b.totalWaitS = Math.round(((b.totalWaitS || 0) + cap.waitS + cap.captureS) * 10) / 10;
  await setBatch(b);
  await badge(`${b.done}/${b.max}`, "#1565c0");
  if (b.done >= b.max) return finish(`완료: ${b.done}화 저장`, true);

  // 다음화 찾기
  let res;
  try {
    const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: findNext, args: [NEXT_TEXTS] });
    res = r && r.result;
  } catch (e) {
    return finish(`완료: ${b.done}화 저장 (다음화 탐색 실패: ${e.message || e})`, false);
  }
  if (!res) return finish(`완료: ${b.done}화 저장 (다음화 링크 없음)`, true);

  if (res.href) {
    const next = acceptUrl(url, res.href);
    if (!next) return finish(`완료: ${b.done}화 저장 (다음 링크가 같은 페이지이거나 다른 사이트)`, true);
    if (b.visited.includes(next)) return finish(`완료: ${b.done}화 저장 (다음 링크가 이미 저장한 회차)`, true);
    await updateBatch({ phase: "navigating", navFrom: url, navStarted: Date.now() });
    await chrome.alarms.create("nav-watchdog", { when: Date.now() + NAV_WATCHDOG_MS });
    await chrome.tabs.update(tab.id, { url: next });
  } else if (res.click) {
    await updateBatch({ phase: "navigating", navFrom: url, navStarted: Date.now() });
    await chrome.alarms.create("nav-watchdog", { when: Date.now() + NAV_WATCHDOG_MS });
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: clickSelector, args: [res.click] });
    } catch (e) {
      return finish(`완료: ${b.done}화 저장 (다음화 버튼 클릭 실패)`, false);
    }
    setTimeout(() => checkClickNavigated().catch(logErr), CLICK_NAV_TIMEOUT_MS);
  } else {
    return finish(`완료: ${b.done}화 저장 (다음화 링크 없음)`, true);
  }
}

// 그림이 로드되지 않은 회차: 처음이면 새로고침 한 번, 그래도 안 되면 사용자가 그림이 보이게 할 때까지 기다린다.
async function handleNoImages(b, tab, url, err) {
  const seq = b.done + 1;
  if (b.reloadedUrl !== url) {
    console.log("이미지 미로드 → 새로고침:", url, err.message);
    await updateBatch({ phase: "retry-reload", reloadedUrl: url, navFrom: url, navStarted: Date.now() });
    await setLast(`${seq}번째 회차 그림이 안 보여 새로고침합니다 (${err.message})`, true);
    await chrome.alarms.create("nav-watchdog", { when: Date.now() + NAV_WATCHDOG_MS });
    await chrome.tabs.reload(tab.id);
    return;
  }
  let failBig = 0;
  try {
    const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: probePage });
    failBig = (r && r.result && r.result.bigLoaded) || 0;
  } catch { /* 무시 */ }
  await updateBatch({ phase: "waiting-images" });
  await badge("!", "#c62828");
  await setLast(
    `${seq}번째 회차 이미지가 로드되지 않았습니다 (사이트가 광고 확인 등을 요구할 수 있음). ` +
    "크롬 창에서 이 회차 그림이 보이게 하면 자동으로 이어갑니다 (최대 3분)", false);
  const deadline = Date.now() + IMAGES_WAIT_MS;
  while (Date.now() < deadline) {
    await sleep(IMAGES_POLL_MS);
    const cur = await getBatch();
    if (!cur || !cur.active || cur.phase !== "waiting-images") return;
    let probe = null;
    try {
      const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: probePage });
      probe = r && r.result;
    } catch { /* 페이지 이동 중 */ }
    if (probe && probe.pending === 0 && probe.bigLoaded > failBig) {
      await updateBatch({ phase: "loaded" });
      await badge(`${seq}/${cur.max}`, "#1565c0");
      return step();
    }
  }
  return finish(`중단: ${b.done}화 저장 후 ${seq}번째 회차 이미지가 3분 안에 로드되지 않음`, false);
}

async function afterReload() {
  const b = await getBatch();
  if (!b || !b.active || b.phase !== "retry-reload") return;
  await chrome.alarms.clear("nav-watchdog");
  await updateBatch({ phase: "loaded" });
  await sleep(POLITE_DELAY_MS);
  const cur = await getBatch();
  if (!cur || !cur.active) return;
  return step();
}

async function onTabUpdated(tabId, info) {
  const b = await getBatch();
  if (!b || !b.active || b.tabId !== tabId) return;
  if (b.phase === "navigating") {
    if (info.status === "complete" || info.url) {
      clearTimeout(settleTimer);
      settleTimer = setTimeout(() => afterNavigate().catch(logErr), NAV_SETTLE_MS);
    }
  } else if (b.phase === "retry-reload") {
    if (info.status === "complete") {
      clearTimeout(settleTimer);
      settleTimer = setTimeout(() => afterReload().catch(logErr), NAV_SETTLE_MS);
    }
  }
}

async function checkClickNavigated() {
  const b = await getBatch();
  if (!b || !b.active || b.phase !== "navigating") return;
  let tab;
  try { tab = await chrome.tabs.get(b.tabId); } catch { return; }
  if (stripHash(tab.url) === b.navFrom) {
    return finish(`완료: ${b.done}화 저장 (다음화 버튼을 눌렀지만 페이지가 바뀌지 않음)`, true);
  }
}

async function navWatchdog() {
  const b = await getBatch();
  if (!b || !b.active) return;
  if (b.phase === "retry-reload") return afterReload(); // 새로고침 완료 이벤트를 놓친 경우
  if (b.phase !== "navigating") return;
  let tab;
  try { tab = await chrome.tabs.get(b.tabId); } catch { return finish("탭이 닫혀서 중단", false); }
  if (stripHash(tab.url) !== b.navFrom) return afterNavigate();
  return finish(`중단: ${b.done}화 저장 후 다음 페이지 이동이 60초 안에 끝나지 않음`, false);
}

async function afterNavigate() {
  const b = await getBatch();
  if (!b || !b.active || b.phase !== "navigating") return;
  let tab;
  try { tab = await chrome.tabs.get(b.tabId); } catch { return finish("탭이 닫혀서 중단", false); }
  const url = stripHash(tab.url);
  if (url === b.navFrom) return; // 아직 이동 전 (클릭 타임아웃/워치독이 처리)
  await chrome.alarms.clear("nav-watchdog");
  await updateBatch({ phase: "loaded", lastTitle: tab.title || "" });

  // 보안 확인 페이지면 사용자가 직접 통과할 때까지 기다린다 (자동 우회하지 않음)
  const deadline = Date.now() + CHALLENGE_WAIT_MS;
  let noticed = false;
  while (await isChallengePage(tab.id)) {
    if (!noticed) {
      noticed = true;
      await updateBatch({ phase: "waiting-challenge" });
      await badge("!", "#c62828");
      await setLast("보안 확인 페이지입니다. 크롬 창에서 직접 통과하면 이어서 진행합니다 (최대 120초)", false);
    }
    if (Date.now() > deadline) return finish(`중단: ${b.done}화 저장 후 보안 확인이 120초 안에 끝나지 않음`, false);
    await sleep(CHALLENGE_POLL_MS);
    const cur = await getBatch();
    if (!cur || !cur.active) return;
  }
  if (noticed) await updateBatch({ phase: "loaded" });

  await sleep(POLITE_DELAY_MS);
  const cur = await getBatch();
  if (!cur || !cur.active) return;
  return step();
}

async function finish(reason, ok) {
  clearTimeout(settleTimer);
  await chrome.alarms.clear("nav-watchdog");
  const b = await getBatch();
  if (b) {
    b.active = false;
    b.phase = "done";
    await setBatch(b);
    if (b.done > 0 && b.totalWaitS) {
      reason += ` · 회차당 평균 ${Math.round(b.totalWaitS / b.done)}초 (마지막: ${b.lastTiming || "-"})`;
    }
  }
  await setLast(reason, ok);
  await badge(ok ? "✓" : "!", ok ? "#2e7d32" : "#c62828", 4000);
  console.log("연속 저장 종료:", reason);
  return { ok: true };
}

// ---------- 캡처 ----------

// 스크롤 → MHTML 캡처 → 다운로드 완료 대기. 저장된 바이트 수를 돌려준다.
async function captureTab(tab, filename) {
  // 캔버스 뷰어(이미지를 <img> 대신 <canvas>에 그리는 사이트)는 페이지 저장에 그림이 안 담긴다.
  // 그런 페이지면 원본 이미지 주소 목록을 찾아 직접 받아서 MHTML을 조립한다.
  // 스크롤보다 먼저 검사한다: 끝까지 스크롤하면 다음화로 자동 이동하는 뷰어가 있어서, 캔버스 뷰어는 스크롤하지 않는다.
  const t0 = Date.now();
  let rounds = 0;
  let blob = null;
  let probe = null;
  try {
    const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: probePage });
    probe = r && r.result;
  } catch (e) {
    console.warn("페이지 검사 생략:", e);
  }
  if (probe && probe.canvases >= 3 && probe.canvases > probe.bigImages) {
    console.log("캔버스 뷰어 감지:", probe);
    blob = await buildSyntheticMhtml(tab);
    if (!blob) console.warn("원본 이미지 목록을 찾지 못해 일반 페이지 저장으로 진행합니다.");
  }
  if (!blob) {
    let scroll = null;
    try {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: scrollAndWaitImages,
        args: [SCROLL_STEP_WAIT_S, SCROLL_MAX_STEPS, IMAGE_WAIT_S, SCROLL_ROUNDS, SCROLL_TOTAL_S, IMAGE_GRACE_S, IMAGE_QUIET_S],
      });
      scroll = r && r.result;
      rounds = (scroll && scroll.rounds) || 0;
      console.log("스크롤 완료:", scroll);
    } catch (e) {
      console.warn("스크롤 생략:", e);
    }
    // 아직 로드 중인 그림이 남았거나 큰 그림이 하나도 없으면 광고만 담긴 파일이 되므로 저장하지 않는다
    if (scroll && (scroll.pending > 0 || scroll.bigLoaded === 0)) {
      throw new NoImagesError(`img ${scroll.total}개 중 로드된 큰 그림 ${scroll.bigLoaded}장, 로드 안 됨 ${scroll.pending}장, ${scroll.seconds}초 대기`);
    }
    blob = await call(chrome.pageCapture.saveAsMHTML, { tabId: tab.id });
  }
  if (!blob) throw new Error("pageCapture가 빈 결과를 돌려줬습니다.");
  const t1 = Date.now();
  // 서비스 워커에는 URL.createObjectURL이 없으므로 data: URL로 변환해 내려받는다.
  const url = await blobToDataUrl(blob);
  const id = await call(chrome.downloads.download, { url, filename, conflictAction: "uniquify", saveAs: false });
  await waitDownload(id);
  const waitS = Math.round((t1 - t0) / 100) / 10;
  const captureS = Math.round((Date.now() - t1) / 100) / 10;
  const timing = `그림 대기 ${waitS}초·저장 ${captureS}초`;
  console.log("저장됨:", filename, `(${mb(blob.size)} MB, ${timing}, 라운드 ${rounds})`);
  return { size: blob.size, waitS, captureS, rounds, timing };
}

// ---------- 캔버스 뷰어용: 원본 이미지를 직접 받아 MHTML 조립 ----------

const FETCH_GAP_MS = 150;   // 이미지 요청 간격
const FETCH_CHUNK = 5;      // 한 번의 executeScript로 가져올 장수 (결과 크기 제한 대비)

// 성공하면 Blob(MHTML), 원본 목록을 못 찾으면 null
async function buildSyntheticMhtml(tab) {
  // 1) 페이지 스크립트의 전역/인라인에서 이미지 주소 배열 찾기 (페이지 세계에서 실행해야 전역이 보인다)
  let urls = [];
  try {
    const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: "MAIN", func: findImageUrlList });
    urls = (r && r.result) || [];
  } catch (e) {
    console.warn("이미지 목록 탐색 실패:", e);
  }
  let images = [];
  if (urls.length >= 3) {
    console.log(`원본 이미지 목록 ${urls.length}장 발견, 받는 중…`);
    for (let i = 0; i < urls.length; i += FETCH_CHUNK) {
      const chunk = urls.slice(i, i + FETCH_CHUNK);
      const [r] = await chrome.scripting.executeScript({
        target: { tabId: tab.id }, func: fetchImagesAsBase64, args: [chunk, FETCH_GAP_MS],
      });
      for (const im of (r && r.result) || []) if (im) images.push(im);
      await badge(`${Math.min(i + FETCH_CHUNK, urls.length)}/${urls.length}`, "#6a1b9a");
    }
  } else {
    // 2) 목록이 없으면 캔버스 자체를 내보내 본다 (다른 도메인 그림이면 보안 제한으로 실패할 수 있음)
    try {
      const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: exportCanvases });
      images = (r && r.result) || [];
    } catch (e) {
      console.warn("캔버스 내보내기 실패:", e);
    }
  }
  if (images.length < 3) {
    console.warn(`받은 이미지가 ${images.length}장뿐이라 조립하지 않습니다.`);
    return null;
  }
  console.log(`이미지 ${images.length}장으로 MHTML 조립`);
  return buildMhtml({ title: tab.title || "page", url: tab.url, images });
}

// 크롬 '웹페이지, 단일 파일'과 같은 구조의 MHTML을 만든다. (webtoon2pdf/mhtml.py가 읽는 형식)
function buildMhtml({ title, url, images }) {
  const B = "----MultipartBoundary--webtoon2pdf";
  const CRLF = "\r\n";
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  const html =
    `<!DOCTYPE html><html><head><meta charset="utf-8"><title>${esc(title)}</title></head><body>` +
    images.map((im) => `<img src="${esc(im.url)}">`).join(CRLF) +
    "</body></html>";
  let s =
    "From: <Saved by webtoon2pdf extension>" + CRLF +
    `Subject: =?utf-8?B?${utf8ToBase64(title)}?=` + CRLF +
    `Snapshot-Content-Location: ${url}` + CRLF +
    "MIME-Version: 1.0" + CRLF +
    `Content-Type: multipart/related;${CRLF}\ttype="text/html";${CRLF}\tboundary="${B}"` + CRLF + CRLF;
  s += `--${B}` + CRLF + "Content-Type: text/html" + CRLF + "Content-Transfer-Encoding: base64" + CRLF +
    `Content-Location: ${url}` + CRLF + CRLF + wrap76(utf8ToBase64(html)) + CRLF + CRLF;
  for (const im of images) {
    s += `--${B}` + CRLF + `Content-Type: ${im.type || "image/jpeg"}` + CRLF + "Content-Transfer-Encoding: base64" + CRLF +
      `Content-Location: ${im.url}` + CRLF + CRLF + wrap76(im.b64) + CRLF + CRLF;
  }
  s += `--${B}--` + CRLF;
  return new Blob([s], { type: "multipart/related" });
}

function utf8ToBase64(s) {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

function wrap76(b64) {
  return b64.replace(/(.{76})/g, "$1\r\n");
}

// ---------- 페이지 안에서 실행되는 함수들 (자체 완결이어야 함 — 바깥 변수 참조 불가) ----------

// 큰 <img> 수와 큰 <canvas> 수. 캔버스가 많고 <img>가 없으면 캔버스 뷰어.
function probePage() {
  const imgs = Array.from(document.images);
  const bigImages = imgs.filter((im) => im.naturalWidth >= 300 && im.getBoundingClientRect().width > 0).length;
  const bigLoaded = imgs.filter((im) => im.complete && im.naturalWidth >= 300).length;
  const pending = imgs.filter((im) => im.src && !im.complete && im.getBoundingClientRect().width >= 100).length;
  const canvases = Array.from(document.querySelectorAll("canvas")).filter((c) => c.width >= 300).length;
  return { bigImages, bigLoaded, pending, canvases };
}

// 페이지 전역 변수(예: img_list)나 인라인 스크립트의 배열 리터럴에서 이미지 주소 목록을 찾는다.
// 여러 개면 가장 긴 것(같으면 먼저 나온 것). 절대 주소로 돌려준다. (world: MAIN 에서 실행)
function findImageUrlList() {
  const isImg = (s) => typeof s === "string" && /^(https?:)?\/\/[^\s"']+\.(jpe?g|png|webp|gif|avif)(\?[^\s"']*)?$/i.test(s.trim());
  const cands = [];
  for (const k of Object.keys(window)) {
    let v;
    try { v = window[k]; } catch { continue; }
    if (Array.isArray(v) && v.length >= 3 && v.every(isImg)) cands.push(v);
  }
  if (!cands.length) {
    const text = Array.from(document.scripts).filter((s) => !s.src).map((s) => s.textContent).join("\n");
    const re = /\[\s*(?:"(?:https?:)?\/\/[^"]+?\.(?:jpe?g|png|webp|gif|avif)(?:\?[^"]*)?"\s*,?\s*){3,}\]/gi;
    for (const m of text.match(re) || []) {
      try {
        const arr = JSON.parse(m);
        if (arr.every(isImg)) cands.push(arr);
      } catch { /* 배열 리터럴이 아님 */ }
    }
  }
  if (!cands.length) return [];
  cands.sort((a, b) => b.length - a.length);
  return cands[0].map((u) => { try { return new URL(u.trim(), location.href).href; } catch { return null; } }).filter(Boolean);
}

// 페이지 안에서 이미지를 받아 base64로. (이미지 서버가 CORS를 허용해야 읽을 수 있다 → 실패는 null)
async function fetchImagesAsBase64(urls, gapMs) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = [];
  for (const url of urls) {
    try {
      const r = await fetch(url);
      if (!r.ok) { out.push(null); continue; }
      const blob = await r.blob();
      const b64 = await new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(String(fr.result).split(",")[1]);
        fr.onerror = () => rej(fr.error);
        fr.readAsDataURL(blob);
      });
      out.push({ url, type: blob.type || "image/jpeg", b64 });
    } catch (e) {
      out.push(null);
    }
    await sleep(gapMs);
  }
  return out;
}

// 캔버스를 JPEG로 내보낸다. 다른 도메인 그림이 그려진(tainted) 캔버스는 건너뛴다.
function exportCanvases() {
  const out = [];
  let n = 0;
  for (const c of document.querySelectorAll("canvas")) {
    if (c.width < 300) continue;
    n += 1;
    try {
      const d = c.toDataURL("image/jpeg", 0.92);
      out.push({ url: `canvas:${n}`, type: "image/jpeg", b64: d.split(",")[1] });
    } catch { /* tainted */ }
  }
  return out;
}

// 그림이 전부 로드될 때까지 기다린다 (webtoon2pdf/browser.py 의 scroll_to_bottom + wait_images 를 빠르게 만든 것).
// 1) loading="lazy" 인 <img>는 eager로 바꿔 스크롤 없이 한꺼번에(병렬로) 받게 한다.
// 2) IntersectionObserver 방식 지연 로딩을 위해 끝까지 한 번 빠르게 훑는다.
// 3) 화면에 자리가 있는 <img>가 전부 로드될 때까지 대기. 첫 라운드 뒤엔 뷰어가 뒤늦게 <img>를 넣는지
//    MutationObserver로 지켜보되, quietS 동안 조용하면 바로 끝낸다. 새 <img>가 생겼을 때만 다시 훑는다.
async function scrollAndWaitImages(stepWaitS, maxSteps, roundWaitS, maxRounds, totalS, graceS, quietS) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const t0 = Date.now();
  const timeLeft = () => t0 + totalS * 1000 - Date.now();
  const stats = () => {
    const imgs = Array.from(document.images);
    return {
      total: imgs.length,
      // 화면에 자리(폭 100px 이상)가 있는데 아직 로드가 안 끝난 그림. 숨겨진 img는 지연 로딩이 영영 안 되므로 제외.
      pending: imgs.filter((im) => im.src && !im.complete && im.getBoundingClientRect().width >= 100).length,
      bigLoaded: imgs.filter((im) => im.complete && im.naturalWidth >= 300).length,
      // 본문 그림(가로·세로 모두 300px 이상). 광고 배너(720×90 등)는 제외되므로 "본문이 아직 없다"를 판단할 수 있다.
      contentLoaded: imgs.filter((im) => im.complete && im.naturalWidth >= 300 && im.naturalHeight >= 300).length,
    };
  };
  let eagered = 0;
  const eagerAll = () => {
    for (const im of document.images) {
      if ((im.getAttribute("loading") || "").toLowerCase() === "lazy") {
        im.loading = "eager";
        eagered += 1;
      }
    }
  };
  const scrollThrough = async () => {
    let last = -1;
    let stable = 0;
    for (let i = 0; i < maxSteps && timeLeft() > 0; i++) {
      const height = document.documentElement.scrollHeight;
      const y = window.scrollY + window.innerHeight;
      if (y >= height && height === last) {
        if (++stable >= 2) break;
      } else {
        stable = 0;
      }
      last = height;
      window.scrollBy(0, Math.max(300, window.innerHeight * 0.9));
      await sleep(stepWaitS * 1000);
    }
  };
  const waitPending = async (maxMs) => {
    const deadline = Date.now() + Math.min(maxMs, Math.max(0, timeLeft()));
    let zero = 0;
    while (Date.now() < deadline) {
      if (stats().pending === 0) {
        if (++zero >= 2) return true;
      } else {
        zero = 0;
      }
      await sleep(200);
    }
    return stats().pending === 0;
  };
  // img 추가나 src 변경이 quietSec 동안 없으면 종료, 있으면 최대 graceS까지 계속 관찰. <img>가 늘면 즉시 종료.
  const watchQuiet = async (quietSec, baseTotal) => {
    let lastChange = Date.now();
    const mo = new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.type === "attributes") { lastChange = Date.now(); return; }
        for (const n of m.addedNodes) {
          if (n.nodeType === 1 && (n.tagName === "IMG" || n.tagName === "CANVAS" || n.querySelector("img,canvas"))) {
            lastChange = Date.now();
            return;
          }
        }
      }
    });
    mo.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["src", "srcset"] });
    const until = Date.now() + Math.min(graceS * 1000, Math.max(0, timeLeft()));
    while (Date.now() < until && Date.now() - lastChange < quietSec * 1000) {
      await sleep(100);
      if (document.images.length > baseTotal) break;
    }
    mo.disconnect();
  };

  let rounds = 0;
  let prevTotal = stats().total;
  eagerAll();
  await scrollThrough();
  let s = stats();
  while (rounds < maxRounds && timeLeft() > 0) {
    rounds += 1;
    eagerAll();
    await waitPending(roundWaitS * 1000);
    s = stats();
    let grew = s.total > prevTotal;
    if (!grew && s.pending === 0 && rounds === 1 && graceS > 0) {
      // 본문 그림이 이미 있으면 잠깐(quietS)만, 아직 없으면(뷰어가 늦게 넣는 사이트) 최대 graceS까지 생기는지 기다린다
      await watchQuiet(s.contentLoaded >= 3 ? quietS : graceS, prevTotal);
      s = stats();
      grew = s.total > prevTotal;
    }
    prevTotal = s.total;
    if (!grew && s.pending === 0) break;
    if (grew) {
      eagerAll();
      await scrollThrough(); // 새로 생긴 그림만 한 번 더 깨운다
    }
  }
  window.scrollTo(0, 0);
  await sleep(200);
  return { ...stats(), rounds, eagered, seconds: Math.round((Date.now() - t0) / 100) / 10 };
}

// webtoon2pdf/navigator.py FIND_NEXT_JS 이식본 (사용자 selector 없음).
// {href} = 링크 이동, {click: cssPath} = href 없는 버튼 클릭, null = 없음
function findNext(texts) {
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const hrefOf = (el) => {
    if (!el) return null;
    const h = el.getAttribute("href") || el.getAttribute("data-href") || el.getAttribute("data-url");
    if (h && h !== "#" && !h.startsWith("javascript:")) return h;
    // onclick="location.href='...'" 형태의 버튼 (그누보드 뷰어 등)
    const oc = el.getAttribute("onclick") || "";
    const m = oc.match(/(?:location(?:\.href)?|window\.location(?:\.href)?)\s*=\s*['"]([^'"]+)['"]/);
    if (m) return m[1];
    return null;
  };
  const label = (el) => norm(el.innerText || el.textContent || el.getAttribute("aria-label") || el.getAttribute("title"));
  const disabled = (el) => el.disabled || el.getAttribute("aria-disabled") === "true" || /disabled|inactive/i.test(String(el.className));
  const NEXT_KEY = /\bnext\b|next[-_]|[-_]next|btn_next|nextepisode/;
  const keyOf = (el) => ((typeof el.className === "string" ? el.className : "") + " " + (el.id || "")).toLowerCase();
  function cssPath(el) {
    if (el.id) return "#" + CSS.escape(el.id);
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.body) {
      let part = el.tagName.toLowerCase();
      const parent = el.parentElement;
      if (parent) {
        const sib = Array.from(parent.children).filter((c) => c.tagName === el.tagName);
        if (sib.length > 1) part += ":nth-of-type(" + (sib.indexOf(el) + 1) + ")";
      }
      parts.unshift(part);
      el = parent;
    }
    return "body > " + parts.join(" > ");
  }

  // (a) rel=next
  for (const el of document.querySelectorAll('link[rel~="next"], a[rel~="next"]')) {
    const h = hrefOf(el);
    if (h) return { href: h, how: "rel" };
  }
  // (b) 텍스트 매칭: 정확히 일치 → 짧은 라벨에 포함
  const anchors = Array.from(document.querySelectorAll("a[href], button")).filter(visible);
  for (const t of texts) {
    const tn = norm(t);
    for (const el of anchors) {
      if (label(el) === tn) {
        const h = hrefOf(el) || hrefOf(el.closest("a"));
        if (h) return { href: h, how: "text" };
      }
    }
  }
  for (const t of texts) {
    const tn = norm(t);
    if (tn.length < 2) continue;
    for (const el of anchors) {
      const l = label(el);
      if (l.length <= 12 && l.includes(tn) && !l.includes("이전") && !l.includes("prev")) {
        const h = hrefOf(el) || hrefOf(el.closest("a"));
        if (h) return { href: h, how: "text" };
      }
    }
  }
  // (c) class/id에 next
  for (const el of document.querySelectorAll("a[href]")) {
    const key = keyOf(el);
    if (NEXT_KEY.test(key) && !/prev/.test(key)) {
      const h = hrefOf(el);
      if (h) return { href: h, how: "class" };
    }
  }
  // (e) href 없는 버튼: JS로 이동하는 사이트. 텍스트가 정확히 일치하는 것을 클릭 후보로
  const clickables = Array.from(document.querySelectorAll('button, [role="button"], a:not([href]), a[href="#"], a[href^="javascript:"]'))
    .filter((el) => visible(el) && !disabled(el));
  for (const t of texts) {
    const tn = norm(t);
    for (const el of clickables) {
      if (label(el) === tn) return { click: cssPath(el), how: "click-text" };
    }
  }
  for (const t of texts) {
    const tn = norm(t);
    if (tn.length < 2) continue;
    for (const el of clickables) {
      const l = label(el);
      if (l.length <= 12 && l.includes(tn) && !l.includes("이전") && !l.includes("prev")) {
        return { click: cssPath(el), how: "click-text" };
      }
    }
  }
  for (const el of clickables) {
    const key = keyOf(el);
    if (NEXT_KEY.test(key) && !/prev/.test(key)) return { click: cssPath(el), how: "click-class" };
  }
  return null;
}

function clickSelector(selector) {
  const el = document.querySelector(selector);
  if (!el) return false;
  el.scrollIntoView({ block: "center" });
  el.click();
  return true;
}

function detectChallenge(titles, texts) {
  const title = (document.title || "").toLowerCase();
  if (titles.some((t) => title.includes(t))) return true;
  const body = ((document.body && document.body.innerText) || "").slice(0, 3000).toLowerCase();
  return texts.some((t) => body.includes(t)) && document.images.length < 3;
}

async function isChallengePage(tabId) {
  try {
    const [r] = await chrome.scripting.executeScript({ target: { tabId }, func: detectChallenge, args: [CHALLENGE_TITLES, CHALLENGE_TEXTS] });
    return Boolean(r && r.result);
  } catch {
    return true; // 페이지 전환 중 등 판정 불가 → 아직 통과로 보지 않는다
  }
}

// ---------- 유틸 ----------

// 콜백형 chrome API를 Promise로 감싼다 (오래된 크롬 호환).
function call(fn, ...args) {
  return new Promise((resolve, reject) => {
    fn(...args, (result) => {
      const err = chrome.runtime.lastError;
      if (err) reject(new Error(err.message));
      else resolve(result);
    });
  });
}

// 팝업이 넘긴 tabId가 있으면 그 탭, 없으면(단축키) 마지막으로 포커스된 창의 활성 탭
async function activeTab(tabId) {
  if (tabId != null) {
    try { return await chrome.tabs.get(tabId); } catch { /* 닫힘 → 아래로 */ }
  }
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab && tab.id != null ? tab : null;
}

function isWebUrl(url) {
  return /^(https?|file):/i.test(url || "");
}

function stripHash(url) {
  return (url || "").split("#", 1)[0];
}

// webtoon2pdf/navigator.py same_site / _accept 와 같은 규칙
function stripSub(host) {
  return (host || "").replace(/^(www|m|comic|mobile)\./, "");
}

function acceptUrl(currentUrl, candidate) {
  let next;
  try { next = new URL(candidate, currentUrl); } catch { return null; }
  next.hash = "";
  const href = next.toString();
  if (href === stripHash(currentUrl)) return null;
  const cur = new URL(currentUrl);
  if (stripSub(cur.hostname) !== stripSub(next.hostname)) return null;
  return href;
}

function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error || new Error("FileReader 실패"));
    reader.readAsDataURL(blob);
  });
}

function waitDownload(id) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.downloads.onChanged.removeListener(onChanged);
      reject(new Error("다운로드가 제한 시간 안에 끝나지 않았습니다."));
    }, DOWNLOAD_TIMEOUT_MS);
    function onChanged(delta) {
      if (delta.id !== id || !delta.state) return;
      if (delta.state.current === "complete") {
        clearTimeout(timer);
        chrome.downloads.onChanged.removeListener(onChanged);
        resolve();
      } else if (delta.state.current === "interrupted") {
        clearTimeout(timer);
        chrome.downloads.onChanged.removeListener(onChanged);
        reject(new Error("다운로드가 중단되었습니다."));
      }
    }
    chrome.downloads.onChanged.addListener(onChanged);
  });
}

function safeName(title) {
  const s = (title || "page")
    .replace(/[\\/:*?"<>|\x00-\x1f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^\.+/, "")
    .slice(0, 60)
    .trim();
  return s || "page";
}

function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
}

function basename(path) {
  return path.split("/").pop();
}

function mb(bytes) {
  return (bytes / 1048576).toFixed(1);
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function badge(text, color, clearAfterMs) {
  try {
    await chrome.action.setBadgeBackgroundColor({ color });
    await chrome.action.setBadgeText({ text });
    if (clearAfterMs) setTimeout(() => chrome.action.setBadgeText({ text: "" }), clearAfterMs);
  } catch (e) {
    /* 배지는 부가 기능 — 실패해도 무시 */
  }
}
