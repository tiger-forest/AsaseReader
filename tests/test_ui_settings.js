/* 界面设置 + 美化一批（v1.10）回归测试
 *
 * 这一版有两块内容：
 *   1) 三个界面设置项（卡片悬浮 / 卡片淡阴影 / 封面形状三档），
 *      机制是「localStorage + <body> class + 即时生效」；
 *   2) 一批纯视觉改动（滚动条 / hover / 阴影 / 字重 / 间距 / 标题层级 / 焦点环）。
 *
 * 第 2 块 jsdom **测不出观感** —— 它不做排版也不支持 :hover，唯一能守的是
 *   「这条 CSS 声明确实写了、并且是预期的那个值」。所以下面一半断言是在读 CSS 文本，
 *   防止日后有人手滑把值改回去（尤其是 button:hover 那个和页面底色撞车的 #f6f7f8）。
 * 观感（滚动条粗细、悬浮高度、阴影浓淡、间距松紧）一律人工看，见 README 回归清单。
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
const LIB = { works: [{ id: 'w1', name: '测试作品', author: '某人', cover_url: null, chapter_count: 1 }], chapters: [], orphans: [], pending: [] };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fail = [];
const ok = (cond, label) => {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + label);
  if (!cond) fail.push(label);
};
/** 只读 CSS 文本。**行首锚定**：否则 `#thumbBar` 这类会先匹配到更靠前的
 *  `body.fs #thumbBar`（全屏态规则），断言张冠李戴。
 *  选择器后允许 [^{]* 是为了支持 `#a button, #b button { ... }` 合并写法。 */
function ruleFor(selector) {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp('(?:^|\\n)\\s*' + esc + '[^{]*\\{([^}]*)\\}');
  const m = re.exec(html);
  return m ? m[1] : null;
}

async function bootDom(state) {
  const errs = [];
  const saved = [];      // 前端交给后端存的界面设置副本（v1.29.0）
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
    get_state: () => Promise.resolve(state || STATE),
    save_ui_settings: (obj) => { saved.push(obj); return Promise.resolve({ ok: true }); },
    library_state: () => Promise.resolve(LIB),
    list_works: () => Promise.resolve({ works: LIB.works }),
    mark_read: () => Promise.resolve({ ok: true }),
    work_detail: (wid) => Promise.resolve({ id: wid, name: '测试作品', author: '某人', cover_url: null, chapters: [] }),
    chapter_images: () => Promise.resolve({ images: [] }),
    work_update: () => Promise.resolve({ ok: true }),
    list_incomplete: () => Promise.resolve({ items: [] }),
    open_dir: () => Promise.resolve({ ok: true }),
    toggle_fullscreen: () => Promise.resolve({ ok: true, fullscreen: true }),
  });
  w.dispatchEvent(new w.Event('pywebviewready'));
  await sleep(250);
  return { dom, w, errs, saved };
}

