/* 视频章节：阅读器内的第三种内容形态（v1.19）
 *
 * 视频章节与单图 / 连续滚动平级，走**整条旁路**，所以这里守的是「让位」是否干净：
 *   1) 进入视频章节：挂 <video>、src 正确、页码显示「视频」，
 *      图片视口 / 缩略图带 / 模式按钮全部隐身；
 *   2) 键盘由代码自己接管（原生 controls 只在元素有焦点时才响应，而焦点在 body）：
 *      空格 = 播放/暂停，←→ = ∓5 秒，且**绝不触发翻页**；
 *   3) duration 还没加载出来（NaN）时按 → 不能把进度拽回 0；
 *   4) 播完复用现成的章末页 END_EL，挂进视频容器，且不给「回到上一页」；
 *   5) error（文件丢失 / 编码播不了）时显示占位文案而不是留一个转圈的播放器；
 *   6) 切回图片章节：视频容器收起、src 断开（不再后台缓冲）、模式按钮恢复。
 *
 * jsdom 不实现 HTMLMediaElement 的解码与播放，所以这里用**实例上的属性**打桩
 * （play / pause / currentTime / duration / load）；**能不能真的播**只能人工验，
 * 见 README 的回归清单。
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
const LIB = {
  works: [{ id: 'w1', name: '测试作品', author: '某人', cover_url: null, chapter_count: 3 }],
  chapters: [], orphans: [], pending: [],
};
const IMAGES = [
  { file: 'images/001.jpg', url: '/outputs/1.jpg', width: 800, height: 1200 },
  { file: 'images/002.jpg', url: '/outputs/2.jpg', width: 800, height: 1200 },
];
const VIDEO_URL = '/outputs/chapters/c2/video.mp4';
const MEDIA = { file: 'video.mp4', url: VIDEO_URL, format: 'mp4', size: 42189012 };
const CHAPTERS = [
  { index: 1, chapter_id: 'c1', opus_id: '111', source: 'opus', title: '第一章', note: '', cover_url: null, path: 'chapters/c1' },
  { index: 2, chapter_id: 'c2', opus_id: '', source: 'video', title: '第二章 动态特典', note: '', cover_url: null, path: 'chapters/c2' },
  { index: 3, chapter_id: 'c3', opus_id: '333', source: 'opus', title: '第三章', note: '', cover_url: null, path: 'chapters/c3' },
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
const vis = (el) => !!el && el.style.display !== 'none';
// gui.html 顶层用的是 let reader / let fullscreen —— **不挂 window**，只能 eval 取
const ev = (w, expr) => w.eval(expr);

/** 起 jsdom 并完成启动；chapter_images 对 c2（视频章节）返回 media */
async function bootDom() {
  const errs = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errs.push('jsdomError: ' + (e.detail || e.message)));
  const calls = [];
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
  const stub = {
    get_state: () => Promise.resolve(STATE),
    library_state: () => Promise.resolve(LIB),
    list_works: () => Promise.resolve({ works: LIB.works }),
    mark_read: () => Promise.resolve({ ok: true }),
    work_detail: (wid) => Promise.resolve({ id: wid, name: '测试作品', author: '某人', cover_url: null, chapters: CHAPTERS }),
    chapter_images: (cid) => {
      calls.push(['chapter_images', cid]);
      return Promise.resolve(cid === 'c2'
        ? { images: [], media: MEDIA }
        : { images: IMAGES, media: null });
    },
    list_incomplete: () => Promise.resolve({ items: [] }),
    open_dir: () => Promise.resolve({ ok: true }),
    toggle_fullscreen: () => { calls.push(['toggle_fullscreen']); return Promise.resolve({ ok: true, fullscreen: true }); },
  };
  Object.assign(w.pywebview.api, stub);
  w.localStorage.setItem('readerMode', 'single');
  w.dispatchEvent(new w.Event('pywebviewready'));
  await sleep(250);

  // jsdom 不实现媒体播放：在**实例**上打桩（动原型会污染别的测试，也不好还原）
  const v = w.document.getElementById('readerVideo');
  const st = { paused: true, t: 0, dur: 30, played: 0, paused_: 0 };
  Object.defineProperty(v, 'paused', { get: () => st.paused, configurable: true });
  Object.defineProperty(v, 'currentTime', {
    get: () => st.t, set: (x) => { st.t = x; }, configurable: true,
  });
  Object.defineProperty(v, 'duration', { get: () => st.dur, configurable: true });
  v.play = () => { st.paused = false; st.played += 1; return Promise.resolve(); };
  v.pause = () => { st.paused = true; st.paused_ += 1; };
  v.load = () => {};   // jsdom 未实现，不桩会往虚拟控制台喷 Not implemented

  // jsdom 也不实现 Fullscreen API：打一个最小实现（记全屏元素 + 派发 fullscreenchange）。
  // 真机上的行为（铺满屏幕、遮任务栏、原生全屏控件）只能人工验，见 README 回归清单。
  const fsState = { el: null };
  Object.defineProperty(w.document, 'fullscreenElement', {
    get: () => fsState.el, configurable: true,
  });
  w.Element.prototype.requestFullscreen = function () {
    fsState.el = this;
    w.document.dispatchEvent(new w.Event('fullscreenchange'));
    return Promise.resolve();
  };
  w.document.exitFullscreen = function () {
    fsState.el = null;
    w.document.dispatchEvent(new w.Event('fullscreenchange'));
    return Promise.resolve();
  };
  return { dom, w, errs, calls, v, st, fsState };
}

