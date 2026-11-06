/* 章节编辑页（v1.16）：章节内删除图片。
 *
 * 覆盖：
 *   1) 三个入口都有「编辑」按钮（书架未入库行 / 编辑页已入库行 / 编辑页未入库行）；
 *   2) 进页要带来源：从编辑页进来，返回键写「← 编辑作品」且确实回编辑页；
 *   3) 点选 / Shift 范围选 / 全选 / 反选 / 清空；
 *   4) **防误删的核心**：只是选中就退出，删除接口一次都不能被调；
 *   5) 点保存 → 先确认框 → 确认后才调一次，带的正是选中的那几张 file；
 *   6) 一张都没选时保存按钮 disabled。
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');
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

const WORK = {
  id: 'w01', name: '作品一', author: '作者A', tags: [], cover_url: null,
  chapters: [{ chapter_id: 'ch1', index: 1, title: '第 1 章', note: '', cover_url: null }],
};
const ORPHAN = {
  id: 'ch9', chapter_id: 'ch9', title: '未入库的一章', author: '作者A',
  source: 'local', opus_id: '', ok_count: 4, image_count: 4, cover_url: null,
  assigned: false, added_at: '2026-10-01T00:00:00+00:00',
};

function imgs(n) {
  const out = [];
  for (let i = 1; i <= n; i++) {
    out.push({
      file: `images/${String(i).padStart(3, '0')}.jpg`, index: i,
      url: `/outputs/chapters/ch1/images/${String(i).padStart(3, '0')}.jpg`,
      width: 800, height: 600,
    });
  }
  return out;
}
let IMAGES = imgs(4);

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
    beforeParse(window) { window.pywebview = { api: {} }; },
  });
  const { window } = dom;
  const doc = window.document;
  const $ = (id) => doc.getElementById(id);
  const cells = () => [...$('ceGrid').children];
  const selCount = () => cells().filter((c) => c.classList.contains('sel')).length;

  const delCalls = [];
  const stub = {
    get_state: () => Promise.resolve(STATE),
    library_state: () => Promise.resolve({
      works: [WORK], chapters: [], orphans: [ORPHAN], pending: [],
    }),
    list_works: () => Promise.resolve({ works: [{
      id: WORK.id, name: WORK.name, author: WORK.author, tags: [],
      cover_url: null, chapter_count: 1, updated_at: '2026-10-01T00:00:00+00:00',
    }] }),
    work_detail: () => Promise.resolve({
      id: WORK.id, name: WORK.name, author: WORK.author, tags: [],
      cover_url: null, chapters: WORK.chapters,
    }),
    chapter_images: () => Promise.resolve({ images: IMAGES }),
    delete_chapter_images: (cid, files) => {
      delCalls.push({ cid, files: files.slice() });
      const left = IMAGES.filter((im) => !files.includes(im.file));
      IMAGES = left.map((im, i) => ({ ...im,
        file: `images/${String(i + 1).padStart(3, '0')}.jpg`, index: i + 1 }));
      return Promise.resolve({ ok: true, removed: files.length, remaining: IMAGES.length });
    },
    begin_edit: () => Promise.resolve({ ok: true }),
    edit_dirty: () => Promise.resolve({ dirty: false }),
    cancel_edit: () => Promise.resolve({ ok: true }),
    commit_edit: () => Promise.resolve({ ok: true }),
    update_work: () => Promise.resolve({ ok: true, name: WORK.name, author: WORK.author }),
    add_chapters: () => Promise.resolve({ ok: true }),
    list_incomplete: () => Promise.resolve({ items: [] }),
    update_config: () => Promise.resolve({ ok: true, config: CFG, shelf_dir: CFG.shelf_dir, download_dir: '' }),
  };
  Object.assign(window.pywebview.api, stub);
  window.dispatchEvent(new window.Event('pywebviewready'));
  await sleep(80);

  /* ---------- 入口 1：书架的未入库章节 ---------- */
  console.log('--- 三个入口 ---');
  await window.renderShelf();
  await sleep(40);
  const orRow = $('orphans').querySelector('.item');
  const orBtns = [...orRow.querySelectorAll('button')].map((b) => b.textContent);
  ok(orBtns.includes('编辑') && orBtns.includes('新建作品并加入') && orBtns.includes('更多 ▾'),
    `未入库行三个按钮并排：${orBtns.join(' / ')}`);
  ok(orBtns.indexOf('编辑') < orBtns.indexOf('更多 ▾'), '「编辑」是平级按钮，不是塞进「更多」里');

  [...orRow.querySelectorAll('button')].find((b) => b.textContent === '编辑').click();
  await sleep(60);
  ok($('page-chapter').classList.contains('show') && !$('page-shelf').classList.contains('show'),
    '从未入库行进入章节编辑页');
  ok(cells().length === 4, `网格列出 4 张图：${cells().length}`);
  ok($('ceCount').textContent === '已选 0 / 共 4 张', `计数：${$('ceCount').textContent}`);
  ok($('btnCeSave').disabled, '一张都没选时保存按钮 disabled');
  ok($('btnCeBack').textContent === '← 书架', `来源写在返回键上：${$('btnCeBack').textContent}`);

  /* ---------- 选择 ---------- */
  console.log('--- 点选 / Shift 范围选 ---');
  cells()[1].click();
  await sleep(20);
  ok(selCount() === 1 && cells()[1].classList.contains('sel'), '点一张就选中');
  ok($('ceCount').textContent === '已选 1 / 共 4 张', `计数跟上：${$('ceCount').textContent}`);
  ok($('btnCeSave').textContent === '删除选中的 1 张', `按钮带上张数：${$('btnCeSave').textContent}`);
  ok(!$('btnCeSave').disabled, '选了之后保存按钮可用');

  // Shift 点第 4 张：把「第 2 张被切成的新状态」套到第 2~4 张
  cells()[3].dispatchEvent(new window.MouseEvent('click', { bubbles: true, shiftKey: true }));
  await sleep(20);
  ok(selCount() === 3, `Shift 选中第 2~4 张：${selCount()}`);
  ok(cells()[0].classList.contains('sel') === false, '范围之外的第 1 张没被带上');

  $('btnCeInvert').click();
  await sleep(20);
  ok(selCount() === 1 && cells()[0].classList.contains('sel'), `反选后只剩第 1 张：${selCount()}`);
  $('btnCeAll').click();
  await sleep(20);
  ok(selCount() === 4, '全选');
  $('btnCeNone').click();
  await sleep(20);
  ok(selCount() === 0 && $('btnCeSave').disabled, '清空后回到 0 张、保存按钮重新 disabled');

  /* ---------- 防误删：不点保存就退出 ---------- */
  console.log('--- 只选中不保存 ---');
  cells()[0].click();
  cells()[2].click();
  await sleep(20);
  ok(selCount() === 2, '选中 2 张');
  $('btnCeBack').click();
  await sleep(40);
  ok(delCalls.length === 0, `点了返回：删除接口一次都没被调用（${delCalls.length} 次）`);
  ok(IMAGES.length === 4, '图片没少');
  ok($('page-shelf').classList.contains('show'), '回到书架');

  /* ---------- 保存才真的删 ---------- */
  console.log('--- 点保存 ---');
  [...$('orphans').querySelector('.item').querySelectorAll('button')]
    .find((b) => b.textContent === '编辑').click();
  await sleep(60);
  cells()[1].click();          // images/002.jpg
  cells()[2].click();          // images/003.jpg
  await sleep(20);
  ok(selCount() === 2, '选中 2 张');
  $('btnCeSave').click();
  await sleep(30);
  ok($('modal').classList.contains('show'), '保存先弹确认框');
  ok($('modalBox').textContent.includes('不可恢复'), '确认框写明不可恢复');
  $('m_cancel').click();
  await sleep(30);
  ok(delCalls.length === 0, '确认框里点取消：不删');
  ok(!$('modal').classList.contains('show'), '取消后弹窗关掉');

  $('btnCeSave').click();
  await sleep(30);
  $('m_ok').click();
  await sleep(80);
  ok(delCalls.length === 1, `确认后调了一次删除：${delCalls.length}`);
  ok(delCalls[0].cid === 'ch9', `删的是这一章：${delCalls[0] && delCalls[0].cid}`);
  ok(JSON.stringify(delCalls[0].files) === JSON.stringify(['images/002.jpg', 'images/003.jpg']),
    `带的正是选中的两张：${JSON.stringify(delCalls[0] && delCalls[0].files)}`);
  ok($('page-shelf').classList.contains('show'), '删完回到来源页（书架）');

  /* ---------- 入口 2 / 3：编辑作品页 ---------- */
  console.log('--- 编辑作品页的两个入口 ---');
  await window.openEdit('w01', 'shelf');
  await sleep(80);
  const inRow = $('editChapters').querySelector('.item');
  const inBtns = [...inRow.querySelectorAll('button')].map((b) => b.textContent);
  ok(inBtns.includes('编辑') && inBtns.includes('移除'),
    `已入库章节行有「编辑」：${inBtns.join(' / ')}`);
  ok(inBtns.indexOf('编辑') < inBtns.indexOf('移除'), '「编辑」在「移除」之前（危险动作放最后）');
  const orRow2 = $('editOrphans').querySelector('.item');
  ok([...orRow2.querySelectorAll('button')].map((b) => b.textContent).includes('编辑'),
    '未入库章节行也有「编辑」');

  [...inRow.querySelectorAll('button')].find((b) => b.textContent === '编辑').click();
  await sleep(60);
  ok($('page-chapter').classList.contains('show'), '从已入库行进入章节编辑页');
  ok($('btnCeBack').textContent === '← 编辑作品', `来源写在返回键上：${$('btnCeBack').textContent}`);
  cells()[0].click();
  await sleep(20);
  $('btnCeSave').click();
  await sleep(30);
  ok($('modalBox').textContent.includes('不受编辑作品页'),
    '从编辑页进来时，确认框提示删除不受「放弃修改」回滚');
  $('m_ok').click();
  await sleep(80);
  ok($('page-edit').classList.contains('show'), '删完回到编辑作品页（而不是书架）');

  /* ---------- 空章节 ---------- */
  console.log('--- 没有图片的章节 ---');
  IMAGES = [];
  await window.openChapterEdit('ch1', 'shelf', '空章');
  await sleep(60);
  ok(!$('page-chapter').classList.contains('show'), '空章节不进编辑页');
  ok($('modalBox').textContent.includes('没有可删除'), '给提示而不是开一个空页面');
  $('m_ok').click();

  console.log();
  window.close();
  if (fail.length) {
    console.log(`FAILED: ${fail.length}`);
    fail.forEach((f) => console.log('  - ' + f));
    process.exit(1);
  }
  console.log('OK 章节编辑页全部通过');
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
