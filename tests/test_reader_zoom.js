/* 单图模式「图片缩放」回归测试（v1.17）
 *
 * 需求：临时放大看细节。只有放没有缩，下限就是「当前窗口下的 fit 尺寸」。
 *     Ctrl+滚轮（锚点跟光标）/ + = - / 0 缩放；缩放态单击不切全屏、可拖动平移；
 *     普通图缩放态滚轮直接翻页（A1）；长图缩放态滚轮先平移、到底才翻页（F1-a）；
 *     长图进入缩放时把滚动折算成平移，0 键 / 缩到下限退出时还原。
 *
 * 为什么这么分层：jsdom **不做排版**（offsetWidth / getBoundingClientRect 全 0），
 * 所以
 *   1) 倍率夹取、平移边界、锚点换算做成**不碰 DOM 的纯函数**，这里直接调；
 *   2) 需要尺寸的地方，用 defineProperty 给元素打桩假尺寸（比替换实现更端到端）；
 *   3) 手感（放大后清晰度、拖动跟手、触控板捏合）测不了 —— 列在 README 人工回归清单。
 *
 * 另注：gui.html 顶层用 `let` 声明的状态（zoomState）不会挂到 window，
 * 所以断言一律走**可观测副作用**：img 的 transform / class、body 的 class、页码。
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');
const { makeState } = require('./helpers');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'src/asase_reader/assets/gui.html'), 'utf8');

const CFG = {
  shelf_dir: 'D:/AsaseReaderLibrary', download_dir: '', import_mode: 'move',
  delay_min: 2, delay_max: 5, image_delay_min: 0.5, image_delay_max: 1.5,
  concurrency: 2, max_per_day: 200, include_cover: true,
  setup_done: true,
};
const STATE = makeState({
  jobs: [], config: CFG, shelf_dir: CFG.shelf_dir, download_dir: '',
  daily_used: 0, running: false, paused: false, pending: 0,
});
const LIB = { works: [{ id: 'w1', name: '测试作品', author: '某人', cover_url: null, chapter_count: 2 }], chapters: [], orphans: [], pending: [] };
/* 0 横版（恋活典型）1 长图 2 竖版单页 */
const IMAGES = [
  { url: '/outputs/1.jpg', width: 1920, height: 1080 },
  { url: '/outputs/2.jpg', width: 800, height: 4000 },
  { url: '/outputs/3.jpg', width: 800, height: 1200 },
];
const CHAPTERS = [
  { index: 1, chapter_id: 'c1', opus_id: '111', source: 'opus', title: '第一章', note: '', cover_url: null, path: 'chapters/c1' },
  { index: 2, chapter_id: 'c2', opus_id: '222', source: 'opus', title: '第二章', note: '', cover_url: null, path: 'chapters/c2' },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fail = [];
const ok = (cond, label) => {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + label);
  if (!cond) fail.push(label);
};
const near = (a, b, eps = 0.51) => Math.abs(a - b) <= eps;

/* ---------------- CSS 静态断言 ---------------- */
function ruleDecls(selector) {
  const m = html.match(/<style>([\s\S]*?)<\/style>/);
  if (!m) return null;
  const css = m[1];
  const i = css.indexOf(selector);
  if (i < 0) return null;
  const open = css.indexOf('{', i);
  const close = css.indexOf('}', open);
  return css.slice(open + 1, close);
}
const hasDecl = (decls, prop, val) =>
  !!decls && new RegExp(`(^|[;\\s{])${prop}\\s*:\\s*${val}\\s*(;|$|})`).test(decls);

