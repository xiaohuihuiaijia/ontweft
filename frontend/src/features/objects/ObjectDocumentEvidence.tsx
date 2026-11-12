import { useState } from 'react';
import { useResource } from '../../shared/hooks/hooks';
import { ErrorNotice, Loading } from '../../shared/ui/components';
import { documentEvidence, DocumentEvidenceLinks } from '../documents/DocumentEvidenceLinks';

interface SourceReference { source_record_id: string; fields: string[] }

export function ObjectDocumentEvidence({ evidence, visibleFields, canReadContributions }: { evidence: unknown[]; visibleFields: string[]; canReadContributions: boolean }) {
  const records = evidence.filter((ref): ref is SourceReference => !!ref && typeof ref === 'object' && 'source_record_id' in ref && typeof ref.source_record_id === 'string' && 'fields' in ref && Array.isArray(ref.fields));
  return <><DocumentEvidenceLinks items={evidence} />{canReadContributions && records.map(ref => {
    const fields = ref.fields.filter(field => visibleFields.includes(field));
    return fields.length ? <SourceRecordEvidence key={`${ref.source_record_id}:${fields.join(',')}`} recordId={ref.source_record_id} fields={fields} /> : null;
  })}</>;
}

function SourceRecordEvidence({ recordId, fields }: { recordId: string; fields: string[] }) {
  const [open, setOpen] = useState(false);
  const record = useResource<{ values: { __ontweft_field_evidence?: Record<string, unknown> } }>(open ? `/source-records/${encodeURIComponent(recordId)}` : null);
  const refs = documentEvidence(fields.flatMap(field => documentEvidence(record.data?.values.__ontweft_field_evidence?.[field])));
  return <div><button type="button" className="text-button" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? '收起字段原文' : '查看字段原文'}（{fields.join('、')}）</button>
    {open && <><ErrorNotice error={record.error} onRetry={record.reload} />{record.loading ? <Loading /> : record.data && (refs.length ? <DocumentEvidenceLinks items={refs} /> : <p className="muted">此来源记录未保存这些字段的文档页证据。</p>)}</>}
  </div>;
}
