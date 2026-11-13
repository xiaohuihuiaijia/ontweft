// Record a continuous, real browser walkthrough of the shared synthetic sample.
// Run after the frontend build with the same Playwright setup as capture_showcase.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

const captions = [
  [0, 6, '序枢 Ontweft：产品、库存与适用条件\n真实应用 · 人工编排合成资料 · 无模型调用'],
  [6, 14, '1. 选择 SYN-04，查看型号与芯数'],
  [14, 22, '沿库存关系查看华东仓 A01：40 件'],
  [22, 32, '2. 打开适用关系：室内干燥环境，电压不超过 24 V'],
  [32, 42, '3. 回查固定来源版本，核对这条条件的依据'],
  [42, 57, '在关系上修改适用条件，并填写人工修改说明'],
  [57, 63, '保存后读取真实结果；关系端点和原有证据保持不变'],
  [63, 75, '查看关系修订历史：修改前后的条件都能回查\n从无 Key 样例开始，再接入自己的资料'],
];
const timestamp = value => `00:${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')},000`;

async function recordExperience({ browser, context, base, project, initialized }) {
  const output = path.join(root, 'var/reports/first-experience');
  fs.mkdirSync(output, { recursive: true });
  const videoContext = await browser.newContext({
    storageState: await context.storageState(), viewport: { width: 1600, height: 1200 },
    deviceScaleFactor: 1, reducedMotion: 'reduce',
    recordVideo: { dir: path.join(output, 'raw'), size: { width: 1600, height: 1200 } },
  });
  const page = await videoContext.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const started = Date.now();
  const at = async second => pause(Math.max(0, started + second * 1000 - Date.now()));
  const stage = async () => page.evaluate(() => window.scrollTo(0, 0));
  let videoPath;
  try {
    await page.goto(`${base}/#graph?project=${encodeURIComponent(project)}`);
    await page.waitForFunction(() => document.querySelector('.graph-caption')?.textContent.includes('20 个节点'));
    await page.getByRole('button', { name: '适配可见图', exact: true }).click();
    const inspector = page.getByRole('complementary', { name: '图谱详情', exact: true });
    const nodes = page.locator('details.node-list').first();

    await at(6);
    await nodes.getByRole('button', { name: 'SYN-04 · 4 芯', exact: true }).click();
    await inspector.getByRole('heading', { name: 'SYN-04 · 4 芯', exact: true }).waitFor();
    await stage();
    await at(14);
    await inspector.getByRole('button', { name: '华东仓 · A01', exact: true }).click();
    await inspector.getByRole('heading', { name: '华东仓 · A01', exact: true }).waitFor();
    await inspector.getByText('40', { exact: true }).waitFor();
    await stage();
    await at(22);
    await inspector.getByRole('button', { name: 'SYN-04 · 4 芯', exact: true }).click();
    const relationRow = inspector.locator('.relationship-list li').filter({
      has: page.getByRole('button', { name: '室内传感采集', exact: true }),
    });
    await relationRow.getByRole('button', { name: /适用场景/ }).click();
    const detail = page.getByRole('region', { name: '关系条件与证据', exact: true });
    await detail.getByText('24 V', { exact: true }).waitFor();
    await stage();
    await at(32);
    await detail.getByRole('button', { name: '查看固定来源版本', exact: true }).click();
    const source = page.getByRole('complementary', { name: '固定历史解析版本', exact: true });
    await source.getByText('product-inventory-v1.md', { exact: true }).waitFor();
    await source.getByText(/^SYN-04 用于室内传感采集，限室内干燥环境/).scrollIntoViewIfNeeded();
    await at(42);
    await source.getByRole('button', { name: '关闭侧栏', exact: true }).click();
    await detail.getByRole('button', { name: '编辑条件与证据', exact: true }).click();
    await page.getByLabel('适用条件', { exact: true }).waitFor();
    await page.getByLabel('适用条件', { exact: true }).scrollIntoViewIfNeeded();
    await at(47);
    await page.getByLabel('适用条件', { exact: true }).fill('室内干燥环境 · 已人工复核');
    await page.getByLabel('修改说明', { exact: true }).fill('演示：人工核对合成资料后确认适用环境');
    await at(57);
    await detail.getByRole('button', { name: '保存关系条件', exact: true }).click();
    await detail.getByText('室内干燥环境 · 已人工复核', { exact: true }).waitFor();
    await detail.getByText('关系条件已保存；关系的来源支持保持原有规则。', { exact: true }).waitFor();
    await at(63);
    await detail.locator('summary', { hasText: '关系修订历史' }).click();
    await detail.locator('.relation-revision').getByText('室内干燥环境 · 合成演示', { exact: true }).waitFor();
    await detail.locator('.relation-revision').getByText('室内干燥环境 · 已人工复核', { exact: true }).waitFor();
    await detail.locator('details').scrollIntoViewIfNeeded();
    await at(75);
    assert.deepEqual(errors, []);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    const edgeId = initialized.relation_ids['suits:product-0:scenario-0'];
    const actual = await videoContext.request.get(`${base}/api/relations/${edgeId}`);
    assert.equal(actual.status(), 200);
    const record = await actual.json();
    assert.equal(record.properties.condition, '室内干燥环境 · 已人工复核');
    assert.equal(record.revision, 2);
    assert.ok(record.evidence.length);
  } finally {
    const video = page.video();
    await videoContext.close();
    videoPath = await video.path();
  }
  const srt = captions.map(([start, end, text], index) => `${index + 1}\n${timestamp(start)} --> ${timestamp(end)}\n${text}\n`).join('\n');
  const subtitlePath = path.join(output, 'ontweft-first-experience.zh-CN.srt');
  fs.writeFileSync(subtitlePath, srt, 'utf8');
  fs.writeFileSync(path.join(output, 'ontweft-first-experience.zh-CN.vtt'), 'WEBVTT\n\n' + srt.replace(/(\d\d:\d\d:\d\d),/g, '$1.'), 'utf8');
  const target = path.join(output, 'ontweft-first-experience.mp4');
  const ffmpeg = process.env.FFMPEG_BIN || 'ffmpeg';
  const converted = spawnSync(ffmpeg, ['-y', '-i', videoPath, '-i', subtitlePath, '-c:v', 'libx264', '-preset', 'medium', '-crf', '20', '-pix_fmt', 'yuv420p', '-c:s', 'mov_text', '-metadata:s:s:0', 'language=zho', '-disposition:s:0', 'default', '-movflags', '+faststart', target], { windowsHide: true, encoding: 'utf8', maxBuffer: 1024 * 1024 });
  assert.equal(converted.status, 0, converted.stderr || String(converted.error));
  const ffprobe = process.env.FFPROBE_BIN || 'ffprobe';
  const probed = spawnSync(ffprobe, ['-v', 'error', '-show_entries', 'format=duration,size:stream=codec_name,width,height', '-of', 'json', target], { windowsHide: true, encoding: 'utf8' });
  assert.equal(probed.status, 0, probed.stderr || String(probed.error));
  const metadata = JSON.parse(probed.stdout);
  const duration = Number(metadata.format.duration);
  assert.ok(duration >= 60 && duration <= 90, `Walkthrough duration ${duration}s is outside 60–90 seconds`);
  const sourceFiles = ['examples/product-inventory/sample.json', 'examples/product-inventory/source.md', 'scripts/experience_sample.py', 'scripts/capture_showcase.cjs', 'scripts/record_first_experience.cjs', ...fs.readdirSync(path.join(root, 'frontend/dist/assets')).filter(name => /\.(js|css)$/.test(name)).map(name => `frontend/dist/assets/${name}`)];
  const sha256 = Object.fromEntries(sourceFiles.map(file => [file, crypto.createHash('sha256').update(fs.readFileSync(path.join(root, file))).digest('hex')]));
  const report = { releaseVersion: 'v0.1.0-beta.1', format: 'continuous actual browser recording', synthetic: true, modelCalls: 0, objects: initialized.objects, relations: initialized.relations, viewport: { width: 1600, height: 1200 }, durationSeconds: duration, bytes: Number(metadata.format.size), audio: 'none', subtitles: 'Chinese, optional MP4 text track and sidecar SRT/VTT', sourceCommit: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(), workingTree: 'includes current branch changes, exact source/build hashes recorded below', sha256, verification: { productAndInventory: true, relationConditions: true, fixedSource: true, propertyEdit: true, persistedRevision: 2, revisionHistory: true, pageErrors: errors }, rawVideo: path.relative(root, videoPath).replaceAll('\\', '/'), video: path.relative(root, target).replaceAll('\\', '/') };
  fs.writeFileSync(path.join(output, 'recording.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(`Recorded continuous ${duration.toFixed(1)}s walkthrough; all displayed operations used the actual application.`);
  return report;
}

module.exports = { recordExperience };
if (require.main === module) require('./capture_showcase.cjs').captureShowcase({ record: true }).catch(error => { console.error(String(error)); process.exitCode = 1; });
