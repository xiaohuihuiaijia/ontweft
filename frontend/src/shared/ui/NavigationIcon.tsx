import type { ReactNode } from 'react';

const shapes = {
  sources: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" /></>,
  ontology: <><rect x="8" y="2" width="8" height="6" rx="1.5" /><rect x="2" y="16" width="7" height="6" rx="1.5" /><rect x="15" y="16" width="7" height="6" rx="1.5" /><path d="M12 8v4M5.5 16v-4h13v4" /></>,
  objects: <><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></>,
  graph: <><path d="m7 7 10 2M6 9l3 9m3 1 6-7" /><circle cx="5" cy="6" r="3" /><circle cx="20" cy="10" r="3" /><circle cx="10" cy="20" r="2.5" /></>,
  documents: <><path d="M14 2H5a1 1 0 0 0-1 1v18a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V8l-6-6Zm0 0v6h6M8 12h8M8 16h6" /></>,
  knowledge: <><path d="M12 5C9 3 5 3 2 4v16c3-1 7-1 10 1 3-2 7-2 10-1V4c-3-1-7-1-10 1Zm0 0v16M5 8h3M16 8h3" /></>,
  'content-search': <><circle cx="10.5" cy="10.5" r="7.5" /><path d="m16 16 5 5M7 9h7M7 12h5" /></>,
  jobs: <><circle cx="12" cy="12" r="9" /><path d="M12 6v6l4 2" /></>,
  functions: <><path d="M16 4h-2c-2 0-3 1-3 3L9 17c-.4 2-1 3-3 3H5M6 10h10M19 8l3 4-3 4" /></>,
  actions: <><path d="M3 7h17m-4-4 4 4-4 4M21 17H4m4-4-4 4 4 4" /></>,
  audit: <><path d="m3 6 1.5 1.5L7 4M10 6h11M3 13h4m3 0h11M3 20h4m3 0h11" /></>,
  settings: <><path d="m10 2-.8 3-2.5 1.4-3-.8-2 3.4L4 11v2l-2.3 2 2 3.4 3-.8L9.2 19l.8 3h4l.8-3 2.5-1.4 3 .8 2-3.4-2.3-2v-2l2.3-2-2-3.4-3 .8L14.8 5 14 2Z" /><circle cx="12" cy="12" r="3" /></>,
} satisfies Record<string, ReactNode>;

export function NavigationIcon({ name }: { name: keyof typeof shapes }) {
  return <svg className="navigation-icon" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{shapes[name]}</svg>;
}
