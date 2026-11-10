// Synthetic local graph benchmark. No credentials, services or production data are reused.
const fs = require('node:fs'); const path = require('node:path'); const os = require('node:os');
const crypto = require('node:crypto'); const net = require('node:net'); const { spawn } = require('node:child_process');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..'); const output = path.join(root, 'var/reports/open-source-readiness-20261109');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const percentile = (values, p) => [...values].sort((a, b) => a - b)[Math.max(0, Math.ceil(values.length * p) - 1)];

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'ontweft-scale-'));
  const reservation = net.createServer(); await new Promise(resolve => reservation.listen(0, '127.0.0.1', resolve));
  const port = reservation.address().port; await new Promise(resolve => reservation.close(resolve));
  const password = crypto.randomBytes(24).toString('base64url'); const base = `http://127.0.0.1:${port}`;
  const python = process.env.ONTOLOGY_TEST_PYTHON || path.join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('ONTOLOGY_')));
  const server = spawn(python, ['scripts/graph_scale_preview.py'], { cwd: root, windowsHide: true,
    env: { ...env, PYTHONUTF8: '1', ONTOLOGY_PREVIEW_DATA_DIR: temp, ONTOLOGY_PREVIEW_PORT: String(port), ONTOLOGY_PREVIEW_PASSWORD: password }, stdio: ['ignore', 'pipe', 'pipe'] });
  const log = fs.createWriteStream(path.join(output, 'graph-scale-server.log'));
  server.stdout.pipe(log, { end: false }); server.stderr.pipe(log, { end: false });
  let browser; let spawnError; server.on('error', error => { spawnError = error; });
  const result = { passed: false, scope: 'Synthetic loopback SQLite, single owner, software WebGL; not the million-record production benchmark',
    environment: { platform: process.platform, osRelease: os.release(), cpu: os.cpus()[0]?.model, logicalCpus: os.cpus().length, memoryGiB: Math.round(os.totalmem() / 2 ** 30), node: process.version },
    sourceSha256: Object.fromEntries(['scripts/graph_scale_preview.py', 'scripts/verify_graph_scale.cjs', 'backend/ontology/core/domain.py', 'backend/ontology/objects/api.py', 'frontend/src/features/graph/GraphPage.tsx', 'frontend/src/features/graph/self-loop-layer.ts'].map(file => [file, crypto.createHash('sha256').update(fs.readFileSync(path.join(root, file))).digest('hex')])),
    timingMethod: 'API: one discarded warmup plus 20 samples per budget. Selection: DOM click to updated inspector plus animation frame, not INP. Worker and long-task observations reset between budgets.',
    seeded: { nodes: 501, edges: 1100 }, api: {}, browser: {}, pageErrors: [] };
  try {
    let ready = false;
    for (let i = 0; i < 180; i++) {
      if (spawnError) throw spawnError;
      if (server.exitCode !== null) throw new Error('Synthetic server exited; inspect graph-scale-server.log');
      try { ready = (await fetch(`${base}/api/health`)).ok; } catch {}
      if (ready) break; await pause(250);
    }
    assert.ok(ready, 'Synthetic server did not become ready');
    browser = await chromium.launch({ headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
    result.environment.chromium = browser.version();
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
    const login = await context.request.post(`${base}/api/auth/login`, { data: { username: 'owner', password } }); assert.ok(login.ok());
    const identity = await (await context.request.get(`${base}/api/auth/me`)).json(); const project = identity.projects[0].id;
    for (const limit of [200, 500]) {
      const services = []; const roundTrips = []; const sql = []; let graph;
      for (let sample = 0; sample < 21; sample++) {
        const start = performance.now(); const response = await context.request.get(`${base}/api/graph?project_id=${project}&limit=${limit}`);
        assert.ok(response.ok()); graph = await response.json(); const elapsed = performance.now() - start;
        assert.equal(graph.nodes.length, limit); assert.ok(graph.edges.length <= 1000); assert.equal(graph.truncated, true);
        const ids = new Set(graph.nodes.map(node => node.id)); assert.ok(graph.edges.every(edge => ids.has(edge.source) && ids.has(edge.target)));
        if (sample > 0) { services.push(Number(response.headers()['x-synthetic-service-ms'])); roundTrips.push(elapsed); sql.push(Number(response.headers()['x-synthetic-sql-count'])); }
      }
      result.api[limit] = { samples: 20, nodes: graph.nodes.length, edges: graph.edges.length, truncated: graph.truncated,
        serviceMedianMs: percentile(services, .5), serviceP95Ms: percentile(services, .95), roundTripP95Ms: Math.round(percentile(roundTrips, .95)), maxSqlStatements: Math.max(...sql) };
    }
    const page = await context.newPage(); page.on('pageerror', error => result.pageErrors.push(error.message));
    await page.addInitScript(() => {
      window.__graphScale = { workers: [], longTasks: [] };
      if (PerformanceObserver.supportedEntryTypes.includes('longtask')) new PerformanceObserver(list => { for (const entry of list.getEntries()) window.__graphScale.longTasks.push(entry.duration); }).observe({ type: 'longtask', buffered: true });
      const NativeWorker = window.Worker;
      window.Worker = class extends NativeWorker {
        postMessage(...args) { const started = performance.now(); this.addEventListener('message', () => window.__graphScale.workers.push(performance.now() - started), { once: true }); super.postMessage(...args); }
      };
    });
    await page.goto(base); await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('button', { name: '图谱探索', exact: true }).click();
    await page.waitForFunction(() => { const button = document.querySelector('[aria-label="放大图谱"]'); return button && !button.disabled; });
    const measure = async (size) => {
      const nodes = page.locator('details.node-list').first();
      assert.equal(await nodes.evaluate(element => element.open), false, 'Large node list should start collapsed');
      await nodes.locator('summary').click();
      assert.equal(await nodes.evaluate(element => element.open), true, 'Keyboard list remains available');
      const stats = await page.evaluate(async () => {
        const times = [];
        for (let index = 0; index < 20; index++) {
          const buttons = [...document.querySelectorAll('.node-list .node-chips button')]; const button = buttons[(index * 7) % Math.min(100, buttons.length)];
          const label = button.textContent; const started = performance.now(); button.click();
          await new Promise((resolve, reject) => {
            const timeout = setTimeout(() => reject(new Error('Node selection did not update the inspector')), 5000);
            const check = () => { if (document.querySelector('.graph-inspector h2')?.textContent === label) requestAnimationFrame(() => { clearTimeout(timeout); resolve(); }); else requestAnimationFrame(check); }; check();
          }); times.push(performance.now() - started);
        }
        const stats = { times, workers: window.__graphScale.workers, longTasks: window.__graphScale.longTasks, selfLoops: document.querySelectorAll('.graph-self-loop').length };
        window.__graphScale.workers = []; window.__graphScale.longTasks = [];
        return stats;
      });
      result.browser[size] = { selectionSamples: stats.times.length, selectionMedianMs: Math.round(percentile(stats.times, .5)), selectionP95Ms: Math.round(percentile(stats.times, .95)), layoutWorkerMs: stats.workers.map(Math.round), longTasksOver50ms: stats.longTasks.length, maxLongTaskMs: Math.round(Math.max(0, ...stats.longTasks)), selfLoops: stats.selfLoops };
      await nodes.locator('summary').click();
    };
    await measure(200);
    if (await page.getByLabel('图谱节点预算', { exact: true }).count()) {
      await page.getByLabel('图谱节点预算', { exact: true }).selectOption('500');
      await page.waitForFunction(() => document.querySelector('.graph-caption')?.textContent.includes('当前显示 500 个节点') && !document.querySelector('[aria-label="放大图谱"]')?.disabled);
      await measure(500);
    }
    await page.getByRole('textbox', { name: '在当前子图查找', exact: true }).fill('合成资产 0000');
    await page.waitForFunction(() => document.querySelector('.graph-caption')?.textContent.includes('当前显示 1 个节点'));
    result.filterSingleNode = true;
    await page.getByRole('button', { name: '重置筛选', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.graph-caption')?.textContent.includes('当前显示 500 个节点'));
    result.responsive = {};
    for (const width of [1440, 375]) {
      await page.setViewportSize({ width, height: 1000 }); await page.evaluate(() => window.scrollTo(0, 0));
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      assert.equal(await page.locator('details.node-list').first().evaluate(element => element.open), false);
      result.responsive[width] = await page.evaluate(() => ({ documentHeight: document.documentElement.scrollHeight, viewportWidth: innerWidth, horizontalOverflow: document.documentElement.scrollWidth > innerWidth }));
      await page.screenshot({ path: path.join(output, `graph-scale-${width}.png`), fullPage: true });
    }
    assert.deepEqual(result.pageErrors, []); result.passed = true;
  } catch (error) { result.error = String(error); process.exitCode = 1; }
  finally {
    if (browser) await browser.close();
    if (server.exitCode === null) { server.kill(); await Promise.race([new Promise(resolve => server.once('exit', resolve)), pause(5000)]); }
    log.end();
    const parent = fs.realpathSync(os.tmpdir());
    if (server.exitCode !== null && path.dirname(fs.realpathSync(temp)) === parent && path.basename(temp).startsWith('ontweft-scale-')) fs.rmSync(temp, { recursive: true, force: true });
    fs.writeFileSync(path.join(output, 'graph-scale-result.json'), JSON.stringify(result, null, 2)); console.log(JSON.stringify(result));
  }
}
main().catch(error => { console.error(String(error)); process.exitCode = 1; });
