import { useEffect, useRef, useState } from 'react';
import { request, write } from '../../shared/api/api';
import type { Definition, RecordData } from '../../shared/model/types';
import { ErrorNotice, ExactJson, Field } from '../../shared/ui/components';
import { OntologyEditor } from '../modeling/OntologyPage';
import './source-intake.css';

export interface IntakeResource {
  resource_id: string; name: string; primary_key: string[];
  fields: { name: string; data_type: string; nullable: boolean; metadata?: { label?: string } }[];
  foreign_keys: unknown[];
  metadata?: { row_count?: number; blank_rows_skipped?: number; formula_cells_as_text?: number; identity_scope?: string };
}
export interface IntakeSource { id: string; name: string; kind: string; revision: number; config: RecordData; resources?: IntakeResource[] }
interface Proposal { definition: Definition; baseline_release_id: string | null; warnings: string[]; resources: { resource_id: string; label: string; type_key: string }[] }

export function UploadedTablePreview({ resource, sample }: { resource: IntakeResource; sample: RecordData }) {
  const rows = Array.isArray(sample.records) ? sample.records as { values: RecordData }[] : [];
  return <section className="upload-table-preview"><h4>原文件数据预览</h4><p className="muted">最多展示 20 行原始值。— 表示缺失；长文本可在下方精确记录中展开查看。</p>
    <div className="table-wrap"><table aria-label="上传表格样本"><thead><tr>{resource.fields.map(field => <th key={field.name}>{field.metadata?.label ?? field.name}</th>)}</tr></thead>
      <tbody>{rows.slice(0, 20).map((row, i) => <tr key={i}>{resource.fields.map(field => <td key={field.name}><span>{row.values[field.name] == null ? '—' : String(row.values[field.name])}</span></td>)}</tr>)}</tbody></table></div>
    <details><summary>展开精确记录与来源信息</summary><ExactJson text={sample.sample_json} label="来源样本" /></details>
  </section>;
}

export function UploadTables({ projectId, disabled, onUploaded, onDirty, onBusy }: {
  projectId: string; disabled: boolean; onUploaded: (source: IntakeSource) => void;
  onDirty: (dirty: boolean) => void; onBusy: (busy: boolean) => void;
}) {
  const [file, setFile] = useState<File>();
  const [name, setName] = useState('');
  const [encoding, setEncoding] = useState('utf-8-sig');
  const [delimiter, setDelimiter] = useState(',');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const form = useRef<HTMLFormElement>(null);
  useEffect(() => { onDirty(!!file || !!name); return () => onDirty(false); }, [file, name, onDirty]);
  useEffect(() => { onBusy(busy); return () => onBusy(false); }, [busy, onBusy]);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!file) return;
    if (!/\.(csv|xlsx)$/i.test(file.name) || file.size > 10 * 1024 * 1024 || !file.size) {
      setError(new Error('请选择不超过 10 MiB 的非空 CSV 或 XLSX 文件。')); return;
    }
    setBusy(true); setError(undefined);
    const data = new FormData();
    data.set('project_id', projectId); data.set('name', name); data.set('encoding', encoding);
    data.set('delimiter', delimiter); data.set('file', file);
    try {
      const source = await request<IntakeSource>('/sources/upload', { method: 'POST', body: data });
      setFile(undefined); setName(''); form.current?.reset(); onUploaded(source);
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <section className="source-intake" aria-labelledby="source-intake-title">
    <div className="source-intake-intro"><span className="intake-kicker">从你的数据开始</span><h2 id="source-intake-title">一份表格，一个可编辑的起点。</h2><p>上传自己的数据，确认业务含义，再发布为本体。也可登记数据库连接，复用相同的建模流程。</p>
      <ol className="intake-steps"><li>上传或连接</li><li>确认类型与字段</li><li>审批发布</li><li>导入并探索</li></ol>
    </div>
    <form ref={form} onSubmit={submit} className="source-intake-form">
      <ErrorNotice error={error} />
      <fieldset disabled={disabled || busy}><legend>上传表格</legend>
        <Field label="CSV / XLSX 文件" hint="最多 10 MiB、每表 10,000 行 / 200 列、总计 200,000 个单元格。每张工作表须有唯一列名与数据行。">
          <input type="file" aria-label="CSV / XLSX 文件" accept=".csv,.xlsx" required onChange={e => setFile(e.target.files?.[0])} />
        </Field>
        <Field label="数据源名称（选填）"><input maxLength={200} value={name} placeholder="默认使用文件名" onChange={e => setName(e.target.value)} /></Field>
        <details><summary>CSV 编码与分隔符</summary><div className="form-grid">
          <Field label="CSV 字符编码"><select value={encoding} onChange={e => setEncoding(e.target.value)}><option value="utf-8-sig">UTF-8（含 BOM）</option><option value="gb18030">GB18030 / 中文 Windows</option><option value="utf-16">UTF-16</option></select></Field>
          <Field label="CSV 分隔符"><select value={delimiter} onChange={e => setDelimiter(e.target.value)}><option value=",">逗号</option><option value=";">分号</option><option value={'\t'}>制表符</option></select></Field>
        </div></details>
        <p className="muted">原文件会保留；空单元格视为缺失，公式保留为文本。每次上传是独立来源，不会自动覆盖或合并已有对象。</p>
        <button disabled={!file || busy}>{busy ? '读取并保留原文件…' : '上传并查看数据'}</button>
      </fieldset>
    </form>
  </section>;
}

