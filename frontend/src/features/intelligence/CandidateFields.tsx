import { useState, type ReactNode } from 'react';
import { PropertyFields } from '../../shared/model/PropertyFields';
import { parseProperties, propertyInputs } from '../../shared/model/model';
import type { Definition, RecordData } from '../../shared/model/types';
import { ErrorNotice, Field } from '../../shared/ui/components';
import type { Candidate } from './types';

export function CandidateFields({ candidate, definition, busy, error, onSave, onDirty, onClose }: {
  candidate: Candidate; definition: Definition; busy: boolean; error: unknown;
  onSave: (candidate: Candidate) => Promise<void>; onDirty: (dirty: boolean) => void; onClose: () => void;
}) {
  const [typeKey, setTypeKey] = useState(candidate.type_key);
  const [values, setValues] = useState(propertyInputs(candidate.properties));
  const [identityFields, setIdentityFields] = useState(Object.keys(candidate.business_key ?? {}));
  const [localError, setLocalError] = useState<unknown>();
  const type = definition.types.find(item => item.key === typeKey);
  const fields = type?.properties ?? [];
  const save = async () => {
    setLocalError(undefined);
    try {
      const declared = new Set(fields.map(field => field.key));
      const base = Object.fromEntries(Object.entries(candidate.properties).filter(([key]) => declared.has(key)));
      const properties = parseProperties(fields, values, base, 'candidate');
      const keys = identityFields.filter(key => declared.has(key));
      if (candidate.selected !== false && (!keys.length || keys.some(key => properties[key] == null || properties[key] === ''))) throw new Error('请选择有明确且非空值的业务键字段。');
      const business_key = Object.fromEntries(keys.map(key => [key, properties[key]]));
      await onSave({ ...candidate, type_key: typeKey, properties, business_key });
    } catch (failure) { setLocalError(failure); }
  };
  return <div className="drawer-body"><ErrorNotice error={localError ?? error} />
    <Field label="对象类型"><select disabled={busy} value={typeKey} onChange={event => { setTypeKey(event.target.value); onDirty(true); }}>{definition.types.map(item => <option value={item.key} key={item.key}>{item.label}</option>)}</select></Field>
    <p className="muted">逐项核对字段与来源；这里编辑的是候选，尚未覆盖正式对象。</p>
    <PropertyFields fields={fields} values={values} disabled={busy} mode="candidate" onChange={(key, value) => { setValues(current => ({ ...current, [key]: value })); onDirty(true); }} />
    <fieldset><legend>稳定业务键</legend><p className="muted">选择型号、编号等明确身份字段；值与上方属性保持一致，不使用页码或行号。</p>{fields.map(field => <label className="checkbox" key={field.key}><input type="checkbox" aria-label={`业务键 ${field.label}`} disabled={busy} checked={identityFields.includes(field.key)} onChange={event => { setIdentityFields(current => event.target.checked ? [...current, field.key] : current.filter(key => key !== field.key)); onDirty(true); }} />{field.label}</label>)}</fieldset>
    <div className="toolbar"><button type="button" disabled={busy || !type} onClick={() => void save()}>保存候选</button><button type="button" disabled={busy} className="secondary" onClick={onClose}>取消编辑</button></div>
  </div>;
}

export function RelationCandidateFields({ relation, candidates, definition, busy, error, onSave, onDirty, children }: {
  relation: RecordData; candidates: Candidate[]; definition: Definition; busy: boolean; error: unknown;
  onSave: (relation: RecordData) => Promise<void>; onDirty: (dirty: boolean) => void; children?: ReactNode;
}) {
  const [draft, setDraft] = useState(relation);
  const [values, setValues] = useState(propertyInputs((relation.properties as RecordData) ?? {}));
  const [localError, setLocalError] = useState<unknown>();
  const source = candidates.find(candidate => candidate.id === draft.source_candidate_id);
  const type = definition.types.find(item => item.key === source?.type_key);
  const declared = type?.relations.find(item => item.key === draft.key);
  const fields = declared?.properties ?? [];
  const save = async () => {
    try {
      const base = Object.fromEntries(Object.entries((relation.properties as RecordData) ?? {}).filter(([key]) => fields.some(field => field.key === key)));
      await onSave({ ...draft, properties: parseProperties(fields, values, base, 'candidate') });
    } catch (failure) { setLocalError(failure); }
  };
  return <div className="drawer-body"><ErrorNotice error={localError ?? error} />
    {(['source_candidate_id', 'target_candidate_id'] as const).map((field, index) => <Field key={field} label={index ? '目标对象' : '起点对象'}><select disabled={busy} value={String(draft[field])} onChange={event => { setDraft(current => ({ ...current, [field]: event.target.value })); onDirty(true); }}>{candidates.map(candidate => <option key={candidate.id} value={candidate.id}>{String(candidate.properties.name ?? candidate.id)} · {candidate.type_key}</option>)}</select></Field>)}
    <Field label="关系类型"><select disabled={busy} value={String(draft.key)} onChange={event => { setDraft(current => ({ ...current, key: event.target.value })); onDirty(true); }}><option value="">选择已定义关系</option>{type?.relations.map(item => <option key={item.key} value={item.key}>{item.label}</option>)}</select></Field>
    <PropertyFields fields={fields} values={values} disabled={busy} mode="candidate" onChange={(key, value) => { setValues(current => ({ ...current, [key]: value })); onDirty(true); }} />
    {children}<button type="button" disabled={busy || !declared} onClick={() => void save()}>保存关系候选</button>
  </div>;
}
