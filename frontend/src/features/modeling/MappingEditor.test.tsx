import { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { MappingEditor } from './MappingEditor';
import type { MappingDef, TypeDef } from '../../shared/model/types';
it('clears the previous identity binding when the target business type changes', async () => {
  const types:TypeDef[] = [
    {key:'customer',label:'客户',properties:[{key:'code',label:'编号',data_type:'string',source_owned:true}],unique_keys:[['code']],relations:[]},
    {key:'order',label:'订单',properties:[{key:'number',label:'单号',data_type:'string',source_owned:true}],unique_keys:[['number']],relations:[]},
  ];
  let latest:MappingDef[] = [];
  function Example() {
    const [value,setValue] = useState<MappingDef[]>([{key:'map',source_id:'s',resource_id:'r',type_key:'customer',identity_fields:['code'],schema_fingerprint:'x',fields:{code:{source_field:'code',transform:'identity'}}}]);
    latest=value;
    return <MappingEditor projectId="p" types={types} value={value} onChange={setValue} disabled={false} canReadSources={false} />;
  }
  render(<Example />);
  fireEvent.change(screen.getByLabelText('目标业务类型'),{target:{value:'order'}});
  expect((screen.getByLabelText('跨来源身份绑定') as HTMLSelectElement).value).toBe('');
  expect(latest[0].identity_fields ?? []).toEqual([]);
});


it('shows a saved identity binding missing from the current type without changing it silently', () => {
  const value:MappingDef[] = [{key:'map',source_id:'s',resource_id:'r',type_key:'order',identity_fields:['legacy_code'],schema_fingerprint:'x',fields:{}}];
  render(<MappingEditor projectId="p" types={[{key:'order',label:'订单',properties:[],unique_keys:[['number']],relations:[]}]} value={value} onChange={() => {}} disabled={false} canReadSources={false} />);
  expect((screen.getByLabelText('跨来源身份绑定') as HTMLSelectElement).value).toBe('legacy_code');
  screen.getByText(/原身份绑定不属于当前类型的唯一键/);
});
