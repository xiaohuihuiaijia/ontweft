import { readFile, readdir, mkdir, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const lock = JSON.parse(await readFile(join(root, 'package-lock.json'), 'utf8'));
const sections = ['Third-party notices for the shipped browser runtime. Generated from the installed, locked packages. Development tools are tracked separately in package-lock.json.'];
for (const [path, entry] of Object.entries(lock.packages)) {
  if (!path || entry.dev || entry.devOptional) continue;
  const directory = join(root, path);
  const pkg = JSON.parse(await readFile(join(directory, 'package.json'), 'utf8'));
  const candidates = (await readdir(directory)).filter(name => /^(licen[sc]e|notice|copying)(\.|$)/i.test(name));
  if (!candidates.length) throw new Error(`Missing license file for ${pkg.name}; review before release.`);
  sections.push(`\n${'='.repeat(72)}\n${pkg.name} ${pkg.version}\nDeclared license: ${pkg.license}\n${pkg.repository?.url ?? ''}\n`);
  for (const filename of candidates) sections.push(`${filename}\n${await readFile(join(directory, filename), 'utf8')}`);
}
await mkdir(join(root, 'public'), { recursive: true });
await writeFile(join(root, 'public', 'THIRD_PARTY_NOTICES.txt'), sections.join('\n'), 'utf8');
console.log('Wrote runtime third-party notices from package-lock.json.');
