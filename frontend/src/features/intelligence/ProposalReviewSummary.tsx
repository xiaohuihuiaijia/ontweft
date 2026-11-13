import type { ReactNode } from 'react';
import type { Definition } from '../../shared/model/types';
import type { Evidence, ModelNote, ProposalImport } from './types';

export function ModelNotes({ notes, definition, renderEvidence }: { notes?: ModelNote[]; definition: Definition; renderEvidence: (items: Evidence[]) => ReactNode }) {
  return <details className="ai-model-notes"><summary>为什么这样建模 · {notes?.length ?? 0} 条说明</summary>
    <p className="muted">这是生成时的建模建议。引文已匹配对应解析文本；仍需对照原页核对事实。</p>
    {!notes?.length ? <p className="muted">这份提案没有记录结构化建模说明，可继续按现有模型和证据审阅；不会为补说明重新调用模型。</p> : notes.map((note, index) => {
      const type = definition.types.find(item => item.key === note.type_key);
      const target = note.target_kind === 'type' ? type : (note.target_kind === 'property' ? type?.properties : type?.relations)?.find(item => item.key === note.key);
      return <article className="ai-candidate" key={`${note.target_kind}:${note.type_key}:${note.key}:${index}`}><h4>{target?.label ?? note.key ?? note.type_key} <span className="badge">{{ added: '新增', modified: '扩展', reused: '复用' }[note.change]}</span></h4>
        <p><strong>回答的问题：</strong>{note.question}</p><p><strong>选择理由：</strong>{note.reason}</p>
        {note.stale && <p className="notice warning">当前定义已修改或移除；这条说明仅反映生成时的选择，不能代替当前修订的审阅。</p>}
        {note.needs_review && <p className="notice warning">依据待复核：没有匹配到本次所选材料中的有效引用。</p>}
        {renderEvidence(note.evidence.filter(item => item.document_id))}
        {note.evidence.filter(item => item.source_id).map((item, i) => <p className="muted" key={i}>所选数据资源：{item.resource_id} · 字段：{Array.isArray(item.fields) ? item.fields.join('、') : ''}</p>)}
      </article>;
    })}
  </details>;
}

export function ImportResult({ result, onRelation }: { result?: ProposalImport; onRelation?: (id: string) => void }) {
  if (!result || result.imported === undefined) return null;
  const blocked = (result.counts?.blocked ?? 0) + (result.relations_blocked ?? 0);
  return <section className="ai-import-result" aria-label="实例导入结果"><h3>{blocked ? '部分导入完成' : '导入结果'}</h3>
    {result.counts ? <p>新增 {result.counts.created} · 更新 {result.counts.updated} · 未变化 {result.counts.unchanged} · 对象阻塞 {result.counts.blocked}</p> : <p>已导入 {result.imported} 个实例</p>}
    <p>已确认关系 {result.relations ?? 0} · 关系待处理 {result.relations_blocked ?? 0}</p>
    {!!result.issues?.length && <><p className="muted">以下问题已随提案保留。已有值没有被冲突候选覆盖；请在来源贡献或关系详情核对处理。重复点击导入不会重跑这份已完成提案。</p><ul>{result.issues.map((issue, index) => <li key={index}>{issue.message ?? issue.reason ?? '记录需要进一步核对'}{issue.candidate_id && <small> · 候选 {issue.candidate_id}</small>}{issue.relation_id && onRelation && <button type="button" className="text-button" onClick={() => onRelation(issue.relation_id!)}>查看关系</button>}</li>)}</ul></>}
  </section>;
}
