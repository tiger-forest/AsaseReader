/* 无边框窗口「拖边缘改大小」：判定、换算与降级。

原生 WM_NCHITTEST 这条路实测走不通（鼠标在边缘时最上层是 WebView2 的渲染窗口，
属于浏览器进程，改不了它的窗口过程），所以判定与拖拽都搬到了页面里做。
这一组就把页面里那部分钉住。

注意：jsdom 里顶层 `let` 不挂 window，读 `resizeDrag` 必须走 `window.eval()`。
*/
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'src/asase_reader/assets/gui.html'), 'utf8');

const fail = [];
const ok = (cond, label) => {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + label);
  if (!cond) fail.push(label);
};

(async () => {
  const calls = [];
  let pendingResolve = null;
  const tick = () => new Promise((r) => setTimeout(r, 0));
  const flush = () => {
    const f = pendingResolve;
    pendingResolve = null;
    if (f) f({ ok: true });
  };
  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8765/',
    runScripts: 'dangerously',
    beforeParse(w) {
      w.pywebview = {
        api: {
          // 返回可控的 Promise：这样才能构造「上一个还没回来」的状态
          set_window_rect: (x, y, ww, hh) => {
            calls.push(['set_window_rect', x, y, ww, hh]);
            return new Promise((res) => { pendingResolve = res; });
          },
        },
      };
    },
  });
  const { window } = dom;
  const $ = (id) => window.document.getElementById(id);
  // 顶层 let 在 jsdom 里取不到，只能 eval
  const dragState = () => window.eval('typeof resizeDrag === "undefined" ? null : resizeDrag');
  const rectNow = () => window.eval('resizeRect');

  const W = window.innerWidth, H = window.innerHeight;
  const B = window.eval('RESIZE_BORDER');

  const pointer = (type, x, y, sx, sy) => {
    const e = new window.MouseEvent(type, {
      bubbles: true, cancelable: true, button: 0,
      clientX: x, clientY: y, screenX: sx === undefined ? x : sx, screenY: sy === undefined ? y : sy,
    });
    window.document.body.dispatchEvent(e);
    return e;
  };

  console.log(`--- 四边八角判定（窗口 ${W}x${H}，边距 ${B}）---`);
  ok(window.eval('edgeAt')(W / 2, H / 2) === null, '正中间不命中（不能把内部也抢成边框）');
  ok(window.eval('edgeAt')(B - 1, H / 2) === 'left', '左边命中 left');
  ok(window.eval('edgeAt')(B, H / 2) === 'left', `恰好第 ${B}px 也算边框`);
  ok(window.eval('edgeAt')(B + 1, H / 2) === null, `越过 ${B}px 就不再算`);
  ok(window.eval('edgeAt')(W - B + 1, H / 2) === 'right', '右边命中 right');
  ok(window.eval('edgeAt')(W / 2, B - 1) === 'top', '上边命中 top');
  ok(window.eval('edgeAt')(W / 2, H - B + 1) === 'bottom', '下边命中 bottom');
  ok(window.eval('edgeAt')(0, 0) === 'topleft', '左上角（同属 left 和 top，要给 topleft）');
  ok(window.eval('edgeAt')(W, 0) === 'topright', '右上角');
  ok(window.eval('edgeAt')(0, H) === 'bottomleft', '左下角');
  ok(window.eval('edgeAt')(W, H) === 'bottomright', '右下角');
  ok(window.eval('edgeAt')(B * 2 - 1, B * 2 - 1) === 'topleft', '角落命中区是纯边的两倍宽');
  ok(window.eval('edgeAt')(-1, H / 2) === null, '窗口外不命中');

  console.log('--- 光标要跟着边走 ---');
  const CUR = window.eval('RESIZE_CURSOR');
  ok(Object.keys(CUR).length === 8, `四种边 + 四个角都有光标（实际 ${Object.keys(CUR).length}）`);
  ok(CUR.left === 'ew-resize' && CUR.top === 'ns-resize', '左右是 ew-resize、上下是 ns-resize');
  ok(CUR.topleft === 'nwse-resize' && CUR.topright === 'nesw-resize',
    '两个斜对角的光标方向相反（左上-右下与右上-左下）');
  pointer('pointermove', 4, H / 2);
  ok(window.document.body.style.cursor === 'ew-resize',
    `鼠标移到左边缘，光标变成 ew-resize（实际 ${window.document.body.style.cursor}）`);
  pointer('pointermove', W / 2, H / 2);
  ok(window.document.body.style.cursor === '', '移到中间，光标还原');

  console.log('--- 拖拽换算 ---');
  const R = rectNow();
  let r = R({ edge: 'left', x: 100, y: 200, w: 800, h: 600 }, 50, 0);
  ok(r.x === 150 && r.w === 750 && r.y === 200 && r.h === 600,
    `拖左边往右 50：左边跟着走、宽度反向减（实际 ${JSON.stringify(r)}）`);
  r = R({ edge: 'right', x: 100, y: 200, w: 800, h: 600 }, 50, 0);
  ok(r.x === 100 && r.w === 850, `拖右边往右 50：位置不动、只加宽（实际 ${JSON.stringify(r)}）`);
  r = R({ edge: 'topleft', x: 100, y: 200, w: 800, h: 600 }, 20, 30);
  ok(r.x === 120 && r.y === 230 && r.w === 780 && r.h === 570,
    `拖左上角：位置和尺寸同时变（实际 ${JSON.stringify(r)}）`);
  r = R({ edge: 'bottom', x: 100, y: 200, w: 800, h: 600 }, 0, -40);
  ok(r.h === 560 && r.y === 200, '拖下边往上 40：只变高度');

  console.log('--- 按下 → 拖动 → 松开 ---');
  calls.length = 0;
  pointer('pointerdown', 4, H / 2, 500, 400);
  const st = dragState();
  ok(!!st && st.edge === 'left', `在左边缘按下就进入改大小（实际 ${st && st.edge}）`);
  pointer('pointermove', 4, H / 2, 502, 400);
  ok(calls.length === 0, '位移不到 3px 不动窗口（保护阅读器里单击图片翻页）');
  pointer('pointermove', 4, H / 2, 560, 400);
  ok(calls.length === 1, '位移够了才真的去改');
  const c = calls[calls.length - 1];
  ok(c && c[0] === 'set_window_rect', '调的是 set_window_rect');
  pointer('pointerup', 4, H / 2, 560, 400);
  ok(dragState() === null, '松开后状态清空');
  flush();
  await tick();

  console.log('--- 洪水式输入不该变成洪水式请求 ---');
  // 每次 api() 调用在 Python 侧都是新起一个线程，先进先出不保证。
  // 所以这里必须做到「任意时刻只有一个在途」，否则窗口会偶发回跳。
  calls.length = 0;
  pointer('pointerdown', 4, H / 2, 500, 400);
  pointer('pointermove', 4, H / 2, 560, 400);
  ok(calls.length === 1, `第一帧立刻发出（实际 ${calls.length}）`);
  pointer('pointermove', 4, H / 2, 580, 400);
  pointer('pointermove', 4, H / 2, 600, 400);
  ok(calls.length === 1, '上一个还没回来时，中间的帧直接丢弃（不积压、也不会乱序）');
  flush();
  await tick();
  ok(calls.length === 2, `上一个回来后立刻补发（实际 ${calls.length}）`);
  {
    const want = 600 - 500;
    const last = calls[calls.length - 1];
    ok(last[1] === want && last[3] === window.outerWidth - want,
      `补发的是最新值而不是积压的旧值（实际 x=${last[1]} w=${last[3]}，应为 ${want} / ${window.outerWidth - want}）`);
  }
  flush();
  await tick();
  pointer('pointerup', 4, H / 2, 600, 400);

  console.log('--- 不该抢的地方 ---');
  calls.length = 0;
  pointer('pointerdown', W / 2, H / 2, 500, 400);
  ok(dragState() === null, '在窗口中间按下不进入改大小');
  pointer('pointermove', W / 2, H / 2, 600, 400);
  ok(calls.length === 0, '中间拖动不产生任何 resize 调用');

  window.document.body.classList.add('fs');
  calls.length = 0;
  pointer('pointerdown', 4, H / 2, 500, 400);
  ok(dragState() === null, '全屏时不接管边缘（尺寸归系统管）');
  pointer('pointermove', 4, H / 2, 560, 400);
  ok(calls.length === 0, '全屏时拖动也不改窗口');
  window.document.body.classList.remove('fs');

  console.log('--- 与拖顶栏不打架 ---');
  pointer('pointerdown', 4, 4, 500, 400);      // 左上角：既是 resize 也是 nav 所在
  ok(window.__resizeActive === true, '改大小期间置上标志，让 mousedown 守卫把它挡住');
  {
    // pywebview 的拖窗口监听绑在 body 的 mousedown；这里必须拦住，否则一边 resize 一边 move
    let reached = 0;
    window.document.body.addEventListener('mousedown', () => { reached++; });
    const e = new window.MouseEvent('mousedown', { bubbles: true, button: 0 });
    window.document.body.dispatchEvent(e);
    ok(reached === 0, '改大小期间的 mousedown 不冒泡到 body');
  }
  pointer('pointerup', 4, 4, 500, 400);
  ok(window.__resizeActive === false, '松开后标志复位');

  console.log('');
  console.log(fail.length ? `FAILED ${fail.length}` : 'ALL PASS');
  process.exit(fail.length ? 1 : 0);
})();
