/* 首启目录引导（v2 Checkpoint 6）的专用断言。

覆盖的不是「有没有几个 DOM」这种表面结构，而是方案里真正会出事故的行为：
- 预填必须尊重已有 shelf_dir，并用后端解析后的绝对路径（M8）；
- 模态不可被 Esc / 遮罩关闭，但窗口关闭按钮必须仍然可用（M7）；
- preview_dir 只在预填与浏览两个时机调用，打字和确认都不能额外触发；
- 校验失败留在模态里，绝不提前进入书架或启动轮询；
- 成功后才一次性补跑 switchPage / poll；
- bootMask 由 finally 兜底摘除；
- B 档 <50MB 的失败要转成可感知 toast，而不是只藏在列表小字里。
*/
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');
const { ROOT, DEFAULT_CFG, DEFAULT_SHELF_DIR, makeState } = require('./helpers');

const html = fs.readFileSync(path.join(ROOT, 'src/asase_reader/assets/gui.html'), 'utf8');
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const fails = [];

function ok(cond, label) {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + label);
  if (!cond) fails.push(label);
}

function setupState(configPatch = {}, statePatch = {}) {
  const config = { ...DEFAULT_CFG, shelf_dir: '', setup_done: false, ...configPatch };
  return makeState({ config, ...statePatch });
}

function makeApi(env) {
  return new Proxy({}, {
    get(_target, name) {
      if (name === 'then') return undefined;
      if (Object.prototype.hasOwnProperty.call(env.handlers, name)) {
        return env.handlers[name];
      }
      return (...args) => {
        env.calls.push([name, ...args]);
        switch (name) {
          case 'get_state':
            return Promise.resolve(env.state);
          case 'preview_dir':
            return Promise.resolve({ ok: true, error: null, warnings: [], free_bytes: 4 * 1024 ** 3 });
          case 'update_config': {
            const patch = args[0] || {};
            const config = { ...env.state.config, ...patch };
            return Promise.resolve({
              ok: true,
              error: '',
              config,
              shelf_dir: patch.shelf_dir || env.state.shelf_dir,
              download_dir: patch.download_dir || env.state.download_dir || '',
            });
          }
          case 'choose_folder':
            return Promise.resolve({ path: '' });
          case 'choose_files':
            return Promise.resolve({ paths: [] });
          case 'library_state':
            return Promise.resolve({ works: [], chapters: [], orphans: [], pending: [] });
          case 'list_works':
            return Promise.resolve({ works: [] });
          case 'list_incomplete':
            return Promise.resolve({ items: [] });
          case 'video_tool_state':
            return Promise.resolve({ found: false, path: '', source: '', exts: 'mp4' });
          case 'is_maximized':
            return Promise.resolve({ maximized: false });
          case 'close_window':
            return Promise.resolve({ ok: true });
          default:
            return Promise.resolve({ ok: true });
        }
      };
    },
  });
}

function loadGui(state, handlers = {}) {
  const env = {
    state,
    handlers,
    calls: [],
    intervals: [],
    dom: null,
    window: null,
    $: null,
  };
  env.dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8765/',
    runScripts: 'dangerously',
    beforeParse(window) {
      const realSetInterval = window.setInterval.bind(window);
      window.setInterval = (fn, delay, ...args) => {
        if (delay === 1000) env.intervals.push(fn);
        return realSetInterval(fn, delay, ...args);
      };
      window.pywebview = { api: makeApi(env) };
    },
  });
  env.window = env.dom.window;
  env.$ = (id) => env.window.document.getElementById(id);
  return env;
}

async function boot(env) {
  env.window.dispatchEvent(new env.window.Event('pywebviewready'));
  await sleep(140);
}

async function scenario(fn) {
  const env = await fn();
  if (env && env.window) env.window.close();
}

function callsOf(env, name) {
  return env.calls.filter((call) => call[0] === name);
}

function lastCall(env, name) {
  const calls = callsOf(env, name);
  return calls.length ? calls[calls.length - 1] : null;
}

