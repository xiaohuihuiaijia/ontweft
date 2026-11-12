import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { OntologyEditor } from './OntologyPage';
import { ModelWorkflow, type ModelTemplate } from './ModelStarter';

const template: ModelTemplate = {
  id: 'project_delivery', version: 1, title: '项目与任务', description: '定义任务所属的项目。',
  questions: ['一个项目有哪些任务？'], definition: { types: [
    { key: 'project', label: '项目', properties: [{ key: 'name', label: '项目名称', data_type: 'string', required: true }], relations: [] },
    { key: 'task', label: '任务', properties: [], relations: [{ key: 'belongs_to', label: '所属项目', target_type: 'project', cardinality: 'one' }] },
  ], mappings: [] },
};
const props = { projectId: 'p', can: () => true, onDirty: vi.fn(), onClose: vi.fn(), onUpdated: vi.fn(), initialDefinition: { types: [], mappings: [] }, initialBaselineReleaseId: null };

it('loads a template locally, allows custom edits, then saves through the ordinary baseline-bound changeset API', async () => {
  let saved: Record<string, unknown> | undefined;
  const requests: string[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    const path = String(url); requests.push(`${options?.method ?? 'GET'} ${path}`);
    if (path.includes('/ontology/templates')) return new Response(JSON.stringify({ items: [structuredClone(template)], total: 1 }));
    if (options?.method === 'POST') {
      saved = JSON.parse(String(options.body));
      return new Response(JSON.stringify({ ...saved, id: 'draft', revision: 1, status: 'draft', validation: {} }));
    }
    return new Response(JSON.stringify({ items: [], total: 0 }));
  });
  render(<OntologyEditor {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: '使用项目与任务模板' }));
  expect(requests.some(value => value.startsWith('POST'))).toBe(false);
  expect(screen.queryByRole('button', { name: '使用项目与任务模板' })).toBeNull();
  expect((screen.getByLabelText('变更名称') as HTMLInputElement).value).toBe('项目与任务 · 初始模型');
  fireEvent.change(screen.getAllByLabelText('显示名称')[0], { target: { value: '我的项目' } });
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
  await waitFor(() => expect(saved).toBeDefined());
  expect(saved?.expected_baseline_release_id).toBeNull();
  expect(saved?.definition).toEqual({ ...template.definition, types: [{ ...template.definition.types[0], label: '我的项目' }, template.definition.types[1]] });
  expect(template.definition.types[0].label).toBe('项目');
  expect(requests.filter(value => value.startsWith('POST'))).toEqual(['POST /api/changesets']);
  expect(props.onDirty).toHaveBeenCalledWith(true);
});

it('keeps manual editing available when the template service fails', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => String(url).includes('/ontology/templates')
    ? new Response(JSON.stringify({ error: { message: '模板暂时不可用' } }), { status: 503 })
    : new Response(JSON.stringify({ items: [], total: 0 })));
  render(<OntologyEditor {...props} />);
  await screen.findByRole('alert');
  fireEvent.change(screen.getByLabelText('变更名称'), { target: { value: '自己的业务' } });
  fireEvent.click(screen.getByRole('button', { name: '添加业务类型' }));
  expect(screen.getByLabelText('类型标识')).toBeTruthy();
  expect((screen.getByLabelText('变更名称') as HTMLInputElement).value).toBe('自己的业务');
});

it('never offers template replacement for an existing definition or mapping', () => {
  const fetch = vi.spyOn(globalThis, 'fetch');
  render(<OntologyEditor {...props} initialDefinition={template.definition} />);
  expect(screen.queryByRole('heading', { name: '先有一个能用的模型，再逐步细化。' })).toBeNull();
  expect(fetch.mock.calls.some(([url]) => String(url).includes('/ontology/templates'))).toBe(false);
});


it('shows an asynchronous validation in the validation step rather than the publication step', () => {
  const { container } = render(<ModelWorkflow status="validating" dirty={false} />);
  expect(container.querySelector('[aria-current="step"]')?.textContent).toContain('校验与修正');
  expect(container.querySelector('[aria-current="step"]')?.textContent).not.toContain('发布');
});
