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
const output = path.join(root, 'var/reports/table-intake-20261110');
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
    page.on('pageerror', error => result.pageErrors.push(error.message));
    await page.goto(base + '/#sources');
    await page.getByRole('textbox', { name: /^用户名/ }).fill('owner');
    await page.getByLabel('密码', { exact: true }).fill(password);
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByLabel('CSV / XLSX 文件', { exact: true }).setInputFiles({ name: '合成清单.csv', mimeType: 'text/csv', buffer: Buffer.from('编号,名称,数量\n001,合成测试 · 输入,2\n002,合成测试 · 确认,3\n') });
    await page.getByLabel('数据源名称（选填）').fill('合成数据 · 通用建模验证');
    const uploadResponse = page.waitForResponse(response => response.url().endsWith('/api/sources/upload') && response.request().method() === 'POST');
    await page.getByRole('button', { name: '上传并查看数据', exact: true }).click();
    const uploaded = await uploadResponse;
    assert.equal(uploaded.status(), 201, await uploaded.text());
    const source = await uploaded.json();
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
    assert.equal(record.snapshot.projection.created, 2);
    await page.getByRole('button', { name: '刷新采集状态' }).click();
    await page.getByText(/对象投影：新增 2/).waitFor();
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
      await page.screenshot({ path: path.join(output, filename), fullPage: true }); result.screenshots.push(filename);
    };
    await capture('table-intake', 1440);
    await capture('table-intake', 375);
    await capture('table-intake', 320);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('button', { name: '图谱探索', exact: true }).click();
    await page.locator('.node-chips').getByRole('button', { name: '合成测试 · 输入', exact: true }).click();
    await page.locator('.graph-inspector').getByRole('heading', { name: '合成测试 · 输入', exact: true }).waitFor();
    await capture('imported-objects', 1440);
    await page.reload();
    const session = await (await context.request.get(base + '/api/auth/me')).json();
    const projectId = session.projects[0].id;
    const objects = await (await context.request.get(`${base}/api/objects?project_id=${projectId}`)).json();
    assert.equal(objects.total, 2);
    assert.ok(objects.items.some(item => item.properties.c1 === '001'));
    assert.deepEqual(result.pageErrors, []);
    result.passed = true; result.objectsCreated = 2; result.customModelSaved = true; result.survivesReload = true;
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
