/* 缩略图（v1.11）回归测试
 *
 * v1.11 把缩略图整个改掉了，起因是用户反馈「收起时那 34px 的底栏压在图片上，
 * 挡住了图片下面的一条」。改后：
 *   - 条**回到正常流**（不再 fixed 覆盖）：撑开时挤压 readerBody，图片自动缩小让位；
 *   - 入口改成右下角按钮，**平时完全不可见**（opacity 0 且不吃鼠标事件），
 *     鼠标移到右下角才淡入 —— 这才是真的"不遮挡"；
 *   - 全屏时不要入口按钮，靠近**整个下边缘**才整条滑出（和顶部条同一套 hud，
 *     不是右下角热区 —— 用户特别嘱咐过这点）。
 *
 * jsdom 不做排版，能守的是：class 状态、热区坐标判断、CSS 声明。
 * 观感（图片有没有真的让位、淡入淡出顺不顺、条高 78px 合不合适）只能人工看。
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
  { url: '/outputs/1.jpg', width: 800, height: 1200 },
  { url: '/outputs/2.jpg', width: 800, height: 1200 },
  { url: '/outputs/3.jpg', width: 1920, height: 1080 },
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
const txt = (w, id) => {
  const el = w.document.getElementById(id);
  return el ? el.textContent : '';
};
/** 只读 CSS 文本。**行首锚定**是必须的：否则 `#thumbBar` 会先匹配到更靠前的
 *  `body.fs #thumbBar`（那是全屏态规则），断言就张冠李戴了。
 *  选择器后允许 [^{]* 是为了支持 `#a button, #b button { ... }` 这种合并写法。 */
function ruleFor(selector) {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp('(?:^|\\n)\\s*' + esc + '[^{]*\\{([^}]*)\\}');
  const m = re.exec(html);
  return m ? m[1] : null;
}

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

