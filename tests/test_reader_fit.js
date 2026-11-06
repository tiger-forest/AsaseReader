/* 单图模式「图片自适应窗口」回归测试
 *
 * 需求背景：用户常看的图里有大量 1920×1080 横版图（恋活等工具做的），
 * 原来 #singleImg 只写了 max-width，缩宽之后高度照样溢出可视区 → 出现纵向滚动条。
 *
 * 覆盖三点：
 *   1) CSS 层面：min-height→fit(height:100%) + #singleImg(max-height) + .tall(max-height:none)
 *      三者缺一不可，任一被删都应报错。
 *   2) 行为层面：横版图 / 竖版图 → 不带 tall、容器加 fit（锁高度，图片缩到装得下）；
 *      长图（h/w > 1.8）→ 带 tall、容器不加 fit（保留滚动，否则它反而滚不了）。
 *   3) 长图首次 toast：每章只提示一次，同章第二张长图不再打扰；换章后可再提示。
 *
 * 说明：jsdom 不做真实排版，无法验证"实际有没有滚动条"——那是人工手感验收项。
 * 这里能守的是**约束条件是否成立**（类名、CSS 声明），回归价值在于防止有人把
 * max-height 改回去。
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
/* 四种形状：横版（恋活典型 1920×1080）、竖版单页、两张长图（用来验"只提示一次"） */
const IMAGES = [
  { url: '/outputs/1.jpg', width: 1920, height: 1080 },
  { url: '/outputs/2.jpg', width: 800, height: 4000 },
  { url: '/outputs/3.jpg', width: 800, height: 1200 },
  { url: '/outputs/4.jpg', width: 800, height: 5000 },
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

/* ---------------- CSS 静态断言：三条约束缺一不可 ---------------- */
/* 只按选择器取该规则块里的声明，避免误判同名选择器 */
function ruleDecls(selector) {
  const styleMatch = html.match(/<style>([\s\S]*?)<\/style>/);
  if (!styleMatch) return null;
  const css = styleMatch[1];
  const i = css.indexOf(selector);
  if (i < 0) return null;
  const open = css.indexOf('{', i);
  const close = css.indexOf('}', open);
  return css.slice(open + 1, close);
}
const hasDecl = (decls, prop, val) =>
  !!decls && new RegExp(`(^|[;\\s{])${prop}\\s*:\\s*${val}\\s*(;|$|})`).test(decls);

console.log('--- CSS 约束：图片不能溢出可视区 ---');
const imgDecls = ruleDecls('#singleImg {');
ok(imgDecls !== null, '样式里有 #singleImg 规则');
ok(hasDecl(imgDecls, 'max-height', '100%'), '#singleImg 有 max-height:100%（否则横版图高度溢出→出滚动条）');
ok(hasDecl(imgDecls, 'max-width', '100%'), '#singleImg 保留 max-width:100%');

const fitDecls = ruleDecls('#singleWrap.fit {');
ok(fitDecls !== null, '样式里有 #singleWrap.fit 规则（给容器确定高度）');
ok(hasDecl(fitDecls, 'height', '100%'), '#singleWrap.fit 是 height:100%（min-height 撑不满，max-height 会失效）');

const tallDecls = ruleDecls('#singleImg.tall {');
ok(tallDecls !== null, '样式里有 #singleImg.tall 规则');
ok(hasDecl(tallDecls, 'max-height', 'none'), '#singleImg.tall 覆盖为 max-height:none（长图仍可滚动看完）');

async function bootDom() {
  const errs = [];
  const calls = [];
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
    mark_read: () => { calls.push(['mark_read']); return Promise.resolve({ ok: true }); },
    work_detail: (wid) => Promise.resolve({ id: wid, name: '测试作品', author: '某人', cover_url: null, chapters: CHAPTERS }),
    chapter_images: () => Promise.resolve({ images: IMAGES }),
    list_incomplete: () => Promise.resolve({ items: [] }),
    open_dir: () => Promise.resolve({ ok: true }),
  });
  w.localStorage.setItem('readerMode', 'single');
  w.dispatchEvent(new w.Event('pywebviewready'));
  await sleep(250);
  return { dom, w, errs, calls };
}

