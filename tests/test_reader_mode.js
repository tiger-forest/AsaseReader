/* 阅读器结尾 / 翻章行为回归测试
 *
 * 覆盖三组需求：
 *   1) 单图模式读完一章**不自动跳章**，停在整屏「本话完」页（不是弹窗，无需回应），
 *      页上有「返回上一章」「进入下一章」两个按钮。
 *   2) 连续滚动模式滚到底时，底部除「进入下一章」外，其左边还有「返回上一章」。
 *   3) 第一章没有「返回上一章」，最后一章没有「进入下一章」。
 *
 * 同时守住两个历史 bug：
 *   - 切到连续滚动报 null.style（readerEnd 被 remove() 摘出文档，子节点 $() 查不到）
 *   - 结尾块必须始终挂在 DOM 里（只切显隐）
 *
 * jsdom + 纯 JS 打桩，不需要后端服务。
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
const LIB = { works: [{ id: 'w1', name: '测试作品', author: '某人', cover_url: null, chapter_count: 3 }], chapters: [], orphans: [], pending: [] };
const IMAGES = [
  { url: '/outputs/1.jpg', width: 800, height: 1200 },
  { url: '/outputs/2.jpg', width: 800, height: 1200 },
  { url: '/outputs/3.jpg', width: 800, height: 4000 },
];
const CHAPTERS = [
  { index: 1, chapter_id: 'c1', opus_id: '111', source: 'opus', title: '第一章', note: '', cover_url: null, path: 'chapters/c1' },
  { index: 2, chapter_id: 'c2', opus_id: '222', source: 'opus', title: '第二章', note: '', cover_url: null, path: 'chapters/c2' },
  { index: 3, chapter_id: 'c3', opus_id: '333', source: 'opus', title: '第三章', note: '', cover_url: null, path: 'chapters/c3' },
];
const LAST = CHAPTERS.length - 1;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fail = [];
const ok = (cond, label) => {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + label);
  if (!cond) fail.push(label);
};

/** 起一个 jsdom 实例并完成启动；readerMode 可预设初始阅读模式 */
async function bootDom(readerMode) {
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
  const stub = {
    get_state: () => Promise.resolve(STATE),
    library_state: () => Promise.resolve(LIB),
    list_works: () => Promise.resolve({ works: LIB.works }),
    mark_read: () => Promise.resolve({ ok: true }),
    work_detail: (wid) => Promise.resolve({ id: wid, name: '测试作品', author: '某人', cover_url: null, chapters: CHAPTERS }),
    chapter_images: (cid) => { calls.push(['chapter_images', cid]); return Promise.resolve({ images: IMAGES }); },
    list_incomplete: () => Promise.resolve({ items: [] }),
    open_dir: () => Promise.resolve({ ok: true }),
  };
  Object.assign(w.pywebview.api, stub);
  if (readerMode) w.localStorage.setItem('readerMode', readerMode);
  w.dispatchEvent(new w.Event('pywebviewready'));
  await sleep(250);
  return { dom, w, errs, calls };
}

/* 错误有两条可见路径：btnMode/openReader 的 catch → showError 弹框；
   或漏出到 window 的 error / unhandledrejection。两条都要断言。 */
async function assertClean(w, errs, label) {
  await sleep(120);
  const modal = w.document.getElementById('modal');
  const shown = modal.classList.contains('show');
  const msg = w.document.getElementById('modalBox').textContent || '';
  const isErrBox = shown && /出错了/.test(msg);
  if (isErrBox) console.log('   弹框内容：' + msg.slice(0, 160).replace(/\s+/g, ' '));
  ok(!isErrBox, `${label}：没有弹出错误框`);
  ok(errs.length === 0, `${label}：没有未捕获错误${errs.length ? ' —— ' + errs.join(' | ') : ''}`);
  if (shown) modal.classList.remove('show');
}

const vis = (el) => !!el && el.style.display !== 'none';

