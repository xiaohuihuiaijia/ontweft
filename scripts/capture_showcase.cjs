// Capture the actual workbench using synthetic records and an isolated local database.
// Requires the built frontend and the optional Playwright setup in CONTRIBUTING.md.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const net = require('node:net');
const assert = require('node:assert/strict');
const { spawn, spawnSync } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'var/reports/showcase');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const data = fs.mkdtempSync(path.join(output, 'synthetic-'));
  const reservation = net.createServer();
  await new Promise(resolve => reservation.listen(0, '127.0.0.1', resolve));
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const password = crypto.randomBytes(24).toString('base64url');
  const python = process.env.ONTOLOGY_TEST_PYTHON || path.join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('ONTOLOGY_')));
  const server = spawn(python, ['scripts/model_starter_preview.py'], {
    cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...env, PYTHONUTF8: '1', ONTOLOGY_PREVIEW_DATA_DIR: data, ONTOLOGY_PREVIEW_PORT: String(port), ONTOLOGY_PREVIEW_PASSWORD: password },
  });
  const log = fs.createWriteStream(path.join(output, 'server.log'));
  server.stdout.pipe(log, { end: false }); server.stderr.pipe(log, { end: false });
  let spawnError; server.on('error', error => { spawnError = error; });
  let browser;
  const errors = [];
  try {
    let ready = false;
    for (let attempt = 0; attempt < 180; attempt++) {
      if (spawnError) throw spawnError;
      if (server.exitCode !== null) throw new Error('Preview server exited');
      try { ready = (await fetch(base + '/api/health')).ok; } catch {}
      if (ready) break;
      await pause(250);
    }
    assert.ok(ready, 'Isolated preview did not start');
    browser = await chromium.launch({ headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
    const context = await browser.newContext({ viewport: { width: 1600, height: 1200 }, deviceScaleFactor: 1, reducedMotion: 'reduce' });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    // Read drawn label coordinates to exercise the same drag gesture as a user.
    await page.addInitScript(() => {
      const rings = new WeakMap(); window.__showcasePoints = {};
      const arc = CanvasRenderingContext2D.prototype.arc;
      const text = CanvasRenderingContext2D.prototype.fillText;
      CanvasRenderingContext2D.prototype.arc = function(x, y, radius, ...rest) { rings.set(this, { x, y, radius }); return arc.call(this, x, y, radius, ...rest); };
      CanvasRenderingContext2D.prototype.fillText = function(label, ...args) {
        if (rings.has(this) && this.canvas.closest('.graph-canvas')) window.__showcasePoints[label] = { ...rings.get(this), canvas: this.canvas };
        return text.call(this, label, ...args);
      };
    });
    await page.goto(base);
    await page.getByRole('textbox', { name: /^用户名/ }).fill('owner');
    await page.getByLabel('密码', { exact: true }).fill(password);
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByRole('button', { name: '新建变更集', exact: true }).waitFor();
    const request = async (method, route, payload) => {
      const response = await context.request[method](base + '/api' + route, payload === undefined ? {} : { data: payload });
      assert.ok(response.ok(), `${method} ${route}: ${response.status()}`);
      return response.json();
    };
    const session = await request('get', '/auth/me');
    const project = session.projects[0].id;
    const property = (key, label) => ({ key, label, data_type: 'string' });
    const relation = (key, label, target_type) => ({ key, label, target_type, cardinality: 'many' });
    const type = (key, label, properties, relations) => ({ key, label, properties: [property('name', '名称'), ...properties], relations });
    let draft = await request('post', '/changesets', {
      project_id: project, title: '演示模型 · 客户与业务数据', expected_baseline_release_id: null,
      definition: { types: [
        type('department', '业务团队', [], [relation('members', '团队成员', 'person')]),
        type('person', '负责人', [], [relation('maintains', '负责系统', 'system'), relation('owns', '数据负责人', 'asset')]),
        type('system', '业务系统', [], [relation('datasets', '提供数据', 'asset')]),
        type('asset', '数据资产', [property('description', '业务定义'), property('status', '治理状态'), property('business_key', '业务键'), property('source', '来源')], [relation('depends', '依赖资产', 'asset')]),
      ], mappings: [] },
    });
    for (const action of ['validate', 'approve', 'publish']) draft = await request('post', `/changesets/${draft.id}/${action}`, { expected_revision: draft.revision });
    const model = await request('get', `/ontology/definition?project_id=${project}`);
    const labels = {
      department: ['客户运营', '数据治理', '供应链团队'],
      person: ['客户负责人', '订单负责人', '数据管理员', '供应链负责人'],
      system: ['客户管理系统', '订单管理系统', '主数据平台', '仓储管理系统'],
      asset: ['客户全景', '客户档案', '订单明细', '合同台账', '产品目录', '服务记录', '地区维表', '交付记录', '库存台账', '业务指标', '供应商档案'],
    };
    const records = {};
    for (const [type_key, names] of Object.entries(labels)) {
      records[type_key] = [];
      for (const name of names) records[type_key].push(await request('post', '/objects', {
        project_id: project, expected_model_release_id: model.release_id, type_key,
        properties: name === '客户全景' ? { name, description: '连接客户、订单与服务记录，建立统一业务视图', status: '已核对 · 演示数据', business_key: 'customer_id', source: '客户主数据 / 订单明细' } : { name },
        evidence: [],
      }));
    }
    let edgeCount = 0;
    const connect = async (kind, source, target) => {
      await request('post', '/relations', { project_id: project, expected_model_release_id: model.release_id, type_key: kind, source_id: source.id, target_id: target.id, properties: {}, evidence: [] });
      edgeCount++;
    };
    const { department: teams, person: people, system: systems, asset: assets } = records;
    for (let i = 0; i < people.length; i++) {
      await connect('members', teams[i % teams.length], people[i]);
      await connect('maintains', people[i], systems[i]);
    }
    for (let i = 0; i < assets.length; i++) await connect('datasets', systems[i % systems.length], assets[i]);
    for (let i = 1; i <= 7; i++) await connect('depends', assets[0], assets[i]);
    await connect('owns', people[2], assets[0]);
    await connect('depends', assets[9], assets[0]);
    await connect('depends', assets[7], assets[8]);
    await connect('depends', assets[8], assets[10]);
    const count = Object.values(records).flat().length;
    await page.reload();
    await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('button', { name: '图谱探索', exact: true }).click();
    await page.waitForFunction(expected => !document.querySelector('[aria-label="放大图谱"]')?.disabled && document.querySelector('.graph-caption')?.textContent.includes(`${expected} 个节点`), count);
    const settle = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.getByRole('button', { name: '适配可见图', exact: true }).click();
    await settle();
    // Arrange this personal view with real drags. Business records/edges remain unchanged.
    const layout = {
      department: [[.08, .18], [.08, .5], [.08, .82]],
      person: [[.23, .12], [.23, .36], [.23, .62], [.23, .88]],
      system: [[.4, .2], [.4, .4], [.4, .65], [.4, .85]],
      asset: [[.62, .46], [.68, .12], [.84, .24], [.88, .44], [.84, .66], [.7, .79], [.6, .9], [.54, .12], [.9, .88], [.59, .67], [.94, .08]],
    };
    const nodeList = page.locator('details.node-list').first();
    for (const [kind, names] of Object.entries(labels)) for (let i = 0; i < names.length; i++) {
      const name = names[i];
      await page.evaluate(label => { delete window.__showcasePoints[label]; }, name);
      await nodeList.getByRole('button', { name, exact: true }).click();
      await page.evaluate(() => window.scrollTo(0, 0));
      await settle();
      await page.waitForFunction(label => !!window.__showcasePoints[label], name);
      const point = await page.evaluate(label => {
        const point = window.__showcasePoints[label]; const box = point.canvas.getBoundingClientRect();
        return { x: box.x + point.x, y: box.y + point.y };
      }, name);
      const box = await page.locator('.graph-canvas').boundingBox();
      await page.mouse.move(point.x, point.y); await page.mouse.down();
      await page.mouse.move(box.x + box.width * layout[kind][i][0], box.y + box.height * layout[kind][i][1], { steps: 8 });
      await page.mouse.up(); await settle();
    }
    await page.getByRole('button', { name: '保存个人视图', exact: true }).click();
    await page.getByRole('button', { name: '取消选择', exact: true }).click();
    await settle();
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: path.join(output, 'graph-overview.png') });
    await page.locator('details.node-list').first().getByRole('button', { name: '客户全景', exact: true }).click();
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.mouse.move(10, 10);
    await settle();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.locator('.graph-inspector').getByRole('heading', { name: '客户全景', exact: true }).waitFor();
    assert.deepEqual(errors, []);
    await page.screenshot({ path: path.join(output, 'graph-workbench.png') });
    // Check the advertised edit entry opens the real form; do not stage a fake modal.
    await page.getByRole('button', { name: '打开对象表单', exact: true }).click();
    await page.getByRole('complementary', { name: '对象详情', exact: true }).getByLabel('名称', { exact: true }).waitFor();
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'capture.json'), JSON.stringify({ source: 'Actual built frontend and authenticated local API', synthetic: true, objects: count, relations: edgeCount, viewport: { width: 1600, height: 1200 }, editEntryOpened: true, pageErrors: errors }, null, 2) + '\n');
    console.log(`Captured ${count} synthetic objects / ${edgeCount} relationships to var/reports/showcase; no UI or response substitution.`);
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null && server.pid) {
      if (process.platform === 'win32') spawnSync('taskkill', ['/PID', String(server.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      else server.kill();
      await Promise.race([new Promise(resolve => server.once('exit', resolve)), pause(5000)]);
    }
    log.end();
    // Keep this run's isolated synthetic database with its ignored report for reproducibility.
  }
}
main().catch(error => { console.error(String(error)); process.exitCode = 1; });
