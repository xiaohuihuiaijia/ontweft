export type RecordData = Record<string, unknown>;
export interface Project { id: string; name: string; role?: string; permissions?: string[] }
export interface Session { user: { id: string; username?: string | null; email: string; display_name: string; admin?: boolean }; tenant: { id: string; name: string }; projects: Project[]; permissions: string[]; features?: string[]; actions_available?: boolean; model_approval?: 'single' | 'separate' }
export interface ListResult<T> { items: T[]; total: number }
export type DataType = 'string' | 'integer' | 'number' | 'boolean' | 'date' | 'datetime' | 'enum' | 'json';
export interface PropertyDef { key: string; label: string; data_type: DataType; required?: boolean; enum?: string[]; unit?: string; source_owned?: boolean; read_roles?: string[]; write_roles?: string[] }
export interface RelationDef { key: string; label: string; target_type: string; cardinality: 'one' | 'many'; properties?: PropertyDef[] }
export interface TypeDef { key: string; label: string; description?: string; aliases?: string[]; properties: PropertyDef[]; unique_keys?: string[][]; key_semantics?: 'legacy' | 'canonical'; relations: RelationDef[] }
export interface TypeList extends ListResult<TypeDef> { release_id?: string | null; version?: number }
export interface FieldMapping { source_field: string; transform: 'identity' | 'trim' | 'lower' | 'upper' | 'integer' | 'number' | 'boolean' | 'date' }
export interface MappingDef { key: string; source_id: string; resource_id: string; type_key: string; schema_fingerprint: string; identity_fields?: string[]; fields: Record<string, FieldMapping> }
export interface Definition { types: TypeDef[]; mappings?: MappingDef[] }
export interface DefinitionResult { definition: Definition; release_id?: string | null; version: number }
export interface ModelApproval { userId: string; mode?: 'single' | 'separate' }
export interface ModelDiffItem { kind: 'type' | 'property' | 'relation' | 'relation_property' | 'mapping'; type_key: string; key: string; change: 'added' | 'modified' | 'removed'; risk: 'display' | 'compatible' | 'review'; label: string; message: string }
export interface ModelDiff { items: ModelDiffItem[]; counts: { added: number; modified: number; removed: number }; affected_type_keys: string[]; affected_mapping_keys: string[] }
export interface ModelImpact { objects: number | null; relations: number | null; complete: boolean; scope: 'validation_scan'; affected_type_keys: string[] }
export interface ChangeSet { author_id?: string; id: string; project_id: string; title: string; revision: number; status: string; definition: Definition; baseline_release_id?: string; model_diff?: ModelDiff; validation?: { valid: boolean; errors: unknown[]; job_id?: string; objects_checked?: number; impact?: ModelImpact }; approved_by?: string; validation_job?: { id: string; status: string; error?: string; objects_checked: number } }
export interface BusinessObject { id: string; project_id: string; type_key: string; properties: RecordData; evidence?: unknown[]; revision: number; data_status?: 'active' | 'conflict' | 'incomplete' | 'withdrawn'; model_release_id?: string | null; model_version?: number; created_at?: string; updated_at?: string }
export interface GraphNode { id: string; label: string; type_key: string; type_label?: string; visible_property_count?: number; kind: string; properties: RecordData; revision?: number }
export interface GraphEdge { id: string; source: string; target: string; label: string; type_key: string; kind: string; cardinality?: 'one' | 'many' }
export interface GraphResult { nodes: GraphNode[]; edges: GraphEdge[]; truncated: boolean; release_id?: string | null; version?: number }
export type GraphKind = 'instances' | 'model' | 'knowledge';
