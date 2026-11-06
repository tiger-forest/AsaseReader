/* 章节封面（v1.20）：三条取封面的路 + 一个按钮的显隐
 *
 * 以前设封面只有「自定义…」一条路 —— 得自己到磁盘上找文件，
 * 而最常见的需求恰恰是「就用我现在看到的这个」。这次补的就是那两条：
 *   1) 阅读器顶条一个按钮，图片章节 = 当前这一页，视频章节 = 当前这一帧；
 *   2) 编辑页「从章节内容选取…」，把这一话的图片摊开点一张。
 *
 * 这里守的是**接线**有没有接对（传给后端的路径、按钮什么时候该在），
 * 「截出来的图好不好看」「缓存有没有真的失效」只能人工验，见 README 回归清单。
 *
 * jsdom 不实现 canvas 的解码，getContext / toDataURL 一律打桩。
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
const LIB = {
  works: [{ id: 'w1', name: '测试作品', author: '某人', cover_url: null, chapter_count: 2 }],
  chapters: [], orphans: [], pending: [],
};
const IMAGES = [
  { file: 'images/001.jpg', url: '/outputs/1.jpg', width: 800, height: 1200 },
  { file: 'images/002.jpg', url: '/outputs/2.jpg', width: 800, height: 1200 },
];
const MEDIA = { file: 'video.mp4', url: '/outputs/chapters/c2/video.mp4', format: 'mp4', size: 1234 };
const CHAPTERS = [
  { index: 1, chapter_id: 'c1', opus_id: '111', source: 'opus', title: '第一章', note: '', cover_url: null, path: 'chapters/c1' },
  { index: 2, chapter_id: 'c2', opus_id: '', source: 'video', title: '第二章 动态特典', note: '', cover_url: null, path: 'chapters/c2' },
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
const vis = (el) => !!el && el.style.display !== 'none';

async function bootDom() {
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
      // jsdom 没有 canvas 后端：不桩会往虚拟控制台喷 Not implemented，断言就被污染了
      w.HTMLCanvasElement.prototype.getContext = () => ({ drawImage() {} });
      w.HTMLCanvasElement.prototype.toDataURL = () => 'data:image/jpeg;base64,ZmFrZQ==';
    },
  });
  const w = dom.window;
  const stub = {
    get_state: () => Promise.resolve(STATE),
    library_state: () => Promise.resolve(LIB),
    list_works: () => Promise.resolve({ works: LIB.works }),
    mark_read: () => Promise.resolve({ ok: true }),
    work_detail: (wid) => {
      calls.push(['work_detail', wid]);
      return Promise.resolve({ id: wid, name: '测试作品', author: '某人', cover_url: null, chapters: CHAPTERS });
    },
    chapter_images: (cid) => Promise.resolve(cid === 'c2'
      ? { images: [], media: MEDIA }
      : { images: IMAGES, media: null }),
    set_chapter_cover_file: (wid, cid, src) => {
      calls.push(['set_chapter_cover_file', wid, cid, src]);
      return Promise.resolve({ ok: true, cover_url: `/outputs/_covers/ch_${cid}.jpg?v=1` });
    },
    save_chapter_cover_data: (wid, cid, dataUrl) => {
      calls.push(['save_chapter_cover_data', wid, cid, String(dataUrl).slice(0, 24)]);
      return Promise.resolve({ ok: true, cover_url: `/outputs/_covers/ch_${cid}.jpg?v=2` });
    },
    list_incomplete: () => Promise.resolve({ items: [] }),
    open_dir: () => Promise.resolve({ ok: true }),
    begin_edit: () => Promise.resolve({ ok: true }),
    edit_dirty: () => Promise.resolve({ dirty: false }),
    cancel_edit: () => Promise.resolve({ ok: true }),
  };
  Object.assign(w.pywebview.api, stub);
  w.localStorage.setItem('readerMode', 'single');
  w.dispatchEvent(new w.Event('pywebviewready'));
  await sleep(250);

  const v = w.document.getElementById('readerVideo');
  Object.defineProperty(v, 'videoWidth', { get: () => 1280, configurable: true });
  Object.defineProperty(v, 'videoHeight', { get: () => 720, configurable: true });
  v.load = () => {};
  return { dom, w, errs, calls, v };
}

async function assertClean(w, errs, label) {
  await sleep(150);
  const modal = w.document.getElementById('modal');
  const shown = modal.classList.contains('show');
  const msg = w.document.getElementById('modalBox').textContent || '';
  const isErrBox = shown && /出错了/.test(msg);
  if (isErrBox) console.log('   弹框内容：' + msg.slice(0, 160).replace(/\s+/g, ' '));
  ok(!isErrBox, `${label}：没有弹出错误框`);
  ok(errs.length === 0, `${label}：没有未捕获错误${errs.length ? ' —— ' + errs.join(' | ') : ''}`);
  if (shown) w.hideModal();
}

/** 在作品编辑页某一行上点开「设置封面」下拉，返回菜单项的文案数组 */
function coverMenuItems(w, rowIdx) {
  const rows = [...w.document.querySelectorAll('#editChapters .item')];
  const wrap = rows[rowIdx].querySelector('.menu');
  wrap.querySelector('button').click();
  return [...wrap.querySelectorAll('.menuItem')];
}

