/* 全屏悬浮条（v1.9）回归测试
 *
 * 需求：以前一进全屏，body.fs 就把 nav 和 #readerBar 直接 display:none 抹掉，
 *   画面上只剩一张图 —— 进度、翻章、切模式全没了，想操作只能先退出全屏。
 *   现在阅读器条改成浮在图片上方：默认收在屏幕外，鼠标移到窗口顶部才滑下来，
 *   移开 0.3 秒自动收起；鼠标停在条上时不收（否则点不到按钮）。
 *
 * 关键设计：沿用**同一个** #readerBar，不新增第二套条 —— 所以这里也要断言
 *   「全屏时用的还是那个 readerBar」，防止日后有人另起一个条导致两套不同步。
 *
 * jsdom 不做排版也不渲染 CSS，能守的只有两块：
 *   1) 状态迁移：hudShow 类的出现 / 消失时机；
 *   2) CSS 声明：body.fs #readerBar 不再是 display:none，且存在 hudShow 规则。
 * 滑出动画顺不顺、0.3 秒收起是不是太快、半透明度压在图上好不好看，只能人工看。
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
const IMAGES = [
  { url: '/outputs/1.jpg', width: 1920, height: 1080 },
  { url: '/outputs/2.jpg', width: 800, height: 1200 },
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

async function bootDom() {
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
    work_detail: (wid) => Promise.resolve({ id: wid, name: '测试作品', author: '某人', cover_url: null, chapters: CHAPTERS }),
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

/** jsdom 不做排版，唯一能验的"CSS 行为"就是声明本身 */
function cssRules() {
  return html;
}
function ruleFor(selector) {
  const re = new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}');
  const m = re.exec(cssRules());
  return m ? m[1] : null;
}

