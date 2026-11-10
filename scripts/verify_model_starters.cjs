// Real browser + API flow. No production credentials, database or external services.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const net = require('node:net');
const { spawn } = require('node:child_process');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'var', 'reports', 'model-starters');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'ontweft-starter-'));
  const reservation = net.createServer();
  await new Promise(resolve => reservation.listen(0, '127.0.0.1', resolve));
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  const password = crypto.randomBytes(24).toString('base64url');
  const python = process.env.ONTOLOGY_TEST_PYTHON || path.join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  // Keep inherited credential-bearing application configuration out of this child.
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('ONTOLOGY_')));
  const serverStarted = performance.now();
  const server = spawn(python, ['scripts/model_starter_preview.py'], { cwd: root, windowsHide: true, env: {
    ...env, PYTHONUTF8: '1', ONTOLOGY_PREVIEW_DATA_DIR: temp, ONTOLOGY_PREVIEW_PORT: String(port), ONTOLOGY_PREVIEW_PASSWORD: password,
  }, stdio: ['ignore', 'pipe', 'pipe'] });
  const log = fs.createWriteStream(path.join(output, 'preview-server.log'));
  server.stdout.pipe(log, { end: false }); server.stderr.pipe(log, { end: false });
  let spawnError; server.on('error', error => { spawnError = error; });
  let browser;
  const result = { passed: false, storage: 'isolated SQLite', externalServices: false, screenshots: [], pageErrors: [] };
  const base = `http://127.0.0.1:${port}`;
  try {
    let ready = false;
    for (let i = 0; i < 120; i++) {
      if (spawnError) throw spawnError;
      if (server.exitCode !== null) throw new Error('Preview server exited; inspect preview-server.log');
      try { ready = (await fetch(`${base}/api/health`)).ok; } catch {}
      if (ready) break;
      await pause(250);
    }
    assert.ok(ready, 'Isolated server did not become ready');
    result.serverReadyMs = Math.round(performance.now() - serverStarted);
    browser = await chromium.launch({ headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce', hasTouch: true });
    const page = await context.newPage();
    await page.addInitScript(() => {
      window.__graphErrors = [];
      window.addEventListener('error', event => {
        window.__graphErrors.push({ message: event.message, width: innerWidth, elements: [...document.querySelectorAll('.graph-stage, .graph-canvas, .graph-controls, .graph-caption')].map(el => ({ class: el.className, width: el.offsetWidth, height: el.offsetHeight, rows: getComputedStyle(el).gridTemplateRows })) });
      });
    });
    let stage = 'login';
    page.on('pageerror', error => result.pageErrors.push({ stage, message: error.message, stack: error.stack }));
    await page.goto(base);
    await page.getByRole('textbox', { name: /^用户名/ }).fill('owner');
    await page.getByLabel('密码', { exact: true }).fill(password);
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByRole('button', { name: '新建变更集', exact: true }).click();
    await page.getByRole('button', { name: '使用项目与任务模板' }).waitFor();
    const capture = async (name, width) => {
      stage = `${name}-${width}`;
      await page.setViewportSize({ width, height: 1000 });
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${name}: overflow at ${width}`);
      const layerAligned = await page.evaluate(() => {
        const layer = document.querySelector('.graph-self-loops'); if (!layer) return true;
        const actual = layer.getBoundingClientRect(); const expected = layer.closest('.graph-canvas').getBoundingClientRect();
        return ['top', 'left', 'width', 'height'].every(key => Math.abs(actual[key] - expected[key]) < 1);
      });
      assert.ok(layerAligned, `${name}: self-loop layer must align with the node canvas at ${width}`);
      await page.screenshot({ path: path.join(output, `${name}-${width}.png`), fullPage: true });
      result.screenshots.push(`${name}-${width}.png`);
    };
    await capture('model-starters', 1440); await capture('model-starters', 375);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole('button', { name: '使用项目与任务模板' }).click();
    await page.getByLabel('显示名称', { exact: true }).first().fill('交付项目');
    const addRelation = async (typeIndex, key, label, target) => {
      const editor = page.locator('.type-editor').nth(typeIndex);
      await editor.getByRole('button', { name: '添加关系', exact: true }).click();
      await editor.getByLabel('关系标识', { exact: true }).last().fill(key);
      await editor.getByLabel('业务角色', { exact: true }).last().fill(label);
      await editor.getByLabel(/^目标类型/).last().selectOption(target);
    };
    await addRelation(0, 'tracks', '跟踪任务', 'task');
    await addRelation(1, 'validated_for', '验证项目', 'project');
    await addRelation(1, 'self_reference', '自引用测试', 'task');
    await addRelation(1, 'self_review', '自检记录', 'task');
    await page.getByRole('button', { name: '保存草稿', exact: true }).click();
    await page.getByRole('button', { name: '验证', exact: true }).click();
    await page.getByRole('button', { name: '批准当前修订', exact: true }).click();
    await page.getByRole('button', { name: '发布已批准修订', exact: true }).click();
    await page.getByText('发布成功，已发布模型已刷新。', { exact: true }).waitFor();
    await page.getByRole('button', { name: '关闭侧栏', exact: true }).click();
    const session = await (await context.request.get(`${base}/api/auth/me`)).json();
    const project = session.projects[0].id;
    const definition = await (await context.request.get(`${base}/api/ontology/definition?project_id=${project}`)).json();
    assert.equal(definition.definition.types[0].label, '交付项目');
    const common = { project_id: project, expected_model_release_id: definition.release_id };
    const post = async (url, data) => {
      const response = await context.request.post(`${base}/api${url}`, { data });
      assert.ok(response.ok(), `${url}: ${response.status()} ${await response.text()}`);
      return response.json();
    };
    const projectObject = await post('/objects', { ...common, type_key: 'project', properties: { name: '合成示例 · 开源首发', status: '进行中' } });
    for (const [name, status] of [['定义业务问题', '已完成'], ['核对模型约束', '进行中'], ['验证图谱关系', '待处理']]) {
      const task = await post('/objects', { ...common, type_key: 'task', properties: { name, status } });
      await post('/relations', { ...common, type_key: 'belongs_to', source_id: task.id, target_id: projectObject.id });
      if (name === '核对模型约束') {
        await post('/relations', { ...common, type_key: 'validated_for', source_id: task.id, target_id: projectObject.id });
        await post('/relations', { ...common, type_key: 'tracks', source_id: projectObject.id, target_id: task.id });
        await post('/relations', { ...common, type_key: 'self_reference', source_id: task.id, target_id: task.id });
        await post('/relations', { ...common, type_key: 'self_review', source_id: task.id, target_id: task.id });
      }
    }
    const graph = await (await context.request.get(`${base}/api/graph?project_id=${project}`)).json();
    assert.equal(graph.nodes.length, 4); assert.equal(graph.edges.length, 7);
    await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('button', { name: '图谱探索', exact: true }).click();
    stage = 'graph-load';
    await page.waitForFunction(() => { const button = document.querySelector('[aria-label="放大图谱"]'); return button && !button.disabled; });
    await page.locator('.node-chips').getByRole('button', { name: '核对模型约束', exact: true }).click();
    await page.locator('.graph-inspector').getByRole('heading', { name: '核对模型约束' }).waitFor();
    await page.getByText('同一节点上的关系以环线显示，可逐条选择。').waitFor();
    const selfReference = page.getByRole('button', { name: '环形关系：核对模型约束 — 自引用测试 → 核对模型约束', exact: true });
    const selfReview = page.getByRole('button', { name: '环形关系：核对模型约束 — 自检记录 → 核对模型约束', exact: true });
    await selfReference.waitFor(); await selfReview.waitFor();
    const firstPath = await selfReference.locator('.loop-hit').getAttribute('d');
    assert.notEqual(firstPath, await selfReview.locator('.loop-hit').getAttribute('d'));
    const clickLoop = async (loop, touch = false) => {
      await loop.scrollIntoViewIfNeeded();
      const point = await loop.locator('.loop-hit').evaluate(path => {
        const local = path.getPointAtLength(path.getTotalLength() / 2);
        const screen = new DOMPoint(local.x, local.y).matrixTransform(path.getScreenCTM());
        return { x: screen.x, y: screen.y };
      });
      if (touch) await page.touchscreen.tap(point.x, point.y);
      else await page.mouse.click(point.x, point.y);
    };
    await clickLoop(selfReference);
    await page.locator('.graph-inspector').getByRole('heading', { name: '自引用测试', exact: true }).waitFor();
    await selfReview.focus(); await page.keyboard.press('Enter');
    await page.locator('.graph-inspector').getByRole('heading', { name: '自检记录', exact: true }).waitFor();
    await page.getByRole('button', { name: '放大图谱', exact: true }).click();
    await page.waitForFunction(before => document.querySelector('.graph-self-loop .loop-hit')?.getAttribute('d') !== before, firstPath);
    await page.getByRole('button', { name: '缩小图谱', exact: true }).click();
    const taskFilter = page.locator('.graph-legend').getByRole('button', { name: /任务/ });
    await taskFilter.click();
    await selfReference.waitFor({ state: 'detached' });
    await taskFilter.click(); await selfReference.waitFor();
    await page.getByRole('tab', { name: '本体模型', exact: true }).click();
    const modelLoop = page.getByRole('button', { name: '环形关系：任务 — 自引用测试 → 任务', exact: true });
    await modelLoop.waitFor(); await clickLoop(modelLoop);
    await page.locator('.graph-inspector').getByRole('heading', { name: '自引用测试', exact: true }).waitFor();
    await page.getByRole('button', { name: '编辑关系定义（变更集）', exact: true }).waitFor();
    await page.getByRole('tab', { name: '业务实例', exact: true }).click();
    await selfReference.waitFor();
    await page.locator('.node-chips').getByRole('button', { name: '核对模型约束', exact: true }).click();
    result.selfLoopsRenderedAndSelectable = true;
    result.selfLoopsModelAndFilters = true;
    await capture('starter-instance-graph', 1440);
    await capture('starter-instance-graph', 375);
    await clickLoop(selfReference, true);
    await page.locator('.graph-inspector').getByRole('heading', { name: '自引用测试', exact: true }).waitFor();
    result.selfLoopTouchSelection = true;
    await page.locator('.node-chips').getByRole('button', { name: '核对模型约束', exact: true }).click();
    stage = 'object-edit-and-reload';
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole('button', { name: '打开对象表单', exact: true }).click();
    await page.getByLabel('任务名称 *', { exact: true }).fill('已自定义 · 核对模型约束');
    await page.getByRole('button', { name: '保存对象', exact: true }).click();
    await page.getByText('已保存至平台，未向外部系统写回。', { exact: true }).waitFor();
    await page.getByRole('button', { name: '关闭侧栏', exact: true }).click();
    await page.reload();
    await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('button', { name: '图谱探索', exact: true }).click();
    await page.locator('.node-chips').getByRole('button', { name: '已自定义 · 核对模型约束', exact: true }).waitFor();
    result.objectEditSurvivesReload = true;
    result.parallelReverseAndSelfRelations = true;
    result.errorLayout = await page.evaluate(() => window.__graphErrors);
    assert.deepEqual(result.pageErrors, []);
    result.passed = true;
    result.publishedModelVersion = definition.version;
    result.graph = { nodes: graph.nodes.length, edges: graph.edges.length };
  } catch (error) { result.error = String(error); process.exitCode = 1; }
  finally {
    if (browser) await browser.close();
    if (server.exitCode === null) {
      server.kill();
      await Promise.race([new Promise(resolve => server.once('exit', resolve)), pause(5000)]);
    }
    log.end();
    // Delete only the unique test directory created above, after the child stops.
    const parent = fs.realpathSync(os.tmpdir());
    if (server.exitCode !== null && path.dirname(fs.realpathSync(temp)) === parent && path.basename(temp).startsWith('ontweft-starter-')) fs.rmSync(temp, { recursive: true, force: true });
    fs.writeFileSync(path.join(output, 'starter-browser-result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  }
}
main().catch(error => { console.error(String(error)); process.exitCode = 1; });
