import type { ReactNode } from 'react';
import { ApiError, describeError } from '../api/api';

export function ErrorNotice({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  if (!error) return null;
  const status = error instanceof ApiError ? error.status : undefined;
  return <div role="alert" className={`notice ${status === 409 ? 'warning' : 'error'}`}><strong>{status === 409 ? '状态或版本冲突' : status === 403 ? '没有操作权限' : status === 404 ? '资源不可用' : status === 501 ? '服务能力尚未提供' : '操作未完成'}</strong><p>{describeError(error)}</p>{status === 409 && <p>当前输入已保留。请核对服务端状态和最新版本，再决定如何处理；不会自动覆盖或重试。</p>}{onRetry && <button type="button" className="secondary" onClick={onRetry}>重新加载</button>}</div>;
}
export function Empty({ title = '还没有数据', children }: { title?: string; children?: ReactNode }) { return <div className="empty"><span aria-hidden="true">◇</span><h3>{title}</h3>{children && <p>{children}</p>}</div>; }
export function Loading() { return <div className="loading" role="status"><span className="spinner" />正在读取…</div>; }
const statuses: Record<string, string> = { draft: '草稿', adopted: '已采纳', imported: '已导入', parsed: '已读取', reviewed: '已审阅', validated: '已验证', approved: '已批准', published: '已发布', pending: '等待处理', pending_approval: '待审批', running: '运行中', succeeded: '已成功', failed: '失败', partial: '部分完成', cancelled: '已取消', unknown: '结果未知', retry_wait: '等待重试', paused: '已暂停', retired: '历史代', completed: '已完成', complete: '已完成', unverified: '未验证', rejected: '已拒绝', ready: '可用', active: '已启用', disabled: '已停用', registered: '已登记', connected: '连接成功', discovered: '已发现结构', error: '异常', requested: '待审批', queued: '已入队', executing: '执行中', remote_accepted: '远端已接受' };
export function Status({ value }: { value?: unknown }) { const text = String(value ?? '未知'); return <span className={`badge status-${text}`}>{statuses[text] ?? text}</span>; }
export function JsonView({ value }: { value: unknown }) { return <pre className="json-view">{JSON.stringify(value, null, 2)}</pre>; }
// Raw source JSON is supplied as text: parsing it in JavaScript would round large integers.
export function ExactJson({ text, label }: { text: unknown; label: string }) { return typeof text === 'string' ? <pre className="json-view exact-json" aria-label={label}>{text}</pre> : <p className="notice warning">服务未返回{label}的精确文本，暂不展示可能丢失整数精度的内容。</p>; }
export function Pager({ offset, total, size = 25, onChange }: { offset: number; total: number; size?: number; onChange: (offset: number) => void }) { return <div className="pager"><span>共 {total} 条 · {total ? `${offset + 1}–${Math.min(offset + size, total)}` : '0'}</span><button className="secondary" disabled={!offset} onClick={() => onChange(Math.max(0, offset - size))}>上一页</button><button className="secondary" disabled={offset + size >= total} onClick={() => onChange(offset + size)}>下一页</button></div>; }
export function PageTitle({ title, description, children }: { title: string; description: string; children?: ReactNode }) { return <div className="page-title"><div><h1>{title}</h1><p>{description}</p></div><div className="toolbar">{children}</div></div>; }
export function Drawer({ title, children, onClose, closeDisabled = false }: { title: string; children: ReactNode; onClose: () => void; closeDisabled?: boolean }) { return <aside className="drawer" aria-label={title}><header><h2>{title}</h2><button className="icon-button" disabled={closeDisabled} onClick={onClose} aria-label="关闭侧栏">×</button></header>{children}</aside>; }
export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) { return <label className="field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>; }
export const formatTime = (value: unknown) => { if (!value) return '—'; const date = new Date(String(value)); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false }); };