export function SourceModelBuilder({ source, projectId, can, disabled, onDirty, onBusy }: {
  source: IntakeSource; projectId: string; can: (permission: string) => boolean; disabled: boolean;
  onDirty: (dirty: boolean) => void; onBusy: (busy: boolean) => void;
}) {
  const resources = source.resources ?? [];
  const [selected, setSelected] = useState<string[]>([]);
  const [proposal, setProposal] = useState<Proposal>();
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [published, setPublished] = useState(false);
  useEffect(() => { onBusy(busy || !!proposal); return () => onBusy(false); }, [busy, proposal, onBusy]);
  const prepare = async () => {
    setBusy(true); setError(undefined); setPublished(false);
    try { setProposal(await write<Proposal>(`/sources/${encodeURIComponent(source.id)}/model-proposal`, { expected_source_revision: source.revision, resource_ids: selected })); }
    catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <section className="source-model-builder" aria-labelledby="source-builder-title">
    <div className="section-heading"><div><span className="intake-kicker">确认后再建模</span><h3 id="source-builder-title">把来源结构变成业务类型</h3></div>{source.kind === 'tabular' && <a className="text-button" href={`/api/sources/${encodeURIComponent(source.id)}/original`} download>下载原文件</a>}</div>
    <p className="muted">选择需要的表，生成名称、字段类型和来源映射。已有发布模型会保留；业务关系和约束由你确认，不根据相似列名猜测。</p>
    <ErrorNotice error={error} />
    {published && <div className="notice success" role="status">模型已发布。关闭编辑器后，在下方选择对应资源并“采集所选资源快照”；完成后可在对象与图谱中查看实际数据。</div>}
    <fieldset disabled={disabled || busy || !!proposal}><legend>选择建模资源（最多 32 张表）</legend>
      <div className="intake-resource-grid">{resources.map(resource => <label className="intake-resource" key={resource.resource_id}>
        <input type="checkbox" checked={selected.includes(resource.resource_id)} disabled={!resource.primary_key.length || (!selected.includes(resource.resource_id) && selected.length >= 32)} onChange={e => setSelected(values => e.target.checked ? [...values, resource.resource_id] : values.filter(id => id !== resource.resource_id))} />
        <span><strong>{resource.name}</strong><small>{resource.metadata?.row_count !== undefined ? `${resource.metadata.row_count.toLocaleString()} 行 · ` : ''}{resource.fields.filter(f => f.name !== '_source_row').length} 个字段{!resource.primary_key.length ? ' · 须先声明稳定主键' : ''}</small>
          {!!resource.metadata?.formula_cells_as_text && <small>含 {resource.metadata.formula_cells_as_text} 个公式单元格，按文本保留</small>}
          {!!resource.metadata?.blank_rows_skipped && <small>忽略 {resource.metadata.blank_rows_skipped} 个空白行，保留原始行号</small>}</span>
      </label>)}</div>
      <button className="secondary" disabled={!selected.length || busy} onClick={prepare}>{busy ? '生成建议…' : '生成可编辑建模建议'}</button>
    </fieldset>
    {proposal && !editing && <div className="intake-proposal" role="region" aria-label="建模建议预览">
      <h4>本次新增 {proposal.resources.length} 个类型</h4><p>{proposal.resources.map(resource => resource.label).join('、')}</p>
      <ul>{proposal.warnings.map(warning => <li key={warning}>{warning}</li>)}</ul>
      <div className="toolbar"><button onClick={() => setEditing(true)}>编辑模型与来源映射</button><button className="secondary" onClick={() => setProposal(undefined)}>返回选择</button></div>
    </div>}
    {proposal && editing && <OntologyEditor projectId={projectId} can={can} onDirty={onDirty} initialDefinition={proposal.definition} initialBaselineReleaseId={proposal.baseline_release_id}
      onClose={() => { setEditing(false); setProposal(undefined); }} onUpdated={isPublished => { if (isPublished) setPublished(true); }} />}
  </section>;
}