(async () => {
  const { dom, w, errs, saved } = await bootDom();
  const $ = (id) => w.document.getElementById(id);
  const body = w.document.body;
  const setCheck = (id, v) => {
    $(id).checked = v;
    $(id).dispatchEvent(new w.Event('change'));
  };
  const setSel = (id, v) => {
    $(id).value = v;
    $(id).dispatchEvent(new w.Event('change'));
  };

  console.log('--- 默认值：卡片悬浮开 / 阴影开 / 封面 16:9 ---');
  ok(body.classList.contains('cardHover'), '默认开启卡片悬浮');
  ok(body.classList.contains('cardShadow'), '默认开启卡片淡阴影');
  ok(!body.classList.contains('covStd') && !body.classList.contains('covSq'),
    '封面默认 16:9（不加任何 covXxx class）');
  ok($('cfg_ui_cardhover').checked === true, '控件回填：悬浮开关是勾上的');
  ok($('cfg_ui_cardshadow').checked === true, '控件回填：阴影开关是勾上的');
  ok($('cfg_ui_cover').value === 'wide', '控件回填：封面形状选中 16:9');

  console.log('');
  console.log('--- 关掉卡片悬浮：立即生效并记进 localStorage ---');
  setCheck('cfg_ui_cardhover', false);
  await sleep(80);
  ok(!body.classList.contains('cardHover'), 'body 上已去掉 cardHover');
  ok(w.localStorage.getItem('uiCardHover') === '0', '已记下关闭');
  setCheck('cfg_ui_cardhover', true);
  ok(body.classList.contains('cardHover'), '再勾回来又生效');
  ok(w.localStorage.getItem('uiCardHover') === '1', '已记下开启');

  console.log('');
  console.log('--- 关掉卡片淡阴影 ---');
  setCheck('cfg_ui_cardshadow', false);
  await sleep(80);
  ok(!body.classList.contains('cardShadow'), 'body 上已去掉 cardShadow');
  ok(w.localStorage.getItem('uiCardShadow') === '0', '已记下关闭');

  console.log('');
  console.log('--- 封面形状三档互斥 ---');
  setSel('cfg_ui_cover', 'std');
  await sleep(80);
  ok(body.classList.contains('covStd') && !body.classList.contains('covSq'), '竖版：只有 covStd');
  setSel('cfg_ui_cover', 'sq');
  await sleep(80);
  ok(body.classList.contains('covSq') && !body.classList.contains('covStd'), '正方形：只有 covSq');
  setSel('cfg_ui_cover', 'wide');
  await sleep(80);
  ok(!body.classList.contains('covSq') && !body.classList.contains('covStd'), '16:9：两个都没有');
  ok(w.localStorage.getItem('uiCoverMode') === 'wide', '已记下 16:9');

  console.log('');
  console.log('--- 界面风格（主题切换）：Apple Books vs Notion / Linear ---');
  ok(body.classList.contains('theme-apple') && !body.classList.contains('theme-notion'),
    '默认主题为 Apple Books（body 包含 theme-apple，不含 theme-notion）');
  ok($('cfg_ui_theme').value === 'apple', '控件回填：风格选中 apple');
  setSel('cfg_ui_theme', 'notion');
  await sleep(80);
  ok(body.classList.contains('theme-notion') && !body.classList.contains('theme-apple'),
    '切换到 Notion：立即生效（body 包含 theme-notion）');
  ok(w.localStorage.getItem('uiTheme') === 'notion', '已记下 notion');
  setSel('cfg_ui_theme', 'apple');
  await sleep(80);
  ok(body.classList.contains('theme-apple') && !body.classList.contains('theme-notion'),
    '切回 Apple：立即生效');
  ok(w.localStorage.getItem('uiTheme') === 'apple', '已记下 apple');

  console.log('');
  console.log('--- 色彩外观（深色模式开关）：浅色 vs 深色 ---');
  ok(body.classList.contains('mode-light') && !body.classList.contains('mode-dark'),
    '默认外观为浅色（body 包含 mode-light，不含 mode-dark）');
  ok($('cfg_ui_mode').checked === false, '控件回填：深色模式开关默认关闭（浅色）');
  ok($('lbl_ui_mode_title').textContent === '浅色日间', '文案联动：浅色状态标题显示「浅色日间」');
  setCheck('cfg_ui_mode', true);
  await sleep(80);
  ok(body.classList.contains('mode-dark') && !body.classList.contains('mode-light'),
    '切换到深色：立即生效（body 包含 mode-dark）');
  ok($('lbl_ui_mode_title').textContent === '深色夜间', '文案联动：深色状态标题显示「深色夜间」');
  ok(w.localStorage.getItem('uiMode') === 'dark', '已记下 dark');
  setCheck('cfg_ui_mode', false);
  await sleep(80);
  ok(body.classList.contains('mode-light') && !body.classList.contains('mode-dark'),
    '切回浅色：立即生效');
  ok($('lbl_ui_mode_title').textContent === '浅色日间', '文案联动：切回后恢复「浅色日间」');
  ok(w.localStorage.getItem('uiMode') === 'light', '已记下 light');

  console.log('');
  console.log('--- 重新应用时把当前值回填到控件上（重启/切页回来一致）---');
  w.localStorage.setItem('uiCardHover', '0');
  w.localStorage.setItem('uiCoverMode', 'sq');
  w.localStorage.setItem('uiTheme', 'notion');
  w.localStorage.setItem('uiMode', 'dark');
  w.applyUiSettings();
  await sleep(80);
  ok($('cfg_ui_cardhover').checked === false, '回填：悬浮开关反映实际值（关）');
  ok($('cfg_ui_cover').value === 'sq', '回填：封面形状反映实际值（正方形）');
  ok($('cfg_ui_theme').value === 'notion', '回填：风格反映实际值（Notion）');
  ok($('cfg_ui_mode').checked === true, '回填：深色模式反映实际值（开启）');
  ok(!body.classList.contains('cardHover') && body.classList.contains('covSq'), 'body class 同步');
  ok(body.classList.contains('theme-notion') && body.classList.contains('mode-dark'), 'body class 同步新主题与深色');

  console.log('');
  console.log('--- 作品卡片大小四档（--cardMin）---');
  ok(w.document.body.style.getPropertyValue('--cardMin') === '230px', '默认 230px（适中）');
  ok($('cfg_ui_cardsize').value === '230', '控件回填：选中适中');
  const szOpts = [...$('cfg_ui_cardsize').options].map((o) => o.value);
  ok(szOpts.join(',') === '180,205,230,280', `四档齐全（实际 ${szOpts.join(',')}）`);
  setSel('cfg_ui_cardsize', '280');
  await sleep(80);
  ok(w.document.body.style.getPropertyValue('--cardMin') === '280px', '切到宽松：立即生效');
  ok(w.localStorage.getItem('uiCardSize') === '280', '已记下');
  setSel('cfg_ui_cardsize', '180');
  await sleep(80);
  ok(w.document.body.style.getPropertyValue('--cardMin') === '180px', '切到紧凑：立即生效');

  console.log('');
  console.log('--- CSS 声明：封面三档（aspect-ratio 随卡片宽度缩放）---');
  const base = ruleFor('.workCard .cover');
  ok(base && /aspect-ratio:\s*16\/9/.test(base),
    `基础（默认 16:9）用 aspect-ratio（实际 ${base ? base.trim().slice(0, 44) : '无'}）`);
  ok(base && !/height:\s*\d+px/.test(base), '封面高度不再写死像素（会随窗口变大变小）');
  const std = ruleFor('body.covStd .workCard .cover');
  ok(std && /aspect-ratio:\s*3\/4/.test(std), '竖版档 3:4');
  const sq = ruleFor('body.covSq .workCard .cover');
  ok(sq && /aspect-ratio:\s*1\/1/.test(sq), '正方形档 1:1');
  const sqImg = ruleFor('body.covSq .workCard .cover img');
  ok(sqImg && /object-fit:\s*contain/.test(sqImg), '正方形档不裁切（contain）');
  const grid = ruleFor('#worksGrid, #allworksGrid');
  ok(grid && /auto-fill/.test(grid) && /minmax\(\s*var\(--cardMin/.test(grid) && /1fr/.test(grid),
    `网格自适应、末尾不留白（minmax + 1fr）（实际 ${grid ? grid.trim().slice(0, 50) : '无'}）`);
  ok(grid && /230px/.test(grid), 'CSS 兜底值是 230（适中，JS 没跑也是这个）');
  ok(grid && !/repeat\(auto-fill,\s*\d+px\)/.test(grid),
    '没有写成固定宽度（那样一行末尾会留空白）');
  const hover = ruleFor('body.cardHover .workCard:hover');
  ok(hover && /translateY\(-2px\)/.test(hover) && /box-shadow/.test(hover), '卡片悬浮：上浮 + 投影');
  const shadow = ruleFor('body.cardShadow .card');
  ok(shadow && /box-shadow/.test(shadow), '卡片淡阴影存在');

  console.log('');
  console.log('--- CSS 声明：美化一批（防止被手滑改回去）---');
  const sb = ruleFor('::-webkit-scrollbar');
  ok(sb && /width:\s*8px/.test(sb), '滚动条收成 8px');
  const navA = ruleFor('nav button.active');
  ok(navA && !/font-weight/.test(navA), `导航激活态不再改字重（修切换抖动）（实际 ${navA ? navA.trim() : '无'}）`);
  const bh = ruleFor('button:hover');
  ok(bh && /#f0f1f2/.test(bh) && !/#f6f7f8/.test(bh),
    `按钮 hover 背景不再是和页面底色撞车的 #f6f7f8（实际 ${bh ? bh.trim() : '无'}）`);
  const ih = ruleFor('.item:hover');
  ok(ih && /#f2f3f5/.test(ih), '列表行有 hover 态');
  const ch2 = ruleFor('.card h2');
  ok(ch2 && /font-size:\s*15px/.test(ch2) && /#18191c/.test(ch2), '卡片标题 15px 且用深色');
  const fv = ruleFor('.workCard:focus-visible, .item:focus-visible');
  ok(fv !== null || /:focus-visible/.test(html), '焦点环统一用 :focus-visible');
  ok(!/:focus\s*\{[^}]*outline:\s*2px solid #fb7299/.test(html),
    '不再有 :focus（鼠标点击也会留粉框）的写法');
  // 断言「极简按钮」这条规格，而不是写死某个 rgba 数值：
  // 常态无底无框，hover 才浮出半透明白色微光。
  const rb = ruleFor('#readerBar button');
  ok(rb && /background\s*:\s*transparent/.test(rb), '阅读器条按钮常态无底色');
  ok(rb && /border-color\s*:\s*transparent/.test(rb), '阅读器条按钮常态无边框');
  const rbHover = ruleFor('#readerBar button:hover');
  ok(rbHover && /rgba\(255,\s*255,\s*255/.test(rbHover),
    '阅读器条按钮 hover 才浮出半透明白色微光');

  console.log('');
  console.log('--- 深色区的按钮必须自带 hover（#id 会挡掉全局 button:hover）---');
  const th = ruleFor('#btnThumb');
  ok(!!th && /background/.test(th), '缩略图入口按钮有独立样式');
  ok(ruleFor('#btnThumb:hover') !== null,
    '缩略图入口按钮有自己的 hover 规则（否则鼠标放上去毫无反应）');
  const eo = ruleFor('#readerEnd .endOps button');
  ok(eo && /rgba\(255, 255, 255/.test(eo), '章末页按钮也是半透明白');
  ok(ruleFor('#readerEnd .endOps button:hover') !== null, '章末页按钮有自己的 hover 规则');
  ok(ruleFor('#readerEnd .endOps button.primary:hover') !== null,
    '章末页主按钮（进入下一章）也有 hover');
  const pg = ruleFor('.page ');
  ok(pg && /padding:\s*16px/.test(pg), '页面内边距统一为 16px');

  console.log('');
  console.log('--- 章节列表 / 编辑页的封面缩略图也不能是竖版裁切 ---');
  const itemThumb = ruleFor('.item img.thumb');
  ok(itemThumb && /height:\s*56px/.test(itemThumb) && /width:\s*auto/.test(itemThumb) &&
     !/object-fit:\s*cover/.test(itemThumb),
    `章节列表缩略图：高度固定、宽度按比例（实际 ${itemThumb ? itemThumb.trim().slice(0, 44) : '无'}）`);
  ok(/id="editWorkCover"[^>]*width:auto/.test(html) &&
     !/id="editWorkCover"[^>]*object-fit:cover/.test(html),
    '编辑页封面预览也是宽度自适应（96×120 竖框已改）');

  console.log('');
  console.log('--- 字号（v1.13）：设置页大一档，别处的小不动 ---');
  const hintBase = ruleFor('.hint');
  ok(hintBase && /font-size:\s*12px/.test(hintBase),
    `全站说明文字基准仍是 12px（列表密度不动）（实际 ${hintBase ? hintBase.trim().slice(0, 44) : '无'}）`);
  ok(hintBase && /color:\s*#7a7f86/.test(hintBase) && !/#9499a0/.test(hintBase),
    '说明文字灰色加深 #9499a0 → #7a7f86（对比度 2.9:1 → 4.6:1）');
  ok(hintBase && /line-height:\s*1\.75/.test(hintBase), '说明段落行高 1.75（长段落不挤成一坨）');
  const hintSet = ruleFor('#page-settings .hint');
  ok(hintSet && /font-size:\s*13px/.test(hintSet),
    `设置页说明文字单独提到 13px（靠 #page-settings 提特异性，不能并进 .hint）`);
  const uirowSet = ruleFor('#page-settings .uirow');
  ok(uirowSet && /font-size:\s*14px/.test(uirowSet), '设置页标签 / 开关行 13 → 14px');
  const uiEm = ruleFor('.uirow em');
  ok(uiEm && /font-size:\s*13px/.test(uiEm) && /#7a7f86/.test(uiEm),
    '开关副说明 12 → 13px 且跟着加深灰度');
  const smallBtn = ruleFor('button.small');
  ok(smallBtn && /font-size:\s*13px/.test(smallBtn), '小按钮 12 → 13px（这个 class 全站共用）');

  console.log('');
  console.log('--- 设置页卡片顺序：界面在最前（v1.13.1）---');
  const settingsHtml = html.slice(html.indexOf('id="page-settings"'), html.indexOf('id="page-shelf"'));
  const pUiCard = settingsHtml.indexOf('<h2>界面</h2>');
  const pDirCard = settingsHtml.indexOf('<h2>目录</h2>');
  ok(pUiCard >= 0 && pDirCard >= 0 && pUiCard < pDirCard,
    '设置页里「界面」排在「目录」前面（即时生效的放最上面，一次性配置往后）');
  ok(settingsHtml.indexOf('cfg_ui_cardsize') < settingsHtml.indexOf('id="settingsDirs"'),
    '「界面」是设置页第一张卡（不是被塞在最后）');

  console.log('');
  console.log('--- Apple 风格下拉接管：原生 select 必须留着（v1.18.1）---');
  {
    const d = w.document;
    const sel = d.getElementById('cfg_ui_cover');
    const optCount = sel ? sel.options.length : 0;
    ok(!!sel && optCount > 0, '接管前：原生 select 在 DOM 里且有选项');
    ok(typeof w.initCustomSelects === 'function', '存在 initCustomSelects 接管函数');
    w.initCustomSelects();
    ok(!!d.getElementById('cfg_ui_cover'), '接管后原生 select 没被删（业务和测试都靠它的 .value）');
    ok(d.getElementById('cfg_ui_cover').options.length === optCount,
      `接管后 option 一条没少（${optCount} 条）`);
    const wrap = sel.nextElementSibling;
    ok(!!wrap && wrap.className === 'apple-select',
      '自定义容器插在原生 select 后面（是接管，不是替换）');

    const fired = [];
    sel.addEventListener('change', () => fired.push(sel.value));
    const items = wrap ? [...wrap.querySelectorAll('.apple-select-item')] : [];
    ok(items.length === optCount, `气泡选项数与 option 一致（${items.length}/${optCount}）`);
    const other = items.find((i) => i.dataset.value !== sel.value);
    if (other) other.click();
    ok(sel.value === (other ? other.dataset.value : sel.value),
      '点自定义项 → 原生 select 的 value 跟着变');
    ok(fired.length === 1, `点自定义项 → 原生 select 派发了 change（设置靠它即时生效）`);
    ok(!d.querySelector('.apple-select.open'), '选完气泡自动收起');

    w.initCustomSelects();
    const wraps = [...sel.parentNode.children].filter((c) => c.className === 'apple-select').length;
    ok(wraps === 1, `重复调用不重复挂载（实际 ${wraps} 套）`);
  }

  console.log('');
  console.log('--- 动态首字艺术封面（v1.21.0）：沙箱交付的测试没覆盖，这里补齐 ---');
  ok(typeof w.getCoverInitial === 'function' && typeof w.getCoverPalette === 'function' &&
     typeof w.applyCoverPlaceholder === 'function',
    '三个首字封面函数都在（顶层，漏接/改名会立刻暴露）');
  ok(w.getCoverInitial('《进击的巨人》') === '进', '剔除书名号再取首字');
  ok(w.getCoverInitial('【测试】作品') === '测', '方括号同样剔除');
  ok(w.getCoverInitial('attack on titan') === 'A', '英文取首字母并转大写');
  ok(w.getCoverInitial('') === '漫' && w.getCoverInitial('《》') === '漫',
    '空标题 / 只剩书名号都退化成「漫」');
  const pA = w.getCoverPalette('同一部作品'), pB = w.getCoverPalette('同一部作品');
  ok(pA.bg === pB.bg && pA.text === pB.text,
    '同一书名恒映射到同一块色板（否则每次刷新书架都在换色）');
  ok(/gradient/.test(pA.bg) && /^#/.test(pA.text), '色板是渐变底 + 明确的文字色');
  {
    const pal = w.eval('COVER_PALETTES');
    ok(pal.length === 12, `内置 12 套色板（实际 ${pal.length}）`);
    ok(new Set(pal.map((p) => p.bg)).size === pal.length, '12 套底板互不重复');
  }
  const ph = w.document.createElement('div');
  w.applyCoverPlaceholder(ph, '雨天的小镇');
  ok(ph.textContent.includes('雨') && ph.textContent.includes('BOOK'),
    '占位里同时有首字与 BOOK 徽记');
  ok(/gradient/.test(ph.style.background), '占位铺了渐变底（不再是灰块）');
  ok(ph.style.display === 'flex' && ph.style.justifyContent === 'center', '首字居中');
  ok(!!ph.querySelector('span.coverInitial'), '首字挂在 .coverInitial 上（CSS 能单独调字号）');

  console.log('');
  console.log('--- 首字封面真的接进了书卡片（不是只在工具函数里自嗨）---');
  await w.renderShelf();
  await sleep(120);
  const shelfCover = w.document.querySelector('#worksGrid .workCard .cover');
  ok(!!shelfCover, '书架渲染出了封面容器');
  ok(shelfCover && !shelfCover.querySelector('img'), '无封面作品不插 <img>');
  ok(shelfCover && shelfCover.textContent.includes('测'),
    `无封面作品走首字占位（实际「${shelfCover ? shelfCover.textContent : ''}」）`);
  const card2 = w.workCard({ id: 'w2', name: '月光下的告白', author: '某人', cover_url: null, chapter_count: 0 }, 'shelf');
  const cover2 = card2.querySelector('.cover');
  ok(cover2 && cover2.textContent.includes('月'),
    '换一部作品首字跟着变（不是写死一个字）');
  ok(cover2 && /gradient/.test(cover2.style.background), '每张卡的渐变底由书名哈希决定');
  const card3 = w.workCard({ id: 'w3', name: '有封面的', author: '', cover_url: '/outputs/x.jpg', chapter_count: 0 }, 'shelf');
  ok(!!card3.querySelector('.cover img'), '有封面的作品照旧走 <img>（没被占位逻辑顶掉）');

  console.log('');
  console.log('--- CSS 声明：深色外观不能漏白（v1.21.0，观感人工验，这里只守声明）---');
  const dmCtx = ruleFor('body.theme-apple.mode-dark #ctxMenu');
  ok(dmCtx && /background/.test(dmCtx) && !/#fff/i.test(dmCtx),
    `深色下右键菜单有深色底（实际 ${dmCtx ? dmCtx.trim().slice(0, 40) : '无'}）`);
  ok(ruleFor('body.theme-apple.mode-dark .apple-select-menu') !== null,
    '深色下自定义下拉气泡也有深色底（原生白弹窗盖不住的地方）');
  ok(ruleFor('body.theme-apple.mode-dark .mselItem') !== null,
    '深色下标签多选浮层的条目也深色化');
  const nNav = ruleFor('body.theme-notion nav button.active');
  ok(nNav && /border-bottom-color/.test(nNav),
    'Notion 风格激活态是下划线（不是背景块）');
  ok(ruleFor('body.theme-notion.mode-dark') !== null &&
     ruleFor('body.theme-apple.mode-dark') !== null,
    '两套骨架 × 深色都各有一份规则（不是只做了浅色）');

  console.log('');
  console.log('--- 界面设置要同时交给后端存副本（v1.29.0）---');
  saved.length = 0;
  setSel('cfg_ui_theme', 'notion');
  await sleep(80);
  ok(w.localStorage.getItem('uiTheme') === 'notion', 'localStorage 照旧写（即时生效靠它）');
  ok(saved.some((o) => o && o.uiTheme === 'notion'),
    `同时把 uiTheme 交给后端存副本（实际 ${JSON.stringify(saved)}）—— 少了这一步，`
    + '端口一换 / WebView2 用了临时目录，下次启动就回到默认值');

  console.log('');
  console.log('--- 启动时把后端副本写回 localStorage（顺序：写回 → 初始化）---');
  const { dom: dom2, w: w2, errs: errs2 } = await bootDom({
    ...STATE, ui_settings: { uiTheme: 'notion', uiCardSize: '280' },
  });
  const $2 = (id) => w2.document.getElementById(id);
  ok(w2.localStorage.getItem('uiTheme') === 'notion', '副本里的风格写回了 localStorage');
  ok(w2.localStorage.getItem('uiCardSize') === '280', '副本里的卡片大小也写回了');
  ok(w2.document.body.classList.contains('theme-notion'),
    '写回的值真的生效了（不是只写进 localStorage 就完事）');
  ok(w2.document.body.style.getPropertyValue('--cardMin') === '280px',
    '写回的卡片大小也生效了（--cardMin 跟着变）');
  ok($2('cfg_ui_theme').value === 'notion' && $2('cfg_ui_cardsize').value === '280',
    '控件回填的是副本里的值（切走再切回来不会打回默认）');
  ok(errs2.length === 0, `写回流程无未捕获错误${errs2.length ? ' —— ' + errs2.join(' | ') : ''}`);

  console.log('');
  ok(errs.length === 0, `无未捕获错误${errs.length ? ' —— ' + errs.join(' | ') : ''}`);
  dom.window.close();
  dom2.window.close();

  console.log('');
  console.log(fail.length ? `FAILED ${fail.length}` : 'ALL PASS');
  process.exit(fail.length ? 1 : 0);
})();
