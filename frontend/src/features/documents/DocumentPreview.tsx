import { useState } from 'react';
import { JsonView, Status } from '../../shared/ui/components';
import type { RecordData } from '../../shared/model/types';

interface Cell { row: number; column: number; row_span: number; column_span: number; text: string }
interface Element { element_id: string; kind: string; text: string; level?: number; evidence: RecordData; provenance?: RecordData[]; cells?: Cell[]; rows?: number; metadata?: RecordData }
interface IR { format?: string; complete: boolean; quality: string; parser: string; parser_version: string; warnings: string[]; pages: { page_number: number; status: string; reason?: string }[]; elements: Element[]; source_elements?: Element[] }

function tableRows(element: Element): number[] {
  if (!element.cells?.length || element.cells.length > 300) return [];
  const count = element.rows ?? Math.max(...element.cells.map(cell => cell.row + (cell.row_span || 1)));
  return Number.isInteger(count) && count > 0 && count <= 300 ? Array.from({ length: count }, (_, row) => row) : [];
}

export function DocumentPreview({ value, compact = false }: { value: unknown; compact?: boolean }) {
  const [selected, setSelected] = useState<Element>();
  if (!value || typeof value !== 'object' || !Array.isArray((value as IR).elements)) return <p className="muted">尚无结构化解析结果，请查看任务状态。</p>;
  const ir = value as IR;
  const native = ir.format === 'feishu_docx';
  return <>
    {!compact && <div className="inline-info"><span>{ir.parser} · {ir.parser_version}</span><Status value={ir.complete ? 'succeeded' : 'partial'} /></div>}
    {native && <p className="notice">飞书原生块快照：按文档修订与块标识定位；图片、附件和关联文档仅保留引用，需分别接入后才能查看其内容。</p>}
    {ir.warnings?.map((warning, i) => <div className="notice warning" key={i}>{warning}</div>)}
    {!native && !compact && <details><summary>逐页处理状态（{ir.pages?.length ?? 0} 页）</summary>{ir.pages?.map(page => <p key={page.page_number}>第 {page.page_number} 页 · {page.status} {page.reason}</p>)}</details>}
    {ir.elements.length > 200 && <p className="notice warning">当前预览前 200 个结构元素；原件保留完整内容。</p>}
    <div className="document-preview">{ir.elements.slice(0, 200).map(element => <article className="document-element" key={element.element_id}>
      <button type="button" className="text-button element-anchor" onClick={() => setSelected(element)}>来源定位</button>
      {element.metadata?.resource_status === 'not_fetched' ? <p className="notice">关联资源（{String(element.metadata.resource_kind ?? element.kind)}）：仅保存引用，尚未读取内容。</p>
        : element.metadata?.unsupported_block ? <p className="notice warning">尚未解释的块类型 {String(element.metadata.block_type)}；原始数据保留在块快照中。</p>
        : element.metadata?.layout_unresolved ? <p className="notice warning">表格布局待核对，下方保留各子块内容。</p>
        : element.kind === 'heading' ? <h3>{element.text}</h3>
        : element.kind === 'table' && tableRows(element).length ? <div className="table-wrap"><table><tbody>{tableRows(element).map(row => <tr key={row}>{element.cells?.filter(cell => cell.row === row).sort((a, b) => a.column - b.column).map(cell => <td key={cell.column} rowSpan={cell.row_span || 1} colSpan={cell.column_span || 1}>{cell.text}</td>)}</tr>)}</tbody></table></div>
        : <p>{element.text || (element.kind === 'paragraph' ? '（空段落）' : `${element.kind} 元素（内容请参见原件）`)}</p>}
    </article>)}</div>
    {!!ir.source_elements?.length && <details><summary>原生结构补充（公式、单元格等）</summary><JsonView value={ir.source_elements.slice(0, 200)} />{ir.source_elements.length > 200 && <p className="muted">仅显示前 200 个原生元素。</p>}</details>}
    {selected && <section className="evidence-preview"><h3>证据定位</h3><p className="muted">{native ? '定位到固定快照中的块或单元格；不对应 Word/PDF 页码。' : '定位来自解析结果；Word 段落不转换为虚构页码。'}</p><JsonView value={selected.provenance?.length ? selected.provenance : selected.evidence} />{native && selected.metadata && <><h4>块与关联资源</h4><JsonView value={selected.metadata} /></>}</section>}
  </>;
}
