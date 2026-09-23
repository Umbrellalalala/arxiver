/**
 * 前端增量刷新逻辑验证（最小 DOM 桩，无需真实浏览器）。
 *
 * 验证 index.html 里的 onNewPapers 链路：
 *   后端推 onNewPapers → 列表去抖刷新 → 新论文卡片带 .fresh 高亮 →
 *   重复推送不重复闪烁、「刚抓取」标记保留 → 后台同步靠 onNewPapers 自己亮进度条
 *
 * 运行: node tests/test_live_refresh.js
 */
const fs = require("fs");
const path = require("path");
const { createDom, runPageScript } = require("./dom_stub");

const HTML = fs.readFileSync(
  path.join(__dirname, "..", "arxiver", "ui", "static", "index.html"), "utf-8");

const PAPERS = [
  { arxiv_id: "2609.11111", title: "A brand new paper", published: "2026-09-18", score: 42, source: "arxiv", tags: "" },
  { arxiv_id: "2609.22222", title: "Another new paper", published: "2026-09-18", score: 38, source: "hf_daily", tags: "" },
  { arxiv_id: "2609.33333", title: "Old paper", published: "2026-09-10", score: 10, source: "arxiv", tags: "" },
];

const { sandbox, getEl, calls } = createDom();
const handlers = {
  get_papers: () => Promise.resolve(PAPERS),
  count_papers: () => Promise.resolve(PAPERS.length),
  get_stats: () => Promise.resolve({ total: 3, downloaded: 0, starred: 0, trash: 0 }),
  get_all_tags: () => Promise.resolve([{ name: "cv", count: 2 }]),
  get_categories: () => Promise.resolve({}),
};
sandbox.pywebview.api = new Proxy({}, {
  get: (_t, name) => (...args) => {
    calls.push([String(name), ...args]);
    const h = handlers[name];
    return h ? h(...args) : Promise.resolve(null);
  },
});

const Arxiver = runPageScript(sandbox, HTML);
if (typeof Arxiver.onNewPapers !== "function") {
  console.error("[FAIL] 前端没有暴露 onNewPapers");
  process.exit(1);
}

const homeList = getEl("home-list");
const progress = getEl("progress");
const ptext = getEl("ptext");

let ok = true;
const fail = (msg) => { console.error("[FAIL] " + msg); ok = false; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const countOf = (s, re) => (s.match(re) || []).length;
const paperCalls = () => calls.filter((c) => c[0] === "get_papers").length;

(async () => {
  // 0) 先正常加载一次列表。页面自带 1s/2.5s/5s 的兜底重试，等它们跑完再测
  Arxiver.onSyncStart();
  await sleep(5600);
  if (homeList.innerHTML.includes("fresh")) fail("初始加载不应有高亮");
  const before = paperCalls();

  // 1) 后端推来一批新论文
  Arxiver.onNewPapers({ new: 2, count: 2, ids: ["2609.11111", "2609.22222"] });
  if (!progress._classes.has("show")) fail("手动同步中进度条应保持显示");
  await sleep(900); // 等去抖 + 异步渲染

  const after = paperCalls();
  if (after <= before) fail("收到 onNewPapers 后没有重新拉取列表");

  let html = homeList.innerHTML;
  if (!html.includes('class="card fresh"')) fail("新论文卡片没有 .fresh 高亮");
  if (!html.includes("刚抓取")) fail("新论文卡片没有「刚抓取」标记");
  if (/class="card fresh"[^>]*id="card-2609\.33333"/.test(html)) fail("旧论文被误标为刚抓取");
  const fresh1 = countOf(html, /class="card fresh"/g);
  if (fresh1 !== 2) fail(`首批应有 2 张高亮卡片，实际 ${fresh1}`);

  // 2) 再推一批（含重复 id）：闪烁动画不重复放，但「刚抓取」标记要保留
  Arxiver.onNewPapers({ new: 1, count: 1, ids: ["2609.11111", "2609.44444"] });
  await sleep(900);
  html = homeList.innerHTML;
  const fresh2 = countOf(html, /class="card fresh"/g);
  const badges = countOf(html, /刚抓取/g);
  if (fresh2 !== 0) fail(`重复推送后不应再闪烁，实际还有 ${fresh2} 张带 .fresh`);
  if (badges !== 2) fail(`「刚抓取」标记应保留 2 个，实际 ${badges}`);

  // 3) 同步结束后进度条隐藏
  Arxiver.onSyncDone({ new: 3, papers: 3, elapsed: 12.3 });
  await sleep(200);
  if (progress._classes.has("show")) fail("同步完成后进度条没有隐藏");

  // 4) 后台定时同步场景：没有 onSyncStart，靠 onNewPapers 自己亮进度条
  Arxiver.onNewPapers({ new: 4, count: 4, ids: ["2609.55555"] });
  if (!progress._classes.has("show")) fail("后台同步时进度条没有自动显示");
  if (!/已新增 4 篇/.test(ptext.textContent)) {
    fail("后台同步进度文案没有实时新增计数（计数器可能没清零）: " + ptext.textContent);
  }

  // 4b) 关键回归：后续批次必须继续累加。
  //     旧实现写的是 `if(!progress.classList.contains("show")) showProgress(...)`，
  //     进度条一旦显示就不再刷新文案，数字永远停在第一批。
  //     真机上表现为：热榜先回来 32 篇 → 界面显示「已新增 32 篇」，
  //     而库里已经有 213 篇（arXiv 的 80/25/75/17 全没算进去）。
  Arxiver.onNewPapers({ new: 80, count: 80, ids: ["2609.66661"] });
  if (!/已新增 84 篇/.test(ptext.textContent)) {
    fail("第二批没有累加进文案（数字停在第一批了）: " + ptext.textContent);
  }
  Arxiver.onNewPapers({ new: 25, count: 25, ids: ["2609.66662"] });
  if (!/已新增 109 篇/.test(ptext.textContent)) {
    fail("第三批没有累加进文案: " + ptext.textContent);
  }

  // 4c) 手动同步时不能抢 onProgress 的文案（后端推的是详细进度）
  Arxiver.onSyncStart();
  Arxiver.onProgress({ text: "arXiv cs.CL：80 篇（新增 80）", pct: 40 });
  Arxiver.onNewPapers({ new: 7, count: 7, ids: ["2609.77777"] });
  if (!/arXiv cs\.CL/.test(ptext.textContent)) {
    fail("手动同步时 onNewPapers 抢掉了后端进度文案: " + ptext.textContent);
  }

  Arxiver.onSyncDone({ new: 4, papers: 4, elapsed: 9 });
  await sleep(100);
  if (progress._classes.has("show")) fail("后台同步结束后进度条没有隐藏");

  console.log("列表刷新次数:", after - before);
  console.log("首批高亮:", fresh1, "／重复推送后闪烁:", fresh2, "／刚抓取标记:", badges);
  console.log("结果:", ok ? "PASS" : "FAIL");
  process.exit(ok ? 0 : 1);
})();
