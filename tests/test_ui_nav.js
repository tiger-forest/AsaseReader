/* jsdom 验收：「新建作品」按钮在书架页「作品」标题那一行的最右
 *   1) 按钮在 .secthead 内，是最后一个子元素，同行左侧是「作品」标题
 *   2) 按钮不在 <nav> 里了；仍是 primary 外观
 *   3) 提示语「章节名和注释都可以自定义」已删除
 *   4) 点击仍能弹出新建作品框
 * 依赖：先跑 .cache/mock_gui.py（127.0.0.1:8765）
 */
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.resolve(__dirname, "..");
const HTML_PATH = path.join(ROOT, "src/asase_reader/assets/gui.html");
const html = fs.readFileSync(HTML_PATH, "utf8");

const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push("jsdomError: " + (e.detail || e.message)));

const SHIM = `
<script>
window.__call = (m, ...a) => fetch("http://127.0.0.1:8765/api/" + m, {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify(a)
}).then(async r => { const t = await r.text(); if (!r.ok) throw new Error(m + ": " + t); return JSON.parse(t); });
window.pywebview = { api: new Proxy({}, { get: (t, k) => (...a) => __call(k, ...a) }) };
</script>`;

const dom = new JSDOM(html.replace("<script>", SHIM + "\n<script>"), {
  runScripts: "dangerously",
  url: "http://127.0.0.1:8765/",
  virtualConsole: vc,
  pretendToBeVisual: true,
});
const w = dom.window;
w.fetch = (...a) => fetch(...a);
w.addEventListener("error", (e) => errors.push("window.error: " + e.message));
w.addEventListener("unhandledrejection", (e) =>
  errors.push("rejection: " + (e.reason && (e.reason.message || e.reason))));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const $ = (id) => w.document.getElementById(id);
const fail = (m) => { console.log("X " + m); process.exitCode = 1; };
const ok = (m) => console.log("v " + m);

(async () => {
  await new Promise((r) => w.addEventListener("load", r));
  w.dispatchEvent(new w.Event("pywebviewready"));
  await sleep(900);

  const btn = $("btnNewWork");
  if (!btn) { fail("找不到 #btnNewWork"); return; }
  ok("存在 #btnNewWork");

  // 1) 所在行：.secthead，左标题「作品」，按钮在最右
  const head = btn.parentElement;
  if (!head.classList.contains("secthead")) fail("按钮不在 .secthead 内（class=" + head.className + "）");
  else ok("按钮在 .secthead 行内");
  if (head.lastElementChild !== btn) fail("按钮不是该行最后一个元素");
  else ok("按钮位于该行最右");
  const h2 = head.querySelector("h2");
  if (!h2 || h2.textContent.trim() !== "作品") fail("同行左侧不是「作品」标题");
  else ok("同行左侧是「作品」标题");
  if (head.querySelector(".hint")) fail("该行还留着 hint 文本");
  else ok("该行没有多余 hint");

  // 2) 不在 nav 内；nav 恰好是 书架 / 下载 / 设置 三个 tab
  const nav = w.document.querySelector("nav");
  if (nav.contains(btn)) fail("按钮仍在 <nav> 内");
  else ok("按钮已移出 <nav>");
  const ids = [...nav.children].map((c) => c.id).join(",");
  if (ids !== "tab-shelf,tab-dl,tab-settings") fail("nav 结构异常：" + ids);
  else ok("nav 只有 书架 / 下载 两个 tab");
  if (!btn.classList.contains("primary")) fail("按钮丢了 primary 类");
  else ok("仍是 primary 外观类");

  // 3) 提示语已删除
  if (html.includes("章节名和注释都可以自定义")) fail("提示语仍残留在 gui.html");
  else ok("「章节名和注释都可以自定义」已删除");

  // 4) 点击仍能弹框
  btn.click();
  await sleep(200);
  if (!$("m_name") || !$("m_author")) fail("点击后没弹出新建作品框");
  else ok("点击弹出新建作品框（作品名/作者输入项在）");
  const cancel = [...w.document.querySelectorAll("#modalBox button")]
    .find((b) => b.textContent.trim() === "取消");
  if (cancel) { cancel.click(); await sleep(150); }

  if (errors.length) { console.log("JS 错误："); errors.forEach(e => console.log("  " + e)); process.exitCode = 1; }
  else console.log("无 JS 错误");
  process.exit(process.exitCode || 0);
})();
