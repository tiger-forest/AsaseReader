/* 测试通用辅助：ROOT 常量与统一的 get_state 载荷构造器（makeState）。
所有 JS 测试的 get_state 桩都应经 makeState 构造，保证两点：
1) 载荷贴近生产——setup_done / default_shelf_dir / cookie / ui_settings
   从第一天就在（方案 v2 §5.5：测试数据应贴近生产载荷，别等首批用例
   写完再回头补字段）；
2) get_state 以后新增顶层字段时只改这一处。
注意：setup_done 默认 true = 存量测试模拟「已确认」的常态会话；
首启模态自身的用例（Checkpoint 7）才显式传 setup_done: false。 */
const path = require('path');

const ROOT = path.resolve(__dirname, '..');

// 生产 config 的 JS 端镜像（对应 backend.DEFAULT_CONFIG）。
// shelf_dir 默认指本仓库 outputs，与既有测试桩一致；测试只拿它当显示值，
// 不会真的往里写东西（真实写入断言在 Python 测试里，全部指向 tmp）。
const DEFAULT_CFG = {
  shelf_dir: path.join(ROOT, 'outputs').replace(/\\/g, '/'),
  download_dir: '',
  import_mode: 'move',
  delay_min: 2, delay_max: 5,
  image_delay_min: 0.5, image_delay_max: 1.5,
  concurrency: 2, max_per_day: 200, include_cover: true,
  setup_done: true,
  export_dir: '', export_naming: 'numbered',
  paid_ups: [], ffmpeg_path: '', video_threshold: 0.02,
};

// get_state 新增字段：首启模态预填用的「文档」默认书库（纯显示桩值，
// 测试绝不触碰真实文档目录，也不含任何真实用户路径）。
// 公开快照白名单按此规则放行：这里只放中性的占位路径。
const DEFAULT_SHELF_DIR = 'D:/AsaseReaderLibrary';

function makeState(overrides = {}) {
  const config = { ...DEFAULT_CFG, ...(overrides.config || {}) };
  return {
    jobs: [],
    config,
    shelf_dir: config.shelf_dir,
    download_dir: config.download_dir || '',
    default_shelf_dir: DEFAULT_SHELF_DIR,
    daily_used: 0,
    running: false,
    paused: false,
    pending: 0,
    // credentials.summary() 的空态形态：只有统计，永不含凭据
    cookie: { configured: false, fields: 0, has_sessdata: false },
    ui_settings: {},
    ...overrides,
    config,
  };
}

module.exports = { ROOT, DEFAULT_CFG, DEFAULT_SHELF_DIR, makeState };
