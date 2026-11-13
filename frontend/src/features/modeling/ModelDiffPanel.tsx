import type { ModelDiff, ModelImpact } from '../../shared/model/types';

const changes = { added: '新增', modified: '修改', removed: '删除' };
const risks = { display: '名称与说明', compatible: '兼容扩展', review: '需核对影响' };

export function ModelDiffPanel({ value, stale = false, impact }: { value?: ModelDiff; stale?: boolean; impact?: ModelImpact }) {
  return <section className="model-diff-panel" aria-label="模型变更与影响"><h3>模型变更与影响</h3>
    {stale ? <p className="muted">有未保存修改；保存后重新计算差异，原校验结果不代表当前输入。</p> : !value ? <p className="muted">保存草稿后显示与基线发布版的差异。</p> : <>
      <p>新增 {value.counts.added} 项 · 修改 {value.counts.modified} 项 · 删除 {value.counts.removed} 项</p>
      {value.items.length ? <ul className="model-change-list">{value.items.map((item, index) => <li key={`${item.kind}:${item.type_key}:${item.key}:${index}`}><strong>{changes[item.change]} · {item.label}</strong><span className="badge">{risks[item.risk]}</span><p>{item.message}</p><code>{item.type_key}{item.key !== item.type_key ? ` / ${item.key}` : ''}</code></li>)}</ul> : <p className="muted">与基线定义一致。</p>}
      <p className="muted">关联来源映射：{value.affected_mapping_keys.length ? value.affected_mapping_keys.join('、') : '本次没有关联映射变化'}。分类帮助审阅，实际是否允许发布仍由校验决定。</p>
      {!impact || impact.objects === null || impact.relations === null ? <p className="muted">已有对象和关系的影响数量尚未统计；运行校验后显示。</p> : <p className="muted">本次校验{impact.complete ? '扫描已完成' : '正在扫描'}：受影响类型中已检查 {impact.objects} 个对象、{impact.relations} 条关系。此数按扫描记录统计，后续数据变化另行复核，不是实时总量。</p>}
    </>}
  </section>;
}
