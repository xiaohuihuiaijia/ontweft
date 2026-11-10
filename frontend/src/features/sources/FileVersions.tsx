import { useState } from 'react';
import { request, write } from '../../shared/api/api';
import { ErrorNotice, Field } from '../../shared/ui/components';
import { useResource } from '../../shared/hooks/hooks';
import type { RecordData } from '../../shared/model/types';

interface Source { id: string; name: string; kind: string; revision: number; config: RecordData; resources?: { resource_id: string; name: string; fields: { name: string; metadata?: { label?: string } }[] }[] }
interface Version { id: string; number: number; applied_revision?: number; preview: Record<string, { added: number; updated: number; unchanged: number; absent: number }>; jobs: string[] }

export function FileVersions({ source, onUpdated }: { source: Source; onUpdated: (value: Source) => void }) {
  const versions = useResource<{ items: Version[] }>(`/sources/${source.id}/versions`);
  const [keys, setKeys] = useState<Record<string, string[]>>(() => source.config.business_keys as Record<string, string[]> ?? {});
  const [file, setFile] = useState<File>(); const [mode, setMode] = useState('upsert');
  const [preview, setPreview] = useState<Version>(); const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>();
  const run = async (action: () => Promise<void>) => { setBusy(true); setError(undefined); try { await action(); } catch (e) { setError(e); } finally { setBusy(false); } };
  return <section className="type-editor"><h3>文件版本与持续更新</h3><ErrorNotice error={error ?? versions.error} />
    {!source.config.business_keys && <><p className="muted">首次采集前，为每张表指定非空、唯一的业务键。行号只记录来源位置。</p>{source.resources?.map(r => <Field key={r.resource_id} label={`${r.name} 的业务键`}><select multiple value={keys[r.resource_id] ?? []} onChange={e => setKeys(current => ({ ...current, [r.resource_id]: Array.from(e.target.selectedOptions, o => o.value) }))}>{r.fields.filter(f => f.name !== '_source_row').map(f => <option key={f.name} value={f.name}>{f.metadata?.label ?? f.name}</option>)}</select></Field>)}<button className="secondary" disabled={busy} onClick={() => void run(async () => { const updated = await write<Source>(`/sources/${source.id}/file-identity`, { expected_revision: source.revision, business_keys: keys }); onUpdated(updated); })}>保存业务键</button></>}
    {!!source.config.business_keys && <form onSubmit={e => { e.preventDefault(); void run(async () => { if (!file) return; const body = new FormData(); body.set('file', file); body.set('expected_revision', String(source.revision)); body.set('mode', mode); setPreview(await request<Version>(`/sources/${source.id}/versions`, { method: 'POST', body })); versions.reload(); }); }}><Field label="更新文件"><input type="file" accept=".csv,.tsv,.xlsx" disabled={busy} onChange={e => { setFile(e.target.files?.[0]); setPreview(undefined); }} /></Field><Field label="更新方式"><select disabled={busy} value={mode} onChange={e => { setMode(e.target.value); setPreview(undefined); }}><option value="upsert">新增与更新（保留未出现记录）</option><option value="replace">完整替换（撤回缺失记录的本来源贡献）</option></select></Field><button disabled={busy || !file}>预览更新</button></form>}
    {preview && <div><h4>版本 {preview.number} · 源记录变化</h4><div className="table-wrap"><table><thead><tr><th>工作表</th><th>新增</th><th>更新</th><th>不变</th><th>缺失</th></tr></thead><tbody>{Object.entries(preview.preview).map(([id, value]) => <tr key={id}><td>{source.resources?.find(r => r.resource_id === id)?.name ?? id}</td><td>{value.added}</td><td>{value.updated}</td><td>{value.unchanged}</td><td>{value.absent}</td></tr>)}</tbody></table></div><p className="muted">{mode === 'replace' ? '完整采集成功后撤回缺失记录的本来源贡献，保留人工关系和其他来源贡献。' : '缺失记录保留；对象以已发布模型与贡献裁决结果为准。'}</p><button disabled={busy} onClick={() => void run(async () => { const result = await write<{ source: Source }>(`/sources/${source.id}/versions/${preview.id}/apply`, { expected_revision: source.revision }); onUpdated(result.source); setPreview(undefined); setFile(undefined); versions.reload(); })}>应用此版本并同步</button></div>}
    <details><summary>保留的文件版本</summary>{versions.data?.items.map(v => <p key={v.id}>版本 {v.number} · {v.applied_revision ? '已应用' : '待应用'} · <a href={`/api/sources/${source.id}/versions/${v.id}/original`}>下载原件</a>{v.jobs.length > 0 && <span className="muted"> · 同步任务可在下方采集记录查看</span>}</p>)}</details>
  </section>;
}
