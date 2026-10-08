// ── VARIANT A ──────────────────────────────────────────────
// Print-grade audit raport. Evolution of the existing DocCompare v6
// layout: kept the dense header / summary / modules / details rhythm,
// added artwork preview tiles inline with each module and a per-diff
// row that shows A vs B values with stylized chips.
//
// Optimized to print to A4 (842 × 1191 at 72dpi → we use 920 × 1300 for
// comfy on-screen reading; chrome handles fit-to-page on export).

function VariantA() {
  return (
    <div style={{
      background: 'var(--paper)',
      width: '100%',
      minHeight: '100%',
      padding: '36px 44px',
      fontFamily: "'Geist', sans-serif",
      color: 'var(--ink)',
      fontSize: 12,
      lineHeight: 1.45,
    }}>
      <ARHeader />
      <div style={{height: 24}} />
      <ARTopGrid />
      <div style={{height: 28}} />
      <ARModules />
      <div style={{height: 28}} />
      <ARArtworkOverview />
      <div style={{height: 28}} />
      <ARCriticalSection />
      <div style={{height: 24}} />
      <ARWarningSection />
      <div style={{height: 24}} />
      <ARFooter />
    </div>
  );
}

function ARHeader() {
  return (
    <header>
      <div style={{display:'flex', justifyContent:'space-between', alignItems:'flex-end', gap: 16}}>
        <div>
          <div style={{
            fontFamily: "'Newsreader', serif",
            fontStyle: 'italic',
            fontSize: 13, color: 'var(--ink-3)',
            letterSpacing: '.01em',
          }}>ACME · ArtCompare v1.0</div>
          <h1 style={{
            margin: '6px 0 0',
            fontFamily: "'Newsreader', serif",
            fontWeight: 500, fontSize: 38, letterSpacing: '-.015em',
            lineHeight: 1.05,
          }}>
            Raport porównania <span style={{fontStyle:'italic'}}>artworków</span>
          </h1>
          <div style={{
            marginTop: 6, fontSize: 12, color: 'var(--ink-2)',
          }}>{CMP.product} &nbsp;·&nbsp; <span className="mono" style={{color:'var(--ink-3)'}}>{CMP.sku}</span></div>
        </div>
        <div style={{textAlign:'right', minWidth: 230}}>
          <AcmeWordmark />
          <div style={{
            marginTop: 10,
            display:'inline-flex', gap: 10,
            fontFamily:"'Geist Mono', monospace", fontSize: 11, color:'var(--ink-3)',
          }}>
            <div><span style={{color:'var(--ink-4)'}}>NR</span>&nbsp;{CMP.no}</div>
            <div><span style={{color:'var(--ink-4)'}}>·</span></div>
            <div>{CMP.date} {CMP.time}</div>
          </div>
        </div>
      </div>
      <div style={{height: 16}} />
      <div style={{height: 1, background:'var(--rule-3)'}} />
      <div style={{height: 4}} />
      <div style={{height: 0.5, background:'var(--rule-3)'}} />
    </header>
  );
}

function ARTopGrid() {
  // 3 columns: Dokument A, Dokument B, Werdykt
  const cellStyle = {
    background:'var(--paper-2)',
    padding: '14px 16px',
    borderRadius: 2,
  };
  const Verdict = ({ count, label, color, bg }) => (
    <div style={{
      flex: 1, padding: '12px 10px',
      background: bg, borderRadius: 2, textAlign:'center',
    }}>
      <div style={{
        fontFamily:"'Geist Mono', monospace", fontSize: 28, fontWeight: 600, color,
        lineHeight: 1, fontVariantNumeric:'tabular-nums',
      }}>{count}</div>
      <div style={{marginTop: 4, fontSize: 10, color, letterSpacing:'.06em', textTransform:'uppercase'}}>{label}</div>
    </div>
  );
  return (
    <section style={{display:'grid', gridTemplateColumns:'1fr 1fr 1.2fr', gap: 12}}>
      <DocCell label="Dokument A · źródło" file={CMP.fileA} />
      <DocCell label="Dokument B · porównywany" file={CMP.fileB} highlight />
      <div style={cellStyle}>
        <div style={{fontSize:10, color:'var(--ink-3)', letterSpacing:'.08em', textTransform:'uppercase'}}>
          Werdykt automatyczny
        </div>
        <div style={{marginTop: 10, display:'flex', gap: 6}}>
          <Verdict count={CMP.summary.critical} label="Krytyczne" color="var(--critical)" bg="var(--critical-bg)" />
          <Verdict count={CMP.summary.warning}  label="Ostrzeżenia" color="var(--warn)" bg="var(--warn-bg)" />
          <Verdict count={CMP.summary.info}     label="Info" color="var(--info)" bg="var(--info-bg)" />
          <Verdict count={CMP.summary.ok}       label="Zgodnych" color="var(--ok)" bg="var(--ok-bg)" />
        </div>
        <div style={{marginTop: 12, display:'flex', alignItems:'center', gap: 10}}>
          <div style={{
            padding:'5px 9px',
            background:'var(--critical)', color:'var(--paper)',
            fontFamily:"'Geist Mono', monospace", fontSize: 10, fontWeight: 600,
            letterSpacing:'.08em',
          }}>NIE DOPUSZCZAĆ DO DRUKU</div>
          <div style={{fontSize: 11, color:'var(--ink-3)'}}>3 rozbieżności blokujące</div>
        </div>
      </div>
    </section>
  );
}

