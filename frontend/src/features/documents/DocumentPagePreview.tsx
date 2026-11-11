import { useResource } from '../../shared/hooks/hooks';
import { ErrorNotice, Loading, Status } from '../../shared/ui/components';
import type { ListResult } from '../../shared/model/types';
import type { DocumentPage } from '../intelligence/types';
import { DocumentPreview } from './DocumentPreview';
import '../intelligence/intelligence.css';

/** AI page IDs are not DocumentParse IDs. Resolve original pages by document + page. */
export function DocumentPagePreview({ documentId, page, quote }: { documentId: string; page: number; quote?: string }) {
  const number = Number.isInteger(page) && page > 0 ? page : 1;
  const result = useResource<ListResult<DocumentPage> & { revision: number; content_hash: string }>(`/documents/${encodeURIComponent(documentId)}/pages?limit=1&offset=${number - 1}`);
  const value = result.data?.items.find(item => item.page_number === number);
  return <section aria-label={`文档来源第 ${number} 页`}>
    <div className="section-heading"><h3>来源 · 第 {number} 页</h3><button type="button" className="text-button" onClick={result.reload}>重新核对页面权限</button></div>
    <ErrorNotice error={result.error} />
    {result.loading ? <Loading /> : value && result.data ? <>
      <p className="muted">原件修订 {result.data.revision} · {value.label} · <Status value={value.status} /></p>
      {quote && <blockquote className="document-evidence">{quote}</blockquote>}
      {value.has_image && Array.from({ length: value.image_count || 1 }, (_, imageIndex) => <img key={imageIndex} className="document-page-image" alt={`第 ${number} 页 · 来源图像 ${imageIndex + 1}`} src={`/api/documents/${encodeURIComponent(documentId)}/pages/${number}/image?${new URLSearchParams({ expected_revision: String(result.data!.revision), expected_hash: result.data!.content_hash, image_index: String(imageIndex) })}`} />)}
      {!!value.elements?.length && <DocumentPreview value={{ elements: value.elements }} compact />}
      {!!value.text && <details open={!value.has_image && !value.elements?.length}><summary>本页文字</summary><pre className="document-page-text">{value.text}</pre></details>}
      {!value.has_image && !value.text && !value.elements?.length && <p className="notice">本页尚无基础解析内容，请在文档工作台核对原件。</p>}
    </> : !result.error && <p className="notice">当前原件没有此页，请核对文档版本与证据。</p>}
  </section>;
}
