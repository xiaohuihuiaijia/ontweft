import { useEffect, useState } from 'react';
import { request, write } from '../../shared/api/api';
import { useResource } from '../../shared/hooks/hooks';
import { PropertyFields } from '../../shared/model/PropertyFields';
import { propertyInputs, scalarValue } from '../../shared/model/model';
import type { BusinessObject, ListResult, RecordData, RelationDef } from '../../shared/model/types';
import { ErrorNotice, Field, Loading } from '../../shared/ui/components';
import { DocumentEvidenceLinks } from '../documents/DocumentEvidenceLinks';
import './relation-details.css';

interface RelationRecord {
  id: string; revision: number; source_id: string; target_id: string; label: string;
  properties: RecordData; evidence: RecordData[]; model_release_id: string | null;
  definition: RelationDef | null; reason?: string; operation?: string;
}

const evidenceKey = (ref: RecordData) => JSON.stringify([ref.document_id, ref.parse_id, ref.page, ref.quote, ref.element_path]);
const display = (value: unknown) => value == null ? '空值' : typeof value === 'boolean' ? value ? '是' : '否' : typeof value === 'object' ? JSON.stringify(value) : String(value);

/** One detail editor for both the object drawer and the graph inspector. */
export function RelationDetails({ relationId, canWrite, role, onChanged, onDirty }: {
  relationId: string; canWrite: boolean; role?: string; onChanged: () => void; onDirty?: (value: boolean) => void;
}) {
  const current = useResource<RelationRecord>(`/relations/${encodeURIComponent(relationId)}`);
  const [editing, setEditing] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [reason, setReason] = useState('');
  const [options, setOptions] = useState<RecordData[]>([]);
  const [chosen, setChosen] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [notice, setNotice] = useState('');
  const [showHistory, setShowHistory] = useState(false);
  const [offset, setOffset] = useState(0);
  const history = useResource<ListResult<RelationRecord>>(showHistory ? `/relations/${encodeURIComponent(relationId)}/history?limit=10&offset=${offset}` : null);
  const data = current.data;
  const fields = data?.definition?.properties ?? [];
  const writable = fields.filter(field => !field.source_owned && (!field.write_roles?.length || (!!role && field.write_roles.includes(role))));
  const initial = propertyInputs(data?.properties ?? {});
  const dirty = editing && (reason !== '' || chosen.length > 0 || writable.some(field => (values[field.key] ?? '') !== (initial[field.key] ?? '')));
  useEffect(() => { onDirty?.(dirty || busy); return () => onDirty?.(false); }, [dirty, busy, onDirty]);

  const edit = async () => {
    if (!data) return;
    setValues(initial); setChosen([]); setReason(''); setError(undefined); setNotice(''); setEditing(true);
    setBusy(true);
    try {
      const objects = await Promise.all([...new Set([data.source_id, data.target_id])].map(id => request<BusinessObject>(`/objects/${encodeURIComponent(id)}`)));
      const existing = new Set(data.evidence.map(evidenceKey));
      setOptions([...new Map(objects.flatMap(object => object.evidence as RecordData[]).filter(ref => ref.document_id && !existing.has(evidenceKey(ref))).map(ref => [evidenceKey(ref), ref])).values()]);
    } catch (caught) { setError(caught); } finally { setBusy(false); }
  };
  const save = async () => {
    if (!data) return;
    setBusy(true); setError(undefined);
    try {
      const properties: RecordData = {}; const remove: string[] = [];
      for (const field of writable) {
        const raw = values[field.key] ?? '';
        if (raw === (initial[field.key] ?? '')) continue;
        if (!raw.trim()) remove.push(field.key);
        else properties[field.key] = scalarValue(field, raw);
      }
      await write(`/relations/${encodeURIComponent(relationId)}`, {
        expected_revision: data.revision, expected_model_release_id: data.model_release_id,
        properties, remove_properties: remove, evidence: options.filter(ref => chosen.includes(evidenceKey(ref))), reason,
      }, 'PATCH');
      setEditing(false); setNotice('关系条件已保存；关系的来源支持保持原有规则。');
      current.reload(); history.reload(); onChanged();
    } catch (caught) { setError(caught); } finally { setBusy(false); }
  };

  return <section className="relation-details" aria-label="关系条件与证据">
    <ErrorNotice error={error ?? current.error} onRetry={!editing ? current.reload : undefined} />
    {current.loading ? <Loading /> : data && <>
      <div className="section-heading"><h3>关系条件</h3><span className="muted">修订 {data.revision}</span></div>
      {notice && <p className="notice success" role="status">{notice}</p>}
      {!data.definition && <p className="notice warning">当前模型已无此关系定义，保留历史记录；请先核对模型。</p>}
      {editing ? <>
        <PropertyFields fields={writable} values={values} disabled={busy} onChange={(key, value) => setValues(previous => ({ ...previous, [key]: value }))} />
        <p className="muted">只保存修改的字段；清空已填写字段会移除该条件。不会改变关系两端或增加人工保留支持。</p>
        <Field label="修改说明"><input value={reason} disabled={busy} onChange={event => setReason(event.target.value)} placeholder="例如：核对规格页后确认适用范围" /></Field>
        {options.length > 0 && <fieldset><legend>补充两端对象中已有的授权证据</legend>{options.map(ref => <label className="evidence-option" key={evidenceKey(ref)}><input type="checkbox" disabled={busy} checked={chosen.includes(evidenceKey(ref))} onChange={event => setChosen(previous => event.target.checked ? [...previous, evidenceKey(ref)] : previous.filter(key => key !== evidenceKey(ref)))} /><span>{ref.page ? `第 ${ref.page} 页 · ` : ''}{String(ref.quote ?? '固定文档修订')}<DocumentEvidenceLinks items={[ref]} /></span></label>)}</fieldset>}
        <div className="toolbar"><button type="button" disabled={busy || !dirty} onClick={() => void save()}>保存关系条件</button><button type="button" className="secondary" disabled={busy} onClick={() => { setEditing(false); setError(undefined); }}>取消修改</button></div>
      </> : <>
        {Object.keys(data.properties).length ? <dl className="graph-properties">{Object.entries(data.properties).map(([key, value]) => <div key={key}><dt>{fields.find(field => field.key === key)?.label ?? key}</dt><dd>{display(value)}{fields.find(field => field.key === key)?.unit ? ` ${fields.find(field => field.key === key)?.unit}` : ''}</dd></div>)}</dl> : <p className="muted">尚无已确认的关系条件。</p>}
        {canWrite && data.definition && <button type="button" className="secondary" onClick={() => void edit()}>编辑条件与证据</button>}
      </>}
      <h3>来源证据</h3>
      {data.evidence.length ? data.evidence.map(ref => <blockquote className="relation-evidence" key={evidenceKey(ref)}>{!!ref.quote && <p>{String(ref.quote)}</p>}<DocumentEvidenceLinks items={[ref]} />{!ref.page && <span className="muted">固定文档修订 · {String(ref.revision ?? '')}</span>}</blockquote>) : <p className="muted">当前没有可查看的来源引文。</p>}
      <details onToggle={event => setShowHistory(event.currentTarget.open)}><summary>关系修订历史</summary>
        <ErrorNotice error={history.error} />{history.loading ? <Loading /> : <>{history.data?.items.map(row => <article className="relation-revision" key={row.revision}><strong>修订 {row.revision}{row.operation === 'deleted' ? ' · 删除前状态' : ''}</strong><p className="muted">{row.reason || '保存关系状态'}</p><dl className="graph-properties">{Object.entries(row.properties).map(([key, value]) => <div key={key}><dt>{fields.find(field => field.key === key)?.label ?? key}</dt><dd>{display(value)}</dd></div>)}</dl><DocumentEvidenceLinks items={row.evidence} /></article>)}{history.data?.total === 0 && <p className="muted">此关系尚无记录的修订；旧关系会从首次修改开始保留历史。</p>}{history.data && history.data.total > 10 && <div className="toolbar"><button type="button" className="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 10))}>上一页</button><span>{offset + 1}–{Math.min(offset + 10, history.data.total)} / {history.data.total}</span><button type="button" className="secondary" disabled={offset + 10 >= history.data.total} onClick={() => setOffset(offset + 10)}>下一页</button></div>}</>}
      </details>
    </>}
  </section>;
}
