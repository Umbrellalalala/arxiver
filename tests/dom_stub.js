/**
 * 最小 DOM 桩：让 index.html 里的 <script> 能在 Node 里跑起来。
 *
 * 关键点（踩过的坑）：
 * 1. window 必须就是 globalThis。页面里写的是 `window.Arxiver = {...}`，
 *    真实浏览器里 window === globalThis，所以 Python 推过来的 `Arxiver.onXxx(...)`
 *    才能当全局解析。桩里若给 window 单独建个对象，重放推送就会 "Arxiver is not defined"。
 * 2. className 的 getter/setter 必须和 classList 共用同一个 Set，
 *    否则 `p.className = "progress show"` 之后 `classList.contains("show")` 是 false。
 * 3. 滚动容器是 <main>（CSS: main{overflow-y:auto}），不是 documentElement。
 */
const vm = require("vm");

function mkEl(id) {
  const cls = new Set();
  return {
    id, _html: "", textContent: "", value: "", disabled: false, checked: false,
    style: {}, dataset: {}, children: [],
    classList: {
      add: (c) => cls.add(c),
      remove: (c) => cls.delete(c),
      toggle: (c, on) => {
        if (on === undefined) { cls.has(c) ? cls.delete(c) : cls.add(c); }
        else { on ? cls.add(c) : cls.delete(c); }
        return cls.has(c);
      },
      contains: (c) => cls.has(c),
    },
    _classes: cls,
    get className() { return [...cls].join(" "); },
    set className(v) { cls.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => cls.add(c)); },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    appendChild(c) { this.children.push(c); return c; },
    remove() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    addEventListener() {},
    removeEventListener() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 100, height: 20 }; },
    getAttribute(k) { return this.dataset[k] || null; },
    setAttribute() {},
  };
}

/** 造一个干净的沙箱；返回 { sandbox, getEl, els, mainEl, calls } */
function createDom() {
  const els = {};
  const getEl = (id) => (els[id] = els[id] || mkEl(id));
  const mainEl = mkEl("main");
  mainEl.scrollTop = 0;

  const document = {
    getElementById: getEl,
    querySelector: (s) => (s === "main" ? mainEl : null),
    querySelectorAll: () => [],
    createElement: (t) => mkEl("_" + t),
    addEventListener: () => {},
    removeEventListener: () => {},
    body: mkEl("body"),
    documentElement: mkEl("html"),
    scrollingElement: mainEl,
  };

  const calls = [];
  const sandbox = {
    document, console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    Promise, Math, JSON, Date, String, Number, Array, Object, Set, Map, RegExp, Error,
    localStorage: { _d: {}, getItem(k) { return this._d[k] ?? null; }, setItem(k, v) { this._d[k] = String(v); } },
    addEventListener: () => {},
    removeEventListener: () => {},
    innerWidth: 1280, innerHeight: 800,
    pywebview: { api: makeApi(calls) },
  };
  sandbox.window = sandbox;      // 关键点 1
  sandbox.globalThis = sandbox;
  return { sandbox, getEl, els, mainEl, calls };
}

/** 万能假 API：任何方法名都返回一个函数，默认 resolve(null) */
function makeApi(calls) {
  return new Proxy({}, {
    get: (_t, name) => (...args) => {
      calls.push([String(name), ...args]);
      return Promise.resolve(null);
    },
  });
}

/** 把 index.html 里的 <script> 抠出来，在沙箱里跑一遍 */
function runPageScript(sandbox, html) {
  const m = html.match(/<script[^>]*>([\s\S]*?)<\/script>/);
  if (!m) throw new Error("index.html 里找不到 <script> 块");
  vm.createContext(sandbox);
  vm.runInContext(m[1], sandbox, { filename: "index.html:script" });
  if (!sandbox.Arxiver) throw new Error("页面没有暴露 window.Arxiver");
  return sandbox.Arxiver;
}

/** 在同一个沙箱里执行一段 JS（用来重放后端推过来的字符串） */
function evalIn(sandbox, code) {
  return vm.runInContext(code, sandbox, { filename: "push.js" });
}

module.exports = { createDom, mkEl, runPageScript, evalIn, makeApi };
