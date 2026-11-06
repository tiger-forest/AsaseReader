/* 原生 DWM 过渡（v1.28.0，替换掉 v1.23~v1.27 的手工补间）：前端能自动验的那部分。

窗口动画整体交给系统 DWM 后，前端只剩三件事：
  - 把动画开关（uiWinAnim）原样传给 maximize_window；
  - 拿后端返回的 maximized 校正自己的状态并翻图标（后端读真实 WindowState，
    同步返回时已落定 —— 连点 / Win+↑ 都不会再判错方向，前端不再抢跑判定）；
  - 启动时用 is_maximized 把图标摆到正确的档位上。

另外钉死：旧补间那一整套（锁布局 / transform 缩放 / 收帧推送 / win-anim
禁交互）不许回潮 —— 那条路永远做不到窗口几何与画面同帧原子呈现。
*/
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');
const { makeState } = require('./helpers');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'src/asase_reader/assets/gui.html'), 'utf8');

const fail = [];
const ok = (cond, label) => {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + label);
  if (!cond) fail.push(label);
};

let apiCalls = [];        // [method, ...args]
let nextMaxResult = null; // 下一次 maximize_window 的返回值

// boot 流程会真实消费 get_state 的返回值（fillDlSettings / fillDirSettings /
// fillVideoSettings 都读 config），给空对象会在 boot 中途抛异常、
// is_maximized 永远执行不到 —— 桩必须和 test_boot_race.js 一样完整。
const CFG = {
  out: 'outputs', delay_min: 2, delay_max: 5, image_delay_min: 0.5, image_delay_max: 1.5,
  concurrency: 2, max_per_day: 200, include_cover: true,
  setup_done: true,
};
const STATE = makeState({
  jobs: [], config: CFG, out_dir: 'D:/AsaseReaderLibrary',
  daily_used: 0, running: false, paused: false, pending: 0,
});

(async () => {
  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8765/',
    runScripts: 'dangerously',
    beforeParse(win) {
      win.pywebview = {
        api: new Proxy({}, {
          get: (_t, name) => {
            if (name === 'get_state') {
              return () => Promise.resolve(STATE);
            }
            if (name === 'maximize_window') {
              return (anim) => {
                apiCalls.push(['maximize_window', anim]);
                return Promise.resolve(nextMaxResult || { ok: true, maximized: true });
              };
            }
            if (name === 'is_maximized') {
              return () => Promise.resolve({ ok: true, maximized: true });
            }
            return () => Promise.resolve({ ok: true });
          },
        }),
      };
    },
  });
  const w = dom.window;
  await new Promise((r) => setTimeout(r, 500));

  const doc = w.document;
  const iconMax = () => doc.getElementById('winMaxIcon');
  const iconRestore = () => doc.getElementById('winRestoreIcon');
  const maxBtn = () => doc.getElementById('winMax');
  /* 图标档位：还原态 = 最大化图标隐藏 / 还原图标显示（用户可点「再最大化」）。 */
  const showing = () => {
    const a = iconMax(), b = iconRestore();
    if (!a || !b) return 'missing';
    if (a.style.display === 'none' && b.style.display === 'block') return 'restore';
    if (a.style.display === 'block' && b.style.display === 'none') return 'max';
    return 'unset';
  };

  console.log('--- 旧补间整套不许回潮 ---');
  ok(w.eval('typeof winAnimScale') === 'undefined', 'winAnimScale 不存在（不锁布局不缩放）');
  ok(w.eval('typeof endWinAnimScale') === 'undefined', 'endWinAnimScale 不存在');
  ok(w.eval('typeof winAnimFrameGeom') === 'undefined', 'winAnimFrameGeom 不存在（没有帧推送了）');
  ok(!/body\.win-anim/.test(html), 'CSS 里没有 body.win-anim（不禁交互）');
  ok(!/transform:\s*scale\(/.test(html.replace(/-webkit-transform/g, '')) ||
     !/body\.style\.transform\s*=\s*"scale\(/.test(html),
     '没有给 body 设 scale() 的代码');
  ok(w.eval('typeof maximizeWindow') === 'function', 'maximizeWindow 仍在（入口不变）');
  ok(w.eval('typeof uiWinAnim') === 'function', 'uiWinAnim 仍在（设置开关不变）');

  console.log('--- 启动时用后端权威状态摆图标 ---');
  // 桩的 is_maximized 返回 maximized:true —— 前端自己没记任何状态，
  // 图标必须是还原态，否则说明前端在瞎猜。
  await new Promise((r) => setTimeout(r, 100));
  ok(showing() === 'restore',
    `is_maximized=true → 图标是还原态（实际 ${showing()}）`);
  ok(w.eval('winMax') === true, 'winMax 跟随后端权威值');

  console.log('--- maximizeWindow：开关传后端 / 返回值校正 / 图标翻转 ---');
  apiCalls = [];
  nextMaxResult = { ok: true, maximized: false };      // 后端说：这次点了是还原
  w.eval('maximizeWindow')();
  await new Promise((r) => setTimeout(r, 50));
  ok(apiCalls.length === 1 && apiCalls[0][0] === 'maximize_window',
    `调的是 maximize_window（实际 ${JSON.stringify(apiCalls)}）`);
  ok(apiCalls[0][1] === true,
    `动画开关默认开 → 传 true（实际 ${JSON.stringify(apiCalls[0][1])}）`);
  ok(showing() === 'max',
    `后端返回 maximized=false → 图标翻成最大化态（实际 ${showing()}）`);
  ok(w.eval('winMax') === false, 'winMax 校正为 false');

  apiCalls = [];
  nextMaxResult = { ok: true, maximized: true };       // 再点：后端说这次是最大化
  w.eval('maximizeWindow')();
  await new Promise((r) => setTimeout(r, 50));
  ok(showing() === 'restore',
    `后端返回 maximized=true → 图标翻成还原态（实际 ${showing()}）`);
  ok(maxBtn() && maxBtn().title === '向下还原',
    `按钮 title 跟着变（实际 "${maxBtn() && maxBtn().title}"）`);

  console.log('--- 设置关掉动画 → 传 false（后端那一次禁 DWM 过渡）---');
  w.localStorage.setItem('uiWinAnim', '0');
  ok(w.eval('uiWinAnim')() === false, 'localStorage uiWinAnim=0 → uiWinAnim() 为假');
  apiCalls = [];
  w.eval('maximizeWindow')();
  await new Promise((r) => setTimeout(r, 50));
  ok(apiCalls.length === 1 && apiCalls[0][1] === false,
    `关掉时传 false（实际 ${JSON.stringify(apiCalls)}）—— 后端据此临时禁过渡一步到位`);
  w.localStorage.removeItem('uiWinAnim');
  ok(w.eval('uiWinAnim')() === true, '默认仍是开启');

  console.log('--- 后端拿不到窗口（ok:false）时不崩、图标不乱翻 ---');
  apiCalls = [];
  nextMaxResult = { ok: false, reason: 'no-window' };
  const before = showing();
  w.eval('maximizeWindow')();
  await new Promise((r) => setTimeout(r, 50));
  ok(showing() === before,
    `ok:false 时图标维持原档（${before}，实际 ${showing()}）—— 拿不到权威值就别乱动`);

  console.log();
  if (fail.length) {
    console.log(`FAILED ${fail.length} 条：`);
    fail.forEach((f) => console.log('  - ' + f));
    process.exit(1);
  }
  console.log('ALL PASS');
  // boot 里的 setInterval(poll) 会挂住事件循环，必须显式退出
  process.exit(0);
})();
