/* 阅读器全屏（v1.4.0）回归测试
 *
 * 需求：单击图片进入全屏，再单击退出；全屏时滚轮/键盘等翻页方式照旧。
 *
 * 这里有**一个刻意的行为变更**，必须被钉死：
 *   单击图片原来是「右半屏=下一页 / 左半屏=上一页」，现在改成了全屏开关。
 *   翻页改由滚轮 / ←→ / PageUp·PageDown 承担。所以本测试专门断言
 *   「点击之后页码不变」——防止有人日后把旧手势改回来而全屏失效。
 *
 * 另外覆盖：
 *   - 原生全屏调用失败/抛错时，仍保留 body.fs（降级成"藏掉导航栏和阅读器条"的伪全屏），
 *     不会变成"点了没反应"。
 *   - Esc 的两种语义：全屏时=退出全屏，非全屏时=返回书架（旧行为不能丢）。
 *   - 离开阅读器时自动退出全屏（否则导航栏还藏着，回到书架没东西可点）。
 *   - 首次进入才提示怎么退出，之后永久不再提示。
 *
 * jsdom 测不了真实的窗口全屏，能守的是**调用契约与状态迁移**。
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
const IMAGES = [
  { url: '/outputs/1.jpg', width: 1920, height: 1080 },
  { url: '/outputs/2.jpg', width: 800, height: 1200 },
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
const txt = (w, id) => {
  const el = w.document.getElementById(id);
  return el ? el.textContent : '';
};
const key = (w, k) => w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));

/** opts.failApi：模拟原生全屏失败（返回 ok:false / 抛错），用来验降级路径 */
async function bootDom(opts = {}) {
  const errs = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errs.push('jsdomError: ' + (e.detail || e.message)));
  const calls = [];
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
    work_update: () => Promise.resolve({ ok: true }),
    list_incomplete: () => Promise.resolve({ items: [] }),
    open_dir: () => Promise.resolve({ ok: true }),
    toggle_fullscreen: () => {
      calls.push('toggle_fullscreen');
      if (opts.failApi === 'throw') return Promise.reject(new Error('原生全屏不可用'));
      return Promise.resolve({ ok: opts.failApi !== 'false', fullscreen: true });
    },
  });
  w.localStorage.setItem('readerMode', 'single');
  w.dispatchEvent(new w.Event('pywebviewready'));
  await sleep(250);
  return { dom, w, errs, calls };
}

