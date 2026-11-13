import { useState } from 'react';
import { DocumentDetails } from './DocumentDetails';

export interface DocumentEvidence { document_id: string; page?: number; page_number?: number; quote?: string; native_parse_id?: string; parse_id?: string }

export function documentEvidence(items: unknown): DocumentEvidence[] {
  if (!Array.isArray(items)) return [];
  const refs = items.filter((item): item is DocumentEvidence => !!item && typeof item === 'object' && typeof item.document_id === 'string' && !!item.document_id && ((Number.isInteger(item.page ?? item.page_number) && (item.page ?? item.page_number) > 0) || (typeof item.parse_id === 'string' && !item.native_parse_id)));
  return [...new Map(refs.map(ref => [`${ref.document_id}:${ref.native_parse_id ?? ref.parse_id ?? 'current'}:${ref.page ?? ref.page_number}:${ref.quote ?? ''}`, ref])).values()];
}

export function DocumentEvidenceLinks({ items }: { items: unknown }) {
  const [selected, setSelected] = useState<DocumentEvidence>();
  const refs = documentEvidence(items);
  return <>{refs.map((ref, index) => <button type="button" className="text-button" key={index} onClick={() => setSelected(ref)} title={ref.quote}>{ref.page ?? ref.page_number ? `查看原文 · 第 ${ref.page ?? ref.page_number} 页` : '查看固定来源版本'}</button>)}
    {selected && <DocumentDetails documentId={selected.document_id} page={selected.page ?? selected.page_number} quote={selected.quote} nativeParseId={selected.native_parse_id} fixedParseId={selected.page ?? selected.page_number ? undefined : selected.parse_id} onClose={() => setSelected(undefined)} />}
  </>;
}