(async () => {
  const { dom, w, errs, calls } = await bootDom();
  const $ = (id) => w.document.getElementById(id);

  console.log('');
  console.log('--- 横版图（1920×1080）：适应窗口，不滚动 ---');
  await w.openReader('w1', 0);
  await sleep(220);
  ok(calls.some((c) => c[0] === 'mark_read'),
     '进入阅读器记一次「最近阅读」（last_read_at，参与排序）');
  ok(!$('singleImg').classList.contains('tall'), '横版图不带 tall 类（不是长图）');
  ok($('singleWrap').classList.contains('fit'), '容器加了 fit —— 定高，图片宽高都不溢出');

  console.log('--- 竖版单页（800×1200）：同样适应窗口 ---');
  await w.showSingle(2);
  await sleep(120);
  ok(!$('singleImg').classList.contains('tall'), 'h/w=1.5 不算长图');
  ok($('singleWrap').classList.contains('fit'), '容器加了 fit');

  console.log('--- 长图（800×4000）：保持按宽滚动，容器不能锁死高度 ---');
  await w.showSingle(1);
  await sleep(120);
  ok($('singleImg').classList.contains('tall'), 'h/w=5 判定为长图');
  ok(!$('singleWrap').classList.contains('fit'), '长图时**没有** fit（否则锁死高度就滚不动了）');

  console.log('--- 第一张长图：弹一次「长图：先滚动看完」---');
  ok($('toast').classList.contains('show'), '提示已冒出来');
  ok(/长图/.test(txt(w, 'toastText')), `提示文案提到长图（实际「${txt(w, 'toastText')}」）`);
  ok(/滚动/.test(txt(w, 'toastText')), '提示说明了「先滚动看完」');
  ok(!$('modal').classList.contains('show'), '是提示不是弹窗（不拦点击）');
  await sleep(2000);
  ok(!$('toast').classList.contains('show'), '过一会儿自己消失');

  console.log('--- 同章第二张长图：不再提示（第二次就成了打扰）---');
  await w.showSingle(3);
  await sleep(200);
  ok($('singleImg').classList.contains('tall'), '第二张长图仍是长图');
  ok(!$('toast').classList.contains('show'), '第二张长图不再弹提示');

  console.log('--- 换章后重新计数：本章第一张长图会再提示一次 ---');
  await w.openReader('w1', 1);
  await sleep(250);
  ok(!$('toast').classList.contains('show'), '新章节首图（横版）不弹提示');
  await w.showSingle(1);
  await sleep(200);
  ok($('toast').classList.contains('show') && /长图/.test(txt(w, 'toastText')),
    '换章后第一张长图重新提示一次');

  console.log('--- 优先级：跨章回落到长图时，不让位给长图提示 ---');
  /* 真实场景：往回翻跨章会先弹「上一章 · xx」，落脚的那张图又正好是长图。
     换章提示优先级更高；且这一次长图提示不算"已提示过"，下次还能补。 */
  await w.openReader('w1', 1);
  await sleep(250);
  w.readerPrev();
  await sleep(320);
  ok(/上一章/.test(txt(w, 'toastText')), `落到上一章时提示是「上一章」（实际「${txt(w, 'toastText')}」）`);
  ok(!/长图/.test(txt(w, 'toastText')), '长图提示没有抢走换章提示');
  ok(txt(w, 'readerPos') === '4 / 4', `落在上一章最后一页（实际 ${txt(w, 'readerPos')}）`);
  await sleep(2000);
  await w.showSingle(1);
  await sleep(160);
  ok($('toast').classList.contains('show') && /长图/.test(txt(w, 'toastText')),
    '之后再遇到长图，提示照常补回来');

  console.log('--- 切到连续滚动再切回来：fit 状态跟着恢复（历史 bug 守门）---');
  $('btnMode').click();
  await sleep(150);
  ok($('scrollWrap').style.display === 'flex', '已切到连续滚动');
  $('btnMode').click();
  await sleep(150);
  ok($('singleWrap').style.display === 'flex', '已切回单图');
  ok($('singleImg').classList.contains('tall') && !$('singleWrap').classList.contains('fit'),
    '切回来当前图仍是长图，fit 状态正确');

  console.log('--- 没有未捕获错误 ---');
  await sleep(150);
  ok(errs.length === 0, `运行期无未捕获错误${errs.length ? ' —— ' + errs.join(' | ') : ''}`);
  dom.window.close();

  console.log('');
  console.log(fail.length ? `FAILED ${fail.length}` : 'ALL PASS');
  process.exit(fail.length ? 1 : 0);
})();
