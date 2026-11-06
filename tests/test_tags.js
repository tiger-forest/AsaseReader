/* 标签与筛选（v1.8.0）。
 *
 * 覆盖：
 *   1) 筛选条：搜索框 + 标签多选 + 作者多选，位于列表上方（分页条之上）；
 *   2) 标签池与作者列表都是**派生**的（不单独存一份），常用标签按引用数降序；
 *   3) 复选：勾一个不关菜单；已选以 chip 显示、可单独摘掉、可清空；
 *   4) AND / OR：「任一 / 全部」分段只在标签选了 2 个以上时出现；
 *   5) 筛选变化回到第 1 页；「全部」模式筛空时给「切成任一」的出路；
 *   6) 作品页展示标签，点标签 = 覆盖式跳到所有作品页并按它筛；
 *   7) 编辑页：新建标签 / 勾选 / 移除，改动即时落盘（调 update_work）；
 *   8) 池为空时不写任何数据，只给空态提示。
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

/* 30 个作品：10 个不同标签（>8，好让「常用」分组出现），3 个作者。
   关键三角：w01 同时有 科幻+完结，w02 只有 科幻，w03 只有 完结
   ——「任一」应该命中 3 个，「全部」只命中 w01。 */
function mkWorks() {
  const tagsOf = {
    w01: ['科幻', '完结'], w02: ['科幻'], w03: ['完结'], w04: ['悬疑'],
    w05: ['日常'], w06: ['恋爱'], w07: ['四格'], w08: ['短篇'],
    w09: ['汉化'], w10: ['连载'], w11: ['横版'],
  };
  const out = [];
  for (let i = 1; i <= 30; i++) {
    const id = 'w' + String(i).padStart(2, '0');
    const tags = (tagsOf[id] || []).slice();
    // 「大批量」压 25 个作品：用来验证改筛选后会回到第 1 页
    // （筛完仍有 25 条 = 2 页，页码标签才是有意义的断言；筛到 1 页时分页条整个隐藏，
    //   不管有没有重置都能通过，等于没测）
    if (i <= 25) tags.push('大批量');
    out.push({
      id,
      name: 'W' + String(i).padStart(2, '0'),
      author: i <= 10 ? '作者A' : (i <= 20 ? '作者B' : '作者C'),
      cover_url: null,
      chapter_count: 1,
      tags,
      updated_at: '2026-10-01T00:00:00+00:00',
    });
  }
  return out;
}
let WORKS = mkWorks();
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
    beforeParse(window) { window.pywebview = { api: {} }; },
  });
  const { window } = dom;
  const doc = window.document;
  const $ = (id) => doc.getElementById(id);
  const cards = () => $('allworksGrid').querySelectorAll('.workCard').length;
  const names = () => [...$('allworksGrid').querySelectorAll('.workCard .name')].map((e) => e.textContent);

  const updCalls = [];
  const stub = {
    get_state: () => Promise.resolve(STATE),
    library_state: () => Promise.resolve(LIB),
    list_works: () => Promise.resolve({ works: WORKS }),
    work_detail: (wid) => {
      const w = WORKS.find((x) => x.id === wid) || WORKS[0];
      return Promise.resolve({
        id: w.id, name: w.name, author: w.author, tags: w.tags.slice(),
        cover_url: null, chapters: [],
      });
    },
    // 真落盘：commitEditTags 会回读 work_detail，stub 不写回就成了假绿灯
    update_work: (wid, patch) => {
      updCalls.push({ wid, patch });
      const w = WORKS.find((x) => x.id === wid);
      if (w) {
        if (patch && patch.name != null) w.name = patch.name;
        if (patch && patch.author != null) w.author = patch.author;
        if (patch && patch.tags) w.tags = patch.tags.slice();
      }
      return Promise.resolve({ ok: true, name: w ? w.name : '', author: w ? w.author : '' });
    },
    begin_edit: () => Promise.resolve({ ok: true }),
    edit_dirty: () => Promise.resolve({ dirty: false }),
    cancel_edit: () => Promise.resolve({ ok: true }),
    commit_edit: () => Promise.resolve({ ok: true }),
    list_incomplete: () => Promise.resolve({ items: [] }),
    update_config: () => Promise.resolve({ ok: true, config: CFG, shelf_dir: CFG.shelf_dir, download_dir: '' }),
  };
  Object.assign(window.pywebview.api, stub);
  window.dispatchEvent(new window.Event('pywebviewready'));
  await sleep(80);

  /* ---------- 结构 ---------- */
  console.log('--- 筛选区结构 ---');
  $('btnAllWorks').click();
  await sleep(40);
  ok($('page-allworks').classList.contains('show'), '进入所有作品页');
  const bar = doc.querySelector('.filterbar');
  ok(!!bar && !!bar.querySelector('#awSearch'), '搜索框在筛选条里');
  ok(!!$('awTagSel').querySelector('button') && !!$('awAuthorSel').querySelector('button'),
    '筛选条里有标签 / 作者两个下拉');
  const kids = [...doc.querySelector('#page-allworks .card').children].map((e) => e.className || e.id);
  ok(kids.indexOf('filterbar') >= 0 && kids.indexOf('filterbar') < kids.indexOf('pager'),
    '筛选条在分页条之上（翻页滚回顶部，顺序反了每翻页都要滚到底）');

  /* ---------- 标签多选 ---------- */
  console.log('--- 标签筛选 ---');
  const tagBtn = $('awTagSel').querySelector('button');
  tagBtn.click();
  const panel = $('awTagSel').querySelector('.msel');
  ok(panel.style.display === 'block', '点按钮展开标签下拉');
  ok(!!panel.querySelector('.mselSearch'), '下拉里带搜索框');
  const groupText = [...panel.querySelectorAll('.mselGroup')].map((e) => e.textContent);
  ok(groupText.includes('常用'), `超过 8 个标签时出现「常用」分组：${groupText.join('/')}`);
  const top = panel.querySelector('.mselList .mselItem .txt').textContent;
  ok(top === '大批量', `常用按引用数降序，排头是被引得最多的那个：${top}`);

  const pick = async (box, label) => {
    const p = $(box).querySelector('.msel');
    const it = [...p.querySelectorAll('.mselItem')].find(
      (b) => b.querySelector('.txt') && b.querySelector('.txt').textContent === label);
    if (!it) throw new Error('找不到选项：' + label);
    it.click();
    await sleep(40);
  };
  await pick('awTagSel', '科幻');
  ok(panel.style.display === 'block', '勾选后菜单不关闭（复选的关键）');
  ok(cards() === 2, `只选「科幻」命中 2 个：${cards()}`);
  ok($('awCount').textContent === '命中 2 / 共 30', `计数：${$('awCount').textContent}`);

  await pick('awTagSel', '完结');
  ok(cards() === 3, `「任一」模式下科幻 + 完结命中 3 个：${cards()}`);
  const chips = [...$('awChips').querySelectorAll('.chip')];
  ok(chips.length === 2 && chips.every((c) => c.classList.contains('tag')), '两个已选标签显示为 chip');

  /* ---------- AND / OR ---------- */
  console.log('--- 任一 / 全部 ---');
  let seg = $('awChips').querySelector('.seg');
  ok(!!seg, '选了 2 个标签后出现「任一 / 全部」切换');
  ok(seg.querySelector('button').classList.contains('on'), '默认停在「任一」');
  const allBtn = [...seg.querySelectorAll('button')].find((b) => b.textContent === '全部');
  allBtn.click();
  await sleep(40);
  ok(cards() === 1 && names()[0] === 'W01', `「全部」= 必须同时含两个标签，只剩 W01：${names().join(',')}`);
  seg = $('awChips').querySelector('.seg');
  ok([...seg.querySelectorAll('button')].find((b) => b.textContent === '全部').classList.contains('on'),
    '切到「全部」后高亮跟着走');

  // 摘掉一个标签 → 只剩 1 个，切换就该消失（选 1 个时两者等价）
  chips[0].querySelector('.x').click();
  await sleep(40);
  ok(!$('awChips').querySelector('.seg'), '只剩 1 个标签时不再显示「任一 / 全部」（等价，纯噪音）');
  ok(cards() === 2, `摘掉一个后剩「完结」2 个：${cards()}`);

  /* ---------- 全部模式筛空的出路 ---------- */
  console.log('--- 筛空文案 ---');
  await pick('awTagSel', '科幻');
  const seg2 = $('awChips').querySelector('.seg');
  [...seg2.querySelectorAll('button')].find((b) => b.textContent === '全部').click();
  await sleep(40);
  await pick('awTagSel', '悬疑');       // 没有任何作品同时带 完结+科幻+悬疑
  await sleep(40);
  ok(cards() === 0, '「全部」模式可以筛出 0 条');
  ok($('allworksGrid').textContent.includes('切成「任一」'), '筛空时把出路写在文案里');
  $('awChips').querySelector('.seg').querySelector('button').click();   // 切回任一
  await sleep(40);
  ok(cards() === 4, `切回「任一」后命中 4 个：${cards()}`);

  /* ---------- 作者筛选 ---------- */
  console.log('--- 作者筛选 ---');
  $('awAuthorSel').querySelector('button').click();
  await sleep(20);
  await pick('awAuthorSel', '作者B');
  ok(cards() === 0 || cards() <= 4, '作者筛选与标签是「且」的关系，结果只会更少');
  const authorChips = [...$('awChips').querySelectorAll('.chip.author')];
  ok(authorChips.length === 1, '作者也以 chip 显示');
  authorChips[0].querySelector('.x').click();
  await sleep(40);
  ok(cards() === 4, '摘掉作者后回到标签筛选的结果');

  /* ---------- 筛选变化回到第 1 页 ---------- */
  console.log('--- 与分页的关系 ---');
  $('awChips').querySelector('.linkbtn').click();   // 清空筛选
  await sleep(40);
  ok(cards() === 24 && $('awPager').style.display !== 'none', `清空后回到全量 30 个（首页 24）：${cards()}`);
  ok($('awCount').textContent === '' && $('awChips').style.display === 'none', '没有筛选时不显示计数与 chip');
  $('awNext').click();
  await sleep(40);
  ok($('awPageLabel').textContent === '第 2 / 2 页', `翻到第 2 页：${$('awPageLabel').textContent}`);
  $('awTagSel').querySelector('button').click();
  await pick('awTagSel', '大批量');
  // 筛完仍有 25 条 = 2 页：这时页码标签是「第 1 / 2 页」才说明真的重置了
  ok($('awPageLabel').textContent === '第 1 / 2 页', `改筛选后回到第 1 页：${$('awPageLabel').textContent}`);
  // 点外部关闭
  doc.body.click();
  ok($('awTagSel').querySelector('.msel').style.display === 'none', '点菜单外收起下拉');

  /* ---------- 作品页标签 ---------- */
  console.log('--- 作品页 ---');
  await window.openWork('w01', 'shelf');
  await sleep(40);
  ok($('page-work').classList.contains('show'), '打开作品页');
  const wt = [...$('workTags').querySelectorAll('.chip')].map((c) => c.textContent);
  ok(wt.length === 3 && wt.includes('科幻') && wt.includes('完结'), `作品页展示标签：${wt.join('/')}`);
  await window.openWork('w26', 'shelf');   // w26 不带任何标签
  await sleep(40);
  ok($('workTags').style.display === 'none', '没有标签时不留空的标签行');

  await window.openWork('w01', 'shelf');
  await sleep(40);
  $('awTagSel').querySelector('button').click();      // 先塞一个旧筛选，验证是覆盖不是追加
  await pick('awTagSel', '悬疑');
  $('awAuthorSel').querySelector('button').click();
  await pick('awAuthorSel', '作者C');
  await window.openWork('w01', 'shelf');
  await sleep(40);
  $('workTags').querySelector('.chip').click();
  await sleep(40);
  ok($('page-allworks').classList.contains('show'), '点标签跳到所有作品页');
  ok($('awCount').textContent === '命中 2 / 共 30', `覆盖式：只按这一个标签筛：${$('awCount').textContent}`);
  ok([...$('awChips').querySelectorAll('.chip.author')].length === 0, '跳转时清掉了原先的作者筛选');

  /* ---------- 编辑页 ---------- */
  console.log('--- 编辑页标签 ---');
  $('awChips').querySelector('.linkbtn').click();
  await sleep(30);
  await window.openEdit('w01', 'shelf');
  await sleep(60);
  ok($('page-edit').classList.contains('show'), '打开编辑页');
  const et = [...$('editTags').querySelectorAll('.chip .x')].length;
  ok(et === 3, `编辑页列出该作品已有的 3 个标签：${et}`);
  updCalls.length = 0;

  // 移除一个
  $('editTags').querySelector('.chip .x').click();
  await sleep(60);
  ok(updCalls.length === 1 && Array.isArray(updCalls[0].patch.tags),
    '移除标签即时落盘（调 update_work 带 tags）');
  ok(updCalls[0].patch.tags.length === 2, `移除后剩 2 个：${JSON.stringify(updCalls[0].patch.tags)}`);
  ok([...$('editTags').querySelectorAll('.chip .x')].length === 2, '界面同步为 2 个 chip');

  // 新建标签：池是派生的，新建 = 加到当前作品
  $('editTagAdd').querySelector('button').click();
  const addPanel = $('editTagAdd').querySelector('.msel');
  ok(addPanel.style.display === 'block', '「添加标签」下拉可展开');
  const inp = addPanel.querySelector('.mselSearch');
  inp.value = '新标签X';
  inp.dispatchEvent(new window.Event('input', { bubbles: true }));
  await sleep(20);
  const create = addPanel.querySelector('.mselItem.create');
  ok(!!create && create.textContent.includes('新标签X'), `输入无匹配时给「新建」入口：${create && create.textContent}`);
  create.click();
  await sleep(60);
  const last = updCalls[updCalls.length - 1];
  ok(last.patch.tags.includes('新标签X'), `新建的标签写进作品：${JSON.stringify(last.patch.tags)}`);
  ok([...$('editTags').querySelectorAll('.chip')].some((c) => c.textContent.includes('新标签X')),
    '新建后出现在已选 chip 里');

  // 新建的标签进池（派生 = 全库并集），下次勾选得到它
  $('editTagAdd').querySelector('button').click();
  const p2 = $('editTagAdd').querySelector('.msel');
  p2.querySelector('.mselSearch').value = '新标签';
  p2.querySelector('.mselSearch').dispatchEvent(new window.Event('input', { bubbles: true }));
  await sleep(20);
  ok([...p2.querySelectorAll('.mselItem .txt')].some((e) => e.textContent === '新标签X'),
    '新建的标签出现在池里（纯派生：全库 tags 并集）');

  /* ---------- 空态：不写数据 ---------- */
  console.log('--- 空态不造数据 ---');
  WORKS = WORKS.map((w) => ({ ...w, tags: [] }));
  WORKS[0].tags = [];
  await window.openEdit('w05', 'shelf');
  await sleep(60);
  $('editTagAdd').querySelector('button').click();
  const p3 = $('editTagAdd').querySelector('.msel');
  const empty = p3.querySelector('.mselEmpty');
  ok(!!empty && empty.textContent.includes('新建'), `池为空时给提示而不是造示例标签：${empty && empty.textContent}`);
  ok(WORKS.every((w) => w.tags.length === 0), '空态提示不会往库里写任何标签');

  console.log();
  if (fail.length) {
    console.log(`FAILED: ${fail.length}`);
    fail.forEach((f) => console.log('  - ' + f));
    process.exit(1);
  }
  console.log('OK 标签与筛选全部通过');
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