(async () => {
  console.log('--- CSS 声明：全屏不再把阅读器条 display:none 掉 ---');
  {
    const body = ruleFor('body.fs #readerBar');
    ok(body !== null, '存在 body.fs #readerBar 规则');
    ok(body !== null && !/display\s*:\s*none/.test(body),
      `全屏时阅读器条不再被隐藏（实际声明：${body ? body.trim().slice(0, 60) : '无'}）`);
    ok(body !== null && /position\s*:\s*fixed/.test(body), '改成浮在图上（position: fixed）');
    // 断言行为而不是写死 -100%：顶条一旦有了 top 内边距（悬浮胶囊），
    // 只退自身高度会留一条边在屏幕上，位移必须是「自身高度 + 该规则的 top」。
    const topM = body ? body.match(/top\s*:\s*(\d+(?:\.\d+)?)px/) : null;
    const topPx = topM ? parseFloat(topM[1]) : 0;
    const ty = body ? body.match(/translateY\((calc\([^()]*\)|[^()]*)\)/) : null;
    const tyVal = ty ? ty[1].trim() : '';
    const extra = tyVal.match(/^calc\(\s*-100%\s*-\s*(\d+(?:\.\d+)?)px\s*\)$/);
    const hidesFully = (topPx === 0 && tyVal === '-100%')
      || (!!extra && parseFloat(extra[1]) >= topPx);
    ok(hidesFully,
      `默认收在屏幕外：位移要盖掉自身高度 + top(${topPx}px)，实际 ${ty ? 'translateY(' + tyVal + ')' : '无 transform'}`);
    const shown = ruleFor('body.fs #readerBar.hudShow');
    ok(shown !== null && /translateY\(0\)/.test(shown), '存在 hudShow 规则把它滑回来');
    ok(ruleFor('body.fs nav') !== null, 'nav 在全屏时仍然隐藏（没被误改）');
  }

  const { dom, w, errs } = await bootDom();
  const $ = (id) => w.document.getElementById(id);
  const bar = () => $('readerBar');
  const shown = () => bar().classList.contains('hudShow');
  const isFs = () => w.document.body.classList.contains('fs');
  const move = (y) => w.document.dispatchEvent(new w.MouseEvent('mousemove', { clientY: y, bubbles: true }));

  await w.openReader('w1', 0);
  await sleep(250);

  console.log('');
  console.log('--- 非全屏：鼠标怎么动都不该冒出条 ---');
  move(10);
  await sleep(150);
  ok(!shown(), '非全屏时不加 hudShow');

  console.log('');
  console.log('--- 进入全屏：默认是收起的（不主动弹出来挡图）---');
  $('btnFullscreen').click();
  await sleep(300);
  ok(isFs(), '已进入全屏');
  ok(!shown(), '刚进全屏时条是收起的');

  console.log('');
  console.log('--- 鼠标移到窗口顶部 40px 内：滑下来 ---');
  move(10);
  await sleep(150);
  ok(shown(), '靠近顶部时条滑出');
  move(39);
  await sleep(100);
  ok(shown(), '边界 39px 仍算"顶部"');

  console.log('');
  console.log('--- 鼠标移开：0.3 秒后自动收起 ---');
  move(400);
  await sleep(150);
  ok(shown(), '刚移开时还没到 0.3 秒，条仍在');
  await sleep(250);
  ok(!shown(), '0.3 秒后自动收起');

  console.log('');
  console.log('--- 鼠标停在条上：不收（否则点不到按钮）---');
  move(10);
  await sleep(150);
  ok(shown(), '条已滑出');
  bar().dispatchEvent(new w.MouseEvent('mouseenter', { bubbles: false }));
  move(400);                      // 鼠标离开顶部区域，但停在条上
  await sleep(1600);
  ok(shown(), '鼠标停在条上时不自动收起');

  console.log('');
  console.log('--- 鼠标离开条：重新开始计时 ---');
  bar().dispatchEvent(new w.MouseEvent('mouseleave', { bubbles: false }));
  await sleep(1500);
  ok(!shown(), '离开条后收起');

  console.log('');
  console.log('--- 全屏时条上的按钮能用（悬浮条不是摆设）---');
  move(10);
  await sleep(150);
  ok(shown(), '条已滑出');
  const before = $('readerTitle').textContent;
  $('btnNextCh').click();
  await sleep(300);
  ok($('readerTitle').textContent !== before,
    `全屏时能翻到下一章（${before} → ${$('readerTitle').textContent}）`);
  ok(isFs(), '翻章后仍在全屏');

  console.log('');
  console.log('--- 全屏：靠近**整个下边缘**滑出底部缩略图条（不是右下角热区）---');
  move(10);
  await sleep(150);
  ok($('readerBar').classList.contains('hudShow'), '鼠标在顶部：顶部条滑出');
  ok(!$('thumbBar').classList.contains('hudShow'), '此时底部条没跟着出来');
  move(w.innerHeight - 20);          // 下边缘附近（jsdom 窗口 768 高）
  await sleep(150);
  ok($('thumbBar').classList.contains('hudShow'), '靠近下边缘：底部缩略图条滑出');
  await sleep(400);
  ok(!$('readerBar').classList.contains('hudShow'), '顶部条同时收起（两条各自独立）');
  move(400);
  await sleep(450);
  ok(!$('thumbBar').classList.contains('hudShow'), '移开后底部条收起');

  console.log('');
  console.log('--- 退出全屏：状态复位（不留悬空的条）---');
  $('btnFullscreen').click();
  await sleep(300);
  ok(!isFs(), '已退出全屏');
  ok(!shown(), '退出时清掉了 hudShow');
  move(10);
  await sleep(150);
  ok(!shown(), '退出后鼠标再靠顶部也不加 hudShow');

  console.log('');
  console.log('--- 再次进入：仍是默认收起（复位不是只清一半）---');
  $('btnFullscreen').click();
  await sleep(300);
  ok(isFs(), '又进入全屏');
  ok(!shown(), '再次进入时是收起态，没有沿用上次的展开');
  move(10);
  await sleep(150);
  ok(shown(), '仍能正常唤出');

  console.log('');
  console.log('--- 没有未捕获错误 ---');
  await sleep(150);
  ok(errs.length === 0, `运行期无未捕获错误${errs.length ? ' —— ' + errs.join(' | ') : ''}`);
  dom.window.close();

  console.log('');
  console.log(fail.length ? `FAILED ${fail.length}` : 'ALL PASS');
  process.exit(fail.length ? 1 : 0);
})();
