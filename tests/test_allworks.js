/* 所有作品页 + 首页限流 + 卡片右键菜单（v1.5）。
 *
 * 覆盖：
 *   1) 首页只渲染「最近更新」前 12 个，超出时底部出现「还有 X 个 → 查看全部」；
 *   2) 卡片不再有三按钮行（.ops），改为悬浮 ⋯ + 右键浮层菜单（编辑/导出/删除）；
 *   3) 所有作品页整页列出全部作品，搜索过滤 + 命中计数（平时不显示）；
 *   4) 封面懒加载（loading="lazy"）；
 *   5) CSS 约束断言（JSDOM 不做排版，只能断类名与声明——实际观感仍要人工看）。
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
// 每次 renderAllWorks 都会调一次 list_works：用它数渲染次数，验搜索防抖
let listWorksCalls = 0;
const STATE = makeState({
  jobs: [], config: CFG, shelf_dir: CFG.shelf_dir, download_dir: '',
  daily_used: 0, running: false, paused: false, pending: 0,
});

// 15 个作品： stub 顺序即「后端已排好的最近更新倒序」，前端应原样透传
function mkWorks(n) {
  const out = [];
  for (let i = 1; i <= n; i++) {
    out.push({
      id: 'w' + String(i).padStart(2, '0'),
      name: 'Alpha' + String(i).padStart(2, '0'),
      author: i % 2 ? '作者A' : '作者B',
      cover_url: '/outputs/_covers/w' + i + '.jpg',
      chapter_count: i,
      tags: i === 3 ? ['横版'] : (i === 7 ? ['连载', '横版'] : []),
      updated_at: '2026-10-' + String(20 - (i % 20)).padStart(2, '0'),
    });
  }
  return out;
}
let WORKS = mkWorks(15);
const LIB = { works: WORKS, chapters: [], orphans: [], pending: [] };

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
  const doc = window.document;
  const $ = (id) => doc.getElementById(id);

  const calls = [];
  const stub = {
    get_state: () => Promise.resolve(STATE),
    library_state: () => Promise.resolve(LIB),
    list_works: () => { listWorksCalls++; return Promise.resolve({ works: WORKS }); },
    work_detail: (wid) => { calls.push(['work_detail', wid]); return Promise.resolve({
      id: wid, name: 'X', author: 'Y', cover_url: null, chapters: [],
    }); },
    delete_work: (wid) => { calls.push(['delete_work', wid]); return Promise.resolve({ ok: true }); },
    begin_edit: () => Promise.resolve({ ok: true }),
    edit_dirty: () => Promise.resolve({ dirty: false }),
    commit_edit: () => Promise.resolve({ ok: true }),
    update_work: (wid, patch) => Promise.resolve({ ok: true, id: wid, patch }),
    cancel_edit: () => Promise.resolve({ ok: true }),
    list_incomplete: () => Promise.resolve({ items: [] }),
    update_config: () => Promise.resolve({ ok: true, config: CFG, shelf_dir: CFG.shelf_dir, download_dir: '' }),
  };
  Object.assign(window.pywebview.api, stub);
  window.dispatchEvent(new window.Event('pywebviewready'));
  await sleep(80);

  /* ---------- CSS 静态断言 ---------- */
  console.log('--- CSS 约束 ---');
  const css = (html.match(/<style>([\s\S]*?)<\/style>/) || [null, ''])[1];
  ok(!/\.workCard\s+\.ops/.test(css), '.workCard .ops 死样式已清理（三按钮行已删）');
  ok(/\.workCard:hover\s+\.moreBtn/.test(css) && /:focus-within\s+\.moreBtn/.test(css),
     '⋯ 按钮在 hover 与 focus-within 时显示（键盘可达）');
  ok(/#ctxMenu\s*\{[^}]*position:\s*fixed/.test(css), '#ctxMenu 是 fixed 定位浮层');
  ok(/\.workCard\s*\{[^}]*position:\s*relative/.test(css), '.workCard 是 ⋯ 的定位参照（relative）');
  // 两个 auto 会把剩余空间均分 → 第一个按钮被推到行中间，必须让第二个归零
  ok(/\.secthead\s*>\s*button\s*~\s*button\s*\{[^}]*margin-left:\s*0/.test(css),
     '.secthead 里第二个及以后的按钮 margin-left:0（否则「所有作品」被均分到行中间）');

  /* ---------- 首页限流 ---------- */
  console.log('--- 首页限流 12 ---');
  ok($('page-shelf').classList.contains('show'), '启动进书架页');
  let cards = $('worksGrid').querySelectorAll('.workCard');
  ok(cards.length === 12, `首页只渲染前 12 个（实际 ${cards.length}）`);
  ok(cards[0].querySelector('.name').textContent === 'Alpha01', '顺序透传后端排序（第一张 = Alpha01）');
  ok($('worksMore').style.display !== 'none', '超出 12 个时显示「还有 N 个」提示');
  ok($('worksMore').textContent.includes('还有 3 个作品'), `提示带数量：${$('worksMore').textContent}`);
  ok($('btnAllWorks').textContent.trim() === '所有作品', '入口按钮就叫「所有作品」，不带数量');

  // 卡片结构：没有 .ops 行，有 ⋯，可聚焦
  const c0 = cards[0];
  ok(!c0.querySelector('.ops'), '卡片上没有 .ops 三按钮行');
  ok(!!c0.querySelector('.moreBtn'), '卡片右上角有 ⋯ 按钮');
  ok(c0.tabIndex === 0, '卡片可键盘聚焦（tabindex=0）');
  const coverImg = c0.querySelector('.cover img');
  ok(coverImg && coverImg.loading === 'lazy', '封面懒加载 loading="lazy"');

  /* ---------- 左键 = 阅读 ---------- */
  console.log('--- 左键 / 键盘 ---');
  c0.click();
  await sleep(20);
  ok(calls.some(c => c[0] === 'work_detail' && c[1] === 'w01'), '左键整卡打开作品（openWork → work_detail）');
  ok($('page-work').classList.contains('show'), '进入作品页');
  $('btnWorkBack').click();
  await sleep(30);
  ok($('page-shelf').classList.contains('show'), '返回书架');
  const beforeEnter = calls.filter(c => c[0] === 'work_detail').length;
  $('worksGrid').querySelectorAll('.workCard')[1]
    .dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  await sleep(20);
  ok(calls.filter(c => c[0] === 'work_detail').length === beforeEnter + 1, 'Enter 键打开作品');
  $('btnWorkBack').click();
  await sleep(30);

  /* ---------- 右键浮层菜单 ---------- */
  console.log('--- 右键菜单 ---');
  const c1 = $('worksGrid').querySelectorAll('.workCard')[1];
  c1.dispatchEvent(new window.MouseEvent('contextmenu', { bubbles: true, clientX: 100, clientY: 100 }));
  const menu = $('ctxMenu');
  ok(menu.style.display === 'block', '右键弹出浮层菜单');
  const items = [...menu.querySelectorAll('.menuItem')].map(b => b.textContent);
  ok(items.join('|') === '编辑作品|导出作品…|删除作品', `菜单项 = ${items.join('|')}`);
  ok(menu.style.left === '100px' && menu.style.top === '100px', '浮层跟随鼠标坐标');

  // 点别处关闭
  doc.body.click();
  ok(menu.style.display === 'none', '点别处关闭菜单');

  // Esc 关闭
  c1.dispatchEvent(new window.MouseEvent('contextmenu', { bubbles: true, clientX: 50, clientY: 50 }));
  doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  ok(menu.style.display === 'none', 'Esc 关闭菜单');

  // 删除：先关菜单再弹确认框（否则浮层压在 modal 上）
  c1.dispatchEvent(new window.MouseEvent('contextmenu', { bubbles: true, clientX: 50, clientY: 50 }));
  [...menu.querySelectorAll('.menuItem')].find(b => b.textContent === '删除作品').click();
  await sleep(10);
  ok(menu.style.display === 'none', '点菜单项后菜单先关闭');
  ok($('modal').classList.contains('show') && $('modalBox').textContent.includes('Alpha02'), '弹出删除确认框');
  const cardToDelete = c1;
  $('m_ok').click();
  await sleep(20);
  ok(cardToDelete.classList.contains('fade-out'), '删除前卡片先淡出（不是凭空消失）');
  ok(!calls.some(c => c[0] === 'delete_work'), '淡出期间还没发请求（等动画演完）');
  await sleep(180);
  ok(calls.some(c => c[0] === 'delete_work' && c[1] === 'w02'), '淡出结束后调用 delete_work');

  /* ---------- ⋯ 按钮同一套菜单 ---------- */
  const moreBtn = $('worksGrid').querySelectorAll('.workCard')[2].querySelector('.moreBtn');
  moreBtn.click();
  ok(menu.style.display === 'block', '⋯ 按钮打开同一个浮层菜单');
  doc.body.click();

  /* ---------- 所有作品页 ---------- */
  console.log('--- 所有作品页 ---');
  $('btnAllWorks').click();
  await sleep(30);
  ok($('page-allworks').classList.contains('show'), '进入所有作品页');
  ok($('allworksGrid').querySelectorAll('.workCard').length === 15, '整页列出全部 15 个作品');
  ok($('awCount').textContent === '', '未筛选时不显示计数');

  // 搜索：名字
  $('awSearch').value = 'Alpha1';
  $('awSearch').dispatchEvent(new window.Event('input', { bubbles: true }));
  await sleep(220);   // 搜索有 150ms 防抖，等它过去
  ok($('allworksGrid').querySelectorAll('.workCard').length === 6, '搜索 Alpha1 命中 6 个（10~15）');
  ok($('awCount').textContent === '命中 6 / 共 15', `筛选时显示计数：${$('awCount').textContent}`);

  // 搜索：标签（字段已埋，UI 先用上）
  $('awSearch').value = '横版';
  $('awSearch').dispatchEvent(new window.Event('input', { bubbles: true }));
  await sleep(220);   // 搜索有 150ms 防抖，等它过去
  ok($('allworksGrid').querySelectorAll('.workCard').length === 2, '搜索标签「横版」命中 2 个');

  // 无匹配
  $('awSearch').value = '不存在的东西';
  $('awSearch').dispatchEvent(new window.Event('input', { bubbles: true }));
  await sleep(220);   // 搜索有 150ms 防抖，等它过去
  ok($('allworksGrid').textContent.includes('没有匹配的作品'), '无匹配时给空态提示');

  // 清空恢复
  $('awSearch').value = '';
  $('awSearch').dispatchEvent(new window.Event('input', { bubbles: true }));
  await sleep(220);   // 搜索有 150ms 防抖，等它过去
  ok($('allworksGrid').querySelectorAll('.workCard').length === 15 && $('awCount').textContent === '',
     '清空搜索恢复全部且计数消失');

  /* ---------- 来源记忆：从哪进的作品页，返回就回哪 ---------- */
  console.log('--- 返回来源记忆 ---');
  $('allworksGrid').querySelectorAll('.workCard')[0].click();
  await sleep(30);
  ok($('page-work').classList.contains('show'), '从所有作品页点卡片进作品页');
  ok($('btnWorkBack').textContent.trim() === '← 所有作品',
     `返回按钮写明来源（实际「${$('btnWorkBack').textContent.trim()}」）`);
  $('btnWorkBack').click();
  await sleep(30);
  ok($('page-allworks').classList.contains('show'), '返回所有作品页，而不是书架');

  // 从书架首页进去的仍然回书架
  $('btnAwBack').click();
  await sleep(30);
  $('worksGrid').querySelectorAll('.workCard')[0].click();
  await sleep(30);
  ok($('btnWorkBack').textContent.trim() === '← 书架', '从书架进的仍显示「← 书架」');
  $('btnWorkBack').click();
  await sleep(30);
  ok($('page-shelf').classList.contains('show'), '从书架进的返回书架');

  // 从所有作品页右键「编辑作品」，编辑页的返回也原路返回
  $('btnAllWorks').click();
  await sleep(30);
  $('allworksGrid').querySelectorAll('.workCard')[0]
    .dispatchEvent(new window.MouseEvent('contextmenu', { bubbles: true, clientX: 10, clientY: 10 }));
  [...$('ctxMenu').querySelectorAll('.menuItem')].find(b => b.textContent === '编辑作品').click();
  await sleep(40);
  ok($('page-edit').classList.contains('show'), '从所有作品页右键进入编辑页');
  $('btnEditBack').click();
  await sleep(40);
  ok($('page-allworks').classList.contains('show'), '编辑页返回所有作品页（原路返回）');
  ok($('btnEditBack').textContent.trim() === '← 所有作品',
     `编辑页返回按钮也写明来源（实际「${$('btnEditBack').textContent.trim()}」）`);

  /* ---------- 作品页右上角的「编辑作品」：来源要跟着作品页走 ---------- */
  console.log('--- 作品页进编辑页的来源记忆 ---');
  // 所有作品页 → 作品页 → 编辑 → 保存并返回，应回所有作品页（不是固定的书架）
  $('allworksGrid').querySelectorAll('.workCard')[0].click();
  await sleep(30);
  $('btnEditWork').click();
  await sleep(40);
  ok($('page-edit').classList.contains('show'), '作品页右上角「编辑作品」进编辑页');
  ok($('btnEditBack').textContent.trim() === '← 所有作品',
     `从所有作品页点进来的，编辑页按钮写明「← 所有作品」（实际「${$('btnEditBack').textContent.trim()}」）`);
  $('btnEditSave').click();
  await sleep(40);
  ok($('page-allworks').classList.contains('show'),
     '作品页进的编辑页，「保存并返回」回所有作品页（不是固定回书架）');

  $('btnAwBack').click();
  await sleep(30);
  ok($('page-shelf').classList.contains('show'), '← 书架返回');

  // 书架 → 作品页 → 编辑 → 保存并返回，回书架（不能因为上一条把来源改成所有作品页就串了）
  $('worksGrid').querySelectorAll('.workCard')[0].click();
  await sleep(30);
  $('btnEditWork').click();
  await sleep(40);
  ok($('btnEditBack').textContent.trim() === '← 书架', '从书架点进来的，编辑页按钮写明「← 书架」');
  $('btnEditSave').click();
  await sleep(40);
  ok($('page-shelf').classList.contains('show'), '从书架点进来的，「保存并返回」回书架');

  // 未截断时不显示提示
  WORKS = mkWorks(5);
  await window.renderShelf();
  ok($('worksGrid').querySelectorAll('.workCard').length === 5, '5 个作品全显示');
  ok($('worksMore').style.display === 'none', '未截断时「还有 N 个」提示隐藏');

  // worksMore 也能进所有作品页
  WORKS = mkWorks(15);
  await window.renderShelf();
  $('worksMore').click();
  await sleep(30);
  ok($('page-allworks').classList.contains('show'), '底部提示可进入所有作品页');

  /* ---------- 分页：每页 24（v1.6）---------- */
  console.log('--- 分页：每页 24 ---');
  ok($('awPager').style.display === 'none', '15 个作品不足一页，不显示分页条');
  ok($('allworksGrid').querySelectorAll('.workCard').length === 15, '不足一页时全部列出');

  WORKS = mkWorks(60);
  await window.renderAllWorks({ resetPage: true });
  ok($('awPager').style.display !== 'none', '60 个作品（3 页）显示分页条');
  ok($('allworksGrid').querySelectorAll('.workCard').length === 24, '第 1 页 24 个');
  ok($('awPageLabel').textContent === '第 1 / 3 页', `页码标签 = ${$('awPageLabel').textContent}`);
  ok($('awFirst').disabled && $('awPrev').disabled, '在第 1 页时「首页 / 上一页」禁用');
  ok(!$('awNext').disabled && !$('awLast').disabled, '在第 1 页时「下一页 / 末页」可用');

  $('awNext').click();
  await sleep(40);
  ok($('awPageLabel').textContent === '第 2 / 3 页', '下一页 → 第 2 页');
  ok($('allworksGrid').querySelectorAll('.workCard').length === 24, '第 2 页也是 24 个');
  ok($('allworksGrid').querySelector('.name').textContent === 'Alpha25',
     `第 2 页从 Alpha25 开始（实际 ${$('allworksGrid').querySelector('.name').textContent}）`);

  $('awLast').click();
  await sleep(40);
  ok($('awPageLabel').textContent === '第 3 / 3 页', '末页 → 第 3 页');
  ok($('allworksGrid').querySelectorAll('.workCard').length === 12, '末页 12 个（60 - 48）');
  ok($('awNext').disabled && $('awLast').disabled, '在末页时「下一页 / 末页」禁用');
  $('awPrev').click();
  await sleep(40);
  ok($('awPageLabel').textContent === '第 2 / 3 页', '上一页回到第 2 页');
  $('awFirst').click();
  await sleep(40);
  ok($('awPageLabel').textContent === '第 1 / 3 页', '「首页」回到第 1 页');

  // 作品变少后当前页越界 → 收回最后一页（clamp）
  $('awLast').click();
  await sleep(40);
  WORKS = mkWorks(30);
  await window.renderAllWorks();
  ok($('awPageLabel').textContent === '第 2 / 2 页',
     `第 3 页没了要收回最后一页（实际 ${$('awPageLabel').textContent}）`);
  ok($('allworksGrid').querySelectorAll('.workCard').length === 6, '新的末页 6 个（30 - 24）');

  /* ---------- 搜索：重置页码 + 防抖 ---------- */
  console.log('--- 搜索：重置页码 / 防抖 ---');
  WORKS = mkWorks(60);
  await window.renderAllWorks({ resetPage: true });
  $('awLast').click();
  await sleep(40);
  $('awSearch').value = '作者A';
  $('awSearch').dispatchEvent(new window.Event('input', { bubbles: true }));
  await sleep(220);
  ok($('awPageLabel').textContent === '第 1 / 2 页',
     `换了关键词回到第 1 页（实际 ${$('awPageLabel').textContent}）`);
  ok($('awCount').textContent === '命中 30 / 共 60', `计数 = ${$('awCount').textContent}`);
  ok($('allworksGrid').querySelectorAll('.workCard').length === 24, '筛选后第 1 页 24 个');

  // 防抖：连打三次只渲染一次
  const before = listWorksCalls;
  for (const v of ['Alpha1', 'Alpha2', 'Alpha3']) {
    $('awSearch').value = v;
    $('awSearch').dispatchEvent(new window.Event('input', { bubbles: true }));
    await sleep(20);
  }
  ok(listWorksCalls === before, `防抖窗口内不渲染（三次输入共 ${listWorksCalls - before} 次）`);
  await sleep(220);
  ok(listWorksCalls === before + 1, `停手后只渲染一次（实际 ${listWorksCalls - before} 次）`);

  /* ---------- 页码记忆：返回保留，主动进入重置 ---------- */
  console.log('--- 页码记忆 ---');
  $('awSearch').value = '';
  $('awSearch').dispatchEvent(new window.Event('input', { bubbles: true }));
  await sleep(220);
  WORKS = mkWorks(60);
  await window.renderAllWorks({ resetPage: true });
  $('awNext').click();
  await sleep(40);
  ok($('awPageLabel').textContent === '第 2 / 3 页', '先停在第 2 页');
  $('allworksGrid').querySelectorAll('.workCard')[0].click();
  await sleep(40);
  ok($('page-work').classList.contains('show'), '从第 2 页点开作品');
  $('btnWorkBack').click();
  await sleep(40);
  ok($('awPageLabel').textContent === '第 2 / 3 页',
     `从作品页返回仍停在第 2 页（实际 ${$('awPageLabel').textContent}）`);

  $('btnAwBack').click();
  await sleep(30);
  $('btnAllWorks').click();
  await sleep(40);
  ok($('awPageLabel').textContent === '第 1 / 3 页',
     `从书架主动进入回到第 1 页（实际 ${$('awPageLabel').textContent}）`);

  // 只有一页时不该出现分页条（5 个作品的库不能凭空多一排按钮）
  WORKS = mkWorks(5);
  await window.renderAllWorks({ resetPage: true });
  ok($('awPager').style.display === 'none', '5 个作品只有一页，分页条隐藏');
  ok($('allworksGrid').querySelectorAll('.workCard').length === 5, '5 个作品全部列出');

  console.log();
  console.log('FAILED:', fail.length);
  for (const f of fail) console.log('  -', f);
  process.exit(fail.length ? 1 : 0);
})();
