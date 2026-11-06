/* 章末页增强（v1.9）回归测试
 *
 * 需求：读完一章停在「— 本话完 —」页时，除了上一章/下一章两个按钮，还要说清
 *   两件事：下一章叫什么、整部读到第几章了。
 *   原来只有孤零零一句「本话完」——不知道下一章是什么，也不知道自己在整部
 *   作品的什么位置，只能退出去看章节列表。
 *
 * 数据全部来自内存里的 reader.work.chapters（没有新增后端接口），所以只要喂
 * 好 work_detail 的返回值就能覆盖各种形态。
 *
 * jsdom 能守的是**文案三态与按钮显隐**；信息块放在哪一行、字号好不好看只能人工看。
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
// 单张图：翻一次就到「本话完」，不用为了到结尾而反复 readerNext
const IMAGES = [{ url: '/outputs/1.jpg', width: 800, height: 1200 }];

const ch = (index, title) => ({
  index, chapter_id: 'c' + index, opus_id: String(100 + index), source: 'opus',
  title, note: '', cover_url: null, path: 'chapters/c' + index,
});
const CH_ALL_TITLED = [ch(1, '雨后的车站'), ch(2, '第二章'), ch(3, '最后一章')];
const CH_UNTITLED_NEXT = [ch(1, '第一章'), ch(2, ''), ch(3, '第三章')];

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

async function bootDom(chapters) {
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
    work_detail: (wid) => Promise.resolve({ id: wid, name: '测试作品', author: '某人', cover_url: null, chapters }),
    chapter_images: () => Promise.resolve({ images: IMAGES }),
    work_update: () => Promise.resolve({ ok: true }),
    list_incomplete: () => Promise.resolve({ items: [] }),
    open_dir: () => Promise.resolve({ ok: true }),
    toggle_fullscreen: () => Promise.resolve({ ok: true, fullscreen: true }),
  });
  w.localStorage.setItem('readerMode', 'single');
  w.dispatchEvent(new w.Event('pywebviewready'));
  await sleep(250);
  return { dom, w, errs };
}

(async () => {
  console.log('--- 非末章：说出下一章叫什么、整部读到第几章 ---');
  {
    const { dom, w, errs } = await bootDom(CH_ALL_TITLED);
    await w.openReader('w1', 0);
    await sleep(250);
    w.readerNext();
    await sleep(250);
    ok(txt(w, 'readerPos') === '本话完', `已到结束页（实际 ${txt(w, 'readerPos')}）`);
    const info = txt(w, 'endInfo');
    ok(/下一章 · 第 2 章 第二章/.test(info), `说出下一章标题（实际「${info}」）`);
    ok(/已读 1 \/ 3 章/.test(info), `说出错位置（实际「${info}」）`);
    ok(w.document.getElementById('btnEndNext').style.display !== 'none', '非末章：「进入下一章」仍在');
    ok(w.document.getElementById('btnEndPrev').style.display === 'none', '首章：「返回上一章」不出现');
    ok(errs.length === 0, `无未捕获错误${errs.length ? ' —— ' + errs.join(' | ') : ''}`);
    dom.window.close();
  }

  console.log('');
  console.log('--- 末章：说「已读完」，且不给下一章 ---');
  {
    const { dom, w, errs } = await bootDom(CH_ALL_TITLED);
    await w.openReader('w1', 2);
    await sleep(250);
    w.readerNext();
    await sleep(250);
    const info = txt(w, 'endInfo');
    ok(/本作已读完 · 共 3 章/.test(info), `末章说已读完（实际「${info}」）`);
    ok(!/下一章/.test(info), '末章不再提「下一章」');
    ok(w.document.getElementById('btnEndNext').style.display === 'none', '末章：「进入下一章」隐藏');
    ok(w.document.getElementById('btnEndPrev').style.display !== 'none', '末章：「返回上一章」出现');
    ok(errs.length === 0, `无未捕获错误${errs.length ? ' —— ' + errs.join(' | ') : ''}`);
    dom.window.close();
  }

  console.log('');
  console.log('--- 下一章没有标题：退回「下一章 · 第 N 章」，不留空 ---');
  {
    const { dom, w, errs } = await bootDom(CH_UNTITLED_NEXT);
    await w.openReader('w1', 0);
    await sleep(250);
    w.readerNext();
    await sleep(250);
    const nx = w.document.querySelector('#endInfo .nx');
    const nxText = nx ? nx.textContent : '';
    ok(/下一章 · 第 2 章/.test(nxText), `仍说清是第几章（实际「${nxText}」）`);
    ok(nxText === '下一章 · 第 2 章', `没有标题时不留尾巴（实际「${nxText}」）`);
    ok(/已读 1 \/ 3 章/.test(txt(w, 'endInfo')), '进度行照常');
    ok(errs.length === 0, `无未捕获错误${errs.length ? ' —— ' + errs.join(' | ') : ''}`);
    dom.window.close();
  }

  console.log('');
  console.log('--- 连续滚动模式：底部结束区共用同一套信息 ---');
  {
    const { dom, w, errs } = await bootDom(CH_ALL_TITLED);
    w.localStorage.setItem('readerMode', 'scroll');
    await w.openReader('w1', 0);
    await sleep(300);
    const info = txt(w, 'endInfo');
    ok(/下一章 · 第 2 章 第二章/.test(info), `滚动模式结束区也有下一章（实际「${info}」）`);
    ok(/已读 1 \/ 3 章/.test(info), '滚动模式也有进度');
    ok(w.document.getElementById('btnEndBack').style.display === 'none',
      '滚动模式不显示「回到上一页」（那个按钮只属于单图模式）');
    ok(errs.length === 0, `无未捕获错误${errs.length ? ' —— ' + errs.join(' | ') : ''}`);
    dom.window.close();
  }

  console.log('');
  console.log('--- 原来的「本话完」不能被顶掉 ---');
  {
    const { dom, w } = await bootDom(CH_ALL_TITLED);
    await w.openReader('w1', 0);
    await sleep(250);
    w.readerNext();
    await sleep(250);
    const endText = w.document.querySelector('#readerEnd .endText');
    ok(endText && /本话完/.test(endText.textContent), '「— 本话完 —」仍在');
    dom.window.close();
  }

  console.log('');
  console.log(fail.length ? `FAILED ${fail.length}` : 'ALL PASS');
  process.exit(fail.length ? 1 : 0);
})();