(async () => {
  const { dom, w, errs, calls } = await bootDom();
  const $ = (id) => w.document.getElementById(id);
  const isFs = () => w.document.body.classList.contains('fs');

  await w.openReader('w1', 0);
  await sleep(250);

  console.log('--- 入口：阅读器条上有全屏按钮 ---');
  ok(!!$('btnFullscreen'), '存在全屏按钮（SVG 图标 + 文字）');

  console.log('--- 单击图片：进入全屏，且*不*翻页（本版的关键行为变更）---');
  const before = txt(w, 'readerPos');
  calls.length = 0;
  $('singleImg').click();
  await sleep(220);
  ok(isFs(), '单击后进入全屏（body.fs）');
  ok(calls.includes('toggle_fullscreen'), `已调用原生全屏（记录 ${calls.join(',') || '空'}）`);
  ok(txt(w, 'readerPos') === before, `单击不再翻页，页码仍是 ${before}（实际 ${txt(w, 'readerPos')}）`);
  ok($('btnFullscreenText').textContent === '退出全屏', `全屏后按钮文案变为「退出全屏」（实际 ${$('btnFullscreenText').textContent}）`);
  ok($('fsIconPath').getAttribute('d') === 'M3 9h6V3M21 9h-6V3M3 15h6v6M21 15h-6v6', `全屏后图标变为四个朝内的直角（实际 ${$('fsIconPath').getAttribute('d')}）`);

  console.log('--- 首次进入：提示怎么退出，并记进 localStorage ---');
  ok($('toast').classList.contains('show'), '提示已冒出来');
  ok(/全屏/.test(txt(w, 'toastText')), `提示说明退出方式（实际「${txt(w, 'toastText')}」）`);
  ok(w.localStorage.getItem('fsHinted') === '1', '已记下「提示过了」');
  await sleep(2000);

  console.log('--- 再次单击：退出全屏 ---');
  $('singleImg').click();
  await sleep(220);
  ok(!isFs(), '再单击已退出全屏');
  ok($('btnFullscreenText').textContent === '全屏', `退出全屏后按钮文案恢复为「全屏」（实际 ${$('btnFullscreenText').textContent}）`);
  ok($('fsIconPath').getAttribute('d') === 'M3 9V3h6M21 9V3h-6M3 15v6h6M21 15v6h-6', `退出全屏后图标恢复为四个朝外的直角（实际 ${$('fsIconPath').getAttribute('d')}）`);

  console.log('--- 第二次进入：不再提示（记过就永久不提了）---');
  $('singleImg').click();
  await sleep(220);
  ok(isFs(), '又进入全屏');
  ok(!$('toast').classList.contains('show'), '第二次不再弹提示');
  await sleep(400);

  console.log('--- Esc：全屏时=退出全屏，不是返回书架 ---');
  key(w, 'Escape');
  await sleep(300);
  ok(!isFs(), 'Esc 退出了全屏');
  ok($('page-reader').classList.contains('show'), '仍留在阅读器里（没被踢回书架）');

  console.log('--- 再按 Esc：没在全屏时才=返回书架（旧行为不能丢）---');
  key(w, 'Escape');
  await sleep(350);
  ok(!$('page-reader').classList.contains('show'), '非全屏时 Esc 返回作品页');

  console.log('--- 键盘：F 与 F11 也能切换 ---');
  await w.openReader('w1', 0);
  await sleep(250);
  /* preventDefault 必须真的生效：不然 WebView2 自己的 F11 会抢着处理同一按键，
     导致「它全屏了、我们的记账没动」，状态就此漂移。jsdom 不派发默认行为，
     唯一能断言的载体就是 defaultPrevented。 */
  let ev = new w.KeyboardEvent('keydown', { key: 'f', bubbles: true, cancelable: true });
  w.document.dispatchEvent(ev);
  await sleep(220);
  ok(isFs(), 'F 键进入全屏');
  ok(ev.defaultPrevented, 'F 键已 preventDefault（不让 WebView 抢）');
  ev = new w.KeyboardEvent('keydown', { key: 'F11', bubbles: true, cancelable: true });
  w.document.dispatchEvent(ev);
  await sleep(220);
  ok(!isFs(), 'F11 退出全屏');
  ok(ev.defaultPrevented, 'F11 已 preventDefault');

  console.log('--- 全屏时仍能用滚轮 / 方向键翻页 ---');
  $('btnFullscreen').click();
  await sleep(220);
  ok(isFs(), '点按钮进入全屏');
  const p0 = txt(w, 'readerPos');
  $('readerBody').dispatchEvent(new w.WheelEvent('wheel', { deltaY: 120, bubbles: true, cancelable: true }));
  await sleep(200);
  ok(txt(w, 'readerPos') !== p0, `滚轮仍能翻页（${p0} → ${txt(w, 'readerPos')}）`);
  ok(isFs(), '翻页后仍在全屏（翻页不退出全屏）');
  const p1 = txt(w, 'readerPos');
  key(w, 'ArrowRight');
  await sleep(200);
  ok(txt(w, 'readerPos') !== p1, `方向键仍能翻页（${p1} → ${txt(w, 'readerPos')}）`);

  console.log('--- 点「返回」自动退出全屏（否则导航栏还藏着）---');
  ok(isFs(), '此刻仍在全屏');
  $('btnReaderBack').click();
  await sleep(350);
  ok(!isFs(), '离开阅读器时自动退出全屏');
  ok(calls.length > 0, '确实调用过原生全屏来还原窗口');

  console.log('--- 连续滚动模式：单击图片同样切换全屏 ---');
  await w.openReader('w1', 0);
  await sleep(250);
  $('btnMode').click();
  await sleep(250);
  const firstImg = $('scrollWrap').querySelector('img');
  ok(!!firstImg, '已进入连续滚动模式，图片已渲染');
  firstImg.click();
  await sleep(220);
  ok(isFs(), '滚动模式点图片也能进全屏');

  console.log('--- 结束页：单击仍是「回到最后一页」，不是全屏开关 ---');
  $('btnMode').click();     // 切回单图
  await sleep(200);
  if (isFs()) { $('singleImg').click(); await sleep(200); }
  w.readerNext(); w.readerNext(); w.readerNext();
  await sleep(200);
  ok(txt(w, 'readerPos') === '本话完', '已到结束页');
  $('readerEnd').click();
  await sleep(250);
  ok(txt(w, 'readerPos') === '3 / 3', `结束页单击回到最后一页（实际 ${txt(w, 'readerPos')}）`);
  ok(!isFs(), '结束页单击不会误进全屏');

  console.log('--- 没有未捕获错误 ---');
  await sleep(150);
  ok(errs.length === 0, `运行期无未捕获错误${errs.length ? ' —— ' + errs.join(' | ') : ''}`);
  dom.window.close();

  console.log('');
  console.log('--- 降级：原生全屏失败时，仍保留 body.fs（不全挂）---');
  for (const mode of ['false', 'throw']) {
    const d = await bootDom({ failApi: mode });
    await d.w.openReader('w1', 0);
    await sleep(250);
    d.w.document.getElementById('singleImg').click();
    await sleep(250);
    ok(d.w.document.body.classList.contains('fs'),
      `原生全屏${mode === 'throw' ? '抛错' : '返回 ok:false'}时仍是伪全屏（导航/阅读器条已藏）`);
    await sleep(150);
    ok(d.errs.length === 0, `降级路径没有未捕获错误${d.errs.length ? ' —— ' + d.errs.join(' | ') : ''}`);
    d.dom.window.close();
  }

  console.log('');
  console.log(fail.length ? `FAILED ${fail.length}` : 'ALL PASS');
  process.exit(fail.length ? 1 : 0);
})();
