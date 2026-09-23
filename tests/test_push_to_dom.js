/**
 * 端到端重放：把 webui.py 实际推出来的 JS 字符串，按真实时序喂给 index.html。
 *
 * 链路: Python Api._push → JS 字符串 → 前端 Arxiver.onXxx → DOM
 * 输入由 tests/test_api_push.py 生成（tests/_api_pushes.json）。
 *
 * 时序很重要：去抖是 600ms，两批论文之间要留出时间，
 * 才能观察到「列表边抓边长大」，而不是最后一次性渲染。
 *
 * 运行: .venv/Scripts/python.exe tests/test_api_push.py && node tests/test_push_to_dom.js
 */
const fs = require("fs");
const path = require("path");
const { createDom, runPageScript, evalIn } = require("./dom_stub");

const PUSH_FILE = path.join(__dirname, "_api_pushes.json");
if (!fs.existsSync(PUSH_FILE)) {
  console.error("[SKIP] 找不到 _api_pushes.json，请先跑 tests/test_api_push.py");
  process.exit(1);
}
const { pushes, papers } = JSON.parse(fs.readFileSync(PUSH_FILE, "utf-8"));

const HTML = fs.readFileSync(
  path.join(__dirname, "..", "arxiver", "ui", "static", "index.html"), "utf-8");

let ok = true;
const fail = (msg) => { console.error("[FAIL] " + msg); ok = false; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const countOf = (s, re) => (s.match(re) || []).length;

// 模拟真实数据库：只返回「此刻已经被推送过」的论文
const known = new Set();
const { sandbox, getEl, calls } = createDom();
sandbox.pywebview.api = new Proxy({}, {
  get: (_t, name) => (...args) => {
    calls.push([String(name), ...args]);
    if (name === "get_papers") {
      // 真实后端是按 limit/offset 分页的，桩也要照做，否则「只取一页」这条
      // 关键行为在测试里等于没验。
      const all = papers.filter((p) => known.has(p.arxiv_id));
      const limit = args[1] || 50, offset = args[2] || 0;
      return Promise.resolve(all.slice(offset, offset + limit));
    }
    if (name === "count_papers") return Promise.resolve(known.size);
    if (name === "get_stats") return Promise.resolve({ total: known.size, downloaded: 0, starred: 0, trash: 0 });
    if (name === "get_all_tags") return Promise.resolve([]);
    if (name === "get_categories") return Promise.resolve({});
    return Promise.resolve(null);
  },
});

const Arxiver = runPageScript(sandbox, HTML);
const homeList = getEl("home-list");
const progress = getEl("progress");
const toastbox = getEl("toastbox");

/** 重放一条推送；若它带 ids，先把这些 id 记进「数据库」 */
function replay(js) {
  const m = js.match(/^Arxiver\.onNewPapers\((\{.*\})\);?$/);
  if (m) {
    const payload = JSON.parse(m[1]);
    (payload.ids || []).forEach((id) => known.add(id));
  }
  evalIn(sandbox, js);
}

(async () => {
  console.log("待重放推送:", pushes.length, "条");

  // 等页面自带的 1s/2.5s/5s 兜底重试跑完，避免它们插进来重渲染
  await sleep(5600);
  if (!Arxiver) { fail("页面脚本没跑起来"); process.exit(1); }

  const paperCalls0 = calls.filter((c) => c[0] === "get_papers").length;

  // ---- 第一条：onSyncStart + onProgress + 首批论文 ----
  const firstNew = pushes.findIndex((js) => js.startsWith("Arxiver.onNewPapers"));
  if (firstNew < 0) { fail("导出的推送里没有 onNewPapers"); process.exit(1); }
  pushes.slice(0, firstNew + 1).forEach(replay);
  await sleep(900);

  let html = homeList.innerHTML;
  const freshA = countOf(html, /class="card fresh"/g);
  const cardsA = countOf(html, /class="card(?: fresh)?"/g);
  if (cardsA !== 1) fail(`首批应渲染 1 张卡片，实际 ${cardsA}`);
  if (freshA !== 1) fail(`首批应有 1 张 .fresh，实际 ${freshA}`);
  if (!progress._classes.has("show")) fail("同步中进度条应显示");
  console.log("  首批后: 卡片", cardsA, "/ 高亮", freshA);

  // ---- 第二条：再推一批，列表应该继续长大 ----
  const secondNew = pushes.findIndex((js, i) => i > firstNew && js.startsWith("Arxiver.onNewPapers"));
  if (secondNew < 0) { fail("只导出了 1 次 onNewPapers，无法验证增量"); }
  else {
    pushes.slice(firstNew + 1, secondNew + 1).forEach(replay);
    await sleep(900);
    html = homeList.innerHTML;
    const cardsB = countOf(html, /class="card(?: fresh)?"/g);
    const freshB = countOf(html, /class="card fresh"/g);
    const badgesB = countOf(html, /刚抓取/g);
    if (cardsB !== 3) fail(`第二批后应渲染 3 张卡片，实际 ${cardsB}`);
    if (freshB !== 2) fail(`第二批后应有 2 张新闪烁（旧的不再闪），实际 ${freshB}`);
    if (badgesB !== 3) fail(`「刚抓取」标记应有 3 个，实际 ${badgesB}`);
    console.log("  第二批后: 卡片", cardsB, "/ 高亮", freshB, "/ 刚抓取", badgesB);
  }

  // ---- 收尾：onSyncDone ----
  pushes.slice((secondNew < 0 ? firstNew : secondNew) + 1).forEach(replay);
  await sleep(900);
  if (progress._classes.has("show")) fail("同步完成后进度条应隐藏");
  const toastText = toastbox.children.map((c) => c.textContent || "").join("|");
  if (!/同步完成/.test(toastText)) fail("没有弹出同步完成提示: " + toastText);

  const paperCalls = calls.filter((c) => c[0] === "get_papers").length - paperCalls0;
  console.log("  收尾后: 进度条已隐藏，toast =", toastText.slice(0, 40));
  console.log("  重放期间列表刷新", paperCalls, "次");
  if (paperCalls < 2) fail("增量刷新次数过少，可能退化成最后一次性渲染");

  console.log("结果:", ok ? "PASS" : "FAIL");
  process.exit(ok ? 0 : 1);
})();
