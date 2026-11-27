/* WebDAV 设置与首启恢复前端交互测试（JSDOM）。 */
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

const calls = [];
const CFG = {
  shelf_dir: 'D:/Comics', download_dir: '', import_mode: 'move',
  delay_min: 2, delay_max: 5, image_delay_min: 0.5, image_delay_max: 1.5,
  concurrency: 2, max_per_day: 200, include_cover: true,
  setup_done: true,
  webdav_url: 'https://example.com/dav/',
  webdav_username: 'u1',
  webdav_password: 'p1',
  webdav_last_backup: {
    filename: 'asase_backup_20261125_210000.zip',
    created_at: '2026-11-25T21:00:00+08:00',
    size: 2048,
  },
};
const STATE = makeState({
  jobs: [], config: CFG, shelf_dir: 'D:/Comics', default_shelf_dir: 'D:/Comics',
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
            if (name === 'update_config') {
              return (obj) => {
                calls.push({ name, obj });
                return Promise.resolve({ ok: true, config: Object.assign({}, CFG, obj) });
              };
            }
            if (name === 'webdav_test') {
              return (u, user, pwd) => {
                calls.push({ name, u, user, pwd });
                return Promise.resolve({
                  ok: true,
                  latest: { filename: 'asase_backup_20261125_210500.zip', size: 4096 },
                });
              };
            }
            if (name === 'webdav_backup_start') {
              return () => { calls.push({ name }); return Promise.resolve({ ok: true }); };
            }
            if (name === 'webdav_status') {
              return () => Promise.resolve({
                running: false, finished: true, cancelled: false,
                mode: 'backup', phase: 'uploading', error: '',
                result: { filename: 'asase_backup_20261125_210600.zip', size: 8192 },
              });
            }
            return () => Promise.resolve({ ok: true, works: [], chapters: [], orphans: [], pending: [], items: [] });
          },
        }),
      };
    },
  });
  const win = dom.window;
  const doc = win.document;
  win.dispatchEvent(new win.Event('pywebviewready'));
  await new Promise((r) => setTimeout(r, 150));

  ok(doc.getElementById('cfg_webdav_url').value === CFG.webdav_url, '设置页回填 WebDAV URL');
  ok(doc.getElementById('cfg_webdav_username').value === 'u1', '设置页回填用户名');
  ok(doc.getElementById('cfg_webdav_password').value === 'p1', '设置页回填密码');
  ok(doc.getElementById('webdavLastInfo').textContent.includes('20261125_210000'), '显示上次备份信息');

  doc.getElementById('btnTestWebdav').click();
  await new Promise((r) => setTimeout(r, 50));
  const testCall = calls.find((c) => c.name === 'webdav_test');
  ok(Boolean(testCall && testCall.u === CFG.webdav_url), '点击测试连接调用 webdav_test');
  ok(doc.getElementById('webdavTestMsg').textContent.includes('连接成功'), '测试连接显示成功文案');

  doc.getElementById('btnWebdavBackup').click();
  await new Promise((r) => setTimeout(r, 80));
  ok(calls.some((c) => c.name === 'webdav_backup_start'), '点击立即备份调用 webdav_backup_start');
  ok(doc.getElementById('m_wd_prog').textContent.includes('备份完成'), '备份完成后弹窗显示完成状态');

  ok(Boolean(doc.getElementById('btnSetupWebdavRestore')), '首启引导页存在「从 WebDAV 恢复书库」按钮');

  console.log(`\n${fail.length ? 'FAIL' : 'PASS'} (${fail.length} failures)`);
  process.exit(fail.length ? 1 : 0);
})();
