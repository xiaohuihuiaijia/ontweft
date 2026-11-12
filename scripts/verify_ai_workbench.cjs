// Real browser, API and worker against disposable storage and a synthetic compatible model.
// This tests integration, not model quality. No keys, private documents or paid requests.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const http = require('node:http');
const crypto = require('node:crypto');
const { inflateSync } = require('node:zlib');
const { spawn } = require('node:child_process');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'var/reports/ai-workbench');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const factText = 'SYN-001 is a synthetic connector rated 12 V.';
function probeColour(messages) {
  const image = messages.flatMap(message => Array.isArray(message.content) ? message.content : []).find(part => part.type === 'image_url');
  if (!image) return 'none';
  // The gateway probe is a uniform RGB PNG, so its first pixel identifies the colour.
  const png = Buffer.from(image.image_url.url.split(',')[1], 'base64'), chunks = [];
  for (let offset = 8; offset < png.length;) {
    const length = png.readUInt32BE(offset), type = png.toString('ascii', offset + 4, offset + 8);
    if (type === 'IDAT') chunks.push(png.subarray(offset + 8, offset + 8 + length));
    offset += length + 12;
  }
  const pixel = inflateSync(Buffer.concat(chunks)).subarray(1, 4);
  return ['red', 'green', 'blue'][[...pixel].indexOf(Math.max(...pixel))];
}
function syntheticPdf() {
  const stream = text => { const value = `BT /F1 16 Tf 50 740 Td (${text}) Tj ET`; return `<< /Length ${value.length} >>\nstream\n${value}\nendstream`; };
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 7 0 R >> >> /Contents 4 0 R >>', stream('Synthetic cover - specifications are on page 2'),
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 7 0 R >> >> /Contents 6 0 R >>', stream(factText),
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'];
  let body = '%PDF-1.4\n'; const offsets = [0];
  objects.forEach((object, i) => { offsets.push(Buffer.byteLength(body)); body += `${i + 1} 0 obj\n${object}\nendobj\n`; });
  const xref = Buffer.byteLength(body);
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(body);
}
const property = (key, label, data_type = 'string', unit = null) => ({ key, label, data_type, unit, required: false, enum: [], source_owned: true });
const modelDefinition = { types: [{ key: 'SyntheticProduct', label: '合成产品', description: '仅用于本地浏览器流程验收', properties: [property('code', '型号'), property('name', '名称'), property('voltage', '额定电压', 'number', 'V')], unique_keys: [['code']], relations: [] }], mappings: [], gaps: [] };

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const temporaryRoot = fs.realpathSync(os.tmpdir());
  const temp = fs.mkdtempSync(path.join(temporaryRoot, 'ontweft-ai-browser-'));
  const reservation = net.createServer();
  await new Promise(resolve => reservation.listen(0, '127.0.0.1', resolve));
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  let modelCalls = 0;
  const modelServer = http.createServer(async (req, res) => {
    let body = ''; for await (const chunk of req) body += chunk;
    try {
      const input = JSON.parse(body), schema = input.response_format.json_schema.schema.properties;
      let data;
      if (schema.token) {
        const contents = JSON.stringify(input.messages);
        data = { token: contents.match(/Token: ([0-9a-f]+)/)[1], colour: probeColour(input.messages) };
      } else if (schema.text) data = { title: '合成产品规格', text: factText, issues: [], needs_review: false, continued_table: false };
      else if (schema.types) data = modelDefinition;
      else if (schema.candidates) data = { candidates: [{ id: 'product-1', type_key: 'SyntheticProduct', values: [{ key: 'code', value_json: '"SYN-001"' }, { key: 'name', value_json: '"合成连接器"' }, { key: 'voltage', value_json: '12' }], business_key_fields: ['code'], quotes: [factText] }], relations: [], gaps: [] };
      else throw new Error('Unexpected synthetic-model schema');
      modelCalls++;
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ id: 'synthetic', model: input.model, choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant', content: JSON.stringify(data) } }], usage: { prompt_tokens: 100, completion_tokens: 100, total_tokens: 200 } }));
    } catch (error) { res.writeHead(400); res.end(JSON.stringify({ error: { message: error.message } })); }
  });
  await new Promise(resolve => modelServer.listen(0, '127.0.0.1', resolve));
  const modelUrl = `http://127.0.0.1:${modelServer.address().port}/v1`;
  const python = process.env.ONTOLOGY_TEST_PYTHON || path.join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('ONTOLOGY_')));
  const password = crypto.randomBytes(24).toString('base64url'), children = [], logs = [];
  function child(args, extra, name) {
    const process = spawn(python, args, { cwd: root, windowsHide: true, env: { ...env, PYTHONUTF8: '1', ...extra }, stdio: ['ignore', 'pipe', 'pipe'] });
    const log = fs.createWriteStream(path.join(output, `${name}.log`));
    process.stdout.pipe(log, { end: false }); process.stderr.pipe(log, { end: false });
    process.on('error', error => { process.spawnError = error; }); children.push(process); logs.push(log); return process;
  }
  const server = child(['scripts/model_starter_preview.py'], { ONTOLOGY_PREVIEW_DATA_DIR: temp, ONTOLOGY_PREVIEW_PORT: String(port), ONTOLOGY_PREVIEW_PASSWORD: password }, 'server');
  const base = `http://127.0.0.1:${port}`;
  let browser, page, phase = 'startup';
  const result = { passed: false, model: 'synthetic compatible HTTP server', storage: 'isolated SQLite', externalServices: false, pageErrors: [], screenshots: [] };
  try {
    let ready = false;
    for (let i = 0; i < 120; i++) { if (server.spawnError) throw server.spawnError; if (server.exitCode !== null) throw new Error('Preview server exited'); try { ready = (await fetch(base + '/api/health')).ok; } catch {} if (ready) break; await pause(250); }
    assert.ok(ready, 'Disposable preview server did not become ready');
    child(['scripts/start_local_demo.py', 'worker', '--data-dir', temp, '--port', String(port)], {}, 'worker');
    browser = await chromium.launch({ headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
    const context = await browser.newContext({ viewport: { width: 1536, height: 1000 }, reducedMotion: 'reduce' });
    page = await context.newPage(); page.setDefaultTimeout(15000);
    page.on('pageerror', error => result.pageErrors.push({ phase, message: error.message }));
    await page.goto(base);
    await page.getByRole('textbox', { name: /^用户名/ }).fill('owner');
    await page.getByLabel('密码', { exact: true }).fill(password);
    await page.getByRole('button', { name: '登录', exact: true }).click();
    phase = 'model-settings';
    await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('button', { name: '设置', exact: true }).click();
    await page.getByRole('button', { name: '添加模型连接' }).click();
    await page.getByLabel('连接名称', { exact: true }).fill('合成模型 · 浏览器验收');
    await page.getByLabel('兼容 API 地址', { exact: true }).fill(modelUrl);
    await page.getByLabel('允许受信任的内网模型地址', { exact: true }).check();
    const [saved] = await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/api/model-connections') && response.request().method() === 'POST'),
      page.getByRole('button', { name: '保存模型连接' }).click(),
    ]);
    assert.equal(saved.status(), 201, await saved.text());
    await page.getByRole('button', { name: '测试提取模型' }).click();
    await page.getByText(/测试通过/).waitFor();
    phase = 'upload';
    await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('button', { name: '数据源', exact: true }).click();
    await page.getByRole('button', { name: '文档资料', exact: true }).click();
    await page.getByLabel('上传业务资料', { exact: true }).setInputFiles({ name: 'synthetic-specification.pdf', mimeType: 'application/pdf', buffer: syntheticPdf() });
    const [uploaded] = await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/api/documents/upload') && response.request().method() === 'POST'),
      page.getByRole('button', { name: '保存原件' }).click(),
    ]);
    assert.equal(uploaded.status(), 201, await uploaded.text());
    const document = (await uploaded.json()).document;
    await page.getByRole('button', { name: '刷新文档' }).click();
    await page.getByRole('button', { name: '查看第 1 页' }).waitFor();
    await page.getByText('限定页码（默认全文）', { exact: true }).click();
    await page.getByLabel(`文档 ${document.id} 的页码范围`, { exact: true }).fill('2');
    await page.getByLabel('你希望理解什么？（可选）', { exact: true }).fill('只提取型号、名称与额定电压，保留单位和来源');
    phase = 'proposal';
    const [submitted] = await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/api/ai/proposals') && response.request().method() === 'POST'),
      page.getByRole('button', { name: '按需求生成候选' }).click(),
    ]);
    assert.equal(submitted.status(), 202, await submitted.text());
    const proposalId = (await submitted.json()).id;
    let proposal;
    for (let i = 0; i < 120; i++) { proposal = await (await context.request.get(`${base}/api/ai/proposals/${proposalId}`)).json(); if (['ready', 'failed', 'partial'].includes(proposal.status)) break; await pause(500); }
    assert.equal(proposal.status, 'ready', JSON.stringify(proposal.job));
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await page.getByRole('button', { name: '实例 1', exact: true }).click();
    await page.getByRole('button', { name: '编辑此候选', exact: true }).click();
    await page.getByLabel('属性值（JSON）', { exact: true }).fill(JSON.stringify({ code: 'SYN-001', name: '审阅后的合成连接器', voltage: 12 }));
    await page.getByRole('button', { name: '保存候选', exact: true }).click();
    await page.getByText('审阅后的合成连接器', { exact: true }).first().waitFor();
    await page.getByRole('button', { name: /^来源 · 第 2 页/ }).click();
    assert.equal(await page.locator('.document-evidence').textContent(), factText);
    phase = 'review-model';
    await page.getByRole('button', { name: '本体候选', exact: true }).click();
    await page.getByRole('button', { name: '编辑模型与映射', exact: true }).click();
    await page.getByLabel('显示名称', { exact: true }).first().fill('合成规格产品');
    await page.getByRole('button', { name: '保存草稿', exact: true }).click();
    await page.getByRole('button', { name: '采纳模型草稿', exact: true }).click();
    phase = 'publish';
    await page.getByRole('button', { name: '验证', exact: true }).click();
    await page.getByRole('button', { name: '批准当前修订', exact: true }).click();
    await page.getByRole('button', { name: '发布已批准修订', exact: true }).click();
    await page.getByText('发布成功，已发布模型已刷新。', { exact: true }).waitFor();
    await page.getByRole('button', { name: '关闭侧栏', exact: true }).click();
    phase = 'import';
    const [imported] = await Promise.all([
      page.waitForResponse(response => response.url().endsWith(`/ai/proposals/${proposalId}/import`) && response.request().method() === 'POST'),
      page.getByRole('button', { name: '导入所选实例', exact: true }).click(),
    ]);
    assert.equal(imported.status(), 200, await imported.text());
    const importResult = await imported.json(); assert.equal(importResult.imported, 1);
    const auth = await (await context.request.get(base + '/api/auth/me')).json();
    const objects = await (await context.request.get(`${base}/api/objects?project_id=${auth.projects[0].id}`)).json();
    assert.equal(objects.items.length, 1); assert.equal(objects.items[0].properties.name, '审阅后的合成连接器');
    phase = 'responsive';
    for (const width of [1536, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      if (width < 700) {
        const panels = page.getByRole('navigation', { name: '文档工作区' });
        await panels.getByRole('button', { name: '资料与页码', exact: true }).click();
        await page.getByRole('button', { name: '查看第 1 页' }).waitFor();
        await panels.getByRole('button', { name: '原文', exact: true }).click();
        await page.locator('.document-page-meta').waitFor();
        await panels.getByRole('button', { name: '理解与候选', exact: true }).click();
        await page.getByRole('button', { name: '本体候选', exact: true }).waitFor();
      }
      await page.evaluate(() => { document.activeElement?.blur(); window.scrollTo(0, 0); });
      await pause(200);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `Overflow at ${width}`);
      const name = `synthetic-workbench-${width}.png`; await page.screenshot({ path: path.join(output, name), fullPage: true }); result.screenshots.push(name);
    }
    await page.setViewportSize({ width: 1536, height: 1000 });
    phase = 'imported-graph-document-evidence';
    await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('button', { name: '图谱探索', exact: true }).click();
    await page.locator('details.node-list').getByRole('button', { name: '审阅后的合成连接器', exact: true }).click();
    await page.getByRole('button', { name: '打开对象表单', exact: true }).click();
    const objectEditor = page.getByRole('complementary', { name: '对象详情', exact: true });
    await objectEditor.getByText('来源证据', { exact: true }).click();
    await objectEditor.getByRole('button', { name: /^查看字段原文/ }).click();
    await objectEditor.getByRole('button', { name: '查看原文 · 第 2 页', exact: true }).click();
    let original = page.getByRole('complementary', { name: '文档来源页', exact: true });
    await original.getByRole('heading', { name: '来源 · 第 2 页', exact: true }).waitFor();
    assert.equal(await original.locator('.document-evidence').textContent(), factText);
    await page.waitForFunction(() => [...document.querySelectorAll('[aria-label="文档来源页"] img')].some(image => image.complete && image.naturalWidth > 0));
    await original.getByRole('button', { name: '关闭侧栏', exact: true }).click();
    await objectEditor.getByRole('button', { name: '关闭侧栏', exact: true }).click();
    result.importedGraphDocumentPage = 2;
    phase = 'ontology-candidate-document-evidence';
    await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('button', { name: '本体模型', exact: true }).click();
    await page.getByRole('button', { name: 'AI 起草本体', exact: true }).click();
    await page.getByLabel('生成记录', { exact: true }).selectOption(proposalId);
    await page.getByRole('button', { name: '实例 1', exact: true }).click();
    await page.getByRole('button', { name: /^来源 · 第 2 页/ }).click();
    original = page.getByRole('complementary', { name: '文档来源页', exact: true });
    await original.getByRole('heading', { name: '来源 · 第 2 页', exact: true }).waitFor();
    assert.equal(await original.locator('.document-evidence').textContent(), factText);
    result.ontologyCandidateDocumentPage = 2;
    assert.deepEqual(result.pageErrors, []);
    result.passed = true; result.modelCalls = modelCalls; result.imported = importResult.imported;
    result.steps = ['settings-save', 'model-probe', 'upload', 'page-selection', 'custom-goal', 'real-worker', 'candidate-edit', 'source-jump', 'model-edit', 'adopt', 'validate', 'approve', 'publish', 'import', 'desktop-mobile-layout', 'imported-graph-document-evidence', 'ontology-candidate-document-evidence'];
  } catch (error) {
    result.failedPhase = phase; result.error = error.stack;
    if (page) { await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {}); result.visibleText = await page.locator('body').innerText().catch(() => ''); }
    throw error;
  } finally {
    await browser?.close();
    for (const process of [...children].reverse()) {
      if (process.exitCode === null && process.signalCode === null) {
        const exited = new Promise(resolve => process.once('exit', resolve));
        process.kill();
        await Promise.race([exited, pause(5000)]);
      }
    }
    for (const log of logs) log.end();
    await new Promise(resolve => modelServer.close(resolve));
    result.childrenStopped = children.every(process => process.exitCode !== null || process.signalCode !== null);
    const resolvedTemp = fs.realpathSync(temp);
    if (result.childrenStopped && resolvedTemp === path.resolve(temp) && path.dirname(resolvedTemp) === temporaryRoot && path.basename(resolvedTemp).startsWith('ontweft-ai-browser-')) {
      for (let attempt = 0; attempt < 10; attempt++) {
        try { fs.rmSync(resolvedTemp, { recursive: true, force: true }); result.temporaryDataRemoved = true; break; }
        catch (error) { result.cleanupError = error.code; await pause(250); }
      }
      if (result.temporaryDataRemoved) delete result.cleanupError;
    }
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify({ passed: result.passed, phase: result.failedPhase, report: path.relative(root, path.join(output, 'result.json')) }));
  }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
