/* 点击反馈（v1.14）回归测试
 *
 * 这一版做的是「点一下东西之后的反馈」：
 *   ① 按下态（:active）      ② 异步操作的「处理中…」     ③ 浮层淡入
 *   ④ 列表行的出现 / 消失过渡  ④ 顺便把 btnCancel 的原生 confirm 统一成自绘确认框
 *
 * jsdom **测不出手感**：它不排版、不支持 :active、也不真的跑 CSS 动画。
 * 所以这里能守的只有两件事：
 *   1) 该写的声明写了、值是对的、顺序（=特异性平手时的胜负）没被改回去；
 *   2) JS 那一半的行为 —— withBusy 期间按钮的状态、markNewRows 只淡新行。
 * 「按下去手感对不对、淡入会不会太慢」一律人工看，见 README「自检与回归」。
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');
const { makeState } = require('./helpers');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'src/asase_reader/assets/gui.html'), 'utf8');
/* 静态断言一律在**剥掉注释**的 CSS 上做：注释里经常会写着反例（比如
   "绝不写 transition: all"），不剥会把注释当成真的声明。 */
const css = html.slice(html.indexOf('<style>'), html.indexOf('</style>'))
  .replace(/\/\*[\s\S]*?\*\//g, '');

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
const LIB = { works: [], chapters: [], orphans: [], pending: [] };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fail = [];
const ok = (cond, label) => {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + label);
  if (!cond) fail.push(label);
};
/** 取某条 CSS 规则的声明体。**行首锚定**（否则 #thumbBar 会先匹配到
 *  `body.fs #thumbBar`，断言张冠李戴）；选择器后允许 [^{]* 以支持
 *  `#a button, #b button {}` 这种合并写法。 */
function ruleFor(selector) {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp('(?:^|\\n)\\s*' + esc + '[^{]*\\{([^}]*)\\}');
  const m = re.exec(css);
  return m ? m[1] : null;
}
/** 同名选择器可能写了**好几条**（`button {}` 就有字体定义和 transition 两条），
 *  这时候要看"任意一条是不是这么写的"，而不是死盯第一条。 */
function anyRule(selector, re) {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const reAll = new RegExp('(?:^|\\n)\\s*' + esc + '[^{]*\\{([^}]*)\\}', 'g');
  let m;
  while ((m = reAll.exec(css)) !== null) { if (re.test(m[1])) return true; }
  return false;
}
const has = (sel) => ruleFor(sel) !== null;
const says = (sel, re) => { const b = ruleFor(sel); return !!b && re.test(b); };
// 源码顺序：特异性相同（如 .item:hover 与 .item:active）时它就是胜负手
const after = (later, earlier) => html.indexOf(later) > html.indexOf(earlier);

