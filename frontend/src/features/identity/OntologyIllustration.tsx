// A conceptual source-to-business diagram, not sample customer data or a UI mock.
export function OntologyIllustration() {
  return <figure className="ontology-illustration">
    <svg viewBox="0 0 560 242" role="img" aria-label="数据库、文档和知识库，经业务本体组织成对象、关系、函数与动作">
      <g className="ontology-illustration__edges" fill="none" strokeWidth="1.5">
        <path d="M120 44h40a16 16 0 0 1 16 16v45a16 16 0 0 0 16 16h26M120 121h98M120 198h40a16 16 0 0 0 16-16v-45a16 16 0 0 1 16-16h26" />
        <path d="M350 121h26a16 16 0 0 0 16-16V77a16 16 0 0 1 16-16h30M350 121h26a16 16 0 0 1 16 16v28a16 16 0 0 0 16 16h30" />
      </g>
      <g className="ontology-illustration__source">
        <rect x="0.75" y="18" width="119" height="52" rx="8" /><rect x="0.75" y="95" width="119" height="52" rx="8" /><rect x="0.75" y="172" width="119" height="52" rx="8" />
      </g>
      <g className="ontology-illustration__icons" fill="none" stroke="currentColor" strokeWidth="1.5">
        <ellipse cx="26" cy="38" rx="7" ry="3" /><path d="M19 38v12c0 4 14 4 14 0V38M19 44c0 4 14 4 14 0M19 111h10l5 5v17H19zM29 111v6h5M23 122h7M23 127h7M18 189h7a4 4 0 0 1 4 4v16a4 4 0 0 0-4-4h-7zM29 193a4 4 0 0 1 4-4h7v16h-7a4 4 0 0 0-4 4" />
      </g>
      <g className="ontology-illustration__labels"><text x="48" y="49">数据库</text><text x="48" y="126">文档</text><text x="48" y="203">知识库</text></g>
      <rect className="ontology-illustration__core" x="218" y="65" width="132" height="112" rx="12" />
      <g className="ontology-illustration__core-icon" fill="none" strokeWidth="1.6"><rect x="278" y="86" width="12" height="12" rx="2" /><path d="M284 98v9m-22 0h44M262 107v8m22-8v8m22-8v8" /><rect x="257" y="115" width="10" height="10" rx="2" /><rect x="279" y="115" width="10" height="10" rx="2" /><rect x="301" y="115" width="10" height="10" rx="2" /></g>
      <text className="ontology-illustration__core-label" x="284" y="151" textAnchor="middle">业务本体</text>
      <g className="ontology-illustration__source"><rect x="438" y="35" width="120" height="52" rx="8" /><rect x="438" y="155" width="120" height="52" rx="8" /></g>
      <g className="ontology-illustration__labels"><text x="498" y="66" textAnchor="middle">对象 / 关系</text><text x="498" y="186" textAnchor="middle">函数 / 动作</text></g>
    </svg>
  </figure>;
}