function DocCell({ label, file, highlight }) {
  return (
    <div style={{
      background: highlight ? 'var(--paper-3)' : 'var(--paper-2)',
      padding: '14px 16px',
      borderRadius: 2,
      borderLeft: highlight ? '2px solid #14130f' : '2px solid transparent',
    }}>
      <div style={{display:'flex', justifyContent:'space-between', alignItems:'center'}}>
        <div style={{fontSize:10, color:'var(--ink-3)', letterSpacing:'.08em', textTransform:'uppercase'}}>{label}</div>
        <div style={{
          fontFamily:"'Geist Mono', monospace", fontSize:10, color:'var(--ink-2)',
          background:'var(--paper-3)', padding:'2px 6px', borderRadius:2,
        }}>{file.version}</div>
      </div>
      <div style={{marginTop:8, fontFamily:"'Geist Mono', monospace", fontSize: 12, color:'var(--ink)', wordBreak:'break-all'}}>
        {file.name}
      </div>
      <div style={{marginTop:10, display:'grid', gridTemplateColumns:'1fr 1fr', gap:'4px 12px'}}>
        <KV k="Data" v={file.date} />
        <KV k="Autor" v={file.author} />
        <KV k="Strony" v={`${file.pages}`} mono />
        <KV k="Rozmiar" v={file.size} mono />
        <KV k="Format" v={file.dim} mono />
        <KV k="Spad" v={file.bleed} mono />
        <KV k="Kolory" v={file.colors} />
        <KV k="Fonty" v={file.fonts} />
      </div>
    </div>
  );
}

