import { useState } from 'react';
import { request, scoped } from '../../shared/api/api';
import { Empty, ErrorNotice, Field, formatTime, Loading, Pager, PageTitle, Status } from '../../shared/ui/components';
import { useResource } from '../../shared/hooks/hooks';
import { DocumentDetails } from './DocumentDetails';
import type { ListResult, RecordData } from '../../shared/model/types';

export function DocumentsPage({ projectId, canWrite }: { projectId: string; canWrite: boolean }) {
  const [offset, setOffset] = useState(0);
  const list = useResource<ListResult<RecordData>>(scoped('/documents', projectId, { limit: 25, offset }));
  const [file, setFile] = useState<File>(); const [engine, setEngine] = useState('auto'); const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>(); const [selected, setSelected] = useState<string>(); const [message, setMessage] = useState('');
  return <><PageTitle title="文档资料" description="保存真实原件并提交解析任务，状态与结构来自处理结果。"><button className="secondary" onClick={list.reload}>刷新状态</button></PageTitle><ErrorNotice error={error} />{message && <div className="notice success" role="status">{message}</div>}{canWrite && <form className="upload-panel" onSubmit={async event => { event.preventDefault(); if (!file) return; const form = event.currentTarget; setBusy(true); setError(undefined); setMessage(''); const data = new FormData(); data.set('project_id', projectId); data.set('file', file); data.set('engine', engine); try { const result = await request<{ document: RecordData; job: RecordData }>('/documents/upload', { method: 'POST', body: data }); setOffset(0); list.reload(); setMessage(`原件已保存，解析任务 ${String(result.job.id ?? '')} 已提交。请在任务中心查看实际进度。`); setFile(undefined); form.reset(); } catch (e) { setError(e); } finally { setBusy(false); } }}><Field label="选择原件"><input type="file" disabled={busy} required onChange={e => setFile(e.target.files?.[0])} /></Field><Field label="解析路径"><select disabled={busy} value={engine} onChange={e => setEngine(e.target.value)}><option value="auto">自动选择（Word/PDF/Excel 使用 Docling）</option><option value="basic">基础解析（明确降级）</option><option value="docling">Docling（需服务端配置）</option></select></Field><button disabled={!file || busy}>{busy ? '正在上传…' : '上传并解析'}</button></form>}
    {list.loading ? <Loading /> : list.error ? <ErrorNotice error={list.error} onRetry={list.reload} /> : !list.data?.items.length ? <Empty title="当前没有可读取的文档">这里只展示当前账号可读取的文档；飞书来源还需要在数据源页面连接个人账号。</Empty> : <div className="table-wrap"><table><thead><tr><th>文件</th><th>状态</th><th>版本</th><th>时间</th><th /></tr></thead><tbody>{list.data.items.map(doc => <tr key={String(doc.id)}><td>{String(doc.title ?? doc.filename ?? doc.name ?? doc.id)}</td><td><Status value={doc.state} /></td><td>{String(doc.revision ?? doc.version ?? '—')}</td><td>{formatTime(doc.updated_at ?? doc.created_at)}</td><td><button className="text-button" onClick={() => setSelected(String(doc.id))}>查看结构与原件</button></td></tr>)}</tbody></table></div>}
    {list.data && list.data.total > 0 && <Pager offset={offset} total={list.data.total} onChange={setOffset} />}{selected && <DocumentDetails documentId={selected} canReparse={canWrite} onClose={() => setSelected(undefined)} onChanged={list.reload} />}
  </>;
}