async function bootDom() {
  const errs = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errs.push('jsdomError: ' + (e.detail || e.message)));
  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8765/',
    runScripts: 'dangerously',
    virtualConsole: vc,
    pretendToBeVisual: true,
    beforeParse(w) {
      w.pywebview = { api: {} };
      w.addEventListener('error', (e) => errs.push('window.error: ' + e.message));
      w.addEventListener('unhandledrejection', (e) => errs.push('rejection: ' + (e.reason && (e.reason.message || e.reason))));
    },
  });
  const w = dom.window;
  Object.assign(w.pywebview.api, {
    get_state: () => Promise.resolve(STATE),
    library_state: () => Promise.resolve(LIB),
    list_works: () => Promise.resolve({ works: LIB.works }),
    mark_read: () => Promise.resolve({ ok: true }),
    work_detail: (wid) => Promise.resolve({ id: wid, name: '测试作品', author: '某人', cover_url: null, chapters: CHAPTERS }),
    chapter_images: () => Promise.resolve({ images: IMAGES }),
    toggle_fullscreen: () => Promise.resolve({ ok: true }),
    list_incomplete: () => Promise.resolve({ items: [] }),
    open_dir: () => Promise.resolve({ ok: true }),
  });
  w.localStorage.setItem('readerMode', 'single');
  w.localStorage.setItem('fsHinted', '1');   // 省掉「全屏提示」那条 toast，免得干扰
  w.dispatchEvent(new w.Event('pywebviewready'));
  await sleep(250);
  return { dom, w, errs };
}

/* 假尺寸：把排版相关的只读属性换成可控值。
   box = 可视区；src = 图片 fit 尺寸；extra.scrollHeight 用于长图余量。
   ⚠️ scrollHeight 特意**不等于**图片高度：真实页面里阅读区还装着缩略图带等占位
   （实测差 146px）。v1.17.2 的 dy 就是被它带偏的 —— 打桩时若把两者设成一样，
   等于把这个 bug 也一起桩掉，这里必须保留这个差异。 */
function stubBox(w, { vw, vh, iw, ih, scrollH }) {
  const body = w.document.getElementById('readerBody');
  const img = w.document.getElementById('singleImg');
  const def = (el, prop, get) => Object.defineProperty(el, prop, { get, configurable: true });
  def(body, 'clientWidth', () => vw);
  def(body, 'clientHeight', () => vh);
  def(body, 'scrollHeight', () => (scrollH === undefined ? Math.max(vh, ih) : scrollH));
  def(img, 'offsetWidth', () => iw);
  def(img, 'offsetHeight', () => ih);
}
/* jsdom 里 scrollTop 是只读的恒 0（不排版），用可读写替身记录写入 */
function stubScroll(w) {
  const body = w.document.getElementById('readerBody');
  let v = 0;
  Object.defineProperty(body, 'scrollTop', { get: () => v, set: (x) => { v = x; }, configurable: true });
  return { get: () => v, set: (x) => { v = x; } };
}