function ARModules() {
  return (
    <section>
      <SectionHead n="01" title="Podsumowanie modułów" hint="8 modułów detekcji · 34 sprawdzane pola" />
      <table style={{width:'100%', borderCollapse:'collapse', fontSize:12}}>
        <thead>
          <tr style={{textAlign:'left', color:'var(--ink-3)'}}>
            <th style={th(36)}></th>
            <th style={th()}>Moduł</th>
            <th style={th(110)}>Status</th>
            <th style={th(70)}>Krytyczne</th>
            <th style={th(80)}>Ostrzeżenia</th>
            <th style={th(60)}>OK</th>
            <th style={th()}>Komentarz</th>
          </tr>
        </thead>
        <tbody>
          {CMP.modules.map((m, i) => (
            <tr key={m.id} style={{borderTop:'0.5px solid var(--rule)'}}>
              <td style={td()}>
                <span style={{
                  display:'inline-flex', alignItems:'center', justifyContent:'center',
                  width: 22, height: 22, borderRadius: 2,
                  background:'var(--paper-2)', fontFamily:"'Geist Mono', monospace",
                  fontSize: 11, color:'var(--ink-2)',
                }}>{CATEGORIES[m.id]?.icon || '·'}</span>
              </td>
              <td style={td()}><div style={{fontWeight:500}}>{m.name}</div></td>
              <td style={td()}>
                <SevTag sev={m.status === 'fail' ? 'crit' : m.status === 'warn' ? 'warn' : m.status === 'info' ? 'info' : 'ok'} />
              </td>
              <td style={{...td(), fontFamily:"'Geist Mono', monospace", color: m.crit ? 'var(--critical)' : 'var(--ink-4)', fontWeight: m.crit ? 600 : 400}}>{m.crit}</td>
              <td style={{...td(), fontFamily:"'Geist Mono', monospace", color: m.warn ? 'var(--warn)' : 'var(--ink-4)', fontWeight: m.warn ? 600 : 400}}>{m.warn}</td>
              <td style={{...td(), fontFamily:"'Geist Mono', monospace", color:'var(--ok)'}}>{m.ok}</td>
              <td style={{...td(), color:'var(--ink-2)'}}>{m.note}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

const th = (w) => ({
  padding: '8px 8px 8px 0', fontSize: 10, fontWeight: 500,
  letterSpacing:'.06em', textTransform:'uppercase',
  borderBottom: '0.5px solid var(--rule-3)',
  width: w, color:'var(--ink-3)',
});
const td = () => ({ padding: '10px 8px 10px 0', verticalAlign:'top' });

function SectionHead({ n, title, hint }) {
  return (
    <div style={{display:'flex', alignItems:'flex-end', gap: 12, marginBottom: 10}}>
      <div style={{
        fontFamily:"'Geist Mono', monospace", fontSize: 10,
        color:'var(--ink-3)', letterSpacing:'.08em',
      }}>{n}</div>
      <h2 style={{
        margin:0, fontFamily:"'Newsreader', serif", fontWeight: 500,
        fontSize: 20, letterSpacing:'-.01em',
      }}>{title}</h2>
      {hint && (
        <div style={{
          flex: 1, paddingBottom: 4, paddingLeft: 8,
          borderBottom: '0.5px dotted var(--rule-2)',
          fontSize: 11, color:'var(--ink-3)',
          alignSelf:'baseline',
        }}>{hint}</div>
      )}
    </div>
  );
}

function ARArtworkOverview() {
  // Marker positions are % of the viewbox, hand-tuned to the SVG
  const aMarkers = [
    { id:'D-05', sev:'crit', x: 3, y: 4, w: 18, h: 14, label:'D-05' },
    { id:'D-02', sev:'crit', x: 4, y: 45, w: 38, h: 8, label:'D-02', side:'right' },
    { id:'D-03', sev:'crit', x: 4, y: 73, w: 38, h: 10, label:'D-03', side:'right' },
  ];
  const bMarkers = [
    { id:'D-01', sev:'crit', x: 72, y: 75, w: 24, h: 17, label:'D-01' },
    { id:'D-04', sev:'crit', x: 4, y: 75, w: 18, h: 8, label:'D-04', side:'right' },
    { id:'D-06', sev:'warn', x: 85, y: 3, w: 12, h: 11, label:'D-06', side:'left' },
    { id:'D-10', sev:'warn', x: 30, y: 78, w: 12, h: 8, label:'D-10', side:'right' },
  ];
  return (
    <section>
      <SectionHead n="02" title="Podgląd artworków" hint="Diff wizualny — krytyczne i wybrane ostrzeżenia" />
      <ComparePair width="100%" markers={{a: aMarkers, b: bMarkers}} />
      <div style={{
        marginTop: 12, display:'flex', gap: 14, fontSize: 10, color:'var(--ink-3)',
        fontFamily:"'Geist Mono', monospace",
      }}>
        <LegendDot color="var(--critical)" label="Krytyczne (blokujące + niezgodne)" />
        <LegendDot color="var(--warn)" label="Ostrzeżenia (przesunięcia, kolor, czcionki)" />
        <LegendDot color="var(--info)" label="Informacyjne (metadane)" />
        <div style={{marginLeft:'auto'}}>spad / bleed · linia kropkowana</div>
      </div>
    </section>
  );
}

function LegendDot({ color, label }) {
  return (
    <div style={{display:'inline-flex', alignItems:'center', gap: 6}}>
      <span style={{width: 8, height: 8, borderRadius: 2, background: color, display:'inline-block'}} />
      <span style={{textTransform:'uppercase', letterSpacing:'.06em'}}>{label}</span>
    </div>
  );
}

function ARCriticalSection() {
  const crits = CMP.diffs.filter((d) => d.sev === 'crit');
  return (
    <section>
      <SectionHead n="03" title="Krytyczne rozbieżności" hint={`${crits.length} pozycji · ${crits.filter(c=>c.blocker).length} blokujących druk`} />
      <div style={{display:'flex', flexDirection:'column'}}>
        {crits.map((d, i) => <DiffRow key={d.id} d={d} idx={i} dense={false} />)}
      </div>
    </section>
  );
}

function ARWarningSection() {
  const warns = CMP.diffs.filter((d) => d.sev === 'warn');
  return (
    <section>
      <SectionHead n="04" title="Ostrzeżenia" hint={`${warns.length} pozycji do przeglądu`} />
      <table style={{width:'100%', borderCollapse:'collapse', fontSize: 12}}>
        <thead>
          <tr>
            <th style={th(46)}>ID</th>
            <th style={th(90)}>Kategoria</th>
            <th style={th()}>Pole</th>
            <th style={th()}>Wartość A</th>
            <th style={th()}>Wartość B</th>
            <th style={th()}>Komentarz</th>
          </tr>
        </thead>
        <tbody>
          {warns.map((d) => (
            <tr key={d.id} style={{borderTop:'0.5px solid var(--rule)'}}>
              <td style={{...td(), fontFamily:"'Geist Mono', monospace", color:'var(--ink-3)'}}>{d.id}</td>
              <td style={td()}><span style={{
                fontSize: 10, padding:'2px 6px', background:'var(--paper-2)',
                color:'var(--ink-2)', letterSpacing:'.04em', textTransform:'uppercase',
                borderRadius: 2,
              }}>{CATEGORIES[d.cat]?.label}</span></td>
              <td style={{...td(), fontWeight:500}}>{d.field}</td>
              <td style={{...td(), fontFamily:"'Geist Mono', monospace", color:'var(--ink-2)'}}>{d.a}</td>
              <td style={{...td(), fontFamily:"'Geist Mono', monospace", color:'var(--warn)'}}>{d.b}</td>
              <td style={{...td(), color:'var(--ink-2)'}}>{d.note}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function DiffRow({ d, idx }) {
  return (
    <div style={{
      padding: '14px 0',
      borderTop: idx === 0 ? '0.5px solid var(--rule-3)' : '0.5px solid var(--rule)',
      display: 'grid',
      gridTemplateColumns: '60px 1fr 1fr 1fr',
      gap: 16,
      alignItems:'start',
    }}>
      <div>
        <div style={{
          fontFamily:"'Geist Mono', monospace", fontSize: 12, fontWeight: 600,
          color:'var(--critical)',
        }}>{d.id}</div>
        <div style={{marginTop: 6}}>
          <SevTag sev={d.sev} />
        </div>
        {d.blocker && (
          <div style={{
            marginTop: 6, fontSize: 9, padding: '2px 5px',
            background:'var(--critical)', color:'var(--paper)',
            display:'inline-block', letterSpacing:'.08em',
            fontFamily:"'Geist Mono', monospace",
          }}>BLOKER</div>
        )}
      </div>
      <div>
        <div style={{
          fontSize: 11, color:'var(--ink-3)',
          letterSpacing:'.06em', textTransform:'uppercase',
        }}>{CATEGORIES[d.cat]?.label} · {d.field}</div>
        <div style={{marginTop: 4, fontSize: 13, color:'var(--ink)', lineHeight: 1.4}}>{d.note}</div>
        <div style={{marginTop: 8, fontSize: 11, color:'var(--ink-3)'}}>
          <span style={{fontFamily:"'Geist Mono', monospace"}}>↳ </span>
          {d.where}
        </div>
      </div>
      <ValueCell label="A · v1.4" value={d.a} tone="neutral" />
      <ValueCell label="B · v1.5" value={d.b} tone="diff" />
    </div>
  );
}

function ValueCell({ label, value, tone }) {
  const isDiff = tone === 'diff';
  return (
    <div style={{
      background: isDiff ? 'var(--critical-bg)' : 'var(--paper-2)',
      borderLeft: isDiff ? '2px solid var(--critical)' : '2px solid var(--ink-4)',
      padding: '10px 12px',
      borderRadius: 0,
    }}>
      <div style={{
        fontSize: 10, color: isDiff ? 'var(--critical)' : 'var(--ink-3)',
        letterSpacing:'.06em', textTransform:'uppercase', fontWeight: 500,
      }}>{label}</div>
      <div style={{
        marginTop: 6, fontFamily: "'Geist Mono', monospace",
        fontSize: 12, color: 'var(--ink)', wordBreak:'break-word',
        lineHeight: 1.4,
      }}>
        {value || '—'}
      </div>
    </div>
  );
}

function ARFooter() {
  return (
    <footer style={{
      marginTop: 24, paddingTop: 14, borderTop: '0.5px solid var(--rule-3)',
      display:'flex', justifyContent:'space-between',
      fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--ink-3)',
      letterSpacing:'.04em',
    }}>
      <div>
        ArtCompare v1.0 · ACME sp. z o.o.
      </div>
      <div>
        Wygenerowano: {CMP.date} {CMP.time} · porównanie {CMP.no} · str. 1/1
      </div>
    </footer>
  );
}

Object.assign(window, { VariantA });
