/** Original, small outline icons shared by graph controls. */
export function GraphIcon({ name }: { name: 'search' | 'edit' | 'fit' | 'focus' | 'pin' | 'save' | 'restore' }) {
  const paths = {
    search: 'M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13ZM15.5 15.5 21 21',
    edit: 'm14 5 5 5M4 20l5-1L20 8a2 2 0 0 0-5-5L4 14v6Z',
    fit: 'M9 4H4v5m11-5h5v5M4 15v5h5m11-5v5h-5',
    focus: 'M12 2v4m0 12v4M2 12h4m12 0h4M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z',
    pin: 'm9 3 6 0-1 6 3 3v2H7v-2l3-3-1-6Zm3 11v7',
    save: 'M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h12l4 4v12a2 2 0 0 1-2 2ZM7 3v6h9V3M7 21v-8h10v8',
    restore: 'M3 10a9 9 0 1 1 1 8M3 4v6h6m3-3v5l3 2',
  };
  return <svg className="graph-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>;
}
