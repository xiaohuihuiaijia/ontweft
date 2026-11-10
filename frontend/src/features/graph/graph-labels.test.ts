import { expect, it, vi } from 'vitest';
import type { Settings } from 'sigma/settings';
import { drawGraphHover, drawGraphLabel } from './graph-labels';

it('keeps a long CJK label within the CSS viewport, including high density canvases', () => {
  const text = vi.fn();
  const context = {
    canvas: { width: 684, getBoundingClientRect: () => ({ width: 342 }) },
    save() {}, restore() {}, beginPath() {}, arc() {}, stroke() {}, roundRect() {}, fill() {},
    measureText: (value: string) => ({ width: Array.from(value).length * 12 }), fillText: text,
  } as unknown as CanvasRenderingContext2D;
  const node = { x: 285, y: 120, size: 13, label: '这是需要完整保留在详情面板里的很长的中文业务对象名称', color: '#236c56' };
  const settings = { labelSize: 12, labelWeight: 'normal', labelFont: 'sans-serif' } as Settings;
  for (const draw of [drawGraphLabel, drawGraphHover]) draw(context, node, settings);
  expect(text).toHaveBeenCalledTimes(2);
  for (const [label, x, y] of text.mock.calls) {
    expect(label.endsWith('…')).toBe(true);
    expect(x).toBeGreaterThanOrEqual(8);
    expect(x + Array.from(label).length * 12).toBeLessThanOrEqual(334);
    expect(y).toBeLessThan(node.y);
  }
  expect(node.label.endsWith('名称')).toBe(true);
});
