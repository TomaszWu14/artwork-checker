// Print-only app — renders each variant on its own page.
// No design canvas; we just stack the artboards with page-breaks.

function PrintApp() {
  // Each variant at its native size; we scale it down via CSS transform
  // so it fits its print page (defined in CSS). Width/height here just
  // match the artboard's native pixel dimensions.
  const pages = [
    { id:'a', orient:'portrait',  w: 920,  h: 1640, label:'A · Raport audytowy',   Component: VariantA },
    { id:'b', orient:'landscape', w: 1440, h: 900,  label:'B · Workspace inspekcji', Component: VariantB },
    { id:'c', orient:'landscape', w: 1280, h: 1100, label:'C · Review board',       Component: VariantC },
    { id:'d', orient:'landscape', w: 1280, h: 1100, label:'D · Annotated zoom',     Component: VariantD },
    { id:'e', orient:'portrait',  w: 920,  h: 1480, label:'E · Email summary',      Component: VariantE },
  ];
  return (
    <>
      {pages.map((p) => (
        <section key={p.id} className={`print-page ${p.orient}`}>
          <div className="page-meta">
            <span className="page-meta-id">{p.id.toUpperCase()}</span>
            <span className="page-meta-title">{p.label}</span>
            <span className="page-meta-dim">{p.w} × {p.h}</span>
          </div>
          <div className="artboard-scale" style={{
            width: p.w, height: p.h,
            ['--native-w']: p.w + 'px',
            ['--native-h']: p.h + 'px',
          }}>
            <p.Component />
          </div>
        </section>
      ))}
    </>
  );
}

// Wait for fonts to settle, then auto-print.
(async () => {
  try { await document.fonts.ready; } catch {}
  // give React + Babel time to mount
  await new Promise((r) => setTimeout(r, 800));
  const root = ReactDOM.createRoot(document.getElementById('root'));
  root.render(<PrintApp />);
  // give layout one more tick, then call print
  await new Promise((r) => setTimeout(r, 1500));
  window.print();
})();
