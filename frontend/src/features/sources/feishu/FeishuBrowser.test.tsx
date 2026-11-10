import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { StrictMode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { request, write } from '../../../shared/api/api';
import { FeishuBrowser } from './FeishuBrowser';
import { captureFeishuCallback, FeishuCallback } from './FeishuCallback';

vi.mock('../../../shared/api/api', async () => ({ ...await vi.importActual('../../../shared/api/api'), request: vi.fn(), write: vi.fn() }));
const app = { id: 'a', name: '企业应用', service: 'feishu', app_id: 'cli_test', external_tenant_key: 't', revision: 1, enabled: true, account: { status: 'active', display_name: '员工一', revision: 2 } };
const applications = { items: [app], redirect_uri: window.location.origin + '/integrations/feishu/callback', required_scopes: ['wiki:wiki:readonly', 'offline_access'] };
const page = { items: [], has_more: true, next_cursor: 'opaque', account_revision: 2 };
beforeEach(() => { vi.clearAllMocks(); window.history.replaceState(null, '', '/'); });

describe('Feishu employee browser', () => {
  it('lets a read-only member browse a personal account without import selection or application configuration', async () => {
    vi.mocked(request).mockResolvedValueOnce(applications)
      .mockResolvedValueOnce({ ...page, items: [{ space_id: 's', name: '可读空间' }], has_more: false })
      .mockResolvedValueOnce({ ...page, items: [{ node_token: 'n', title: '可读文档', obj_type: 'docx' }], has_more: false });
    render(<FeishuBrowser projectId="p" isAdmin={false} canSelect={false} onDirty={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: '浏览知识库' }));
    fireEvent.click(await screen.findByRole('button', { name: '打开目录' }));
    await screen.findByText('可读文档');
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(screen.queryByRole('button', { name: '配置企业应用' })).toBeNull();
    expect(screen.getByRole('button', { name: '重新授权当前账号' })).toBeTruthy();
  });
  it('checks only explicitly selected nodes at the displayed binding revision', async () => {
    vi.mocked(request).mockResolvedValueOnce(applications)
      .mockResolvedValueOnce({ ...page, items: [{ space_id: 's', name: '研发空间' }], has_more: false })
      .mockResolvedValueOnce({ ...page, items: [{ node_token: 'node1', title: '设计文档', obj_type: 'docx', has_child: true }], has_more: false })
      .mockResolvedValueOnce({ items: [{ node_token: 'node1', title: '设计文档', status: 'eligible', revision_id: 17 }], selected_nodes: 1, eligible_unique_resources: 1, account_revision: 2 });
    render(<FeishuBrowser projectId="p" isAdmin={false} onDirty={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: '浏览知识库' }));
    fireEvent.click(await screen.findByRole('button', { name: '打开目录' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: '选择 设计文档' }));
    fireEvent.click(screen.getByRole('button', { name: '检查所选文档的导入条件' }));
    await screen.findByText(/文档修订 17/);
    const [path, options] = vi.mocked(request).mock.calls[3];
    expect(path).toBe('/integrations/feishu/applications/a/selection-preview');
    expect(JSON.parse(String(options?.body))).toEqual({ project_id: 'p', expected_account_revision: 2, selections: [{ space_id: 's', node_token: 'node1' }] });
    expect(screen.getByText(/仅检查节点自身，不包含后代/)).toBeTruthy();
    expect(screen.getByText(/这里没有保存文档/)).toBeTruthy();
  });

  it('withholds cached titles when selected resource permission cannot be confirmed', async () => {
    vi.mocked(request).mockResolvedValueOnce(applications)
      .mockResolvedValueOnce({ ...page, items: [{ space_id: 's', name: '曾可见的空间' }], has_more: false })
      .mockResolvedValueOnce({ ...page, items: [{ node_token: 'node1', title: '暂时不能确认的标题', obj_type: 'docx' }], has_more: false })
      .mockResolvedValueOnce({ items: [{ node_token: 'node1', status: 'unknown', error_code: 'rate_limited' }], selected_nodes: 1, eligible_unique_resources: 0, account_revision: 2 });
    render(<FeishuBrowser projectId="p" isAdmin={false} onDirty={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: '浏览知识库' }));
    fireEvent.click(await screen.findByRole('button', { name: '打开目录' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: '选择 暂时不能确认的标题' }));
    fireEvent.click(screen.getByRole('button', { name: '检查所选文档的导入条件' }));
    await screen.findByText(/所选节点 1：暂时无法确认/);
    expect(screen.queryByText('暂时不能确认的标题')).toBeNull();
    expect(screen.queryByText('曾可见的空间')).toBeNull();
    expect(screen.getByText(/已选择 1 个节点/)).toBeTruthy();
  });

  it('clears an earlier successful preview after a later directory request is denied', async () => {
    vi.mocked(request).mockResolvedValueOnce(applications)
      .mockResolvedValueOnce({ ...page, items: [{ space_id: 's', name: '研发空间' }], has_more: false })
      .mockResolvedValueOnce({ ...page, items: [{ node_token: 'node1', title: '目录标题', obj_type: 'docx' }], has_more: false })
      .mockResolvedValueOnce({ items: [{ node_token: 'node1', title: '曾授权的文档标题', status: 'eligible', revision_id: 17 }], selected_nodes: 1, eligible_unique_resources: 1, account_revision: 2 })
      .mockRejectedValueOnce(new Error('账号权限已失效'));
    render(<FeishuBrowser projectId="p" isAdmin={false} onDirty={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: '浏览知识库' }));
    fireEvent.click(await screen.findByRole('button', { name: '打开目录' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: '选择 目录标题' }));
    fireEvent.click(screen.getByRole('button', { name: '检查所选文档的导入条件' }));
    await screen.findByText(/曾授权的文档标题/);
    await waitFor(() => expect((screen.getByRole('button', { name: '浏览知识库' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: '浏览知识库' }));
    await screen.findByText('账号权限已失效');
    expect(screen.queryByText(/曾授权的文档标题/)).toBeNull();
    expect(screen.queryByRole('region', { name: '飞书导入条件结果' })).toBeNull();
  });

  it('shows an incomplete configuration error without crashing the host page', async () => {
    vi.mocked(request).mockResolvedValueOnce({ items: [app] });
    render(<FeishuBrowser projectId="p" isAdmin={false} onDirty={vi.fn()} />);
    expect(await screen.findByText('飞书应用配置响应不完整，请刷新重试。')).toBeTruthy();
    expect(screen.getByRole('heading', { name: '飞书知识库' })).toBeTruthy();
  });

  it('does not merge pages from two employee credential revisions', async () => {
    vi.mocked(request).mockResolvedValueOnce(applications).mockResolvedValueOnce({ ...page, items: [{ space_id: 's', name: '旧授权目录' }] }).mockResolvedValueOnce({ ...page, account_revision: 3, items: [{ space_id: 's2', name: '新授权目录' }] });
    render(<FeishuBrowser projectId="p" isAdmin={false} onDirty={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: '浏览知识库' }));
    await screen.findByText('旧授权目录');
    fireEvent.click(screen.getByRole('button', { name: '继续加载' }));
    await screen.findByText('账号授权已改变，请重新加载目录。');
    expect(screen.queryByText('旧授权目录')).toBeNull();
    expect(screen.queryByText('新授权目录')).toBeNull();
  });

  it('keeps pagination usable after an empty permission-filtered page', async () => {
    vi.mocked(request).mockResolvedValueOnce(applications).mockResolvedValueOnce(page).mockResolvedValueOnce({ ...page, items: [{ space_id: 's', name: '研发空间' }], has_more: false });
    render(<FeishuBrowser projectId="p" isAdmin={false} onDirty={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: '浏览知识库' }));
    fireEvent.click(await screen.findByRole('button', { name: '继续加载' }));
    expect(await screen.findByText('研发空间')).toBeTruthy();
    expect(vi.mocked(request).mock.calls[2][0]).toContain('cursor=opaque');
    expect(screen.queryByRole('button', { name: '配置企业应用' })).toBeNull();
    expect(screen.getByText(/尚未导入文档/)).toBeTruthy();
  });

  it('clears cached directory after a denied next page', async () => {
    vi.mocked(request).mockResolvedValueOnce(applications).mockResolvedValueOnce({ ...page, items: [{ space_id: 's', name: '已撤权资源' }] }).mockRejectedValueOnce(new Error('权限已撤销'));
    render(<FeishuBrowser projectId="p" isAdmin={false} onDirty={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: '浏览知识库' }));
    expect(await screen.findByText('已撤权资源')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '继续加载' }));
    await screen.findByText('权限已撤销');
    expect(screen.queryByText('已撤权资源')).toBeNull();
  });

  it('ignores late results after the project changes', async () => {
    let resolve!: (value: unknown) => void;
    vi.mocked(request).mockResolvedValueOnce(applications).mockImplementationOnce(() => new Promise(r => { resolve = r; })).mockResolvedValueOnce({ ...applications, items: [] });
    const view = render(<FeishuBrowser projectId="p1" isAdmin={false} onDirty={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: '浏览知识库' }));
    view.rerender(<FeishuBrowser projectId="p2" isAdmin={false} onDirty={vi.fn()} />);
    await screen.findByText('尚未配置飞书应用');
    await act(async () => resolve({ ...page, items: [{ space_id: 'old', name: '旧项目私密空间' }] }));
    expect(screen.queryByText('旧项目私密空间')).toBeNull();
  });

  it('disconnects only the current employee and refreshes the status', async () => {
    vi.mocked(request).mockResolvedValueOnce(applications).mockResolvedValueOnce({ ...applications, items: [{ ...app, account: { ...app.account, status: 'revoked' } }] });
    vi.mocked(write).mockResolvedValue({ disconnected: true });
    render(<FeishuBrowser projectId="p" isAdmin={false} onDirty={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: '断开本平台连接' }));
    await screen.findByText(/已断开/);
    expect(write).toHaveBeenCalledWith('/integrations/feishu/applications/a/disconnect', { project_id: 'p' });
  });
});

describe('OAuth callback', () => {
  it('removes code/state from history and exchanges once in StrictMode', async () => {
    window.history.replaceState(null, '', '/integrations/feishu/callback?state=opaque-state&code=private-code');
    const input = captureFeishuCallback()!;
    expect(window.location.search).toBe('');
    vi.mocked(write).mockResolvedValue({ connected: true });
    render(<StrictMode><FeishuCallback input={input} /></StrictMode>);
    await screen.findByText(/当前员工账号已连接/);
    expect(write).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).not.toContain('private-code');
  });
  it('does not retry an authorization code when the result is unknown', async () => {
    vi.mocked(write).mockRejectedValue(new Error('结果未确认'));
    render(<FeishuCallback input={{ state: 'state', code: 'private-code' }} />);
    await waitFor(() => expect(screen.getByText('结果未确认')).toBeTruthy());
    expect(write).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/此回调不会自动重试/)).toBeTruthy();
  });
});