(async () => {
  const { dom, w, errs, calls, v } = await bootDom();
  const $ = (id) => w.document.getElementById(id);

  console.log('--- 图片章节：按钮在，文案是「设为封面」---');
  await w.openReader('w1', 0);
  await sleep(250);
  ok(vis($('btnSetCover')), '「设为封面」按钮已显示');
  ok($('btnSetCover').textContent === '设为封面', `文案是「设为封面」（实际「${$('btnSetCover').textContent}」）`);

  console.log('--- 点它：把**当前这一页**的相对路径交给后端 ---');
  w.readerNext();                       // 翻到第 2 页
  await sleep(150);
  const b1 = calls.length;
  $('btnSetCover').click();
  await sleep(200);
  const call1 = calls.slice(b1).find((c) => c[0] === 'set_chapter_cover_file');
  ok(!!call1, '调了 set_chapter_cover_file');
  ok(call1 && call1[2] === 'c1', `传的是本章 chapter_id（实际 ${call1 && call1[2]}）`);
  // 相对路径而不是绝对路径：前端手里只有「章节目录 + 文件名」，拼不出磁盘路径
  ok(call1 && call1[3] === 'chapters/c1/images/002.jpg',
    `传的是第 2 页的相对路径（实际 ${call1 && call1[3]}）`);
  ok(w.eval("reader.work.chapters[reader.chIdx].cover_url") === '/outputs/_covers/ch_c1.jpg?v=1',
    '返回的新封面 URL 写回了章节对象（省一次 work_detail）');
  await assertClean(w, errs, '图片章节设封面');

  console.log('--- 停在「本话完」页时按钮收起（那一页不是任何一张图）---');
  w.readerNext();
  await sleep(200);
  ok(txt(w, 'readerPos') === '本话完', '已停在结束页');
  ok(!vis($('btnSetCover')), '结束页上按钮已隐藏');

  console.log('--- 从结束页翻回图片：按钮要跟着回来 ---');
  w.readerPrev();
  await sleep(200);
  ok(txt(w, 'readerPos') === '2 / 2', `回到了第 2 页（实际 ${txt(w, 'readerPos')}）`);
  ok(vis($('btnSetCover')), '按钮重新出现（结束页那次隐藏不会一直留着）');

  console.log('--- 连续滚动模式：没有「当前页」，按钮也要收起 ---');
  await w.openReader('w1', 0);
  await sleep(250);
  $('btnMode').click();
  await sleep(200);
  ok(!vis($('singleWrap')) && vis($('scrollWrap')), '已切到连续滚动');
  ok(!vis($('btnSetCover')), '连续滚动下按钮已隐藏');
  await assertClean(w, errs, '连续滚动隐藏按钮');

  console.log('--- 视频章节：同一个按钮，文案变「截取封面」---');
  await w.openReader('w1', 1);
  await sleep(280);
  ok(w.eval('!!reader.video'), '已进入视频章节');
  ok(vis($('btnSetCover')), '按钮仍显示');
  ok($('btnSetCover').textContent === '截取封面',
    `文案变成「截取封面」（实际「${$('btnSetCover').textContent}」）`);

  console.log('--- 点它：抓当前这一帧交给后端 ---');
  const b2 = calls.length;
  $('btnSetCover').click();
  await sleep(220);
  const call2 = calls.slice(b2).find((c) => c[0] === 'save_chapter_cover_data');
  ok(!!call2, '调了 save_chapter_cover_data');
  ok(call2 && call2[2] === 'c2', `传的是本章 chapter_id（实际 ${call2 && call2[2]}）`);
  ok(call2 && /^data:image\/jpeg;base64,/.test(call2[3]),
    `传的是 canvas 抓出来的 DataURL（实际 ${call2 && call2[3]}）`);
  ok(w.eval("reader.work.chapters[reader.chIdx].cover_url") === '/outputs/_covers/ch_c2.jpg?v=2',
    '返回的新封面 URL 已写回');
  await assertClean(w, errs, '视频章节截封面');

  console.log('--- 画面还没加载出来时：说清楚，不要截一张空白图存进去 ---');
  Object.defineProperty(v, 'videoWidth', { get: () => 0, configurable: true });
  const b3 = calls.length;
  $('btnSetCover').click();
  await sleep(220);
  ok(calls.length === b3, '没有调后端（没画面就什么都不存）');
  ok($('modal').classList.contains('show')
    && /还没加载出画面/.test(txt(w, 'modalBox')), '弹出了「还没加载出画面」的提示');
  w.hideModal();
  Object.defineProperty(v, 'videoWidth', { get: () => 1280, configurable: true });
  await assertClean(w, errs, '无画面时的提示');

  console.log('--- 播完停在章末页：按钮收起（没画面可截）---');
  v.dispatchEvent(new w.Event('ended'));
  await sleep(180);
  ok(!vis($('btnSetCover')), '视频章末页上按钮已隐藏');

  console.log('--- 编辑页：图片章节多一项「从章节内容选取…」---');
  await w.openEdit('w1', 'shelf');
  await sleep(300);
  const items1 = coverMenuItems(w, 0).map((b) => b.textContent);
  ok(items1.includes('自定义…') && items1.includes('恢复默认（头图）'),
    `图片章节有自定义与恢复默认（实际 ${JSON.stringify(items1)}）`);
  ok(items1.includes('从章节内容选取…'), '图片章节有「从章节内容选取…」');
  w.closeMenus();

  console.log('--- 编辑页：视频章节没有这一项（它的封面只能从画面里截）---');
  const items2 = coverMenuItems(w, 1).map((b) => b.textContent);
  ok(!items2.includes('从章节内容选取…'), `视频章节没有「从章节内容选取…」（实际 ${JSON.stringify(items2)}）`);
  ok(items2.includes('自定义…'), '视频章节仍然可以自定义封面');
  w.closeMenus();

  console.log('--- 点开图片墙：点一张就设为封面 ---');
  coverMenuItems(w, 0).find((b) => b.textContent.includes('从章节内容选取')).click();
  await sleep(300);
  ok($('modal').classList.contains('show'), '弹出了选择窗口');
  ok($('modalBox').style.maxWidth === '720px', '选择窗口撑宽了（360px 放不下图片墙）');
  const cards = [...w.document.querySelectorAll('.coverPickCard')];
  ok(cards.length === 2, `列出本章 2 张图（实际 ${cards.length}）`);
  const b4 = calls.length;
  cards[1].click();
  await sleep(320);
  const call3 = calls.slice(b4).find((c) => c[0] === 'set_chapter_cover_file');
  ok(!!call3, '点第二张 → 调了 set_chapter_cover_file');
  ok(call3 && call3[3] === 'chapters/c1/images/002.jpg',
    `传的是被点的那一张（实际 ${call3 && call3[3]}）`);
  ok(!$('modal').classList.contains('show'), '选完自动收起');
  ok(calls.slice(b4).some((c) => c[0] === 'work_detail'), '收起后刷新了编辑页（列表里的封面跟着变）');
  await assertClean(w, errs, '从章节内容选封面');

  console.log('--- 弹窗宽度要还原，不能污染之后弹出的小窗口 ---');
  ok($('modalBox').style.maxWidth === '', 'maxWidth 已清空');
  ok($('modalBox').style.width === '', 'width 已清空');

  dom.window.close();
  console.log('');
  console.log(fail.length ? `FAILED ${fail.length}` : 'ALL PASS');
  process.exit(fail.length ? 1 : 0);
})();
