/* 重构后的界面冒烟测试：三页签、设置页目录配置、未入库区导入下拉、章节主键。 */
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
const CID = 'opus_1261150454223470609';
const LIB = {
  works: [{ id: 'w1', name: '测试作品', author: '长夜月的长夜', cover_url: null, chapter_count: 1 }],
  chapters: [{ id: CID, opus_id: '1261150454223470609', source: 'opus', title: '第一章',
               author: '长夜月的长夜', path: `chapters/${CID}`, image_count: 3, ok_count: 3,
               note: '', assigned: true, cover_url: null }],
  orphans: [{ id: 'cabc1234', opus_id: '', source: 'local', title: '导入章节 2026-10-21',
              author: '', path: 'chapters/cabc1234', image_count: 5, ok_count: 5,
              note: '', assigned: false, cover_url: null }],
  pending: [{ opus_id: '999', title: '暂存的一篇', author: '某人', path: 'X:/dl/某人/999_x',
              image_count: 4, ok_count: 4 }],
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
      window.pywebview = { api: {} };
    },
  });
  const { window } = dom;
  const $ = (id) => window.document.getElementById(id);
  const txt = (id) => ($(id) ? $(id).textContent : '');

  const calls = [];
  const stub = {
    get_state: () => Promise.resolve(STATE),
    library_state: () => Promise.resolve(LIB),
    list_works: () => Promise.resolve({ works: LIB.works }),
    mark_read: () => Promise.resolve({ ok: true }),
    work_detail: (wid) => Promise.resolve({
      id: wid, name: '测试作品', author: '长夜月的长夜', cover_url: null,
      chapters: [{ index: 1, chapter_id: CID, opus_id: '1261150454223470609', source: 'opus',
                   title: '第一章', note: '', cover_url: null, path: `chapters/${CID}` }],
    }),
    chapter_images: (cid) => { calls.push(['chapter_images', cid]);
      return Promise.resolve({ images: [{ url: '/outputs/a.jpg', width: 800, height: 1200 }] }); },
    list_incomplete: () => Promise.resolve({ items: [{ id: 'opus_999', name: 'opus_999', files: 2, path: 'chapters/opus_999' }] }),
    purge_incomplete: (n) => { calls.push(['purge_incomplete', n]); return Promise.resolve({ ok: true, removed: 1 }); },
    import_files: (t, a, p) => { calls.push(['import_files', p.length]); return Promise.resolve({ ok: true }); },
    import_folder: (t, a, p) => { calls.push(['import_folder', p]); return Promise.resolve({ ok: true }); },
    import_pending: (ids) => { calls.push(['import_pending', ids]); return Promise.resolve({ ok: true }); },
    rename_chapter: () => Promise.resolve({ ok: true }),
    delete_chapter: () => Promise.resolve({ ok: true }),
    choose_files: () => Promise.resolve({ paths: ['C:/a/1.jpg', 'C:/a/2.jpg'] }),
    choose_folder: () => Promise.resolve({ path: 'C:/a' }),
    choose_file: () => Promise.resolve({ path: '' }),
    add_chapters: (wid, ids) => { calls.push(['add_chapters', ids]); return Promise.resolve({ ok: true }); },
    update_config: (cfg) => { calls.push(['update_config', Object.keys(cfg).join(',')]);
      return Promise.resolve({ ok: true, config: CFG, shelf_dir: CFG.shelf_dir, download_dir: '' }); },
    create_work: () => Promise.resolve({ id: 'w2' }),
    begin_edit: () => Promise.resolve({ ok: true }),
    commit_edit: () => Promise.resolve({ ok: true }),
    cancel_edit: () => Promise.resolve({ ok: true }),
    edit_dirty: () => Promise.resolve({ dirty: false }),
    open_dir: () => Promise.resolve({ ok: true }),
    // 无边框窗口自绘控制栏（v1.22）
    minimize_window: () => { calls.push(['minimize_window']); return Promise.resolve({ ok: true }); },
    maximize_window: () => { calls.push(['maximize_window']); return Promise.resolve({ ok: true, maximized: true }); },
    close_window: () => { calls.push(['close_window']); return Promise.resolve({ ok: true }); },
    is_maximized: () => Promise.resolve({ maximized: false }),
    set_taskbar_mode: (k) => { calls.push(['set_taskbar_mode', k]); return Promise.resolve({ ok: true }); },
    restore_at: (x, y) => { calls.push(['restore_at', x, y]); return Promise.resolve({ ok: true, maximized: false }); },
    move_window: (x, y) => { calls.push(['move_window', x, y]); return Promise.resolve({ ok: true }); },
  };
  Object.assign(window.pywebview.api, stub);

  window.dispatchEvent(new window.Event('pywebviewready'));
  await sleep(200);

  ok(!$('bootMask'), '启动遮罩已移除');
  ok($('page-shelf').classList.contains('show'), '默认进书架页');

  console.log('--- 页签 ---');
  ok(!!$('tab-settings'), 'nav 里有「设置」页签');
  ok(!!$('page-settings'), '存在设置页容器');

  console.log('--- 下载页 ---');
  ok(!$('cfg_out'), '下载页已没有「输出目录」输入框');
  ok(!$('btnOpenOut'), '下载页已没有「打开输出目录」按钮');
  ok(/下载参数/.test(txt('settings')), '下载页卡片标题是「下载参数」');
  ok(!!$('cfg_concurrency') && !!$('cfg_include_cover'), '下载参数（并发/头图）仍在下载页');

  console.log('--- 设置页 ---');
  $('tab-settings').click();
  await sleep(150);
  ok($('page-settings').classList.contains('show'), '能切到设置页');
  ok($('tab-settings').classList.contains('active'), '设置页签高亮');
  ok($('cfg_shelf_dir').value === CFG.shelf_dir, '书架目录已填入当前值');
  ok($('cfg_download_dir').value === '', '暂存目录为空（单目录模式）');
  ok($('btnOpenDownload').disabled === true, '暂存目录为空时「打开暂存目录」禁用');
  await sleep(120);
  ok(/opus_999/.test(txt('incompleteList')), '设置页列出了未完成残留');
  const cb = window.document.querySelector('#incompleteList .icb');
  ok(!!cb, '残留项有勾选框');
  cb.checked = true;
  $('btnPurgeIncomplete').click();
  await sleep(120);
  const okBtn = $('m_ok');
  ok(!!okBtn, '点清理会先弹确认框');
  okBtn.click();
  await sleep(150);
  ok(calls.some(c => c[0] === 'purge_incomplete'), '确认后调用了 purge_incomplete');

  console.log('--- 书架页：未入库区与导入下拉 ---');
  $('tab-shelf').click();
  await sleep(200);
  ok($('orphans').children.length === 1, '未入库区列出 1 条');
  ok(/本地导入/.test(txt('orphans')), '本地导入的章节标注了来源');
  const menuBtn = $('importMenu').querySelector('button');
  ok(!!menuBtn && /导入/.test(menuBtn.textContent), '未入库区头部有「导入…」下拉按钮');
  menuBtn.click();
  const items = Array.from($('importMenu').querySelectorAll('.menuItem')).map(b => b.textContent);
  // 5 项：图片文件 / 文件夹 / 视频章节（原样播） / 视频转图集 / 暂存目录
  // （v1.19 起视频有两条并列的去处，最后一项是有待导入内容时才出现）
  ok(items.length === 5, `下拉有 5 项：${items.join(' / ')}`);
  ok(/^导入视频章节/.test(items[2] || ''), '第三项是「导入视频章节（原样播放）…」');
  ok(/^从视频导入（转图集）/.test(items[3] || ''), '第四项是「从视频导入（转图集）…」');
  ok(/从暂存目录导入（1）/.test(items[4] || ''), '暂存目录有 1 篇时动态出现第五项并标数量');
  // 点「导入图片文件…」
  const fileItem = Array.from($('importMenu').querySelectorAll('.menuItem'))
    .find(b => /导入图片文件/.test(b.textContent));
  fileItem.click();
  await sleep(200);
  ok(calls.some(c => c[0] === 'import_files' && c[1] === 2), '导入图片文件传了 2 个路径');

  console.log('--- 章节主键 ---');
  window.openReader('w1', 0);
  await sleep(200);
  ok(calls.some(c => c[0] === 'chapter_images' && c[1] === CID),
    `阅读器按 chapter_id 取图（实际 ${JSON.stringify(calls.filter(c => c[0] === 'chapter_images'))}）`);

  console.log('--- 设置页：实验性功能分区 ---');
  $('tab-settings').click();
  await sleep(150);
  ok(!!$('expHead') && !!$('expBody'), '设置页有「实验性功能」折叠分区');
  ok(/实验性功能/.test(txt('expHead')), '分区标题是「实验性功能」');
  ok(!!$('cfg_import_mode'), '分区里有导入方式下拉');
  ok(/单目录模式/.test(txt('expState')), '未填暂存目录时状态显示「单目录模式」');
  ok($('expBody').style.display === 'none', '单目录模式下默认折叠');
  $('expHead').click();
  await sleep(80);
  ok($('expBody').style.display === 'block', '点标题能展开');
  ok($('expHead').classList.contains('open'), '展开时标题有 open 态（箭头朝下）');
  $('expHead').click();
  await sleep(80);
  ok($('expBody').style.display === 'none', '再点能收起');

  console.log('--- 最大化 / 还原动画：默认必须是开着的 ---');
  // v1.24.0 起走合成层做法（锁布局 + transform 缩放，不重排），默认打开。
  // 万一哪天又变回默认关，用户打开程序根本看不到这段过渡 —— 等于白做。
  ok(window.eval('uiWinAnim')() === true,
     '默认开启：没存过设置时就应该有动画');
  ok($('cfg_ui_win_anim') && $('cfg_ui_win_anim').checked,
     '设置页那个开关默认勾着（要和实际生效值一致）');
  window.localStorage.setItem('uiWinAnim', '0');
  ok(window.eval('uiWinAnim')() === false, '手动关掉后要真的生效');
  window.localStorage.removeItem('uiWinAnim');
  ok(window.eval('uiWinAnim')() === true, '清掉后又回到默认开启');

  console.log('--- 保存设置 ---');
  $('tab-settings').click();
  await sleep(150);
  $('cfg_download_dir').value = 'D:/dl';
  $('btnSaveDirs').click();
  await sleep(150);
  const cfgCall = calls.filter(c => c[0] === 'update_config').pop();
  ok(cfgCall && /shelf_dir/.test(cfgCall[1]) && /download_dir/.test(cfgCall[1])
     && /import_mode/.test(cfgCall[1]),
    `保存设置提交目录与导入方式（实际 ${cfgCall && cfgCall[1]}）`);
  ok(/设置已保存/.test(txt('toast')), '保存设置成功后给一个明确的成功提示');
  ok(calls.some(c => c[0] === 'set_taskbar_mode'),
    '启动时把「最大化避开任务栏」的设置同步给了后端（否则开关是死的）');

  console.log('--- 无边框窗口：自绘控制栏 ---');
  ok(!!$('winControls'), '存在窗口控制栏 #winControls');
  ok(!!$('winMin') && !!$('winMax') && !!$('winClose'), '控制栏有最小化/最大化/关闭三个按钮');
  // 无边框之后没有原生标题栏，拖窗口全靠 nav 上的 pywebview-drag-region
  ok(window.document.querySelector('nav').classList.contains('pywebview-drag-region'),
    'nav 带 pywebview-drag-region（否则无边框后拖不动窗口）');
  $('winMin').click(); await sleep(60);
  ok(calls.some(c => c[0] === 'minimize_window'), '点最小化调 minimize_window');
  $('winMax').click(); await sleep(80);
  ok(calls.some(c => c[0] === 'maximize_window'), '点最大化调 maximize_window');
  $('winClose').click(); await sleep(60);
  ok(calls.some(c => c[0] === 'close_window'), '点关闭调 close_window');

  // 最大化时 pywebview 自己的拖拽会和「拖顶栏脱离最大化」抢 move，必须摘掉
  ok(!window.document.querySelector('nav').classList.contains('pywebview-drag-region'),
    '最大化后摘掉 nav 的 drag-region（避免两套拖拽互相抢 move）');
  ok($('winRestoreIcon').style.display === 'block' && $('winMaxIcon').style.display === 'none',
    '最大化后最大化按钮切成「还原」图标');

  console.log('--- 无边框窗口：拖拽守卫的边界 ---');
  {
    // 全局捕获阶段会对交互元素 stopPropagation，但绝不能连图片一起吃掉 ——
    // singleImg 的 mousedown 是缩放拖动的起手，被吃掉就等于拖不动放大后的图
    let imgProbe = 0;
    $('singleImg').addEventListener('mousedown', () => { imgProbe++; });
    $('singleImg').dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true, button: 0 }));
    ok(imgProbe === 1, '图片上的 mousedown 不被拖拽守卫吃掉（缩放拖动的前提）');

    // 反过来：按钮上的 mousedown 必须被拦住，否则 pywebview 会把点按钮当成拖窗口
    let btnProbe = 0;
    window.document.addEventListener('mousedown', () => { btnProbe++; });
    $('tab-shelf').dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true, button: 0 }));
    ok(btnProbe === 0, '按钮上的 mousedown 不冒泡到 body（pywebview 不会误判成拖窗口）');
  }

  console.log('--- 阅读器内控制栏让位 ---');
  window.openReader('w1', 0); await sleep(200);
  ok(window.document.body.classList.contains('in-reader'), '进阅读器时 body 加 in-reader');
  ok($('winControls').style.height === '44px',
    `进阅读器时控制栏收到 44px（实际 ${$('winControls').style.height}）`);
  window.switchPage('shelf'); await sleep(150);
  ok(!window.document.body.classList.contains('in-reader'), '离开阅读器时 body 去掉 in-reader');

  console.log('--- 已启用双目录的用户必须能找到入口 ---');
  {
    const cfg2 = { ...CFG, download_dir: 'D:/dl' };
    const stub2 = { ...stub, get_state: () => Promise.resolve({ ...STATE, config: cfg2, download_dir: 'D:/dl' }) };
    const dom2 = new JSDOM(html, {
      url: 'http://127.0.0.1:8765/', runScripts: 'dangerously',
      beforeParse(w) { w.pywebview = { api: {} }; },
    });
    Object.assign(dom2.window.pywebview.api, stub2);
    dom2.window.dispatchEvent(new dom2.window.Event('pywebviewready'));
    await sleep(220);
    const body2 = dom2.window.document.getElementById('expBody');
    ok(body2.style.display === 'block',
      '已填暂存目录且没手动折叠过 → 默认展开（否则这类用户再也找不到入口）');
    ok(/双目录模式/.test(dom2.window.document.getElementById('expState').textContent),
      '状态标签显示「双目录模式」');
    dom2.window.close();
  }

  console.log('');
  console.log(fail.length ? `FAILED ${fail.length}` : 'ALL PASS');
  process.exit(fail.length ? 1 : 0);
})();
