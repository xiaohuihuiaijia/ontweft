import type { NodeLabelDrawingFunction } from 'sigma/rendering';

/** Keep labels in the actual canvas; full names remain available in the inspector/list. */
const drawLabel = (highlighted: boolean): NodeLabelDrawingFunction => (context, data, settings) => {
  if (!data.label) return;
  context.save();
  const fontSize = settings.labelSize;
  context.font = `${settings.labelWeight} ${fontSize}px ${settings.labelFont}`;
  const width = context.canvas.getBoundingClientRect().width;
  const maxWidth = Math.max(0, Math.min(240, width - 24));
  const characters = Array.from(data.label);
  let label = data.label;
  while (characters.length && context.measureText(label).width > maxWidth) {
    characters.pop(); label = `${characters.join('')}…`;
  }
  if (!characters.length) { context.restore(); return; }
  const labelWidth = context.measureText(label).width;
  let x = data.x + data.size + 8; let y = data.y + fontSize / 3;
  if (x + labelWidth > width - 12) {
    x = Math.max(12, Math.min(width - labelWidth - 12, data.x - labelWidth / 2));
    y = Math.max(fontSize + 8, data.y - data.size - 12);
  }
  if (highlighted) {
    context.beginPath(); context.arc(data.x, data.y, data.size + 4, 0, Math.PI * 2);
    context.strokeStyle = data.color; context.lineWidth = 2; context.stroke();
    context.fillStyle = '#ffffff'; context.beginPath();
    context.roundRect(x - 4, y - fontSize, labelWidth + 8, fontSize + 8, 4); context.fill();
  }
  context.fillStyle = '#273f48'; context.textAlign = 'left'; context.fillText(label, x, y);
  context.restore();
};

export const drawGraphLabel = drawLabel(false);
export const drawGraphHover = drawLabel(true);
