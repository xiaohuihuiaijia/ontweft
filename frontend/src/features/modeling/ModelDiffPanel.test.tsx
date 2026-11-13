import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { ModelDiffPanel } from './ModelDiffPanel';
import type { ModelDiff } from '../../shared/model/types';

const value: ModelDiff = { items: [{ kind: 'relation_property', type_key: 'Product', key: 'fits.condition', change: 'added', risk: 'compatible', label: '适用条件', message: '新增可选字段' }], counts: { added: 1, modified: 0, removed: 0 }, affected_type_keys: ['Product'], affected_mapping_keys: ['catalog'] };

it('distinguishes unknown impact from measured zero and invalidates unsaved results', () => {
  const view = render(<ModelDiffPanel value={value} />);
  expect(screen.getByText(/影响数量尚未统计/)).toBeTruthy();
  expect(screen.getByText('新增 · 适用条件')).toBeTruthy();
  view.rerender(<ModelDiffPanel value={value} impact={{ objects: 0, relations: 0, complete: true, scope: 'validation_scan', affected_type_keys: ['Product'] }} />);
  expect(screen.getByText(/已检查 0 个对象、0 条关系/)).toBeTruthy();
  view.rerender(<ModelDiffPanel value={value} stale />);
  expect(screen.queryByText('新增 · 适用条件')).toBeNull();
  expect(screen.getByText(/原校验结果不代表当前输入/)).toBeTruthy();
});
