// Real browser, actual API, synthetic local data. Never reads an application .env.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const net = require('node:net');
const { spawn } = require('node:child_process');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'var/reports/graph-workbench');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'ontweft-workbench-'));
  const reservation = net.createServer();
  await new Promise(resolve => reservation.listen(0, '127.0.0.1', resolve));
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const password = crypto.randomBytes(24).toString('base64url');
  const python = process.env.ONTOLOGY_TEST_PYTHON || path.join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('ONTOLOGY_')));
  const server = spawn(python, ['scripts/model_starter_preview.py'], { cwd: root, windowsHide: true, env: {
    ...env, PYTHONUTF8: '1', ONTOLOGY_PREVIEW_DATA_DIR: temp, ONTOLOGY_PREVIEW_PORT: String(port), ONTOLOGY_PREVIEW_PASSWORD: password,
  }, stdio: ['ignore', 'pipe', 'pipe'] });
  const log = fs.createWriteStream(path.join(output, 'server.log'));
  server.stdout.pipe(log, { end: false }); server.stderr.pipe(log, { end: false });
  let spawnError; server.on('error', error => { spawnError = error; });
  let browser; let page;
  const result = { passed: false, scope: 'Synthetic data, isolated SQLite, real API and Chromium; not a production-scale acceptance', pageErrors: [], screenshots: [] };
  try {
    let ready = false;
    for (let i = 0; i < 180; i++) {
      if (spawnError) throw spawnError;
      if (server.exitCode !== null) throw new Error('Preview server exited');
      try { ready = (await fetch(base + '/api/health')).ok; } catch {}
      if (ready) break; await pause(250);
    }
    assert.ok(ready, 'Preview did not become ready');
    browser = await chromium.launch({ headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce', hasTouch: true });
    page = await context.newPage();
    page.on('pageerror', error => result.pageErrors.push({ message: error.message, stack: error.stack, stage: result.stage }));
    // Observe the actual drawn selection ring to locate the node in screen pixels.
    // No production globals, mock API responses, or synthetic click callbacks.
    await page.addInitScript(() => {
      const rings = new WeakMap(); window.__drawnGraphNodes = {}; window.__graphErrors = [];
      window.addEventListener('error', event => window.__graphErrors.push({ message: event.message, width: innerWidth, canvases: [...document.querySelectorAll('.graph-canvas')].map(el => ({ connected: el.isConnected, width: el.clientWidth, height: el.clientHeight })) }));
      const arc = CanvasRenderingContext2D.prototype.arc;
      const text = CanvasRenderingContext2D.prototype.fillText;
      CanvasRenderingContext2D.prototype.arc = function(x, y, radius, ...rest) { rings.set(this, { x, y, radius }); return arc.call(this, x, y, radius, ...rest); };
      CanvasRenderingContext2D.prototype.fillText = function(label, ...args) {
        const ring = rings.get(this);
        if (ring && this.canvas.closest('.graph-canvas')) window.__drawnGraphNodes[label] = { ...ring, canvas: this.canvas };
        return text.call(this, label, ...args);
      };
    });
    const capture = async (name, width) => {
      result.stage = `${name}-${width}`;
      await page.setViewportSize({ width, height: 1000 });
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${name}: horizontal overflow at ${width}`);
      const filename = `${name}-${width}.png`;
      await page.screenshot({ path: path.join(output, filename), fullPage: true }); result.screenshots.push(filename);
    };
    await page.goto(base);
    await page.getByRole('heading', { name: '登录工作台' }).waitFor();
    await capture('login', 1440); await capture('login', 375);
    assert.equal(await page.getByRole('img', { name: '序枢 Ontweft' }).count(), 1);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole('textbox', { name: /^用户名/ }).fill('owner');
    await page.getByLabel('密码', { exact: true }).fill(password);
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByRole('button', { name: '新建变更集', exact: true }).waitFor();
    const request = async (method, url, data) => {
      const response = await context.request[method](base + '/api' + url, data === undefined ? {} : { data });
      assert.ok(response.ok(), `${method} ${url}: ${response.status()} ${await response.text()}`);
      return response.json();
    };
    const session = await request('get', '/auth/me'); const project = session.projects[0].id;
    const property = (key, label) => ({ key, label, data_type: 'string' });
    const relation = (key, label, target_type) => ({ key, label, target_type, cardinality: 'many' });
    const type = (key, label, relations) => ({ key, label, properties: [property('name', '名称'), property('status', '状态')], relations });
    let draft = await request('post', '/changesets', { project_id: project, title: '合成验证 · 企业数据协作', expected_baseline_release_id: null, definition: { types: [
      type('department', '业务团队', [relation('members', '团队成员', 'person')]),
      type('person', '负责人', [relation('maintains', '负责系统', 'system')]),
      type('system', '业务系统', [relation('datasets', '提供数据', 'asset'), relation('supports', '支撑系统', 'system')]),
      type('asset', '数据资产', [relation('depends', '依赖资产', 'asset')]),
    ], mappings: [] } });
    for (const action of ['validate', 'approve', 'publish']) draft = await request('post', `/changesets/${draft.id}/${action}`, { expected_revision: draft.revision });
    const definition = await request('get', `/ontology/definition?project_id=${project}`);
    const names = { department: ['客户运营团队', '数据治理团队'], person: ['负责人 A', '负责人 B', '负责人 C', '负责人 D'], system: ['客户管理系统', '订单管理系统', '分析工作台', '主数据平台'], asset: ['客户档案', '订单明细', '产品目录', '合同台账', '地区维表', '业务指标', '服务记录', '数据目录'] };
    const objects = {};
    for (const [type_key, labels] of Object.entries(names)) {
      objects[type_key] = [];
      for (const name of labels) objects[type_key].push(await request('post', '/objects', { project_id: project, expected_model_release_id: definition.release_id, type_key, properties: { name, status: '合成验证数据' }, evidence: [] }));
    }
    const connect = (type_key, source, target) => request('post', '/relations', { project_id: project, expected_model_release_id: definition.release_id, type_key, source_id: source.id, target_id: target.id, properties: {}, evidence: [] });
    for (let i = 0; i < 4; i++) { await connect('members', objects.department[i % 2], objects.person[i]); await connect('maintains', objects.person[i], objects.system[i]); await connect('supports', objects.system[i], objects.system[(i + 1) % 4]); }
    for (let i = 0; i < 8; i++) { await connect('datasets', objects.system[i % 4], objects.asset[i]); await connect('depends', objects.asset[i], objects.asset[(i + 1) % 8]); }
    result.seeded = { objects: 18, relations: 28 };
    await page.reload();
    await capture('ontology', 1440);
    await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('button', { name: '图谱探索', exact: true }).click();
    assert.equal(await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('button').count(), 5);
    await page.waitForFunction(() => !document.querySelector('[aria-label="放大图谱"]')?.disabled && document.querySelector('.graph-caption')?.textContent.includes('18'));
    await capture('graph-overview', 1440);
    const nodeList = page.locator('details.node-list').first();
    await nodeList.getByRole('button', { name: '客户档案', exact: true }).click();
    await page.getByRole('button', { name: '定位所选', exact: true }).click();
    await page.locator('.graph-canvas').scrollIntoViewIfNeeded();
    const point = async label => {
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await page.waitForFunction(name => !!window.__drawnGraphNodes[name], label);
      return page.evaluate(name => { const p = window.__drawnGraphNodes[name]; const rect = p.canvas.getBoundingClientRect(); return { x: rect.x + p.x, y: rect.y + p.y }; }, label);
    };
    const savePosition = async id => {
      await page.getByRole('button', { name: '保存个人视图', exact: true }).click();
      return page.evaluate(id => { const key = Object.keys(localStorage).find(key => key.startsWith('ontweft.graph.v1:')); return JSON.parse(localStorage.getItem(key)).positions.find(point => point.id === id); }, id);
    };
    const asset = objects.asset[0];
    const before = await savePosition(asset.id);
    let p = await point('客户档案');
    await page.mouse.move(p.x, p.y); await page.mouse.down(); await page.mouse.move(p.x + 64, p.y + 40, { steps: 12 }); await page.mouse.up();
    const after = await savePosition(asset.id);
    assert.ok(Math.hypot(after.x - before.x, after.y - before.y) > .001, 'Mouse drag must persist changed coordinates'); result.mouseDrag = true;
    await page.getByRole('button', { name: '固定节点', exact: true }).click();
    p = await point('客户档案');
    await page.mouse.move(p.x, p.y); await page.mouse.down(); await page.mouse.move(p.x + 40, p.y + 24, { steps: 6 }); await page.mouse.up();
    assert.deepEqual(await savePosition(asset.id), after, 'Pinned node must not move'); result.pin = true;
    await page.getByRole('button', { name: '取消固定', exact: true }).click();
    await page.getByRole('button', { name: '恢复个人视图', exact: true }).click();
    await page.getByText('已按当前授权恢复视图', { exact: true }).waitFor();
    await page.getByRole('button', { name: '取消固定', exact: true }).click();
    result.savedPinRestored = true;
    await nodeList.getByRole('button', { name: '客户档案', exact: true }).click();
    await page.getByRole('button', { name: '定位所选', exact: true }).click();
    await page.locator('.graph-canvas').scrollIntoViewIfNeeded();
    p = await point('客户档案');
    await page.mouse.dblclick(p.x, p.y, { delay: 100 });
    const editor = page.getByRole('complementary', { name: '对象详情', exact: true });
    await editor.getByLabel('名称', { exact: true }).fill('客户档案 · 已核对');
    await editor.getByRole('button', { name: '保存对象', exact: true }).click();
    await page.getByText('已保存至平台，未向外部系统写回。', { exact: true }).waitFor();
    await capture('graph-editor', 1440);
    await page.getByRole('button', { name: '关闭侧栏', exact: true }).click();
    await page.locator('.graph-inspector').getByRole('heading', { name: '客户档案 · 已核对', exact: true }).waitFor();
    assert.equal((await request('get', `/objects/${asset.id}?project_id=${project}`)).properties.name, '客户档案 · 已核对');
    result.doubleClickEditsRealObject = true; result.restoredViewRefreshesAfterSave = true;
    await page.getByRole('button', { name: '展开邻域', exact: true }).click();
    await page.getByRole('button', { name: '收起邻域', exact: true }).waitFor();
    await page.getByRole('button', { name: '收起邻域', exact: true }).click(); result.expandCollapse = true;
    const keyboardBefore = await savePosition(asset.id);
    await page.locator('.graph-canvas').focus();
    await page.keyboard.press('ArrowRight');
    const keyboardAfter = await savePosition(asset.id);
    assert.ok(Math.hypot(keyboardAfter.x - keyboardBefore.x, keyboardAfter.y - keyboardBefore.y) > .001, 'Keyboard must move an unpinned node');
    await page.locator('.graph-canvas').focus(); await page.keyboard.press('Enter');
    await editor.getByLabel('名称', { exact: true }).waitFor();
    await page.getByRole('button', { name: '关闭侧栏', exact: true }).click(); result.keyboardEdit = true;
    const edges = page.locator('details.node-list').nth(1);
    await edges.locator('summary').click();
    await edges.getByRole('button', { name: '客户档案 · 已核对 — 依赖资产 → 订单明细', exact: true }).click();
    await page.getByRole('button', { name: '打开来源对象与关系', exact: true }).click();
    await editor.getByRole('combobox', { name: /^业务角色 \/ 方向/ }).waitFor();
    assert.equal(await editor.getByRole('combobox', { name: /^业务角色 \/ 方向/ }).inputValue(), 'depends');
    await editor.getByRole('combobox', { name: /^目标对象/ }).selectOption(objects.asset[2].id);
    const relationRefreshed = page.waitForResponse(response => response.url().includes('/graph/nodes') && response.ok());
    await editor.getByRole('button', { name: '保存关系', exact: true }).click();
    await relationRefreshed;
    await page.getByRole('button', { name: '关闭侧栏', exact: true }).click();
    await edges.getByRole('button', { name: '客户档案 · 已核对 — 依赖资产 → 产品目录', exact: true }).waitFor();
    result.relationshipEditorFocusAndSave = true;
    await edges.locator('summary').click();
    await nodeList.getByRole('button', { name: '客户档案 · 已核对', exact: true }).click();
    await capture('graph-selected', 1440);
    for (const width of [1024, 768, 375, 320]) await capture('graph-selected', width);
    await page.setViewportSize({ width: 375, height: 1000 });
    await page.getByRole('button', { name: '定位所选', exact: true }).click();
    await page.locator('.graph-canvas').scrollIntoViewIfNeeded();
    const touchBefore = await savePosition(asset.id);
    await page.locator('.graph-canvas').scrollIntoViewIfNeeded(); p = await point('客户档案 · 已核对');
    result.touchDebug = { point: p, canvas: await page.locator('.graph-canvas').boundingBox(), before: touchBefore };
    assert.ok(p.x > 0 && p.y > 0 && p.x < 375 && p.y < 1000, 'Touch node must be in the viewport');
    const cdp = await context.newCDPSession(page);
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: p.x, y: p.y }] });
    for (let step = 1; step <= 6; step++) await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: p.x + step * 6, y: p.y + step * 4 }] });
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    const touchAfter = await savePosition(asset.id);
    result.touchDebug.after = touchAfter;
    result.touchDebug.selection = await page.locator('.graph-inspector h2').textContent();
    assert.ok(Math.hypot(touchAfter.x - touchBefore.x, touchAfter.y - touchBefore.y) > .001, 'One-finger drag must move the actual node'); result.touchDrag = true;
    result.stage = 'mobile-navigation';
    await page.getByRole('button', { name: '打开导航', exact: true }).click();
    await page.getByRole('navigation', { name: '移动导航', exact: true }).waitFor();
    assert.equal(await page.getByRole('navigation', { name: '移动导航', exact: true }).getByRole('button').count(), 5);
    await page.keyboard.press('Escape'); result.mobileNavigation = true;
    result.canvasErrors = await page.evaluate(() => window.__graphErrors);
    assert.deepEqual(result.pageErrors, []); result.passed = true;
  } catch (error) {
    result.error = String(error); process.exitCode = 1;
    if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {});
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) { server.kill(); await Promise.race([new Promise(resolve => server.once('exit', resolve)), pause(5000)]); }
    log.end();
    result.childrenStopped = server.exitCode !== null || server.signalCode !== null;
    if (result.childrenStopped && path.dirname(fs.realpathSync(temp)) === fs.realpathSync(os.tmpdir()) && path.basename(temp).startsWith('ontweft-workbench-')) {
      for (let i = 0; i < 10; i++) { try { fs.rmSync(temp, { recursive: true, force: true }); result.temporaryDataRemoved = true; break; } catch { await pause(250); } }
    }
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2)); console.log(JSON.stringify(result));
  }
}
main().catch(error => { console.error(String(error)); process.exitCode = 1; });
