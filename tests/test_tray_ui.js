/* 托盘关闭行为（v2.2.0）的前端部分：设置项默认值 / 回填 / 写入通道。

原生托盘本身 jsdom 测不了，这里只钉死「用户能正确设置」这一层：
  - 默认不勾选（直接关闭），不能因为一次页面加载就变成进托盘；
  - 后端副本写回 localStorage 后，applyUiSettings 要如实回填开关；
  - 改动必须走 setUiPref（localStorage + 后端各一份），不能只写浏览器那份；
  - 最小化按钮仍旧是 minimize_window，没被顺手改成进托盘。
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

const saved = [];        // save_ui_settings 收到的对象
const apiCalls = [];     // 其它 api 调用
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
            if (name === 'get_state') return () => Promise.resolve(STATE);
            if (name === 'is_maximized') return () => Promise.resolve({ ok: true, maximized: false });
            if (name === 'save_ui_settings') {
              return (obj) => {
                saved.push(Object.assign({}, obj));
                return Promise.resolve({ ok: true });
              };
            }
            return (...args) => {
              apiCalls.push([name, ...args]);
              return Promise.resolve({ ok: true });
            };
          },
        }),
      };
    },
  });
  const w = dom.window;
  await new Promise((r) => setTimeout(r, 500));

  const el = w.document.getElementById('cfg_ui_close_to_tray');
  ok(!!el, '设置页存在「关闭时进入托盘」开关');
  ok(!!el && el.type === 'checkbox', '是复选框（即时生效，不进 config）');
  ok(!!el && el.checked === false, '默认不勾选 —— 拍板的默认值是「直接关闭」');

  // 启动写回：后端副本里有 closeToTray=1 时，界面要如实回填
  w.localStorage.setItem('closeToTray', '1');
  w.applyUiSettings();
  ok(el.checked === true, '后端副本写回 localStorage 后，开关回填为勾选');
  w.localStorage.removeItem('closeToTray');
  w.applyUiSettings();
  ok(el.checked === false, '没有这个键时不勾选（回默认值，不残留上次状态）');

  // 改动通道：走 setUiPref ⇒ localStorage 与后端副本都要有
  w.initUiSettings();
  saved.length = 0;
  el.checked = true;
  el.dispatchEvent(new w.Event('change'));
  await new Promise((r) => setTimeout(r, 30));
  ok(saved.some((o) => o.closeToTray === '1'),
    '勾选时 setUiPref 把 closeToTray="1" 交给后端副本');
  ok(w.localStorage.getItem('closeToTray') === '1',
    '勾选时同时也写了 localStorage（setUiPref 的两份都到位）');

  saved.length = 0;
  el.checked = false;
  el.dispatchEvent(new w.Event('change'));
  await new Promise((r) => setTimeout(r, 30));
  ok(saved.some((o) => o.closeToTray === '0'),
    '取消勾选时写回 closeToTray="0"');
  ok(w.localStorage.getItem('closeToTray') === '0',
    '取消勾选时 localStorage 也同步');

  // 最小化按钮：保持「最小化＝任务栏」，没被顺手改成进托盘
  const minBtn = w.document.getElementById('winMin');
  ok(!!minBtn, '最小化按钮仍在');
  apiCalls.length = 0;
  if (minBtn) minBtn.click();
  await new Promise((r) => setTimeout(r, 30));
  ok(apiCalls.some((c) => c[0] === 'minimize_window'),
    '点最小化仍调用 minimize_window（没有改成进托盘）');

  dom.window.close();
  if (fail.length) {
    console.log(`FAILED: ${fail.length}`);
    process.exit(1);
  }
  console.log('ALL PASS');
})().catch((e) => {
  console.log('FAILED: ' + (e && e.stack ? e.stack : e));
  process.exit(1);
});
