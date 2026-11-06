/* 回归测试：pywebview 注入尚未完成时切标签页，不能报 library_state is not a function。
   用 jsdom 载入真实的 gui.html，模拟注入中间态（pywebview.api = {} 空对象）。 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');
const { makeState } = require('./helpers');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'src/asase_reader/assets/gui.html'), 'utf8');

const CFG = {
  out: 'outputs', delay_min: 2, delay_max: 5, image_delay_min: 0.5, image_delay_max: 1.5,
  concurrency: 2, max_per_day: 200, include_cover: true,
  setup_done: true,
};
const STATE = makeState({
  jobs: [], config: CFG, out_dir: 'D:/AsaseReaderLibrary',
  daily_used: 0, running: false, paused: false, pending: 0,
});
const LIB = {
  works: [{ id: 'w1', name: '测试作品', author: '某人', cover_url: null, chapter_count: 2 }],
  downloads: [], orphans: [],
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fail = [];
const ok = (cond, label) => {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + label);
  if (!cond) fail.push(label);
};

(async () => {
  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8765/',
    runScripts: 'dangerously',
    beforeParse(window) {
      // 注入第一步已完成、api 函数还没创建 —— 正是启动瞬间的真实状态
      window.pywebview = { api: { get_state: () => Promise.resolve(STATE) } };
    },
  });
  const { window } = dom;
  const $ = (id) => window.document.getElementById(id);

  await sleep(120);
  ok(!!$('bootMask'), '启动遮罩存在');
  ok($('worksGrid').children.length === 0, '初始化前书架为空');

  // 启动瞬间迅速点「下载」再点回「书架」
  $('tab-dl').click();
  $('tab-shelf').click();
  await sleep(150);
  const modalShown = $('modal').classList.contains('show');
  const modalText = ($('modalBox').textContent || '').trim();
  ok(!modalShown, '注入未完成时切页不弹错误框' + (modalShown ? `（实际弹出：${modalText}）` : ''));
  ok(!/is not a function/.test(modalText), '没有出现 "... is not a function"');

  // 注入完成：把方法挂上去（pywebview 的 _createApi 做完）
  Object.assign(window.pywebview.api, {
    library_state: () => Promise.resolve(LIB),
    list_works: () => Promise.resolve({ works: LIB.works }),
    work_detail: () => Promise.resolve({ id: 'w1', name: '测试作品', author: '某人', cover_url: null, chapters: [] }),
    create_work: () => Promise.resolve({ id: 'w2' }),
    delete_work: () => Promise.resolve({ ok: true }),
    add_chapters: () => Promise.resolve({ ok: true }),
    edit_dirty: () => Promise.resolve({ dirty: false }),
    cancel_edit: () => Promise.resolve({ ok: true }),
    begin_edit: () => Promise.resolve({ ok: true }),
  });
  await sleep(500); // 等兜底轮询（200ms）触发 boot

  ok(!$('bootMask'), '就绪后遮罩自动移除');
  const cards = $('worksGrid').querySelectorAll('.workCard');
  ok(cards.length === 1, '书架渲染出作品卡片');
  ok(cards.length && cards[0].querySelector('.name').textContent === '测试作品', '卡片标题正确');
  ok($('statusText').textContent === '就绪', '下载页状态栏已由 get_state 填充');

  // 就绪后再切页，仍然正常
  $('tab-dl').click(); $('tab-shelf').click();
  await sleep(100);
  ok($('worksGrid').querySelectorAll('.workCard').length === 1, '就绪后反复切页仍正常');

  console.log(fail.length ? `\n${fail.length} 项失败` : '\n全部通过');
  process.exit(fail.length ? 1 : 0);
})();
