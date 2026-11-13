import { render, screen, within, fireEvent } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { WorkspaceNavigation, graphProjectFromHash, sectionViews } from './Navigation';

it('opens a graph deep link only for a project in the current session', () => {
  const projects = [{ id: 'empty' }, { id: 'sample' }];
  expect(graphProjectFromHash('#graph?project=sample', projects)).toBe('sample');
  expect(graphProjectFromHash('#graph?project=private', projects)).toBeUndefined();
  expect(graphProjectFromHash('#graph?project=sample', [])).toBeUndefined();
  expect(graphProjectFromHash('#graph', projects)).toBeUndefined();
  expect(graphProjectFromHash('#sources', projects)).toBeUndefined();
  expect(graphProjectFromHash('#ontology?project=sample', projects)).toBeUndefined();
});

it('keeps five core entry points and moves jobs into settings', () => {
  const navigate = vi.fn();
  render(<WorkspaceNavigation view="jobs" tenantName="Test" onNavigate={navigate} />);
  const nav = screen.getByRole('navigation', { name: '主导航' });
  expect(within(nav).getAllByRole('button').map(b => b.textContent)).toEqual(['数据', '本体', '对象', '图谱', '设置']);
  expect(within(nav).queryByRole('button', { name: '内容检索' })).toBeNull();
  fireEvent.click(within(nav).getByRole('button', { name: '设置' }));
  expect(navigate).toHaveBeenCalledWith('settings');
  expect(sectionViews('settings', false, false).map(v => v[0])).toEqual(['settings', 'jobs', 'audit']);
});
