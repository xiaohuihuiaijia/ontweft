import { expect, it } from 'vitest';
import { parsePageRange } from './page-range';

it('keeps blank as full-document mode and merges explicit page selections', () => {
  expect(parsePageRange(' ')).toEqual([]);
  expect(parsePageRange('8, 2-4，3,1')).toEqual([1, 2, 3, 4, 8]);
});

it.each(['0', '5-2', '2.5', '1,', '1-1001', '全部'])('rejects an invalid range without silently changing scope: %s', value => {
  expect(() => parsePageRange(value)).toThrow();
});