async function assertClean(w, errs, label) {
  await sleep(120);
  const modal = w.document.getElementById('modal');
  const shown = modal.classList.contains('show');
  const msg = w.document.getElementById('modalBox').textContent || '';
  if (shown && /出错了/.test(msg)) console.log('   弹框内容：' + msg.slice(0, 160).replace(/\s+/g, ' '));
  ok(!(shown && /出错了/.test(msg)), `${label}：没有弹出错误框`);
  ok(errs.length === 0, `${label}：没有未捕获错误${errs.length ? ' —— ' + errs.join(' | ') : ''}`);
  if (shown) modal.classList.remove('show');
}

const key = (w, k) => w.document.dispatchEvent(
  new w.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));

(async () => {
  const { dom, w, errs, calls, v, st, fsState } = await bootDom();
  const $ = (id) => w.document.getElementById(id);

  console.log('--- 进入视频章节：整条旁路 ---');
  await w.openReader('w1', 1);
  await sleep(250);
  ok($('page-reader').classList.contains('show'), '已进入阅读器页');
  ok(ev(w, '!!(reader && reader.video)'), 'reader.video 有值（后端 media 字段分流）');
  ok(vis($('readerVideoWrap')), '视频容器已显示');
  ok(!vis($('singleWrap')), '单图视口已隐藏');
  ok(!vis($('scrollWrap')), '连续滚动视口已隐藏');
  ok(!vis($('thumbBar')), '缩略图带已隐藏');
  ok(!vis($('btnThumb')), '缩略图入口按钮已隐藏');
  ok(!vis($('btnMode')), '「模式」按钮已隐藏（单图/连续滚动对视频没意义）');
  ok((v.getAttribute('src') || '').indexOf('video.mp4') >= 0,
    `<video> 的 src 指向视频文件（实际 ${v.getAttribute('src')}）`);
  ok(txt(w, 'readerPos') === '视频', `页码显示「视频」（实际 ${txt(w, 'readerPos')}）`);
  ok(/第二章/.test(txt(w, 'readerTitle')), '顶条章名正确');
  await assertClean(w, errs, '进入视频章节');

  console.log('--- 键盘：空格 = 播放/暂停（不依赖焦点）---');
  ok(st.paused === true, '初始是暂停的');
  key(w, ' ');
  await sleep(80);
  ok(st.paused === false, '空格 → play()');
  ok(st.played === 1, `play 被调用 1 次（实际 ${st.played}）`);
  key(w, ' ');
  await sleep(80);
  ok(st.paused === true, '再按空格 → pause()');
  ok(st.paused_ === 1, `pause 被调用 1 次（实际 ${st.paused_}）`);

  console.log('--- 键盘：←→ = ∓5 秒，且不翻页 ---');
  st.t = 10;
  const before = calls.length;
  key(w, 'ArrowRight');
  await sleep(80);
  ok(st.t === 15, `→ 快进 5 秒（实际 ${st.t}）`);
  key(w, 'ArrowLeft');
  await sleep(80);
  ok(st.t === 10, `← 快退 5 秒（实际 ${st.t}）`);
  ok(calls.length === before, '按方向键没有触发新的 chapter_images（确实没翻页）');
  ok(txt(w, 'readerPos') === '视频', '页码仍是「视频」（没被翻页改成 1/0）');

  console.log('--- 边界：夹在 0 与 duration 之间 ---');
  st.t = 28; st.dur = 30;
  key(w, 'ArrowRight');
  await sleep(80);
  ok(st.t === 30, `快进夹到 duration（实际 ${st.t}）`);
  st.t = 2;
  key(w, 'ArrowLeft');
  await sleep(80);
  ok(st.t === 0, `快退夹到 0（实际 ${st.t}）`);

  console.log('--- A / D 与 ←→ 同义：视频里没有「页」，单手翻页就是快进快退 ---');
  st.t = 10;
  key(w, 'd');
  await sleep(80);
  ok(st.t === 15, `D 快进 5 秒（实际 ${st.t}）`);
  key(w, 'a');
  await sleep(80);
  ok(st.t === 10, `A 快退 5 秒（实际 ${st.t}）`);
  ok(txt(w, 'readerPos') === '视频', 'A / D 没有去翻章（视频章节没有「页」可翻）');

  console.log('--- duration 还没加载出来（NaN）时，→ 不能把进度拽回 0 ---');
  st.t = 10; st.dur = NaN;   // 元数据未就绪时的真实形态
  key(w, 'ArrowRight');
  await sleep(80);
  ok(st.t === 15, `不夹上界，正常 +5（实际 ${st.t}）`);
  st.dur = 30;

  console.log('--- f / Escape 仍然通用（没被视频吃光）---');
  key(w, 'f');
  await sleep(200);
  ok(ev(w, 'fullscreen') === true, 'f 仍然能进全屏（视频模式没把通用键吃光）');
  ok(calls.some((c) => c[0] === 'toggle_fullscreen'), '确实调了原生全屏');
  key(w, 'Escape');
  await sleep(200);
  ok(ev(w, 'fullscreen') === false, 'Escape 仍然能退出全屏');

  console.log('--- 全屏：两层一起进（窗口 + 元素）---');
  await w.openReader('w1', 1);
  await sleep(250);
  const wFs = () => calls.filter((c) => c[0] === 'toggle_fullscreen').length;
  const b3 = wFs();
  key(w, 'f');
  await sleep(220);
  ok(ev(w, 'fullscreen') === true, '按 f 进入全屏');
  ok(wFs() === b3 + 1, '窗口那层也进了（调了 toggle_fullscreen）');
  ok(fsState.el === $('readerVideoWrap'),
    '元素全屏的是**容器**而不是 <video> 本身（章末页才跟着一起全屏）');
  ok($('readerVideoWrap').contains($('readerVideo')), '<video> 在全屏容器内');

  console.log('--- 播完：章末页在全屏容器内（跟着一起全屏，不退出）---');
  v.dispatchEvent(new w.Event('ended'));
  await sleep(150);
  ok($('readerEnd').parentElement === fsState.el, '章末页挂在全屏元素里（全屏里点得到「下一章」）');
  ok(fsState.el === $('readerVideoWrap'), '全屏元素没变（播完**不**退出全屏）');
  ok(ev(w, 'fullscreen') === true, '窗口那层也还在');
  ok(vis($('readerEnd')), '章末页可见');
  await assertClean(w, errs, '全屏里播完');

  console.log('--- 退出：两层一起退 ---');
  key(w, 'f');
  await sleep(220);
  ok(ev(w, 'fullscreen') === false, '再按 f 退出全屏');
  ok(fsState.el === null, '元素全屏也退了（不会留一块黑屏）');

  console.log('--- 反向联动：在原生控件里退出全屏，窗口那层不能留在那儿 ---');
  key(w, 'f');
  await sleep(220);
  ok(fsState.el === $('readerVideoWrap') && ev(w, 'fullscreen') === true, '先进入两层全屏');
  const b4 = wFs();
  w.document.exitFullscreen();      // 模拟用户点了原生控件里的「退出全屏」
  await sleep(220);
  ok(fsState.el === null, '元素全屏已退出');
  ok(ev(w, 'fullscreen') === false, '窗口那层跟着一起退（否则回书架连导航栏都没有）');
  ok(wFs() === b4 + 1, `确实调了一次窗口退出（实际 ${wFs() - b4} 次）`);

  console.log('--- 窗口已全屏时点内置全屏按钮（<video> 进全屏）不能把窗口退掉 ---');
  // 这是 v1.19.4 修的 bug：旧代码拿 fullscreenElement 和容器比，用户点内置按钮时
  // Chromium 全屏的是 <video> 本身，不相等 → 误判成「元素全屏已退出」→ 反向退掉窗口全屏
  key(w, 'f');
  await sleep(220);
  ok(fsState.el === $('readerVideoWrap') && ev(w, 'fullscreen') === true, '先进入两层全屏');
  const b6 = wFs();
  w.Element.prototype.requestFullscreen.call($('readerVideo'));   // 模拟点内置全屏按钮
  await sleep(220);
  ok(fsState.el === $('readerVideo'), '全屏元素变成了 <video> 本身（内置按钮的语义）');
  ok(ev(w, 'fullscreen') === true, '窗口全屏**没有**被退掉（旧代码这里会缩回去）');
  ok(wFs() === b6, `没有多调 toggle_fullscreen（实际多调 ${wFs() - b6} 次）`);
  await assertClean(w, errs, '内置全屏按钮不退窗口');

  console.log('--- 窗口未全屏时点内置全屏按钮 → 窗口联动进（遮任务栏）---');
  w.document.exitFullscreen();            // 先退元素全屏
  await sleep(220);
  ok(ev(w, 'fullscreen') === false, '元素退出后窗口那层也跟着退了');
  const b7 = wFs();
  w.Element.prototype.requestFullscreen.call($('readerVideo'));   // 再点内置全屏按钮
  await sleep(220);
  ok(ev(w, 'fullscreen') === true, '窗口联动进全屏（不然任务栏露着）');
  ok(wFs() === b7 + 1, `确实调了一次窗口全屏（实际 ${wFs() - b7} 次）`);
  await assertClean(w, errs, '内置全屏按钮联动进窗口');

  // 归零：后面旧节的前提是「未全屏」（此时 el=video、fullscreen=true）
  w.document.exitFullscreen();
  await sleep(220);
  ok(ev(w, 'fullscreen') === false, '恢复到未全屏状态');

  console.log('--- 切到图片章节：退元素全屏，但**保留**窗口全屏 ---');
  key(w, 'f');
  await sleep(220);
  ok(fsState.el === $('readerVideoWrap'), '又进了两层全屏');
  const b5 = wFs();
  await w.openReader('w1', 0);      // 第一章是图片
  await sleep(320);
  ok(fsState.el === null, '元素全屏已退（容器收起来了，留着就是一块黑屏）');
  ok(ev(w, 'fullscreen') === true, '窗口全屏**保留**（图片章节的全屏就是它）');
  ok(wFs() === b5, `没有多调一次窗口退出（实际多调 ${wFs() - b5} 次）`);

  console.log('--- 图片章节不该碰元素全屏 ---');
  fsState.el = null;
  key(w, 'Escape');
  await sleep(220);
  ok(ev(w, 'fullscreen') === false, '先退回非全屏');
  key(w, 'f');
  await sleep(220);
  ok(ev(w, 'fullscreen') === true, '图片章节按 f 照样进窗口全屏');
  ok(fsState.el === null, '图片章节不进元素全屏（那是视频的事）');
  key(w, 'Escape');
  await sleep(220);
  ok(ev(w, 'fullscreen') === false, 'Escape 退回非全屏');
  await assertClean(w, errs, '全屏联动');

  await w.openReader('w1', 1);      // 回到视频章节，后面的断言还要用
  await sleep(250);

  console.log('--- 滚轮不再翻页（Ctrl+滚轮缩放也一起让位）---');
  const b2 = calls.length;
  $('readerBody').dispatchEvent(new w.WheelEvent('wheel', { deltaY: 100, bubbles: true, cancelable: true }));
  await sleep(120);
  ok(calls.length === b2, '滚轮没有触发 chapter_images');
  // 这一条才真正钉住 wheel 监听里的 reader.video 提前 return：
  // 翻页那边 readerNext/readerPrev 各自也有 reader.video 守卫，光测「不翻页」测不出来
  $('readerBody').dispatchEvent(new w.WheelEvent('wheel', { deltaY: -100, ctrlKey: true, bubbles: true, cancelable: true }));
  await sleep(120);
  ok(ev(w, 'zoomState') === null, 'Ctrl+滚轮 在视频章节不进缩放（缩放是图片的功能）');
  ok(!w.document.body.classList.contains('zooming'), 'body 没进 zooming 态');

  console.log('--- 播完：复用现成的章末页 ---');
  v.dispatchEvent(new w.Event('ended'));
  await sleep(150);
  const endEl = $('readerEnd');
  ok(endEl.parentElement === $('readerVideoWrap'), '章末页挂在视频容器里');
  ok(vis(endEl), '章末页已显示');
  ok(txt(w, 'readerPos') === '本话完', `页码显示「本话完」（实际 ${txt(w, 'readerPos')}）`);
  ok(!vis($('btnEndBack')), '视频章末页不给「回到上一页」（它会拿空图片列表去算）');
  ok(vis($('btnEndNext')), '「进入下一章」仍在（不是末章）');
  ok(endEl.classList.contains('forVideo'), '章末页标注了 forVideo（藏掉那条不适用的提示）');
  ok(!vis(v), '播放器已收起');
  await assertClean(w, errs, '视频播完');

  console.log('--- error：文件丢失 / 编码播不了 ---');
  await w.openReader('w1', 1);
  await sleep(250);
  v.dispatchEvent(new w.Event('error'));
  await sleep(150);
  ok(!vis(v), '播放器已隐藏（不留一个转圈的东西在那儿）');
  ok(vis($('readerVideoError')), '占位卡片已显示');
  ok(/H\.264|抽帧/.test(txt(w, 'readerVideoError')),
    `文案给出了能照做的建议（实际「${txt(w, 'readerVideoError')}」）`);
  await assertClean(w, errs, '播放失败占位');

  console.log('--- 切回图片章节：收干净 ---');
  await w.openReader('w1', 0);
  await sleep(300);
  ok(!vis($('readerVideoWrap')), '视频容器已收起');
  ok(vis($('singleWrap')), '单图视口回来了');
  ok(vis($('btnMode')), '「模式」按钮恢复了');
  ok(!v.hasAttribute('src'), 'src 已断开（不会继续在后台缓冲）');
  ok(txt(w, 'readerPos') === '1 / 2', `页码回到图片模式（实际 ${txt(w, 'readerPos')}）`);
  ok(ev(w, '!(reader && reader.video)'), 'reader.video 已清空');
  await assertClean(w, errs, '切回图片章节');

  console.log('--- 离开阅读器必须停声音（不能后台还在放）---');
  await w.openReader('w1', 1);
  await sleep(300);
  key(w, ' ');                        // 空格 = 播放
  await sleep(150);
  ok(st.paused === false, '先用空格播起来（st.paused === false）');
  ok(v.hasAttribute('src'), 'src 挂着');
  $('btnReaderBack').click();         // 点顶条的「← 返回」
  await sleep(350);
  ok(st.paused === true, '返回后**已暂停**（不会继续出声）');
  ok(!v.hasAttribute('src'), 'src 已断开（后台也不再缓冲）');
  ok(!$('page-reader').classList.contains('show'), '确实离开了阅读器页');
  await assertClean(w, errs, '返回书架停视频');

  console.log('--- 切页签离开，声音同样要停 ---');
  await w.openReader('w1', 1);
  await sleep(300);
  key(w, ' ');
  await sleep(150);
  ok(st.paused === false, '又播起来了');
  w.switchPage('shelf');
  await sleep(300);
  ok(st.paused === true, '切到书架后已暂停');
  ok(!v.hasAttribute('src'), 'src 已断开');
  await assertClean(w, errs, '切页签停视频');

  console.log('--- stopVideo 幂等：没在播时调也不出错 ---');
  w.eval('stopVideo()');
  w.eval('stopVideo()');
  await sleep(120);
  ok(st.paused === true, '重复调用不会把状态弄坏');
  ok(!v.hasAttribute('src'), '重复调用后 src 仍然是断开的');
  await assertClean(w, errs, 'stopVideo 幂等');

  console.log('--- 双击进出全屏：视频唯一的全屏手势 ---');
  const dbl = (target) => target.dispatchEvent(
    new w.MouseEvent('dblclick', { bubbles: true, cancelable: true }));
  await w.openReader('w1', 1);
  await sleep(300);
  ok(ev(w, 'fullscreen') === false, '起始不是全屏');
  const b8 = wFs();
  dbl($('readerVideo'));          // 点在播放器上（会冒泡到容器）
  await sleep(250);
  ok(ev(w, 'fullscreen') === true, '双击进入全屏');
  // 这条钉的是「只绑容器」：再单独给 <video> 绑一次，冒泡会让 toggleFullscreen
  // 连着跑两遍 —— 一进一退，表现就是「双击没反应」
  ok(wFs() === b8 + 1, `只调了一次窗口全屏（实际 ${wFs() - b8} 次）`);
  ok(fsState.el === $('readerVideoWrap'), '元素全屏的仍是容器（章末页才能跟着一起全屏）');
  dbl($('readerVideoWrap'));
  await sleep(250);
  ok(ev(w, 'fullscreen') === false, '再双击退出全屏');
  ok(fsState.el === null, '元素全屏也退了');
  ok(wFs() === b8 + 2, `总共正好两次（实际 ${wFs() - b8} 次）`);

  console.log('--- 双击只认播放器与空白：点在占位卡片上不算 ---');
  const b9 = wFs();
  dbl($('readerVideoError'));
  await sleep(200);
  ok(wFs() === b9, '占位卡片上的双击不进全屏（那儿双击另有用途）');

  console.log('--- 图片章节不该响应视频容器的双击 ---');
  await w.openReader('w1', 0);
  await sleep(300);
  const b10 = wFs();
  dbl($('readerVideoWrap'));
  await sleep(200);
  ok(wFs() === b10, '图片章节下双击视频容器不做事（容器是藏着的）');
  await assertClean(w, errs, '双击全屏');

  console.log('--- 作品页：视频章节带徽章 ---');
  await w.openWork('w1', 'shelf');
  await sleep(250);
  const badges = [...$('page-work').querySelectorAll('.vbadge')];
  ok(badges.length === 1, `作品页有 1 个「视频」徽章（实际 ${badges.length}）`);
  ok(badges[0] && badges[0].textContent === '视频', '徽章文案是「视频」');
  await assertClean(w, errs, '作品页徽章');

  console.log('--- 章节编辑页对视频章节要挡住（没有图片可删）---');
  await w.openChapterEdit('c2', 'shelf', '第二章 动态特典');
  await sleep(250);
  const box = txt(w, 'modalBox');
  ok($('modal').classList.contains('show'), '弹出了提示');
  ok(/视频章节/.test(box), `提示说是视频章节（实际「${box.slice(0, 40)}」）`);
  if ($('modal').classList.contains('show')) $('m_ok').click();
  await assertClean(w, errs, '章节编辑页挡住视频章节');

  dom.window.close();
  console.log('');
  console.log(fail.length ? `FAILED ${fail.length}` : 'ALL PASS');
  process.exit(fail.length ? 1 : 0);
})();