/* ---------------- 单图模式：读完不自动跳章 ---------------- */
(async () => {
  const { dom, w, errs, calls } = await bootDom('single');
  const $ = (id) => w.document.getElementById(id);

  console.log('--- 单图模式：翻到最后一页 ---');
  await w.openReader('w1', 0);
  await sleep(200);
  ok($('page-reader').classList.contains('show'), '已进入阅读器页');
  ok(txt(w, 'readerPos') === '1 / 3', `起始进度 1 / 3（实际 ${txt(w, 'readerPos')}）`);
  w.readerNext();
  w.readerNext();
  await sleep(120);
  ok(txt(w, 'readerPos') === '3 / 3', `翻到最后一页 3 / 3（实际 ${txt(w, 'readerPos')}）`);

  console.log('--- 再往后翻：停在「本话完」页，不自动跳章 ---');
  const before = calls.length;
  w.readerNext();
  await sleep(200);
  ok(calls.length === before, '没有触发新的 chapter_images 请求（确实没自动跳章）');
  ok(/第一章/.test(txt(w, 'readerTitle')), '标题仍停在第一章');
  ok(txt(w, 'readerPos') === '本话完', `进度显示「本话完」（实际 ${txt(w, 'readerPos')}）`);
  ok($('singleImg').style.display === 'none', '图片已隐藏');
  const endEl = $('readerEnd');
  ok(!!endEl && endEl.parentElement === $('singleWrap'), '「本话完」页挂在 singleWrap 里');
  ok(!!endEl && endEl.style.display === 'flex', '结束页已显示');
  ok(!!endEl && endEl.classList.contains('inSingle'), '结束页用了整屏样式 inSingle');
  ok(/本话完/.test(endEl.textContent), '结束页写着「— 本话完 —」');
  ok(!$('modal').classList.contains('show'), '不是弹窗（无需用户回应）');
  await assertClean(w, errs, '进入结束页');

  console.log('--- 第一章：没有「返回上一章」---');
  ok(!vis($('btnEndPrev')), '第一章结束页不显示「返回上一章」');
  ok(vis($('btnEndNext')), '第一章结束页显示「进入下一章」');

  console.log('--- 结束页上继续往后翻：不动 ---');
  w.readerNext();
  await sleep(120);
  ok(calls.length === before, '结束页上按下一页不会自己跳章');

  console.log('--- 从结束页往前翻：退回最后一页 ---');
  w.readerPrev();
  await sleep(120);
  ok(txt(w, 'readerPos') === '3 / 3', '退回最后一页');
  ok($('singleImg').style.display !== 'none', '图片重新显示');

  console.log('--- 结束页上「回到这一章」的四条路（最关键的一条）---');
  const backToLast = async (how) => {
    // 先回到结束页
    w.readerNext(); w.readerNext(); w.readerNext();
    await sleep(150);
    if (txt(w, 'readerPos') !== '本话完') return '没能回到结束页';
    how();
    await sleep(200);
    return txt(w, 'readerPos') === '3 / 3' ? '' : `实际 ${txt(w, 'readerPos')}`;
  };
  let r = await backToLast(() => $('btnEndBack').click());
  ok(!r, `点「← 回到上一页」按钮能回到最后一页${r ? ' —— ' + r : ''}`);
  r = await backToLast(() => $('readerEnd').click());
  ok(!r, `点结束页空白处能回到最后一页${r ? ' —— ' + r : ''}`);
  r = await backToLast(() => {
    $('readerBody').dispatchEvent(new w.WheelEvent('wheel', { deltaY: -100, bubbles: true, cancelable: true }));
  });
  ok(!r, `滚轮上滚能回到最后一页${r ? ' —— ' + r : ''}`);
  r = await backToLast(() => {
    w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
  });
  ok(!r, `按 ← 能回到最后一页${r ? ' —— ' + r : ''}`);
  ok(vis($('btnEndBack')), '单图结束页上有「回到上一页」按钮');
  await assertClean(w, errs, '结束页回退');

  console.log('--- 点「进入下一章」：整章前进 ---');
  await w.openReader('w1', 0);
  await sleep(200);
  w.readerNext(); w.readerNext(); w.readerNext();   // 走到结束页
  await sleep(150);
  ok(txt(w, 'readerPos') === '本话完', '已到结束页');
  $('btnEndNext').click();
  await sleep(300);
  ok(/第二章/.test(txt(w, 'readerTitle')), '点「进入下一章」跳到第二章（没被回退吃掉）');
  ok(txt(w, 'readerPos') === '1 / 3', '新章节从第 1 页开始');
  await assertClean(w, errs, '跳下一章');

  console.log('--- 最后一章：没有「进入下一章」---');
  await w.openReader('w1', LAST);
  await sleep(200);
  w.readerNext(); w.readerNext(); w.readerNext();
  await sleep(150);
  ok(txt(w, 'readerPos') === '本话完', '末章也能走到结束页');
  ok(!vis($('btnEndNext')), '末章结束页不显示「进入下一章」');
  ok(vis($('btnEndPrev')), '末章结束页显示「返回上一章」');
  console.log('--- 点「返回上一章」---');
  $('btnEndPrev').click();
  await sleep(250);
  ok(/第二章/.test(txt(w, 'readerTitle')), '已退回第二章');
  await assertClean(w, errs, '返回上一章');

  console.log('--- 非首章第一页往前翻：落到上一章「最后一页」而不是开头 ---');
  await w.openReader('w1', 1);          // 第二章
  await sleep(220);
  ok(txt(w, 'readerPos') === '1 / 3', '第二章从第 1 页开始');
  w.readerPrev();
  await sleep(320);
  ok(/第一章/.test(txt(w, 'readerTitle')), '往回翻到了第一章');
  ok(txt(w, 'readerPos') === '3 / 3', `落在上一章最后一页（实际 ${txt(w, 'readerPos')}）`);

  console.log('--- 换章时中间冒小黑框，然后自己淡出 ---');
  ok($('toast').classList.contains('show'), '提示已冒出来');
  ok(/上一章/.test(txt(w, 'toastText')), `提示文案提到上一章（实际「${txt(w, 'toastText')}」）`);
  ok(!$('modal').classList.contains('show'), '是提示不是弹窗（不拦点击）');
  await sleep(2000);
  ok(!$('toast').classList.contains('show'), '过一会儿自己消失了');
  await assertClean(w, errs, '往回翻跨章');

  console.log('--- 首章第一页往前翻：不换章，只提示 ---');
  await w.openReader('w1', 0);
  await sleep(220);
  w.readerPrev();
  await sleep(320);
  ok(/第一章/.test(txt(w, 'readerTitle')), '仍在第一章');
  ok(txt(w, 'readerPos') === '1 / 3', '仍在第 1 页');
  ok($('toast').classList.contains('show') && /已经是第一章/.test(txt(w, 'toastText')),
    '提示「已经是第一章」');

  console.log('--- 顶部「上一章」按钮：明确点名 = 从头看 ---');
  await w.openReader('w1', 2);
  await sleep(220);
  $('btnPrevCh').click();
  await sleep(320);
  ok(/第二章/.test(txt(w, 'readerTitle')), '顶部按钮回到第二章');
  ok(txt(w, 'readerPos') === '1 / 3', '顶部按钮进的是该章开头');
  await assertClean(w, errs, '顶部上一章按钮');
  dom.window.close();

  /* ---------------- 连续滚动模式 ---------------- */
  console.log('');
  console.log('--- 连续滚动：底部两个章间按钮 ---');
  const s = await bootDom('single');
  const $2 = (id) => s.w.document.getElementById(id);
  await s.w.openReader('w1', 1);        // 中间章：两个按钮都该在
  await sleep(200);
  $2('btnMode').click();
  await assertClean(s.w, s.errs, '切到连续滚动');
  ok($2('scrollWrap').querySelectorAll('img').length === 3, '3 张图已渲染');
  const end2 = $2('readerEnd');
  ok(!!end2 && end2.parentElement === $2('scrollWrap'), '结尾块挂在图片流末尾');
  ok(!!end2 && end2.style.display === 'block', '结尾块可见');
  ok(vis($2('btnEndPrev')) && vis($2('btnEndNext')), '中间章：两个按钮都在');
  const ops = [...$2('readerEnd').querySelectorAll('.endOps button')].filter(vis);
  ok(ops.length === 2 && ops[0].id === 'btnEndPrev' && ops[1].id === 'btnEndNext',
    `「返回上一章」在「进入下一章」左边（实际顺序 ${ops.map(b => b.id).join(' → ')}）`);

  console.log('--- 连续滚动：第一章 / 末章的按钮 ---');
  await s.w.openReader('w1', 0);
  await sleep(200);
  // 模式偏好是记在 localStorage 里的，换章不会重置：此时仍是连续滚动，不要再点一次切换
  ok(/连续滚动/.test(txt(s.w, 'btnMode')), '换章后仍是连续滚动模式');
  ok(!vis($2('btnEndPrev')), '第一章底部没有「返回上一章」');
  ok(vis($2('btnEndNext')), '第一章底部有「进入下一章」');
  await s.w.openReader('w1', LAST);
  await sleep(200);
  ok($2('readerEnd').style.display === 'block', '末章底部仍显示「本话完」（只是没有下一章按钮）');
  ok(vis($2('btnEndPrev')), '末章底部有「返回上一章」');
  ok(!vis($2('btnEndNext')), '末章底部没有「进入下一章」');

  console.log('--- 来回切模式不炸（历史 bug 守门）---');
  $2('btnMode').click(); await assertClean(s.w, s.errs, '末章 连续滚动 → 单图');
  $2('btnMode').click(); await assertClean(s.w, s.errs, '末章 单图 → 连续滚动');
  ok($2('scrollWrap').querySelectorAll('img').length === 3, '切回来仍渲染 3 张图');
  s.dom.window.close();

  console.log('--- 以连续滚动模式直接进阅读器 ---');
  {
    const d3 = await bootDom('scroll');
    await d3.w.openReader('w1', 1);
    await assertClean(d3.w, d3.errs, '以连续滚动模式直接打开');
    ok(d3.w.document.getElementById('scrollWrap').querySelectorAll('img').length === 3,
      '直接进连续滚动也渲染了 3 张图');
    d3.dom.window.close();
  }

  console.log('--- A / D 单手翻页（v1.20）：与 ←→ 完全同义 ---');
  {
    const d4 = await bootDom('single');
    const w4 = d4.w;
    const $4 = (id) => w4.document.getElementById(id);
    const key = (k, opt) => w4.document.dispatchEvent(
      new w4.KeyboardEvent('keydown', Object.assign({ key: k, bubbles: true, cancelable: true }, opt)));
    await w4.openReader('w1', 0);
    await sleep(220);
    ok(txt(w4, 'readerPos') === '1 / 3', `起始 1 / 3（实际 ${txt(w4, 'readerPos')}）`);
    key('d'); await sleep(150);
    ok(txt(w4, 'readerPos') === '2 / 3', `D 前进一页（实际 ${txt(w4, 'readerPos')}）`);
    key('a'); await sleep(150);
    ok(txt(w4, 'readerPos') === '1 / 3', `A 后退一页（实际 ${txt(w4, 'readerPos')}）`);
    key('D'); await sleep(150);
    ok(txt(w4, 'readerPos') === '2 / 3', '大写 D 同样有效（不要求松开 Shift 之外的状态）');
    key('A'); await sleep(150);
    ok(txt(w4, 'readerPos') === '1 / 3', '大写 A 同样有效');
    // 带修饰键必须放行：Ctrl+A 是全选，被吃掉还顺手翻页是最难查的那类 bug
    const before = txt(w4, 'readerPos');
    key('a', { ctrlKey: true }); await sleep(150);
    ok(txt(w4, 'readerPos') === before, `Ctrl+A 不翻页（实际 ${txt(w4, 'readerPos')}）`);
    key('d', { metaKey: true }); await sleep(150);
    ok(txt(w4, 'readerPos') === before, `Cmd+D 不翻页（实际 ${txt(w4, 'readerPos')}）`);
    await assertClean(w4, d4.errs, 'A / D 翻页');
    d4.dom.window.close();
  }

  console.log('');
  console.log(fail.length ? `FAILED ${fail.length}` : 'ALL PASS');
  process.exit(fail.length ? 1 : 0);
})();

function txt(w, id) {
  const el = w.document.getElementById(id);
  return el ? el.textContent : '';
}