(async () => {
  const { dom, w, errs } = await bootDom();
  const $ = (id) => w.document.getElementById(id);
  // jsdom 的窗口是 1024×768
  const move = (x, y) => w.document.dispatchEvent(
    new w.MouseEvent('mousemove', { clientX: x, clientY: y, bubbles: true }));

  // 这个实例当作"已经提示过"，测的是常态；首次闪现单独在最后测
  w.localStorage.setItem('thumbHinted', '1');
  await w.openReader('w1', 0);
  await sleep(250);

  console.log('--- 默认：条收起、入口按钮完全不可见 ---');
  ok(!$('thumbBar').classList.contains('open'), '缩略图条是收起的');
  ok(!$('btnThumb').classList.contains('show'), '入口按钮默认不可见（不占画面）');
  ok($('thumbList').children.length === 3, `渲染 3 个缩略图（实际 ${$('thumbList').children.length}）`);
  ok(txt(w, 'thumbLabel').trim() === '缩略图', `按钮文案（实际「${txt(w, 'thumbLabel').trim()}」）`);
  ok(/共 3 张/.test($('btnThumb').title), `张数放在 title（实际「${$('btnThumb').title}」）`);

  console.log('');
  console.log('--- 缩略图按原图比例横向铺开（不强行裁成竖条）---');
  const w0 = parseInt($('thumbList').children[0].style.width, 10);   // 800×1200 竖版
  const w2 = parseInt($('thumbList').children[2].style.width, 10);   // 1920×1080 横版
  ok(w0 > 0 && w2 > 0, `都按原图比例预留了宽度（${w0}px / ${w2}px）`);
  ok(w2 > w0, `横版图的缩略图比竖版宽（${w0}px vs ${w2}px）—— 没被裁成统一竖条`);
  const thRule = ruleFor('#thumbList .th');
  ok(thRule && /object-fit:\s*contain/.test(thRule), '不用 cover 裁切，原图完整可见');

  console.log('');
  console.log('--- 右下角热区：鼠标移过去才淡入，移开淡出 ---');
  move(500, 300);
  await sleep(400);
  ok(!$('btnThumb').classList.contains('show'), '鼠标在画面中间：按钮仍不可见');
  move(950, 720);                       // 右下角（jsdom 窗口 1024×768）
  await sleep(100);
  ok($('btnThumb').classList.contains('show'), '鼠标移到右下角：按钮淡入');
  move(500, 300);
  await sleep(100);                     // 移开**立刻**淡出，不留 0.3 秒
  ok(!$('btnThumb').classList.contains('show'), '鼠标移开：立刻淡出（不延迟）');

  console.log('');
  console.log('--- 点按钮：条撑开（挤压 readerBody，不覆盖画面）---');
  move(950, 720);
  await sleep(100);
  $('btnThumb').click();
  await sleep(100);
  ok($('thumbBar').classList.contains('open'), '条已展开');
  ok($('btnThumb').classList.contains('open'), '按钮自己也带 open（图标翻转方向）');
  ok(txt(w, 'thumbLabel').trim() === '收起', `按钮文案变「收起」（实际「${txt(w, 'thumbLabel').trim()}」）`);
  ok(w.localStorage.getItem('uiThumbLast') === '1', '已记下"展开过"');
  move(500, 300);
  await sleep(200);
  ok($('btnThumb').classList.contains('show'), '展开态：按钮常显（鼠标移开也不消失，能收起）');

  console.log('');
  console.log('--- 点缩略图跳页 + 当前项高亮 ---');
  $('thumbList').children[2].click();
  await sleep(250);
  ok(txt(w, 'readerPos') === '3 / 3', `跳到了第 3 页（实际 ${txt(w, 'readerPos')}）`);
  ok($('thumbList').children[2].classList.contains('cur'), '第 3 项高亮');
  ok(!$('thumbList').children[0].classList.contains('cur'), '第 1 项不高亮');

  console.log('');
  console.log('--- 再点按钮收起 ---');
  $('btnThumb').click();
  await sleep(100);
  ok(!$('thumbBar').classList.contains('open'), '条已收起');
  ok(txt(w, 'thumbLabel').trim() === '缩略图', '按钮文案变回「缩略图」');
  ok(w.localStorage.getItem('uiThumbLast') === '0', '已记下"收起过"');

  console.log('');
  console.log('--- 结束页：不高亮任何一张 ---');
  w.readerNext();
  await sleep(250);
  ok(txt(w, 'readerPos') === '本话完', '已到结束页');
  ok([...$('thumbList').children].every((c) => !c.classList.contains('cur')), '结束页无高亮项');

  console.log('');
  console.log('--- 连续滚动模式：条和入口按钮都不显示 ---');
  $('btnMode').click();
  await sleep(250);
  ok($('thumbBar').style.display === 'none', '滚动模式下条隐藏');
  ok($('btnThumb').style.display === 'none', '滚动模式下入口按钮也隐藏');
  $('btnMode').click();
  await sleep(250);
  ok($('thumbBar').style.display !== 'none', '切回单图又显示');
  ok($('btnThumb').style.display !== 'none', '切回单图按钮也恢复');

  console.log('');
  console.log('--- CSS：条在正常流里，不是浮层 ---');
  const barRule = ruleFor('#thumbBar');
  ok(barRule && !/position:\s*fixed/.test(barRule),
    `非全屏时条不是 fixed（实际 ${barRule ? barRule.trim().slice(0, 46) : '无'}）`);
  ok(barRule && /flex:\s*0 0 auto/.test(barRule), '条是 flex 子项（撑开时挤压 readerBody）');
  const btnRule = ruleFor('#btnThumb');
  ok(btnRule && /opacity:\s*0/.test(btnRule) && /pointer-events:\s*none/.test(btnRule),
    '入口按钮默认透明且不吃鼠标事件');
  const fsBtn = ruleFor('body.fs #btnThumb');
  ok(fsBtn && /display:\s*none/.test(fsBtn), '全屏时不显示入口按钮');
  const fsBar = ruleFor('body.fs #thumbBar');
  ok(fsBar && /translateY\(100%\)/.test(fsBar), '全屏时条收在屏幕外');
  const fsBarShow = ruleFor('body.fs #thumbBar.hudShow');
  ok(fsBarShow && /translateY\(0\)/.test(fsBarShow), '全屏时靠 hudShow 滑上来');
  const ico = ruleFor('#btnThumb .ico');
  ok(ico && /rotate\(90deg\)/.test(ico), `收起态图标尖端朝上（实际 ${ico ? ico.slice(0, 40) : '无'}）`);
  const icoOpen = ruleFor('#btnThumb.open .ico');
  ok(icoOpen && /rotate\(-90deg\)/.test(icoOpen), '展开态图标翻转成朝下');

  console.log('');
  console.log('--- 首次使用提示一次入口在哪 ---');
  ok(w.localStorage.getItem('thumbHinted') === '1', '已记下"提示过了"（下次不再打扰）');

  ok(errs.length === 0, `无未捕获错误${errs.length ? ' —— ' + errs.join(' | ') : ''}`);
  dom.window.close();

  console.log('');
  console.log('--- 首次使用：入口按钮自己闪一下（不占用 toast）---');
  {
    const d = await bootDom();
    await d.w.openReader('w1', 0);
    await sleep(150);
    ok(d.w.document.getElementById('btnThumb').classList.contains('show'),
      '第一次进阅读器：按钮自己闪出来');
    ok(d.w.localStorage.getItem('thumbHinted') === '1', '已记下"提示过了"');
    await sleep(1400);
    ok(!d.w.document.getElementById('btnThumb').classList.contains('show'),
      '闪一下就淡出（不常驻，不挡画面）');
    // 关键：不能占用 toast，否则会把「长图：先滚动看完」挤掉
    ok(!d.w.document.getElementById('toast').classList.contains('show'),
      '没有占用 toast（长图提示才能正常发声）');
    ok(d.errs.length === 0, `无未捕获错误${d.errs.length ? ' —— ' + d.errs.join(' | ') : ''}`);
    d.dom.window.close();
  }

  console.log('');
  console.log('--- 三档设置仍然有效 ---');
  {
    const a = await bootDom();
    a.w.localStorage.setItem('uiThumbMode', 'open');
    await a.w.openReader('w1', 0);
    await sleep(250);
    ok(a.w.document.getElementById('thumbBar').classList.contains('open'), '「默认开启」：进来就是展开的');
    ok(a.errs.length === 0, `无未捕获错误${a.errs.length ? ' —— ' + a.errs.join(' | ') : ''}`);
    a.dom.window.close();

    const b = await bootDom();
    b.w.localStorage.setItem('uiThumbMode', 'remember');
    b.w.localStorage.setItem('uiThumbLast', '1');
    await b.w.openReader('w1', 0);
    await sleep(250);
    ok(b.w.document.getElementById('thumbBar').classList.contains('open'), '「记住上一次」：上次展开 → 这次也展开');
    b.dom.window.close();

    const c = await bootDom();
    c.w.localStorage.setItem('uiThumbMode', 'remember');
    c.w.localStorage.setItem('uiThumbLast', '0');
    await c.w.openReader('w1', 0);
    await sleep(250);
    ok(!c.w.document.getElementById('thumbBar').classList.contains('open'), '「记住上一次」：上次收起 → 这次也收起');
    c.dom.window.close();
  }

  console.log('');
  console.log(fail.length ? `FAILED ${fail.length}` : 'ALL PASS');
  process.exit(fail.length ? 1 : 0);
})();
