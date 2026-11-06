/* 充电专属 UI：折叠区、Cookie 输入、UP 主列表、缺登录态的任务行按钮。
   最关键的一条：保存成功后输入框里的明文必须立刻清空，不能留在页面上。 */
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
  paid_ups: [{ mid: 12345678, name: '测试作者' }],
  setup_done: true,
};
const STATE = makeState({
  jobs: [
    { opus_id: '1000000000000000001', status: 'failed', title: '测试付费专栏',
      done: 0, total: 27, error: '这是充电专属内容：只拿到预览 27 张', dir: '',
      stage: '', author_mid: 12345678, need_cookie: true },
  ],
  config: CFG, shelf_dir: CFG.shelf_dir, download_dir: '',
  daily_used: 0, running: false, paused: false, pending: 0,
  cookie: { configured: false, fields: 0, has_sessdata: false },
});

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
  const $ = (id) => window.document.getElementById(id);
  const txt = (id) => ($(id) ? $(id).textContent : '');

  const calls = [];
  const stub = {
    get_state: () => Promise.resolve(STATE),
    library_state: () => Promise.resolve({ works: [], chapters: [], orphans: [], pending: [] }),
    list_works: () => Promise.resolve({ works: [] }),
    list_incomplete: () => Promise.resolve({ items: [] }),
    open_dir: () => Promise.resolve({ ok: true }),
    // 真实后端保存/清除后会 _notify_state 推新状态，这里同样推一次
    submit_cookie: (v) => {
      calls.push(['submit_cookie', v]);
      STATE.cookie = { configured: true, fields: 3, has_sessdata: true };
      window.__onState(STATE);
      return Promise.resolve({ ok: true, retried: 1, cookie: STATE.cookie });
    },
    clear_cookie: () => {
      calls.push(['clear_cookie']);
      STATE.cookie = { configured: false, fields: 0, has_sessdata: false };
      window.__onState(STATE);
      return Promise.resolve({ ok: true, cookie: STATE.cookie });
    },
    add_paid_up: (mid, name) => {
      calls.push(['add_paid_up', mid, name]);
      return Promise.resolve({ ok: true, paid_ups: CFG.paid_ups });
    },
    remove_paid_up: (mid) => {
      calls.push(['remove_paid_up', mid]);
      return Promise.resolve({ ok: true, paid_ups: [] });
    },
  };
  Object.assign(window.pywebview.api, stub);

  window.dispatchEvent(new window.Event('pywebviewready'));
  await sleep(250);

  console.log('--- 充电专属分区 ---');
  ok(!!$('chargeCard'), '下载页有「充电专属」卡片');
  ok(/充电专属/.test(txt('chargeHead')), '标题含「充电专属」');
  ok($('chargeBody').style.display === 'none', '默认是折叠的');
  ok(/未配置/.test(txt('cookieStatus')), '未配置时显示「未配置」');
  ok($('btnClearCookie').disabled === true, '未配置时「清除」按钮禁用');

  $('chargeHead').click();
  await sleep(100);
  ok($('chargeBody').style.display === 'block', '点标题能展开');

  console.log('--- UP 主列表 ---');
  ok(/测试作者/.test(txt('paidUps')), '列出了已记住的 UP 主名字');
  ok(/12345678/.test(txt('paidUps')), '列出了 UP 主 ID');
  const rmBtn = [...window.document.querySelectorAll('#paidUps button')]
    .find((b) => b.textContent === '移除');
  ok(!!rmBtn, '每个 UP 有「移除」按钮');
  if (rmBtn) {
    rmBtn.click();
    await sleep(120);
    ok(calls.some((c) => c[0] === 'remove_paid_up' && c[1] === '12345678'),
      '点「移除」会调 remove_paid_up');
  }

  console.log('--- Cookie 输入 ---');
  ok($('cookieInputWrap').style.display === 'none', '输入框默认是收起的');
  $('btnSetCookie').click();
  await sleep(100);
  ok($('cookieInputWrap').style.display === 'block', '点「输入 Cookie…」后展开');

  const SECRET = 'SESSDATA=ui_test_secret; bili_jct=x; DedeUserID=1';
  $('cookieInput').value = SECRET;
  $('btnSubmitCookie').click();
  await sleep(200);
  const sent = calls.find((c) => c[0] === 'submit_cookie');
  ok(!!sent && sent[1] === SECRET, '把粘贴的内容传给了后端');
  ok($('cookieInput').value === '', '★ 保存后输入框里的明文被立刻清空');
  ok($('cookieInputWrap').style.display === 'none', '输入框重新收起');
  ok(/已配置/.test(txt('cookieStatus')), '状态变成「已配置」');
  ok($('btnClearCookie').disabled === false, '「清除」按钮现在可用');

  console.log('--- 页面上不该残留凭据 ---');
  const pageHtml = window.document.body.innerHTML;
  ok(!pageHtml.includes('ui_test_secret'), '页面 DOM 里搜不到 Cookie 明文');
  ok(!window.localStorage.getItem('cookie'), 'localStorage 里没有存 Cookie');

  console.log('--- 缺登录态的任务行 ---');
  const jobOps = [...window.document.querySelectorAll('#jobs .ops')]
    .map((o) => o.textContent).join('|');
  ok(/输入 Cookie/.test(jobOps), 'need_cookie 的任务行有「输入 Cookie」按钮');

  console.log('--- 清除 ---');
  $('btnClearCookie').click();
  await sleep(150);
  ok(calls.some((c) => c[0] === 'clear_cookie'), '点「清除」会调 clear_cookie');
  ok(/未配置/.test(txt('cookieStatus')), '清除后状态回到「未配置」');

  dom.window.close();
  console.log(`\nFAILED: ${fail.length}`);
  fail.forEach((f) => console.log('  - ' + f));
  process.exit(fail.length ? 1 : 0);
})();
