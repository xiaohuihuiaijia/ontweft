// Browser verification against a disposable local database and real worker.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const net = require('node:net');
const { spawn } = require('node:child_process');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'var/reports/enterprise-beta-browser');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'ontweft-intake-'));
  const reserve = net.createServer();
  await new Promise(resolve => reserve.listen(0, '127.0.0.1', resolve));
  const port = reserve.address().port;
  await new Promise(resolve => reserve.close(resolve));
  const password = crypto.randomBytes(24).toString('base64url');
  const python = process.env.ONTOLOGY_TEST_PYTHON || path.join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('ONTOLOGY_')));
  const logs = [];
  const children = [];
  function child(args, extra, name) {
    const process = spawn(python, args, { cwd: root, windowsHide: true, env: { ...env, PYTHONUTF8: '1', ...extra }, stdio: ['ignore', 'pipe', 'pipe'] });
    const log = fs.createWriteStream(path.join(output, name + '.log'));
    process.stdout.pipe(log, { end: false }); process.stderr.pipe(log, { end: false });
    process.on('error', error => { process.spawnError = error; });
    children.push(process); logs.push(log); return process;
  }
  const server = child(['scripts/model_starter_preview.py'], { ONTOLOGY_PREVIEW_DATA_DIR: temp, ONTOLOGY_PREVIEW_PORT: String(port), ONTOLOGY_PREVIEW_PASSWORD: password }, 'server');
  const base = `http://127.0.0.1:${port}`;
  let browser;
  const result = { passed: false, storage: 'isolated SQLite', externalServices: false, pageErrors: [], screenshots: [] };
  try {
    let ready = false;
    for (let i = 0; i < 120; i++) {
      if (server.spawnError) throw server.spawnError;
      if (server.exitCode !== null) throw new Error('Preview server exited');
      try { ready = (await fetch(base + '/api/health')).ok; } catch {}
      if (ready) break;
      await pause(250);
    }
    assert.ok(ready, 'Server did not become ready');
    child(['scripts/start_local_demo.py', 'worker', '--data-dir', temp, '--port', String(port)], {}, 'worker');
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
    const page = await context.newPage();
    let phase = 'intake';
    page.on('pageerror', error => {
      result.pageErrors.push(error.message);
      (result.pageErrorDetails ??= []).push({ phase, stack: error.stack, url: page.url(), time: new Date().toISOString() });
    });
    await page.goto(base + '/#sources');
    await page.getByRole('textbox', { name: /^用户名/ }).fill('owner');
    await page.getByLabel('密码', { exact: true }).fill(password);
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByLabel('CSV / XLSX 文件', { exact: true }).setInputFiles({ name: '合成清单.csv', mimeType: 'text/csv', buffer: Buffer.from('编号,名称,数量\n001,合成测试 · 输入,2\n' + Array.from({ length: 26 }, (_, index) => `${String(index + 2).padStart(3, '0')},合成测试 · 待匹配 ${index + 2},3`).join('\n') + '\n') });
    await page.getByLabel('数据源名称（选填）').fill('合成数据 · 通用建模验证');
    const uploadResponse = page.waitForResponse(response => response.url().endsWith('/api/sources/upload') && response.request().method() === 'POST');
    await page.getByRole('button', { name: '上传并查看数据', exact: true }).click();
    const uploaded = await uploadResponse;
    assert.equal(uploaded.status(), 201, await uploaded.text());
    const source = await uploaded.json();
    assert.equal(await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('button').count(), 5);
    await page.getByLabel(/的业务键/).selectOption('c1');
    await page.getByRole('button', { name: '保存业务键', exact: true }).click();
    await page.getByLabel('更新文件', { exact: true }).waitFor();
    await page.getByText('表格已保存，可查看数据并生成建模建议。尚未创建业务对象。').waitFor();
    await page.getByRole('button', { name: '读取最多 20 条样本' }).click();
    await page.getByRole('table', { name: '上传表格样本' }).waitFor();
    assert.ok((await page.getByRole('table', { name: '上传表格样本' }).textContent()).includes('001'));
    assert.ok((await page.getByLabel('来源样本', { exact: true }).textContent()).includes('001'));
    await page.locator('.intake-resource input').check();
    await page.getByRole('button', { name: '生成可编辑建模建议' }).click();
    await page.getByText('本次新增 1 个类型').waitFor();
    await page.getByRole('button', { name: '编辑模型与来源映射' }).click();
    await page.getByLabel('变更名称', { exact: true }).fill('我的通用清单 · 首版');
    await page.getByLabel('显示名称', { exact: true }).first().fill('我的通用清单');
    const itemTypeKey = await page.getByLabel('类型标识', { exact: true }).first().inputValue();
    await page.getByRole('button', { name: '添加业务类型', exact: true }).click();
    const groupEditor = page.locator('fieldset.type-editor').nth(1);
    await groupEditor.getByLabel('类型标识', { exact: true }).fill('Group');
    await groupEditor.getByLabel('显示名称', { exact: true }).fill('我的分组');
    for (const [key, label] of [['name', '名称'], ['code', '编号']]) {
      await groupEditor.getByRole('button', { name: '添加字段', exact: true }).click();
      await groupEditor.getByLabel('字段标识', { exact: true }).last().fill(key);
      await groupEditor.getByLabel('字段名称', { exact: true }).last().fill(label);
    }
    const itemEditor = page.locator('fieldset.type-editor').first();
    await itemEditor.getByRole('button', { name: '添加关系', exact: true }).click();
    await itemEditor.getByLabel('关系标识', { exact: true }).fill('belongs_to');
    await itemEditor.getByLabel('业务角色', { exact: true }).fill('所属分组');
    await itemEditor.getByLabel(/^目标类型/).selectOption('Group');
    await itemEditor.getByLabel(/^基数/).selectOption('one');
    await page.getByRole('button', { name: '保存草稿', exact: true }).click();
    await page.getByRole('button', { name: '验证', exact: true }).click();
    await page.getByRole('button', { name: '批准当前修订', exact: true }).click();
    await page.getByRole('button', { name: '发布已批准修订', exact: true }).click();
    await page.getByText('发布成功，已发布模型已刷新。', { exact: true }).waitFor();
    await page.getByRole('button', { name: '关闭侧栏', exact: true }).click();
    const responsePromise = page.waitForResponse(response => response.url().endsWith(`/sources/${source.id}/snapshots`) && response.request().method() === 'POST');
    await page.getByRole('button', { name: '采集所选资源快照', exact: true }).click();
    const snapshotResponse = await responsePromise;
    assert.equal(snapshotResponse.status(), 201, await snapshotResponse.text());
    const { snapshot } = await snapshotResponse.json();
    let record;
    for (let i = 0; i < 120; i++) {
      record = await (await context.request.get(`${base}/api/snapshots/${snapshot.id}/records`)).json();
      if (['complete', 'failed'].includes(record.snapshot.status)) break;
      await pause(250);
    }
    assert.equal(record.snapshot.status, 'complete', JSON.stringify(record.snapshot));
    assert.equal(record.snapshot.projection.created, 27);
    await page.getByRole('button', { name: '刷新采集状态' }).click();
    await page.getByText(/对象投影：新增 27/).waitFor();
    const auth = await (await context.request.get(base + '/api/auth/me')).json();
    const ownerProject = auth.projects[0].id;
    const definition = await (await context.request.get(`${base}/api/ontology/definition?project_id=${ownerProject}`)).json();
    const group = await context.request.post(base + '/api/objects', { data: { project_id: ownerProject, expected_model_release_id: definition.release_id, type_key: 'Group', properties: { name: '合成分组', code: '001' } } });
    assert.equal(group.status(), 201, await group.text());
    await page.getByRole('button', { name: '刷新已发布模型' }).click();
    await page.getByLabel('起点类型').selectOption(itemTypeKey);
    await page.getByLabel('要建立的关系').selectOption('belongs_to');
    await page.getByLabel('起点关联字段').selectOption('c1');
    await page.getByLabel('目标匹配字段').selectOption('code');
    await page.getByRole('button', { name: '预览关系匹配' }).click();
    const applyRelations = page.getByRole('button', { name: '建立 1 条关系', exact: true });
    await applyRelations.waitFor();
    assert.equal(await applyRelations.isDisabled(), true);
    await page.getByText('查看未解决记录（共 26 条）').click();
    await page.getByRole('navigation', { name: '未解决记录分页' }).getByRole('button', { name: '下一页', exact: true }).click();
    await page.getByText('第 26–26 条，共 26 条').waitFor();
    await page.getByRole('navigation', { name: '未解决记录分页' }).getByRole('button', { name: '上一页', exact: true }).click();
    await page.getByText('第 1–25 条，共 26 条').waitFor();
    await page.getByRole('checkbox', { name: /我已知有 26 条未解决/ }).check();
    await applyRelations.click();
    await page.getByText(/已建立 1 条关系，保留 0 条已有关系，26 条未解决/).waitFor();
    await page.getByRole('button', { name: '保存为持续规则', exact: true }).click();
    await page.getByText('持续规则已保存，后台开始匹配。人工关系保留，冲突记录单独暂停。').waitFor();
    await page.getByLabel('更新文件', { exact: true }).setInputFiles({ name: '更新清单.csv', mimeType: 'text/csv', buffer: Buffer.from('编号,名称,数量\n001,合成测试 · 更新,4\n') });
    await page.getByRole('button', { name: '预览更新', exact: true }).click();
    const versionApplied = page.waitForResponse(response => /\/versions\/[^/]+\/apply$/.test(response.url()));
    await page.getByRole('button', { name: '应用此版本并同步', exact: true }).click();
    assert.equal((await versionApplied).status(), 200);
    let updated = false;
    for (let i = 0; i < 120; i++) {
      const items = await (await context.request.get(`${base}/api/objects?project_id=${ownerProject}`)).json();
      updated = items.total === 28 && items.items.some(item => item.properties.name === '合成测试 · 更新');
      if (updated) break;
      await pause(250);
    }
    assert.ok(updated, 'Version update must keep absent records and change the original object');
    result.fileVersionUpdate = true; result.persistentRuleSaved = true; result.defaultNavigation = 5;
    const importedGraph = await (await context.request.get(`${base}/api/graph?project_id=${ownerProject}`)).json();
    assert.equal(importedGraph.nodes.length, 28); assert.equal(importedGraph.edges.length, 1);
    const capture = async (name, width) => {
      await page.setViewportSize({ width, height: 1000 });
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const overflow = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth,
        elements: [...document.querySelectorAll('#main-content *')].filter(element => {
          const r = element.getBoundingClientRect(); return r.right > innerWidth + 1 && getComputedStyle(element).position !== 'fixed';
        }).map(element => ({ tag: element.tagName, class: element.className, text: element.textContent?.slice(0, 70), width: element.getBoundingClientRect().width })).slice(0, 15) }));
      assert.equal(overflow.scroll > width, false, `Overflow at ${width}: ${JSON.stringify(overflow)}`);
      const filename = `${name}-${width}.png`;
      // Chromium full-page capture can briefly resize the viewport to 1px.
      // Keep the live WebGL view at the tested desktop/mobile width instead.
      await page.screenshot({ path: path.join(output, filename), fullPage: name !== 'imported-objects' }); result.screenshots.push(filename);
    };
    await capture('table-intake', 1440);
    await capture('table-intake', 375);
    await capture('table-intake', 320);
    await page.setViewportSize({ width: 1440, height: 1000 });
    phase = 'open-graph';
    await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('button', { name: '图谱探索', exact: true }).click();
    let indexReady = false;
    for (let i = 0; i < 120; i++) {
      const status = await (await context.request.get(`${base}/api/search/status?project_id=${ownerProject}`)).json();
      if (status.status === 'ready') { indexReady = true; break; }
      await pause(250);
    }
    assert.ok(indexReady, 'The real worker must index the updated object');
    await page.getByLabel('全库对象搜索', { exact: true }).fill('更新');
    const searchResponse = page.waitForResponse(response => response.url().endsWith('/api/search/objects'));
    await page.getByRole('button', { name: '搜索', exact: true }).click();
    assert.equal((await (await searchResponse).json()).total, 1);
    phase = 'add-node';
    await page.getByRole('button', { name: '合成测试 · 更新 · 加入画布', exact: true }).click();
    result.globalAuthorizedSearch = true;
    await page.locator('.node-chips').getByRole('button', { name: '合成测试 · 更新', exact: true }).click();
    await page.locator('.graph-inspector').getByRole('heading', { name: '合成测试 · 更新', exact: true }).waitFor();
    await page.getByRole('button', { name: '固定节点', exact: true }).click();
    phase = 'expand';
    await page.getByRole('button', { name: '展开邻域', exact: true }).click();
    await page.getByRole('button', { name: '收起邻域', exact: true }).waitFor();
    await page.getByRole('button', { name: '保存个人视图', exact: true }).click();
    phase = 'restore';
    await page.getByRole('button', { name: '恢复个人视图', exact: true }).click();
    await page.getByText('已按当前授权恢复视图').waitFor();
    result.neighborsPinAndRestore = true;
    phase = 'capture-graph';
    await capture('imported-objects', 1440);
    await capture('imported-objects', 375);
    phase = 'reload';
    await page.reload();
    const session = await (await context.request.get(base + '/api/auth/me')).json();
    const projectId = session.projects[0].id;
    const objects = await (await context.request.get(`${base}/api/objects?project_id=${projectId}`)).json();
    assert.equal(objects.total, 28);
    assert.ok(objects.items.some(item => item.properties.c1 === '001'));
    assert.deepEqual(result.pageErrors, []);
    result.passed = true; result.objectsImported = 27; result.fixtureTargetObjects = 1; result.matchedRelations = 1; result.unmatchedRecords = 26; result.issuePagingVerified = true; result.customModelSaved = true; result.survivesReload = true;
  } catch (error) { result.error = String(error); process.exitCode = 1; }
  finally {
    if (browser) await browser.close();
    for (const process of [...children].reverse()) {
      if (process.exitCode === null) { process.kill(); await Promise.race([new Promise(resolve => process.once('exit', resolve)), pause(5000)]); }
    }
    for (const log of logs) log.end();
    const parent = fs.realpathSync(os.tmpdir());
    result.childrenStopped = children.every(process => process.exitCode !== null || process.signalCode !== null);
    if (result.childrenStopped && path.dirname(fs.realpathSync(temp)) === parent && path.basename(temp).startsWith('ontweft-intake-')) {
      // Windows may release SQLite handles just after process termination. Preserve
      // verification evidence even when a temporary directory must be retained.
      for (let attempt = 0; attempt < 10; attempt++) {
        try { fs.rmSync(temp, { recursive: true, force: true }); result.temporaryDataRemoved = true; break; }
        catch (error) { result.cleanupError = error.code; await pause(250); }
      }
      if (result.temporaryDataRemoved) delete result.cleanupError;
    }
    fs.writeFileSync(path.join(output, 'browser-result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  }
}
main().catch(error => { console.error(String(error)); process.exitCode = 1; });
