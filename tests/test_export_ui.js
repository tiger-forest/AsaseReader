/* 导出对话框的界面测试：三个入口、目录与命名、进度轮询、完成后「打开文件夹」。
   jsdom + 打桩 api，不启真实服务（照抄 test_ui.js 的路子）。 */
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
  export_dir: 'D:/exported', export_naming: 'title',
  setup_done: true,
};
const STATE = makeState({
  jobs: [], config: CFG, shelf_dir: CFG.shelf_dir, download_dir: '',
  daily_used: 0, running: false, paused: false, pending: 0,
});
const CID = 'opus_1261150454223470609';
const LIB = {
  works: [{ id: 'w1', name: '测试作品', author: '某人', cover_url: null, chapter_count: 1 }],
  chapters: [{ id: CID, opus_id: '1261150454223470609', source: 'opus', title: '第一章',
               author: '某人', path: `chapters/${CID}`, image_count: 3, ok_count: 3,
               note: '', assigned: true, cover_url: null }],
  orphans: [{ id: 'cabc1234', opus_id: '', source: 'local', title: '导入章节',
              author: '', path: 'chapters/cabc1234', image_count: 5, ok_count: 5,
              note: '', assigned: false, cover_url: null }],
  pending: [],
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
    beforeParse(window) { window.pywebview = { api: {} }; },
  });
  const { window } = dom;
  const $ = (id) => window.document.getElementById(id);
  const txt = (id) => ($(id) ? $(id).textContent : '');

  const calls = [];
  // 导出状态：第一次轮询返回「正在跑」，之后返回「已完成」
  let statusCalls = 0;
  const stub = {
    get_state: () => Promise.resolve(STATE),
    library_state: () => Promise.resolve(LIB),
    list_works: () => Promise.resolve({ works: LIB.works }),
    work_detail: (wid) => Promise.resolve({
      id: wid, name: '测试作品', author: '某人', cover_url: null,
      chapters: [{ index: 1, chapter_id: CID, opus_id: '1261150454223470609', source: 'opus',
                   title: '第一章', note: '', cover_url: null, path: `chapters/${CID}` }],
    }),
    list_incomplete: () => Promise.resolve({ items: [] }),
    choose_folder: () => Promise.resolve({ path: 'E:/picked' }),
    open_dir: (p) => { calls.push(['open_dir', p]); return Promise.resolve({ ok: true }); },
    export_start: (kind, id, dest, naming) => {
      calls.push(['export_start', kind, id, dest, naming]);
      statusCalls = 0;
      return Promise.resolve({ ok: true, total: 3, dest });
    },
    export_status: () => {
      statusCalls += 1;
      const running = statusCalls < 2;
      return Promise.resolve({
        running, finished: !running, cancelled: false, done: running ? 1 : 3, total: 3,
        current: '002.jpg', copied: 3, skipped: 0, failed: 0, dest: 'D:/exported', error: '',
      });
    },
    export_cancel: () => { calls.push(['export_cancel']); return Promise.resolve({ ok: true }); },
  };
  Object.assign(window.pywebview.api, stub);
  window.dispatchEvent(new window.Event('pywebviewready'));
  await sleep(220);

  console.log('--- 设置页入口 ---');
  $('tab-settings').click();
  await sleep(150);
  ok(!!$('btnExportAll'), '设置页有「导出整个书库」按钮');
  $('btnExportAll').click();
  await sleep(120);
  ok($('modal').classList.contains('show'), '点导出会弹出对话框');
  ok(/导出整个书库/.test(txt('modalBox')), '对话框标题跟着范围变');
  ok(!!$('m_dest') && !!$('m_naming') && !!$('m_pickDest'), '对话框里有目录输入、命名下拉、浏览按钮');
  ok($('m_dest').value === 'D:/exported', '目录默认带出上次导出的位置');
  ok($('m_naming').value === 'title', '命名方式默认带出上次的选择');
  ok(/请勿二次分发/.test(txt('modalBox')), '对话框里有版权提醒');

  console.log('--- 浏览… 与命名切换 ---');
  $('m_pickDest').click();
  await sleep(150);
  ok($('m_dest').value === 'E:/picked', '「浏览…」把选中的目录填回输入框');

  console.log('--- 开始导出 + 进度 ---');
  $('m_naming').value = 'numbered';
  $('m_ok').click();
  await sleep(200);
  const startCall = calls.find((c) => c[0] === 'export_start');
  ok(!!startCall, '点「开始导出」调用了 export_start');
  ok(startCall && startCall[1] === 'all' && startCall[3] === 'E:/picked'
     && startCall[4] === 'numbered',
     `参数正确：${startCall && JSON.stringify(startCall.slice(1))}`);
  ok($('m_prog').style.display === 'block', '进度区域已显示');
  ok(/正在导出/.test(txt('m_prog')), `轮询中显示进度（${txt('m_prog')}）`);
  ok($('m_ok').disabled === true, '导出中「开始导出」禁用，避免重复触发');
  ok($('m_cancelExport').style.display !== 'none', '导出中出现「取消导出」按钮');

  await sleep(450);
  ok(/已导出 3 个文件/.test(txt('m_prog')), `完成后显示结果（${txt('m_prog')}）`);
  ok($('m_ok').textContent === '打开文件夹', '完成后按钮变成「打开文件夹」');
  ok($('m_cancelExport').style.display === 'none', '完成后取消按钮收起');
  $('m_ok').click();
  await sleep(120);
  ok(calls.some((c) => c[0] === 'open_dir' && c[1] === 'E:/picked'), '点它能打开导出目录');

  console.log('--- 取消 ---');
  window.openExport('all', '');
  await sleep(120);
  $('m_ok').click();
  await sleep(120);
  $('m_cancelExport').click();
  await sleep(80);
  ok(calls.some((c) => c[0] === 'export_cancel'), '「取消导出」调用了 export_cancel');

  console.log('--- 没填目录时不要瞎跑 ---');
  calls.length = 0;
  window.openExport('all', '');
  await sleep(120);
  $('m_dest').value = '   ';
  $('m_ok').click();
  await sleep(150);
  ok(!calls.some((c) => c[0] === 'export_start'), '目录为空时不发起导出');
  ok(/先选一个导出目录/.test(txt('modalBox')), '并提示先选目录');
  window.hideModal();

  console.log('--- 作品页入口 ---');
  await window.openWork('w1');
  await sleep(200);
  ok(!!$('btnExportWork'), '作品页有「导出作品…」按钮');
  calls.length = 0;
  $('btnExportWork').click();
  await sleep(150);
  $('m_ok').click();
  await sleep(200);
  const workCall = calls.find((c) => c[0] === 'export_start');
  ok(workCall && workCall[1] === 'work' && workCall[2] === 'w1',
     `作品页传的是 work + 作品 id（${workCall && JSON.stringify(workCall.slice(1))}）`);
  window.hideModal();

  console.log('--- 章节行入口（未入库区） ---');
  $('tab-shelf').click();
  await sleep(220);
  const menuBtns = Array.from($('orphans').querySelectorAll('.menuItem')).map((b) => b.textContent);
  ok(menuBtns.some((t) => /导出这一章/.test(t)), `章节行的更多菜单里有「导出这一章…」（${menuBtns.join('/')}）`);
  calls.length = 0;
  const exportItem = Array.from($('orphans').querySelectorAll('.menuItem'))
    .find((b) => /导出这一章/.test(b.textContent));
  exportItem.click();
  await sleep(150);
  $('m_ok').click();
  await sleep(200);
  const chCall = calls.find((c) => c[0] === 'export_start');
  ok(chCall && chCall[1] === 'chapter' && chCall[2] === 'cabc1234',
     `单章导出传的是 chapter + chapter_id（${chCall && JSON.stringify(chCall.slice(1))}）`);

  console.log('');
  console.log(fail.length ? `FAILED ${fail.length}` : 'ALL PASS');
  process.exit(fail.length ? 1 : 0);
})();
