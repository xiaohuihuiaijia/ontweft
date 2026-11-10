import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { Login } from './Login';

afterEach(() => vi.unstubAllGlobals());

it('logs in with only a username and password and accepts the verified session', async () => {
  const session = { user: { id: 'u', username: 'admin' }, tenant: { id: 't' }, projects: [] };
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ user: session.user })))
    .mockResolvedValueOnce(new Response(JSON.stringify(session)));
  vi.stubGlobal('fetch', fetcher);
  const onLogin = vi.fn();
  render(<Login onLogin={onLogin} />);
  expect(screen.queryByLabelText('企业标识')).toBeNull();
  expect(screen.queryByLabelText('邮箱')).toBeNull();
  fireEvent.change(screen.getByRole('textbox', { name: /^用户名/ }), { target: { value: ' admin ' } });
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'test-password' } });
  fireEvent.click(screen.getByRole('button', { name: '登录' }));
  await waitFor(() => expect(onLogin).toHaveBeenCalledWith(session));
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ username: 'admin', password: 'test-password' });
  expect(fetcher.mock.calls[1][0]).toBe('/api/auth/me');
  expect((screen.getByLabelText('密码') as HTMLInputElement).value).toBe('');
});

it('allows email in the username field and keeps failed login recoverable', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { message: '用户名或密码不正确' } }), { status: 401 })));
  const onLogin = vi.fn();
  render(<Login onLogin={onLogin} />);
  fireEvent.change(screen.getByRole('textbox', { name: /^用户名/ }), { target: { value: 'owner@example.test' } });
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'incorrect' } });
  fireEvent.click(screen.getByRole('button', { name: '登录' }));
  expect(await screen.findByText('用户名或密码不正确')).toBeTruthy();
  expect(onLogin).not.toHaveBeenCalled();
  expect((screen.getByRole('button', { name: '登录' }) as HTMLButtonElement).disabled).toBe(false);
  expect((screen.getByRole('textbox', { name: /^用户名/ }) as HTMLInputElement).value).toBe('owner@example.test');
});
