/** A blank range means the complete selected document. */
export function parsePageRange(value: string): number[] {
  if (!value.trim()) return [];
  const pages = new Set<number>();
  for (const part of value.split(/[,，]/)) {
    const match = part.trim().match(/^(\d+)(?:\s*[-–]\s*(\d+))?$/);
    if (!match) throw new Error('页码格式应为 1-3,8，留空表示全文。');
    const start = Number(match[1]), end = Number(match[2] ?? match[1]);
    if (start < 1 || end < start || end > 1000) throw new Error('页码须在 1–1000 内，起始页不能晚于结束页。');
    for (let page = start; page <= end; page++) pages.add(page);
  }
  return [...pages].sort((a, b) => a - b);
}