(async () => {
  const { dom, w, errs } = await bootDom();
  const $ = (id) => w.document.getElementById(id);
  const zoomed = () => $('singleImg').classList.contains('zoomed');
  const locking = () => w.document.body.classList.contains('zooming');
  const tf = () => $('singleImg').style.transform;
  const scaleOf = () => {
    const m = /scale\(([\d.]+)\)/.exec(tf());
    return m ? parseFloat(m[1]) : 1;
  };
  const xyOf = () => {
    const m = /translate\(([-\d.]+)px,\s*([-\d.]+)px\)/.exec(tf());
    return m ? { x: parseFloat(m[1]), y: parseFloat(m[2]) } : { x: 0, y: 0 };
  };
  const wheel = (dy, opts = {}) => $('readerBody').dispatchEvent(
    new w.WheelEvent('wheel', Object.assign({ deltaY: dy, bubbles: true, cancelable: true }, opts)));
  const key = (k) => w.document.dispatchEvent(
    new w.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));
  const mouse = (target, type, opts) => target.dispatchEvent(
    new w.MouseEvent(type, Object.assign({ bubbles: true, cancelable: true, button: 0 }, opts)));

  /* ============ 纯逻辑：不需要排版就能守的规格 ============ */
  console.log('--- 倍率：只有放没有缩，上限 8 倍 ---');
  ok(w.zoomClamp(0.5) === 1, 'zoomClamp(0.5) = 1（缩不到 fit 以下）');
  ok(w.zoomClamp(99) === 8, 'zoomClamp(99) = 8（上限，再放大只是马赛克）');
  ok(w.zoomClamp(NaN) === 1, 'zoomClamp(NaN) 兜底为 1（不能让脏值污染状态）');
  ok(near(w.zoomStepScale(1, 1), 1.25, 1e-9), '放大一档 = ×1.25（乘性步进，从 1× 到 8× 手感均匀）');
  ok(near(w.zoomStepScale(1.5625, -1), 1.25, 1e-9), '缩小一档 = ÷1.25');
  ok(near(w.zoomStepScale(8, 1), 8, 1e-9), '已经在上限再放大仍是 8（不越界）');

  console.log('--- 平移边界：|c| ≤ (图×s − 视口)/2 ---');
  let m = w.zoomPanMax(400, 300, 1, 400, 300);
  ok(m.x === 0 && m.y === 0, '未放大（图正好铺满视口）时不能平移');
  m = w.zoomPanMax(400, 300, 2, 400, 300);
  ok(near(m.x, 200) && near(m.y, 150), `放大 2 倍后半幅 = 200 / 150（实际 ${m.x} / ${m.y}）`);
  let c = w.zoomClampPan(9999, -9999, 400, 300, 2, 400, 300);
  ok(near(c.x, 200) && near(c.y, -150), `超出边界被夹回来（实际 ${c.x} / ${c.y}）`);

  console.log('--- 锚点：光标下那一点缩放前后不动 ---');
  /* 判据是把结果代回去验证，而不是照抄实现公式 */
  const anchorCheck = (s0, cx, cy, s1, mx, my, label) => {
    const r = w.zoomAnchorPan(s0, cx, cy, s1, mx, my);
    const px = (mx - cx) / s0, py = (my - cy) / s0;      // 光标指向的图片那一点
    ok(near(px * s1 + r.x, mx, 0.01) && near(py * s1 + r.y, my, 0.01), label);
  };
  anchorCheck(1, 0, 0, 2, 100, 50, '未平移时缩放：光标处内容留在原地');
  anchorCheck(2, 10, -20, 4, -60, 80, '已平移再缩放：同样留在原地');
  anchorCheck(1, 0, 0, 1.25, 0, 0, '以视口中心为锚（键盘缩放）：可平移范围随之放大');

  /* ============ CSS 约束 ============ */
  console.log('--- CSS：transform 的溢出不能变出滚动条 ---');
  ok(hasDecl(ruleDecls('body.zooming #readerBody {'), 'overflow', 'hidden'),
     '缩放态锁掉阅读区滚动（transform 溢出不产生滚动区域，不锁会被 overflow-y:auto 接管）');
  ok(hasDecl(ruleDecls('#singleImg.zoomed {'), 'cursor', 'grab'),
     '缩放态光标是 grab（可拖动的暗示）');
  ok(hasDecl(ruleDecls('#singleImg.zoomed:active {'), 'cursor', 'grabbing'),
     '按下时是 grabbing');

  /* ============ 进入 / 退出缩放态 ============ */
  await w.openReader('w1', 0);
  await sleep(220);
  stubScroll(w);
  console.log('--- dy 只由「图片自身」决定，不能被阅读区其它内容带偏 ---');
  /* 真实页面：视口 600、图片正好 600 高，但 scrollHeight 是 746（缩略图带占了 146）。
     dy 该是 0，用 scrollHeight 算会得到 73 —— 一进缩放整张图就凭空跳一段（v1.17.2 修）。 */
  stubBox(w, { vw: 1000, vh: 600, iw: 400, ih: 600, scrollH: 746 });
  ok(w.zoomMetrics().dy === 0, '图片正好等于视口高时 dy = 0（scrollHeight 更大也不该带偏）');
  stubBox(w, { vw: 1000, vh: 600, iw: 400, ih: 4000, scrollH: 4146 });
  ok(near(w.zoomMetrics().dy, 1700), `图片高于视口时 dy = (图高 − 视口)/2（实际 ${w.zoomMetrics().dy}）`);
  stubBox(w, { vw: 400, vh: 300, iw: 400, ih: 300, scrollH: 300 });
  key('0');
  key('+');
  ok(xyOf().x === 0 && xyOf().y === 0,
     `普通图进入缩放不该凭空位移（实际 ${xyOf().x}, ${xyOf().y}）`);
  key('0');

  console.log('--- Ctrl+滚轮：只有「放大」方向才进入缩放 ---');
  wheel(-100, { ctrlKey: true });
  ok(zoomed(), 'Ctrl+向上滚 → 进入缩放态（图片带 zoomed 类）');
  ok(near(scaleOf(), 1.25, 1e-6), `倍率是 1.25（实际 ${scaleOf()}）`);
  ok(locking(), 'body 加了 zooming（锁滚动）');
  ok(/缩放/.test($('toastText').textContent), `首次提示提到「缩放」（实际「${$('toastText').textContent}」）`);
  wheel(-100, { ctrlKey: true });
  ok(near(scaleOf(), 1.5625, 1e-6), `继续 Ctrl+向上滚 → 继续放大（实际 ${scaleOf()}）`);

  key('0');
  ok(!zoomed(), '0 键 → 退出缩放态');
  ok(!locking(), '退出后解锁滚动');
  ok(tf() === '', `退出后 transform 清空（实际「${tf()}」）`);
  wheel(100, { ctrlKey: true });
  ok(!zoomed(), '没缩放时 Ctrl+向下滚不进缩放态（只有放没有缩）');

  console.log('--- 键盘：+ / = 放大，- 缩小，缩到下限自动退出 ---');
  key('+');
  ok(zoomed() && near(scaleOf(), 1.25), '「+」放大一档');
  key('=');
  ok(near(scaleOf(), 1.5625), '「=」同样放大（键盘惯例，不用按 Shift）');
  key('-');
  ok(near(scaleOf(), 1.25), '「-」缩小一档');
  key('-');
  ok(!zoomed() && tf() === '', '缩到下限 → 自动退出缩放态');

  console.log('--- 缩放态：单击不再切全屏，未缩放时照旧 ---');
  key('+');
  $('singleImg').click();
  await sleep(60);
  ok(!w.document.body.classList.contains('fs'), '缩放态下单击图片不进全屏（左键是用来拖动的）');
  key('0');
  $('singleImg').click();
  await sleep(60);
  ok(w.document.body.classList.contains('fs'), '退出缩放后单击仍进全屏（v1.4 的手势没丢）');
  $('singleImg').click();
  await sleep(60);
  ok(!w.document.body.classList.contains('fs'), '再点一次退出全屏');

  /* ============ 拖动平移 ============ */
  console.log('--- 拖动平移：跟手且不越界 ---');
  stubBox(w, { vw: 400, vh: 300, iw: 400, ih: 300, scrollH: 300 });
  key('+'); key('+');   // 1.5625 —— 放大后才有可平移的余量
  const before = xyOf();
  mouse($('singleImg'), 'mousedown', { clientX: 0, clientY: 0 });
  mouse(w, 'mousemove', { clientX: 40, clientY: 25 });
  const after = xyOf();
  ok(after.x !== before.x || after.y !== before.y,
     `拖动后图片位置跟着移动（${before.x},${before.y} → ${after.x},${after.y}）`);
  mouse(w, 'mouseup', { clientX: 40, clientY: 25 });
  const pos1 = xyOf();
  mouse(w, 'mousemove', { clientX: 500, clientY: 500 });
  ok(pos1.x === xyOf().x && pos1.y === xyOf().y, '松开后继续移动鼠标不再跟着走');

  mouse($('singleImg'), 'mousedown', { clientX: 0, clientY: 0 });
  mouse(w, 'mousemove', { clientX: 9999, clientY: 9999 });
  const bound = xyOf();
  const mx = w.zoomPanMax(400, 300, scaleOf(), 400, 300);
  ok(bound.x <= mx.x + 0.6 && bound.y <= mx.y + 0.6,
     `狂拖也不会拖出边界（上限 ${mx.x}/${mx.y}，实际 ${bound.x}/${bound.y}）`);
  mouse(w, 'mouseup', { clientX: 9999, clientY: 9999 });

  console.log('--- 双击退出缩放（与 0 键同义）---');
  key('+');
  ok(zoomed(), '先放大');
  mouse($('singleImg'), 'dblclick', {});
  ok(!zoomed(), '双击 → 退出缩放');
  ok(tf() === '', `transform 清空（实际「${tf()}」）`);
  mouse($('singleImg'), 'dblclick', {});
  ok(!zoomed(), '没缩放时双击不叠加语义（不会因此进全屏）');

  console.log('--- 起手即跟手：没有阈值突跳（曾有的 bug：先跳一小段才开始跟）---');
  stubBox(w, { vw: 400, vh: 300, iw: 400, ih: 300, scrollH: 300 });
  key('0'); key('+'); key('+');      // 重新放大，位置从 0 起算
  const startJ = xyOf();
  mouse($('singleImg'), 'mousedown', { clientX: 0, clientY: 0 });
  mouse(w, 'mousemove', { clientX: 3, clientY: 0 });
  ok(near(xyOf().x, startJ.x + 3) && near(xyOf().y, startJ.y),
     `移动 3px 图片就跟着走 3px（实际走了 ${xyOf().x - startJ.x}）—— 不存在"攒够阈值才动"`);
  mouse(w, 'mousemove', { clientX: 30, clientY: 10 });
  ok(near(xyOf().x, startJ.x + 30) && near(xyOf().y, startJ.y + 10),
     `总位移恒等于鼠标总位移（实际 ${xyOf().x - startJ.x}, ${xyOf().y - startJ.y}）`);
  mouse(w, 'mouseup', { clientX: 30, clientY: 10 });
  const held = xyOf();
  mouse(w, 'mousemove', { clientX: 200, clientY: 200 });
  ok(held.x === xyOf().x && held.y === xyOf().y, '松开后移动鼠标不再带着图走');

  /* ============ 普通图：A1 滚轮直接翻页 ============ */
  console.log('--- 普通图缩放态：滚轮直接翻页并复位（A1）---');
  stubBox(w, { vw: 400, vh: 300, iw: 400, ih: 300, scrollH: 300 });
  await w.showSingle(0);
  await sleep(80);
  key('+');
  ok(zoomed(), '先在横版图上放大');
  wheel(120);
  await sleep(80);
  ok(!zoomed(), '滚轮翻页的同时退出缩放');
  ok(w.document.getElementById('readerPos').textContent === '2 / 3',
     `确实翻到了下一页（实际 ${w.document.getElementById('readerPos').textContent}）`);

  /* ============ 长图：F1-a 先平移到底才翻页 ============ */
  console.log('--- 长图缩放态：滚轮先平移，到底才翻页（F1-a）---');
  const sc = stubScroll(w);
  await w.showSingle(1);                      // 800×4000 长图
  await sleep(120);
  stubBox(w, { vw: 400, vh: 300, iw: 400, ih: 4000, scrollH: 4300 });
  ok($('singleImg').classList.contains('tall'), '当前是长图');
  sc.set(500);
  wheel(-100, { ctrlKey: true });
  await sleep(60);
  ok(zoomed(), '长图也能进入缩放态（不再排除长图）');
  ok(sc.get() === 0, '进入缩放时页面滚动归零（缩放态下滚动交给平移）');
  /* 判据不是"translate 是不是负数"（长图布局本身就偏下，怎么都是负的），
     而是**滚动位置不同的两次进入，起始画面必须不同** —— 否则就是跳到顶了。 */
  const y500 = xyOf().y;
  /* 精确判据：进入缩放后**视口中心的那一点内容不变**（不该跳）。
     cy 是中心偏移，视口中心对应的图片点 = −cy/s；进入前是 −(dy − base)。
     translate.y = cy − dy，所以可观测形式是 −(translate.y + dy)/s。 */
  {
    const md = w.zoomMetrics().dy, s1 = scaleOf();
    ok(near(-(xyOf().y + md) / s1, -(md - 500), 1),
       `进入缩放时画面不跳：视口中心对应的图片点不变（base=500, dy=${md}, 实际 ${(-(xyOf().y + md) / s1).toFixed(1)} vs ${-(md - 500)}）`);
  }
  key('0');
  sc.set(1000);
  wheel(-100, { ctrlKey: true });
  await sleep(60);
  const y1000 = xyOf().y;
  ok(y500 !== y1000,
     `不同滚动位置进入缩放，起始位置不同（${y500} vs ${y1000}）—— 滚动被折算进了平移`);
  key('0');
  ok(sc.get() === 1000, `退出后还原为放大前的 1000（实际 ${sc.get()}）`);

  sc.set(500);
  wheel(-100, { ctrlKey: true });
  await sleep(60);
  const pos0 = xyOf().y;
  wheel(120);
  await sleep(60);
  ok(xyOf().y !== pos0, `长图缩放态下滚轮 = 纵向平移（${pos0} → ${xyOf().y}）`);
  ok(w.document.getElementById('readerPos').textContent === '2 / 3',
     '这一次没有翻页');

  for (let i = 0; i < 60 && zoomed(); i++) wheel(120);   // 一路滚到底（翻页后即停）
  await sleep(80);
  ok(!zoomed(), '平移到尽头后再滚 → 退出缩放');
  ok(w.document.getElementById('readerPos').textContent === '3 / 3',
     `确实翻了页（实际 ${w.document.getElementById('readerPos').textContent}）`);

  console.log('--- 长图：0 键退出还原放大前的滚动位置 ---');
  await w.showSingle(1);
  await sleep(80);
  stubBox(w, { vw: 400, vh: 300, iw: 400, ih: 4000, scrollH: 4300 });
  sc.set(500);
  wheel(-100, { ctrlKey: true });
  await sleep(60);
  ok(sc.get() === 0, '缩放态下页面滚动被归零');
  key('0');
  ok(!zoomed(), '0 键退出缩放');
  ok(sc.get() === 500, `滚动位置还原为放大前的 500（实际 ${sc.get()}）`);

  console.log('--- 长图：翻页退出不还原（换了图就没有原位置可言）---');
  sc.set(800);
  wheel(-100, { ctrlKey: true });
  await sleep(60);
  key('ArrowRight');
  await sleep(80);
  ok(!zoomed(), '键盘翻页也复位');
  ok(sc.get() === 0, '翻页后滚动归零');

  console.log('--- 长图：双击退出同样还原滚动位置 ---');
  sc.set(700);
  wheel(-100, { ctrlKey: true });
  await sleep(60);
  mouse($('singleImg'), 'dblclick', {});
  ok(!zoomed(), '长图双击也能退出缩放');
  ok(sc.get() === 700, `双击与 0 键同义：还原到放大前的 700（实际 ${sc.get()}）`);

  /* ============ 复位时机 ============ */
  console.log('--- 换模式 / 结束页 ---');
  await w.showSingle(0);
  await sleep(80);
  key('+');
  ok(zoomed(), '放大中');
  $('btnMode').click();
  await sleep(120);
  ok(!zoomed(), '切到连续滚动 → 复位');
  $('btnMode').click();
  await sleep(120);
  key('+');
  await w.showEnd();
  await sleep(80);
  ok(!zoomed(), '翻到「本话完」页 → 复位');
  wheel(-100, { ctrlKey: true });
  ok(!zoomed(), '结束页不接受缩放（那儿不是图片）');

  console.log('--- 没有未捕获错误 ---');
  await sleep(150);
  ok(errs.length === 0, `运行期无未捕获错误${errs.length ? ' —— ' + errs.join(' | ') : ''}`);
  dom.window.close();

  console.log('');
  console.log(fail.length ? `FAILED ${fail.length}` : 'ALL PASS');
  process.exit(fail.length ? 1 : 0);
})();