console.log('--- ① 按下态（全站原先 0 处 :active）---');
ok(says('button:active', /transform:\s*translateY\(1px\)/), 'button:active 有 1px 下沉');
ok(says('button:active', /background:\s*#e8eaed/), 'button:active 底色比 hover 再深一档');
ok(says('button.primary:active', /background:\s*#e05b86/), '主按钮按下 #e05b86（比 hover 的 #f25d8d 更深）');
ok(after('button.primary:active', 'button.primary:hover'),
  '主按钮的 :active 排在 :hover 之后（特异性相同，靠顺序生效）');
ok(anyRule('button', /transition:[^;]*transform\s+\.06s/), '位移 60ms（按下要比松开快）');
ok(anyRule('button', /transition:[^;]*background\s+\.12s/), '底色 120ms');
ok(!/transition:\s*all/.test(css), '没有 transition: all（会连布局一起动）');
ok(says('.item:active', /background:\s*#e9ebee/) && after('.item:active', '.item:hover'),
  '列表行按下态存在且排在 :hover 之后');
ok(says('body.cardHover .workCard:hover:active', /translateY\(0\)/),
  '卡片按下把 hover 的抬升归零（否则两个 transform 打架）');
ok(says('.workCard:active', /scale\(\.995\)/), '卡片按下微缩');
// 深色区是自己写过背景色的，全局 button:active 压不过它们的特异性
ok(has('#readerBar button:active'), '阅读器条有自己的 :active');
ok(has('#readerEnd .endOps button:active'), '章末页按钮有自己的 :active');
ok(has('#readerBar button.primary:active'), '阅读器条的粉按钮有自己的 :active');
ok(has('#readerEnd .endOps button:active'), '章末页按钮有自己的 :active');
ok(has('#readerEnd .endOps button.primary:active'), '章末页主按钮有自己的 :active');
ok(has('#btnThumb:active'), '缩略图入口按钮有自己的 :active');
ok(has('#ctxMenu .menuItem:active') && after('#ctxMenu .menuItem:active, .menuList .menuItem:active, .mselItem:active', '.mselItem:hover'),
  '三类菜单项的 :active 排在各自 :hover 之后');
// nav 页签是扁的，不该在按下时凭空长出一块灰底
ok(says('nav button:active', /background:\s*none/), '页签按下不冒灰底（只有下沉）');
ok(says('.linkbtn:active', /background:\s*none/), '纯文字按钮按下不冒灰底');

console.log('--- ③ 浮层淡入（只给入场）---');
ok(/@keyframes\s+overlayPop/.test(html) && /@keyframes\s+overlayFade/.test(html),
  '浮层入场用 keyframes 定义');
ok(says('#ctxMenu, .menuList', /animation:\s*overlayPop\s+\.1s/), '右键菜单 / 下拉淡入 100ms');
ok(anyRule('#modal.show', /animation:\s*overlayFade\s+\.1s/), '模态遮罩淡入 100ms');
ok(says('#modal.show #modalBox', /animation:\s*overlayPop\s+\.1s/), '模态框本体带轻微缩放淡入');
ok(!/transition:/.test(ruleFor('#ctxMenu') || ''),
  '浮层用 animation 而不是 transition（display 切换时 animation 才肯自己跑）');
ok(!/hideModal[\s\S]{0,200}fade/.test(html.slice(html.indexOf('function hideModal'))),
  '关闭是瞬时的（没有退场动画）');

console.log('--- ④ 列表行的出现 / 消失 ---');
ok(/@keyframes\s+rowIn/.test(html) && says('.fade-in', /animation:\s*rowIn\s+\.12s/),
  '新增行淡入 120ms');
ok(says('.fade-out', /transition:\s*opacity\s+\.12s/), '删除先淡出 120ms');
// 下载任务列表每秒被轮询整份重建一次，加了就是每秒闪一次
const renderDlBody = html.slice(html.indexOf('function renderDl'), html.indexOf('function fillDlSettings'));
ok(!/markNewRows|fade-in/.test(renderDlBody), '#jobs 刻意不参与渐显（它每秒重建一次）');
ok(/prefers-reduced-motion/.test(html), '有 prefers-reduced-motion 兜底');

console.log('--- ② 处理中（withBusy）---');
ok(/async function withBusy\(btn, fn, busyText\)/.test(html), 'withBusy 已抽成公用函数');
for (const id of ['btnSaveCfg', 'btnSaveDirs', 'btnSubmitCookie', 'btnClearCookie', 'btnPurgeIncomplete']) {
  ok(html.includes(`withBusy($("${id}")`), `${id} 走了 withBusy`);
}
// 用了 disabled 防连点，就别再用在 disabled 被轮询接管的按钮上（详见函数上的注释）
ok(/disabled 被周期重画接管的按钮别用/.test(html), 'withBusy 的适用边界写在注释里');

console.log('--- 顺手统一的原生 confirm ---');
ok(!/[^A-Za-z_.]confirm\(/.test(html), '全站不再有原生 confirm()（都走 askConfirm）');

(async () => {
  const errs = [];
  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8765/',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.pywebview = { api: {} };
      window.addEventListener('error', (e) => errs.push('window.error: ' + e.message));
      window.addEventListener('unhandledrejection', (e) => errs.push('rejection: ' + (e.reason && e.reason.message)));
    },
  });
  const w = dom.window; const doc = w.document;
  const $ = (id) => doc.getElementById(id);

  let cfgCalls = 0;
  let incomplete = [{ id: 'opus_a', name: 'opus_a', files: 2, path: 'chapters/opus_a' }];
  Object.assign(w.pywebview.api, {
    get_state: () => Promise.resolve(STATE),
    library_state: () => Promise.resolve(LIB),
    list_works: () => Promise.resolve({ works: [] }),
    list_incomplete: () => Promise.resolve({ items: incomplete }),
    purge_incomplete: () => Promise.resolve({ ok: true, removed: 1 }),
    open_dir: () => Promise.resolve({ ok: true }),
    submit_cookie: () => Promise.resolve({ ok: true, fields: 3 }),
    clear_cookie: () => Promise.resolve({ ok: true }),
    // 慢一点的返回，才看得出「处理中」那一段
    update_config: () => { cfgCalls++; return new Promise((r) => setTimeout(
      () => r({ ok: true, config: CFG, shelf_dir: CFG.shelf_dir, download_dir: '' }), 160)); },
  });
  w.dispatchEvent(new w.Event('pywebviewready'));
  await sleep(250);

  console.log('--- 运行时：withBusy 的行为 ---');
  const before = $('btnSaveCfg').textContent;
  $('btnSaveCfg').click();
  await sleep(40);
  ok($('btnSaveCfg').textContent === '处理中…', '等待期间按钮文案变成「处理中…」');
  ok($('btnSaveCfg').disabled === true, '等待期间按钮禁用（顺带防连点）');
  await sleep(260);
  ok($('btnSaveCfg').textContent === before, `结束后文案还原（实际「${$('btnSaveCfg').textContent}」）`);
  ok($('btnSaveCfg').disabled === false, '结束后按钮恢复可用');
  ok(cfgCalls === 1, `一次点击只发一次请求（实际 ${cfgCalls}）`);

  console.log('--- 运行时：失败也要还原（不能卡在「处理中…」）---');
  w.pywebview.api.update_config = () => Promise.reject(new Error('炸了'));
  $('btnSaveCfg').click();
  await sleep(300);
  ok($('btnSaveCfg').textContent === before, '抛错后按钮文案照样还原');
  ok($('btnSaveCfg').disabled === false, '抛错后按钮照样恢复可用（不能被自己锁死）');

  console.log('--- 运行时：markNewRows 只淡**新出现**的行 ---');
  await w.refreshIncomplete();
  let rows = [...$('incompleteList').children];
  ok(rows.length === 1, '第一次渲染出 1 行');
  ok(!rows[0].classList.contains('fade-in'), '首次渲染整屏不闪（第一次没有上一轮可比较）');
  incomplete = incomplete.concat([{ id: 'opus_b', name: 'opus_b', files: 1, path: 'chapters/opus_b' }]);
  await w.refreshIncomplete();
  rows = [...$('incompleteList').children];
  ok(rows.length === 2, '多出一行');
  ok(!rows[0].classList.contains('fade-in'), '老行不闪（每次都是整份重画，全淡会闪一屏）');
  ok(rows[1].classList.contains('fade-in'), '新出现的行淡入');

  console.log('--- 运行时：btnCancel 走自绘确认框 ---');
  let cancelled = 0;
  w.pywebview.api.cancel_all = () => { cancelled++; return Promise.resolve({ ok: true }); };
  // 队列里没有东西时这个按钮是禁用的，先让它可用（disabled 的按钮点了不触发）
  $('modal').classList.remove('show');
  w.__onState({ jobs: [], config: CFG, shelf_dir: CFG.shelf_dir, download_dir: '',
    daily_used: 0, running: true, paused: false, pending: 2 });
  $('btnCancel').click();
  await sleep(20);
  ok($('modal').classList.contains('show') && !!$('m_cancel'), '弹出自绘确认框');
  ok(cancelled === 0, '确认之前什么都不做');
  $('m_ok').click();
  await sleep(40);
  ok(cancelled === 1, '确定后才真的取消');
  ok(!$('modal').classList.contains('show'), '确定后确认框收起');

  ok(errs.length === 0, `无未捕获错误${errs.length ? '：' + errs.join(' / ') : ''}`);

  console.log('');
  // 一定要关窗口：页面里有 setInterval 轮询，不关 node 进程不会自己退出
  // （自检是按超时判定失败的，挂在那里会被当成 FAIL）
  dom.window.close();
  if (fail.length) {
    console.log(`FAILED: ${fail.length}`);
    fail.forEach((f) => console.log('  - ' + f));
    process.exit(1);
  }
  console.log('ALL PASS 点击反馈 v1.14');
  process.exit(0);
})();
