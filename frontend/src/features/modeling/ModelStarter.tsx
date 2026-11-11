import { scoped } from '../../shared/api/api';
import { useResource } from '../../shared/hooks/hooks';
import type { Definition, ListResult } from '../../shared/model/types';
import { ErrorNotice, Loading } from '../../shared/ui/components';
import './model-starter.css';

export interface ModelTemplate {
  id: string;
  version: number;
  title: string;
  description: string;
  questions: string[];
  definition: Definition;
}

export function ModelStarter({ projectId, disabled, onChoose }: {
  projectId: string;
  disabled: boolean;
  onChoose: (template: ModelTemplate) => void;
}) {
  const templates = useResource<ListResult<ModelTemplate>>(scoped('/ontology/templates', projectId));
  return <section className="model-starter" aria-labelledby="model-starter-title">
    <div className="model-starter-heading"><span className="model-starter-kicker">从业务问题开始</span><h3 id="model-starter-title">先有一个能用的模型，再逐步细化。</h3>
      <p>选择结构模板后可以自由改名、增删字段和关系；也可以直接在下方添加自己的业务类型。模板不含业务数据，不会自动发布。</p></div>
    {templates.loading ? <Loading /> : <ErrorNotice error={templates.error} onRetry={templates.reload} />}
    <div className="model-starter-grid">{templates.data?.items.map(template => <article className="model-starter-option" key={`${template.id}:${template.version}`}>
      <h4>{template.title}</h4><p>{template.description}</p>
      <ul>{template.questions.map(question => <li key={question}>{question}</li>)}</ul>
      <div className="model-starter-schema" aria-label={`${template.title}结构预览`}>{template.definition.types.map(type => <span key={type.key}>{type.label}<small>{type.properties.length} 个字段</small></span>)}</div>
      <p className="model-starter-relations">{template.definition.types.flatMap(type => type.relations.map(relation => `${type.label} → ${relation.label} → ${template.definition.types.find(target => target.key === relation.target_type)?.label ?? relation.target_type}`)).join('；')}</p>
      <button type="button" className="secondary" disabled={disabled} onClick={() => onChoose(template)}>使用{template.title}模板</button>
    </article>)}</div>
  </section>;
}

export function ModelWorkflow({ status, dirty }: { status?: string; dirty: boolean }) {
  const current = dirty || !status || status === 'draft' ? 0 : ['validating', 'validated'].includes(status) ? 1 : status === 'approved' ? 2 : status === 'published' ? 3 : 0;
  const steps = ['定义与保存', '校验与修正', '审批当前修订', '发布后创建对象'];
  return <ol className="model-workflow" aria-label="本体构建步骤">{steps.map((step, index) => <li key={step} className={index === current ? 'current' : index < current ? 'done' : ''} aria-current={index === current ? 'step' : undefined}><span>{String(index + 1).padStart(2, '0')}</span><strong>{step}</strong></li>)}</ol>;
}
