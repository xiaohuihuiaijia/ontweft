import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SettingsPage } from './SettingsPage';

afterEach(() => vi.unstubAllGlobals());

it('does not expose user or member administration through permission union from another project', () => {
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
  render(<SettingsPage projectId="read-only" onDirty={() => {}} onSessionRefresh={async () => {}} session={{
    user: { id: 'u', email: 'member@example.test', display_name: '成员', admin: false }, tenant: { id: 't', name: '测试企业' },
    projects: [{ id: 'read-only', name: '只读项目', role: 'member', permissions: ['objects:read'] }, { id: 'managed', name: '管理项目', role: 'owner', permissions: ['members:manage'] }],
    permissions: ['objects:read', 'members:manage'],
  }} />);
  expect(screen.queryByRole('heading', { name: '项目成员' })).toBeNull();
  expect(screen.queryByRole('button', { name: '创建账号' })).toBeNull();
  expect(fetcher).not.toHaveBeenCalled();
});

it('creates an account with a username that can be used on the login form', async () => {
  const fetcher = vi.fn().mockImplementation((_url, options) => Promise.resolve(new Response(JSON.stringify(
    options?.method === 'POST' ? { id: 'new-user' } : { items: [], total: 0 },
  ))));
  vi.stubGlobal('fetch', fetcher);
  render(<SettingsPage projectId="p" onDirty={() => {}} onSessionRefresh={async () => {}} session={{
    user: { id: 'u', username: 'admin', email: 'owner@example.test', display_name: '管理员', admin: true },
    tenant: { id: 't', name: '测试企业' }, projects: [{ id: 'p', name: '项目', role: 'owner', permissions: [] }], permissions: [],
  }} />);
  fireEvent.click(screen.getByText('创建企业账号'));
  fireEvent.change(screen.getByLabelText('姓名'), { target: { value: '分析员' } });
  fireEvent.change(screen.getByRole('textbox', { name: /^用户名/ }), { target: { value: 'analyst' } });
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'analyst@example.test' } });
  fireEvent.change(screen.getByLabelText('初始密码（至少 12 位）'), { target: { value: 'Synthetic-Password' } });
  fireEvent.click(screen.getByRole('button', { name: '创建账号' }));
  await waitFor(() => expect(screen.getByText('企业账号已创建。请按需要分配项目角色。')).toBeTruthy());
  const creation = fetcher.mock.calls.find(([url, options]) => url === '/api/users' && options?.method === 'POST');
  expect(JSON.parse(creation![1].body)).toEqual({ username: 'analyst', email: 'analyst@example.test', display_name: '分析员', password: 'Synthetic-Password' });
  expect((screen.getByRole('textbox', { name: /^用户名/ }) as HTMLInputElement).value).toBe('');
});