function msgVisible(el) {
  return !!el && el.style.display !== 'none' && !!el.textContent.trim();
}

(async () => {
  console.log('--- 空配置：默认预填 + 主界面延后 + 不可跳过 ---');
  await scenario(async () => {
    const state = setupState();
    const env = loadGui(state);
    await boot(env);

    ok(!env.$('bootMask'), '首启决策完成后 bootMask 被摘除');
    ok(env.$('setupModal').classList.contains('show'), 'setup_done=false 时首启模态显示');
    ok(env.$('setupShelfDir').value === DEFAULT_SHELF_DIR, '空配置预填 default_shelf_dir');
    ok(lastCall(env, 'preview_dir')
      && lastCall(env, 'preview_dir')[1] === DEFAULT_SHELF_DIR,
      '首次预检调用默认书库路径');
    ok(callsOf(env, 'preview_dir').length === 1, '预填只预检一次');
    // 初始 DOM 的 #page-shelf 已带 show，不能据此判断 boot 是否补跑主界面；
    // 真正可观测的是书库读取与 1000ms 轮询。
    ok(callsOf(env, 'library_state').length === 0, '确认前不读取书库');
    ok(callsOf(env, 'list_works').length === 0, '确认前不读取作品列表');
    ok(env.intervals.length === 0, '确认前不注册 1 秒轮询');

    env.window.dispatchEvent(new env.window.Event('pywebviewready'));
    await sleep(80);
    ok(callsOf(env, 'get_state').length === 1, '重复 pywebviewready 不重复 boot');
    ok(callsOf(env, 'preview_dir').length === 1, '重复 pywebviewready 不重复预检');

    env.window.document.dispatchEvent(new env.window.KeyboardEvent('keydown', {
      key: 'Escape', bubbles: true, cancelable: true,
    }));
    env.$('setupModal').click();
    ok(env.$('setupModal').classList.contains('show'), 'Esc 与遮罩点击都不能关闭首启模态');
    const buttons = Array.from(env.$('setupModal').querySelectorAll('button'))
      .map((button) => button.textContent);
    ok(!buttons.some((text) => /取消|以后再说/.test(text)), '首启模态没有取消入口');
    env.$('winClose').click();
    await sleep(30);
    ok(callsOf(env, 'close_window').length === 1, '窗口关闭按钮仍可调 close_window');
    ok(env.$('setupModal').classList.contains('show'), '调用关窗不是前端伪装关闭模态');
    const setupZ = Number(env.window.getComputedStyle(env.$('setupModal')).zIndex);
    const controlsZ = Number(env.window.getComputedStyle(env.$('winControls')).zIndex);
    ok(setupZ < controlsZ, `窗口控制栏层级高于首启遮罩（${setupZ} < ${controlsZ}）`);
    return env;
  });

  console.log('--- 已有相对配置：解析后绝对值预填，确认幂等进入主界面 ---');
  await scenario(async () => {
    const resolved = 'D:/Comics/CustomLibrary';
    const state = setupState({ shelf_dir: 'outputs' }, { shelf_dir: resolved });
    const env = loadGui(state);
    await boot(env);

    ok(env.$('setupShelfDir').value === resolved, '已有相对配置预填解析后的绝对路径');
    ok(lastCall(env, 'preview_dir')[1] === resolved, '预检同样使用绝对路径');
    ok(env.$('setupShelfDir').value !== DEFAULT_SHELF_DIR, '预填没有被默认值覆盖');

    env.$('btnSetupConfirm').click();
    await sleep(120);
    const update = lastCall(env, 'update_config');
    ok(!!update, '确认后调用 update_config');
    ok(update && update[1].shelf_dir === resolved, '确认提交解析后的书架路径');
    ok(update && update[1].setup_done === true, '确认提交布尔值 setup_done=true');
    ok(update && !('download_dir' in update[1]), '首启确认不夹带暂存目录');
    ok(!env.$('setupModal').classList.contains('show'), '成功后关闭首启模态');
    ok(callsOf(env, 'library_state').length === 1, '成功后读取书库');
    ok(callsOf(env, 'list_works').length === 1, '成功后读取作品列表');
    ok(env.intervals.length === 1, '成功后只注册一次 1 秒轮询');
    ok(/书库目录已确认/.test(env.$('toastText').textContent), '成功后给出明确提示');
    return env;
  });

  console.log('--- preview：仅预填与浏览调用；警告按 code 分位 ---');
  await scenario(async () => {
    const state = setupState();
    const handlers = {
      preview_dir: (dir) => {
        env.calls.push(['preview_dir', dir]);
        return Promise.resolve({
          ok: true,
          error: null,
          warnings: [
            { code: 'install_dir', message: '位于安装目录下' },
            { code: 'low_space', message: '剩余空间不足 2 GB' },
          ],
          free_bytes: 1024 ** 3,
        });
      },
    };
    const env = loadGui(state, handlers);
    await boot(env);

    ok(msgVisible(env.$('setupWarnInstallDir')), 'install_dir 警告落到安装目录警告位');
    ok(env.$('setupWarnInstallDir').textContent === '位于安装目录下', '安装目录警告文案正确');
    ok(msgVisible(env.$('setupWarnLowSpace')), 'low_space 警告落到空间警告位');
    ok(env.$('setupWarnLowSpace').textContent === '剩余空间不足 2 GB', '空间警告文案正确');
    ok(!msgVisible(env.$('setupError')), '预检成功时不显示错误');

    env.handlers.choose_folder = () => {
      env.calls.push(['choose_folder']);
      return Promise.resolve({ path: '' });
    };
    env.$('btnSetupPickShelf').click();
    await sleep(80);
    ok(callsOf(env, 'preview_dir').length === 1, '浏览取消不触发预检');
    ok(env.$('setupShelfDir').value === DEFAULT_SHELF_DIR, '浏览取消不改变预填值');

    env.handlers.choose_folder = () => {
      env.calls.push(['choose_folder']);
      return Promise.resolve({ path: 'D:/Comics' });
    };
    env.$('btnSetupPickShelf').click();
    await sleep(80);
    ok(env.$('setupShelfDir').value === 'D:/Comics', '浏览选择填回输入框');
    ok(callsOf(env, 'preview_dir').length === 2, '浏览选择后追加一次预检');
    ok(lastCall(env, 'preview_dir')[1] === 'D:/Comics', '第二次预检使用新路径');

    env.$('setupShelfDir').value = 'D:/Typed';
    env.$('setupShelfDir').dispatchEvent(new env.window.Event('input', { bubbles: true }));
    await sleep(80);
    ok(callsOf(env, 'preview_dir').length === 2, '手工输入不触发逐键预检');

    env.$('btnSetupConfirm').click();
    await sleep(100);
    ok(callsOf(env, 'preview_dir').length === 2, '确认时不再重复预检');
    ok(callsOf(env, 'update_config').length === 1, '确认时只提交 update_config');
    return env;
  });

  console.log('--- 空输入与提交失败：留在模态，不启动主界面 ---');
  await scenario(async () => {
    const state = setupState();
    const env = loadGui(state);
    let failMode = 'empty';
    env.handlers.update_config = (cfg) => {
      env.calls.push(['update_config', cfg]);
      if (failMode === 'reject') return Promise.reject(new Error('IPC 中断'));
      return Promise.resolve({
        ok: false,
        error: '目录不可写',
        config: env.state.config,
        shelf_dir: env.state.shelf_dir,
        download_dir: '',
      });
    };
    await boot(env);

    env.$('setupShelfDir').value = '';
    env.$('btnSetupConfirm').click();
    await sleep(80);
    ok(callsOf(env, 'update_config').length === 0, '空输入在前端拦下，不调用后端');
    ok(/选择一个书库目录/.test(env.$('setupError').textContent), '空输入显示页内错误');
    ok(env.$('setupModal').classList.contains('show'), '空输入后模态保持');

    env.$('setupShelfDir').value = DEFAULT_SHELF_DIR;
    failMode = 'fail';
    env.$('btnSetupConfirm').click();
    await sleep(100);
    ok(callsOf(env, 'update_config').length === 1, '非空输入会提交后端校验');
    ok(/目录不可写/.test(env.$('setupError').textContent), '后端失败原因显示在首启页内');
    ok(env.$('setupModal').classList.contains('show'), '校验失败后模态保持');
    ok(callsOf(env, 'library_state').length === 0
      && callsOf(env, 'list_works').length === 0, '校验失败不补跑主界面书库读取');
    ok(env.intervals.length === 0, '校验失败不注册轮询');
    ok(!env.$('modal').classList.contains('show'), '失败不占用通用错误弹窗');
    ok(!env.$('toast').classList.contains('show'), '失败不弹成功 toast');
    ok(!env.$('btnSetupConfirm').disabled, '失败后按钮恢复可点');

    failMode = 'reject';
    env.$('btnSetupConfirm').click();
    await sleep(100);
    ok(callsOf(env, 'update_config').length === 2, '修正后允许再次提交');
    ok(/IPC 中断/.test(env.$('setupError').textContent), 'Promise 异常同样留在首启页内');
    ok(env.$('setupModal').classList.contains('show'), 'Promise 异常后模态仍保持');
    return env;
  });

  console.log('--- bootMask 异常兜底 ---');
  await scenario(async () => {
    const state = setupState();
    const env = loadGui(state);
    env.handlers.get_state = () => {
      env.calls.push(['get_state']);
      return Promise.reject(new Error('状态读取失败'));
    };
    await boot(env);
    ok(!env.$('bootMask'), 'get_state 抛错时 bootMask 仍由 finally 摘除');
    ok(env.$('modal').classList.contains('show'), '启动失败走通用错误弹窗');
    ok(/状态读取失败/.test(env.$('modalBox').textContent), '错误内容可见');
    ok(!env.$('setupModal').classList.contains('show'), '状态没拿到时不误弹首启模态');
    return env;
  });

  console.log('--- B 档空间不足：下载状态与视频开始都给 toast ---');
  await scenario(async () => {
    const state = makeState();
    const env = loadGui(state);
    await boot(env);
    ok(!env.$('setupModal').classList.contains('show'), 'setup_done=true 的正常会话不弹首启');

    env.window.__onState(makeState({
      jobs: [{
        opus_id: '1', title: '第一篇', status: 'failed', done: 0, total: 3,
        stage: 'downloading', error: '磁盘可用空间不足 50 MB（剩余 20 MB），本章节中止',
      }],
    }));
    ok(env.$('toast').classList.contains('show'), '下载 B 档失败触发 toast');
    ok(/第一篇.*空间不足 50 MB/.test(env.$('toastText').textContent), 'toast 带章节与空间原因');

    env.window.__onState(makeState({ jobs: [] }));
    env.window.__onState(makeState({
      jobs: [{
        opus_id: '2', title: '第二篇', status: 'failed', done: 0, total: 2,
        stage: 'downloading', error: '磁盘可用空间不足 50 MB（剩余 20 MB），本章节中止',
      }],
    }));
    ok(/第二篇/.test(env.$('toastText').textContent), '任务消失后重试同类失败会再次提示');

    env.handlers.choose_files = () => {
      env.calls.push(['choose_files', 'video']);
      return Promise.resolve({ paths: ['D:/video.mp4'] });
    };
    env.handlers.video_import_start = (paths, mode) => {
      env.calls.push(['video_import_start', paths, mode]);
      return Promise.resolve({ ok: false, error: '磁盘可用空间不足 50 MB（剩余 20 MB），已取消转换' });
    };
    await env.window.doImportVideos('frames');
    ok(/已取消转换/.test(env.$('toastText').textContent), '视频开始前的 B 档失败也走 toast');
    ok(!/已取消转换/.test(env.$('modalBox').textContent), '视频 B 档失败不再被通用错误框吞掉');
    return env;
  });

  console.log(fails.length ? `\n${fails.length} 项失败` : '\n全部通过');
  process.exit(fails.length ? 1 : 0);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
