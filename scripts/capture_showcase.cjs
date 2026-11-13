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
  let browser, worker;
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
    worker = spawn(python, ['scripts/start_local_demo.py', 'worker', '--data-dir', data, '--port', String(port)], { cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], env: { ...env, PYTHONUTF8: '1' } });
    worker.stdout.pipe(log, { end: false }); worker.stderr.pipe(log, { end: false });
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
    const property = (key, label, data_type = 'string', unit) => ({ key, label, data_type, ...(unit ? { unit } : {}) });
    const relation = (key, label, target_type, properties = []) => ({ key, label, target_type, cardinality: 'many', properties });
    const type = (key, label, properties, relations) => ({ key, label, properties: [property('name', '名称'), ...properties], relations });
    let draft = await request('post', '/changesets', {
      project_id: project, title: '合成产品 · 库存与选型场景', expected_baseline_release_id: null,
      definition: { types: [
        type('series', '产品系列', [], [relation('contains', '包含型号', 'product')]),
        type('product', '产品型号', [property('code', '型号'), property('pins', '芯数', 'integer')], [relation('suits', '适用场景', 'scenario', [property('condition', '适用条件'), property('voltage', '电压上限', 'number', 'V'), property('verification', '确认方式')])]),
        type('stock', '库存批次', [property('quantity', '可用库存', 'integer', '件')], [relation('holds', '库存产品', 'product')]),
        type('scenario', '业务场景', [property('description', '业务要求')], []),
      ], mappings: [] },
    });
    for (const action of ['validate', 'approve', 'publish']) draft = await request('post', `/changesets/${draft.id}/${action}`, { expected_revision: draft.revision });
    const model = await request('get', `/ontology/definition?project_id=${project}`);
    const documentResponse = await context.request.post(base + '/api/documents/upload', { multipart: { project_id: project, file: { name: 'synthetic-selection.md', mimeType: 'text/markdown', buffer: Buffer.from('# 合成选型资料\n\nSYN-04 用于室内传感采集，电压不超过 24 V。此处全部为功能演示数据，不构成真实产品建议。\n') } } });
    assert.equal(documentResponse.status(), 201, await documentResponse.text());
    let document = (await documentResponse.json()).document;
    for (let i = 0; i < 120; i++) { document = await request('get', `/documents/${document.id}`); if (document.state === 'ready' || document.state === 'failed') break; await pause(250); }
    assert.equal(document.state, 'ready', JSON.stringify(document.error));
    const evidence = [{ document_id: document.id, revision: document.revision, parse_id: document.current_parse_id, parse_revision: document.parse_revision, ir_fingerprint: document.ir_fingerprint, content_hash: document.content_hash }];
    const labels = {
      series: ['传感连接系列', '控制连接系列', '电源连接系列'],
      product: ['SYN-04 · 4 芯', 'SYN-05 · 5 芯', 'SYN-08 · 8 芯', 'SYN-C4 · 控制', 'SYN-C8 · 控制', 'SYN-P3 · 电源'],
      stock: ['华东仓 · A01', '华南仓 · B02', '华北仓 · C03', '控制库存 · D04', '备件库存 · E05', '电源库存 · F06'],
      scenario: ['室内传感采集', '设备状态监测', '控制柜布线', '现场信号传输', '低压电源接入'],
    };
    const records = {};
    for (const [type_key, names] of Object.entries(labels)) {
      records[type_key] = [];
      for (let i = 0; i < names.length; i++) records[type_key].push(await request('post', '/objects', {
        project_id: project, expected_model_release_id: model.release_id, type_key,
        properties: { name: names[i], ...(type_key === 'product' ? { code: names[i].split(' · ')[0], pins: [4, 5, 8, 4, 8, 3][i] } : type_key === 'stock' ? { quantity: (i + 1) * 40 } : {}) }, evidence: type_key === 'product' && i === 0 ? evidence : [],
      }));
    }
    let edgeCount = 0;
    const connect = async (kind, source, target, properties = {}, refs = []) => {
      const edge = await request('post', '/relations', { project_id: project, expected_model_release_id: model.release_id, type_key: kind, source_id: source.id, target_id: target.id, properties, evidence: refs });
      edgeCount++; return edge;
    };
    const { series, product: products, stock, scenario: scenarios } = records;
    for (let i = 0; i < products.length; i++) {
      await connect('contains', series[Math.min(2, Math.floor(i / 2))], products[i]);
      await connect('holds', stock[i], products[i]);
      await connect('suits', products[i], scenarios[i % scenarios.length]);
      await connect('suits', products[i], scenarios[(i + 1) % scenarios.length]);
    }
    const allRelations = await request('get', `/relations?project_id=${project}&object_id=${products[0].id}`);
    const featured = allRelations.items.find(edge => edge.type_key === 'suits' && edge.target_id === scenarios[0].id);
    await request('patch', `/relations/${featured.id}`, { expected_revision: featured.revision, expected_model_release_id: model.release_id, properties: { condition: '室内干燥环境 · 合成演示', voltage: 24, verification: '已核对合成选型资料' }, evidence, reason: '为合成展示确认关系条件' });
    const count = Object.values(records).flat().length;
    await page.reload();
    await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('button', { name: '图谱探索', exact: true }).click();
    await page.waitForFunction(expected => !document.querySelector('[aria-label="放大图谱"]')?.disabled && document.querySelector('.graph-caption')?.textContent.includes(`${expected} 个节点`), count);
    const settle = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.getByRole('button', { name: '适配可见图', exact: true }).click();
    await settle();
    // Arrange this personal view with real drags. Business records/edges remain unchanged.
    const layout = {
      series: [[.09, .2], [.09, .5], [.09, .8]],
      product: [[.35, .12], [.35, .27], [.35, .43], [.35, .58], [.35, .74], [.35, .9]],
      stock: [[.58, .12], [.58, .27], [.58, .43], [.58, .58], [.58, .74], [.58, .9]],
      scenario: [[.87, .16], [.87, .34], [.87, .51], [.87, .69], [.87, .87]],
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
    const relationList = page.locator('details.node-list').last();
    await relationList.locator('summary').click();
    await relationList.getByRole('button', { name: 'SYN-04 · 4 芯 — 适用场景 → 室内传感采集', exact: true }).click();
    await page.getByRole('region', { name: '关系条件与证据' }).getByText('已核对合成选型资料', { exact: true }).waitFor();
    await page.evaluate(() => window.scrollTo(0, 0)); await page.mouse.move(10, 10); await settle();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
    await page.screenshot({ path: path.join(output, 'graph-workbench.png') });
    const detailShot = await page.locator('.graph-workspace').screenshot();
    const share = await context.newPage();
    await share.setViewportSize({ width: 1280, height: 640 });
    await share.setContent(`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>
      *{box-sizing:border-box}body{margin:0;width:1280px;height:640px;background:#18382f;color:#f7f8f4;font-family:"Segoe UI","Microsoft YaHei",sans-serif;display:grid;grid-template-columns:300px 1fr;gap:32px;padding:40px;align-items:center}
      .brand{font-size:24px;font-weight:600;letter-spacing:.04em;color:#c5e4d3;margin-bottom:44px}h1{font-size:54px;line-height:1.35;font-weight:600;margin:0 0 24px}p{font-size:18px;line-height:1.8;color:#c5e4d3;margin:0}small{display:block;font-size:14px;line-height:1.7;margin-top:40px;color:#c5e4d3}.preview{background:#f2f5f4;border:1px solid #698c7b;border-radius:12px;overflow:hidden;box-shadow:0 20px 70px #051a1470}.preview img{display:block;width:100%;height:auto}.caption{padding:12px 16px;color:#18382f;font-size:14px;line-height:1.5}
      </style><body><div><div class="brand">序枢 / ONTWEFT</div><h1>让知识<br>连接业务。</h1><p>企业本体 · 知识图谱<br>条件可编辑 · 证据可回查</p><small>Self-hosted · Apache-2.0<br>github.com/xiaohuihuiaijia/ontweft</small></div><div class="preview"><img alt="真实应用中的合成产品图谱" src="data:image/png;base64,${detailShot.toString('base64')}"><div class="caption">真实工作台 · 合成演示数据 · Enterprise Beta</div></div></body></html>`);
    await share.screenshot({ path: path.join(output, 'social-preview.png') });
    await share.close();
    assert.ok(fs.statSync(path.join(output, 'social-preview.png')).size < 1000000, 'Social preview exceeds 1 MB');

    // Exercise the real partial relation PATCH, then reopen its authorized source.
    await page.getByRole('button', { name: '编辑条件与证据', exact: true }).click();
    await page.getByLabel('适用条件', { exact: true }).fill('室内干燥环境 · 已人工复核');
    await page.getByRole('button', { name: '保存关系条件', exact: true }).click();
    await page.getByText('室内干燥环境 · 已人工复核', { exact: true }).waitFor();
    await page.getByRole('button', { name: '查看固定来源版本', exact: true }).click();
    await page.getByRole('complementary', { name: '固定历史解析版本', exact: true }).waitFor();
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'capture.json'), JSON.stringify({ source: 'Actual built frontend and authenticated local API', synthetic: true, objects: count, relations: edgeCount, viewport: { width: 1600, height: 1200 }, relationEditSaved: true, fixedSourceOpened: true, sourceCommit: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(), workingTree: 'capture includes current branch changes', pageErrors: errors }, null, 2) + '\n');
    console.log(`Captured ${count} synthetic objects / ${edgeCount} relationships to var/reports/showcase; no UI or response substitution.`);
  } finally {
    if (browser) await browser.close();
    for (const child of [worker, server].filter(Boolean)) if (child.exitCode === null && child.pid) {
      if (process.platform === 'win32') spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      else child.kill();
      await Promise.race([new Promise(resolve => child.once('exit', resolve)), pause(5000)]);
    }
    log.end();
    // Keep this run's isolated synthetic database with its ignored report for reproducibility.
  }
}
main().catch(error => { console.error(String(error)); process.exitCode = 1; });
