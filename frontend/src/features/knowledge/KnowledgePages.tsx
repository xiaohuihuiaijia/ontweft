import { useEffect, useState } from 'react';
import { request, scoped, write } from '../../shared/api/api';
import { Drawer, Empty, ErrorNotice, Field, formatTime, Loading, Pager, PageTitle, Status } from '../../shared/ui/components';
import { useResource } from '../../shared/hooks/hooks';
import { KnowledgeReferences, KnowledgeHistory } from './KnowledgeReferences';
import type { ListResult, RecordData } from '../../shared/model/types';

export interface KnowledgePage { id: string; title: string; body: string; references: RecordData[]; revision: number; published_revision?: number; updated_at?: string }
const pageStatus = (page: KnowledgePage) => page.published_revision === page.revision ? 'published' : 'draft';

export function KnowledgePages({ projectId, canWrite, canPublish, onDirty }: { projectId: string; canWrite: boolean; canPublish: boolean; onDirty: (dirty: boolean) => void }) {
  const [offset, setOffset] = useState(0);
  const list = useResource<ListResult<KnowledgePage>>(scoped('/knowledge', projectId, { limit: 25, offset }));
  const [editor, setEditor] = useState<KnowledgePage | null | undefined>();
  const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>();
  const edit = async (page?: KnowledgePage) => {
    setError(undefined); setBusy(true);
    try { setEditor(page ? await request<KnowledgePage>(`/knowledge/${encodeURIComponent(page.id)}`) : null); }
    catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <><PageTitle title="知识页面" description="手工整理知识与引用，按修订保存和发布。">{canWrite && <button disabled={busy || editor !== undefined} onClick={() => edit()}>新建知识页</button>}</PageTitle><ErrorNotice error={error} />{list.loading ? <Loading /> : list.error ? <ErrorNotice error={list.error} onRetry={list.reload} /> : !list.data?.items.length ? <Empty title="还没有知识页面">无需配置模型，也可以手工编写和维护知识。</Empty> : <div className="card-grid">{list.data.items.map(page => <article className="model-card" key={page.id}><Status value={pageStatus(page)} /><h3 style={{ marginTop: 18 }}>{page.title}</h3><p>修订 {page.revision} · {formatTime(page.updated_at)}</p><button className="text-button" disabled={busy || editor !== undefined} onClick={() => edit(page)}>打开页面 →</button></article>)}</div>}
    {list.data && list.data.total > 0 && <Pager offset={offset} total={list.data.total} onChange={setOffset} />}{editor !== undefined && <KnowledgeEditor key={editor?.id ?? 'new'} initialPage={editor ?? undefined} projectId={projectId} canWrite={canWrite} canPublish={canPublish} onDirty={onDirty} onClose={() => setEditor(undefined)} onUpdated={() => { if (!editor) setOffset(0); list.reload(); }} />}
  </>;
}

export function KnowledgeEditor({ projectId, canWrite, canPublish, onDirty, initialPage, onClose, onUpdated }: { projectId: string; canWrite: boolean; canPublish: boolean; onDirty: (dirty: boolean) => void; initialPage?: KnowledgePage; onClose: () => void; onUpdated: () => void }) {
  const [current, setCurrent] = useState<KnowledgePage | undefined>(initialPage);
  const [title, setTitle] = useState(initialPage?.title ?? ''); const [body, setBody] = useState(initialPage?.body ?? ''); const [references, setReferences] = useState(() => JSON.stringify(initialPage?.references ?? [], null, 2));
  const [dirty, setDirty] = useState(false); const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>(); const [remote, setRemote] = useState<KnowledgePage>(); const [message, setMessage] = useState('');
  useEffect(() => { onDirty(dirty); return () => onDirty(false); }, [dirty, onDirty]);
  const save = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError(undefined); setMessage('');
    try {
      const refs: unknown = JSON.parse(references); if (!Array.isArray(refs) || refs.some(r => !r || typeof r !== 'object' || Array.isArray(r))) throw new Error('引用须为 JSON 对象数组，引用身份和版本由服务端验证。');
      const result = await write<KnowledgePage>(current ? `/knowledge/${encodeURIComponent(current.id)}` : '/knowledge', { title, body, references: refs, ...(current ? { expected_revision: current.revision } : { project_id: projectId }) }, current ? 'PATCH' : 'POST');
      setCurrent(result); setReferences(JSON.stringify(result.references, null, 2)); setDirty(false); setMessage('草稿已保存。知识内容不会自动转为已审核事实。'); onUpdated();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <Drawer title={current ? '知识页编辑' : '新建知识页'} onClose={() => { if (!busy && (!dirty || window.confirm('放弃尚未保存的内容？'))) onClose(); }}><form className="drawer-form" onSubmit={save}><div className="drawer-body"><ErrorNotice error={error} />{message && <div className="notice success" role="status">{message}</div>}{current && <div className="inline-info"><Status value={pageStatus(current)} /><span>修订 {current.revision}</span></div>}<Field label="标题"><input required disabled={!canWrite || busy} value={title} onChange={e => { setTitle(e.target.value); setDirty(true); }} /></Field><Field label="正文（Markdown）" hint="内容作为文本保存。来源引用在下方单独登记，正文链接不会自动成为事实。"><textarea rows={20} disabled={!canWrite || busy} value={body} onChange={e => { setBody(e.target.value); setDirty(true); }} /></Field><KnowledgeReferences projectId={projectId} disabled={!canWrite || busy} value={references} onChange={value => { setReferences(value); setDirty(true); }} />{current && canWrite && <KnowledgeHistory key={`${current.id}:${current.revision}`} id={current.id} />}{error && current ? <button type="button" className="secondary" onClick={async () => { try { setRemote(await request<KnowledgePage>(`/knowledge/${encodeURIComponent(current.id)}`)); } catch (e) { setError(e); } }}>读取最新版本作比较</button> : null}{remote && <section><h3>远端版本 · 修订 {remote.revision}</h3><pre className="json-view">{remote.body}</pre></section>}</div><footer className="drawer-actions"><span className="muted">{dirty ? '有未保存修改' : '当前修订'}</span>{canWrite && <button disabled={busy || !title.trim() || (!!current && !dirty)}>保存草稿</button>}{current && canPublish && !dirty && current.published_revision !== current.revision && <button type="button" disabled={busy} onClick={async () => { setBusy(true); setError(undefined); try { setCurrent(await write<KnowledgePage>(`/knowledge/${encodeURIComponent(current.id)}/publish`, { expected_revision: current.revision })); onUpdated(); setMessage('知识页已发布。'); } catch (e) { setError(e); } finally { setBusy(false); } }}>发布当前修订</button>}</footer></form></Drawer>;
}

