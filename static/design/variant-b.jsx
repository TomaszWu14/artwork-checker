// ── VARIANT B ──────────────────────────────────────────────
// Operational inspection workspace. Full-bleed app screen:
//   ┌────────────────────────────────────────────────────────────┐
//   │ topbar: org · run · status · users · actions               │
//   ├──────┬───────────────────────────────────────────────┬─────┤
//   │ runs │   viewer (side-by-side / overlay / onion)     │     │
//   │ tree │   ───── selected diff focus inset ─────       │ diff│
//   │      │                                                │list │
//   │      │   timeline rail + tools                       │     │
//   └──────┴───────────────────────────────────────────────┴─────┘
//
// Component is sized to 1440 × 900 (laptop class).

const vbDims = { W: 1440, H: 900 };

function VariantB() {
  const [mode, setMode] = React.useState('split');    // split | overlay | onion
  const [selected, setSelected] = React.useState('D-01');
  const [filter, setFilter] = React.useState('all');  // all | crit | warn | info

  const diffs = CMP.diffs.filter((d) => filter === 'all' ? true : d.sev === filter);
  const sel = CMP.diffs.find((d) => d.id === selected);

  return (
    <div style={{
      width: vbDims.W, height: vbDims.H,
      background: 'var(--paper)',
      fontFamily:"'Geist', sans-serif",
      color:'var(--ink)', fontSize: 13,
      display:'flex', flexDirection:'column',
      overflow:'hidden',
    }}>
      <BTopbar />
      <div style={{flex:1, display:'flex', minHeight: 0}}>
        <BLeftRail selected={selected} />
        <BViewer mode={mode} setMode={setMode} selected={sel} />
        <BRightPanel diffs={diffs} selected={selected} setSelected={setSelected}
                     filter={filter} setFilter={setFilter}
                     sel={sel} />
      </div>
      <BStatusBar />
    </div>
  );
}

// ── topbar ─────────────────────────────────────────────────
function BTopbar() {
  return (
    <header style={{
      height: 52, flexShrink: 0,
      borderBottom: '0.5px solid var(--rule-2)',
      background:'var(--paper-2)',
      display:'flex', alignItems:'center', padding:'0 18px',
      gap: 18,
    }}>
      <div style={{display:'flex', alignItems:'center', gap: 10}}>
        <AcmeWordmark size={12} />
        <span style={{color:'var(--ink-4)'}}>›</span>
        <span style={{fontWeight:500}}>ArtCompare</span>
        <span style={{
          fontSize: 10, padding:'2px 6px', background:'var(--paper-2)',
          color:'var(--ink-3)', letterSpacing:'.06em',
          fontFamily:"'Geist Mono', monospace", borderRadius: 2,
        }}>v1.0 BETA</span>
      </div>
      <div style={{height: 18, width: 1, background:'var(--rule-2)'}} />
      <div style={{display:'flex', alignItems:'center', gap: 8, fontFamily:"'Geist Mono', monospace", fontSize: 11}}>
        <span style={{color:'var(--ink-3)'}}>RUN</span>
        <span>{CMP.no}</span>
        <span style={{color:'var(--ink-4)'}}>/</span>
        <span>{CMP.sku}</span>
        <span style={{color:'var(--ink-4)'}}>·</span>
        <span style={{color:'var(--ink-3)'}}>{CMP.date} {CMP.time}</span>
      </div>
      <div style={{flex:1}} />
      <div style={{
        display:'inline-flex', alignItems:'center', gap: 6,
        padding:'5px 10px', background:'var(--critical-bg)', color:'var(--critical)',
        fontFamily:"'Geist Mono', monospace", fontSize: 11, fontWeight: 600,
        borderRadius: 2, letterSpacing:'.04em',
      }}>
        <span style={{width:6, height:6, borderRadius:'50%', background:'var(--critical)'}} />
        NIE DOPUSZCZAĆ DO DRUKU
      </div>
      <div style={{display:'flex', gap: 6, alignItems:'center'}}>
        <Avatar initials="AK" tone="ink" />
        <Avatar initials="MN" tone="warn" />
        <Avatar initials="JZ" tone="ok" />
        <div style={{fontSize: 11, color:'var(--ink-3)', marginLeft: 4}}>3 online</div>
      </div>
      <div style={{height: 18, width: 1, background:'var(--rule-2)', marginLeft: 8}} />
      <BButton variant="ghost">Eksport</BButton>
      <BButton variant="dark">Wyślij do dostawcy</BButton>
    </header>
  );
}

function Avatar({ initials, tone }) {
  const colors = {
    ink: { bg:'var(--rule-3)', fg:'var(--ink)' },
    warn:{ bg:'var(--warn)', fg:'var(--paper)' },
    ok:  { bg:'var(--ok)', fg:'var(--paper)' },
  }[tone];
  return (
    <div style={{
      width: 26, height: 26, borderRadius:'50%', background: colors.bg, color: colors.fg,
      display:'flex', alignItems:'center', justifyContent:'center',
      fontSize: 10, fontWeight: 600, fontFamily:"'Geist Mono', monospace",
    }}>{initials}</div>
  );
}

function BButton({ variant='ghost', children, onClick, active }) {
  const styles = {
    ghost: { background:'transparent', color:'var(--ink-2)', border:'0.5px solid var(--rule-2)' },
    dark:  { background:'var(--accent)', color:'var(--paper)', border:'0.5px solid var(--accent)' },
    pill:  { background:'transparent', color:'var(--ink-2)', border:'0.5px solid var(--rule-2)' },
  }[variant];
  return (
    <button onClick={onClick} style={{
      ...styles,
      padding:'6px 12px',
      fontSize: 11, fontFamily:"'Geist', sans-serif",
      fontWeight: 500, letterSpacing:'.02em',
      borderRadius: 2, cursor:'pointer',
      ...(active ? { background:'var(--accent)', color:'var(--paper)', borderColor:'var(--accent)' } : {}),
    }}>{children}</button>
  );
}

// ── left rail ───────────────────────────────────────────────
function BLeftRail({ selected }) {
  return (
    <aside style={{
      width: 240, flexShrink: 0,
      borderRight:'0.5px solid var(--rule-2)',
      background:'var(--paper-2)',
      display:'flex', flexDirection:'column',
    }}>
      <div style={{padding:'14px 16px 8px'}}>
        <div style={{fontSize:10, color:'var(--ink-3)', letterSpacing:'.08em', textTransform:'uppercase'}}>Bieżący run</div>
        <div style={{marginTop:6, fontWeight:500, fontSize:13, lineHeight:1.3}}>{CMP.product}</div>
        <div style={{marginTop:4, fontSize:11, color:'var(--ink-3)', fontFamily:"'Geist Mono', monospace"}}>{CMP.sku}</div>
      </div>
      <div style={{padding:'4px 12px'}}>
        <FileNode active label="v1.4 · źródło" sub={CMP.fileA.date} icon="A" tone="ink-2" />
        <FileNode active label="v1.5 · porównywany" sub={CMP.fileB.date} icon="B" tone="warn" highlight />
      </div>

      <div style={{padding:'18px 16px 6px'}}>
        <div style={{display:'flex', alignItems:'center', justifyContent:'space-between'}}>
          <div style={{fontSize:10, color:'var(--ink-3)', letterSpacing:'.08em', textTransform:'uppercase'}}>Historia porównań</div>
          <div style={{fontSize: 10, color:'var(--ink-4)'}}>8</div>
        </div>
      </div>
      <div style={{padding:'2px 12px', overflowY:'auto', flex: 1}}>
        <HistRow id="#08" sku="ZAY-easyCARE(200)" date="20.05" status="crit" current />
        <HistRow id="#07" sku="ZAY-easyCARE(100)" date="14.05" status="warn" />
        <HistRow id="#06" sku="ZAY-COMFIT(50)"    date="11.05" status="ok" />
        <HistRow id="#05" sku="ZAY-MEDISORB"      date="08.05" status="ok" />
        <HistRow id="#04" sku="ZAY-easyCARE(500)" date="02.05" status="warn" />
        <HistRow id="#03" sku="ZAY-DERMO-PRO"     date="28.04" status="crit" />
        <HistRow id="#02" sku="ZAY-COMFIT(200)"   date="22.04" status="ok" />
        <HistRow id="#01" sku="ZAY-easyCARE(50)"  date="18.04" status="ok" />
      </div>

      <div style={{
        padding:'12px 16px', borderTop:'0.5px solid var(--rule-2)',
        background:'var(--paper-2)',
      }}>
        <div style={{fontSize: 10, color:'var(--ink-3)', letterSpacing:'.08em', textTransform:'uppercase', marginBottom: 6}}>
          Reguły QA
        </div>
        <div style={{fontSize: 11, color:'var(--ink-2)', lineHeight: 1.6}}>
          MDR · PN-EN 980 · GS1 GTIN<br/>
          Pantone ΔE ≤ 2.0<br/>
          Min. typografia 7 pt
        </div>
      </div>
    </aside>
  );
}

function FileNode({ label, sub, icon, tone, highlight, active }) {
  return (
    <div style={{
      display:'flex', alignItems:'center', gap: 10,
      padding: '8px 10px', borderRadius: 3,
      background: highlight ? 'var(--paper-3)' : 'transparent',
      marginBottom: 2,
    }}>
      <div style={{
        width: 22, height: 28, borderRadius: 2,
        background: tone === 'warn' ? 'var(--warn)' : 'var(--rule-3)',
        color: tone === 'warn' ? 'var(--paper)' : 'var(--ink)',
        fontSize: 10, fontFamily:"'Geist Mono', monospace",
        display:'flex', alignItems:'center', justifyContent:'center',
        fontWeight: 600,
      }}>{icon}</div>
      <div style={{flex: 1, minWidth: 0}}>
        <div style={{fontSize: 12, fontWeight: 500}}>{label}</div>
        <div style={{fontSize: 10, color:'var(--ink-3)', fontFamily:"'Geist Mono', monospace"}}>{sub}</div>
      </div>
    </div>
  );
}

function HistRow({ id, sku, date, status, current }) {
  const color = status === 'crit' ? 'var(--critical)' : status === 'warn' ? 'var(--warn)' : 'var(--ok)';
  return (
    <div style={{
      display:'flex', alignItems:'center', gap: 8,
      padding: '6px 8px', borderRadius: 2,
      background: current ? 'var(--paper-2)' : 'transparent',
      marginBottom: 1, cursor:'pointer',
    }}>
      <span style={{width: 6, height: 6, borderRadius:'50%', background: color, flexShrink: 0}} />
      <span style={{fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--ink-3)', minWidth: 24}}>{id}</span>
      <span style={{flex: 1, fontSize: 11, color:'var(--ink-2)', overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap'}}>{sku}</span>
      <span style={{fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--ink-4)'}}>{date}</span>
    </div>
  );
}

// ── viewer ──────────────────────────────────────────────────
function BViewer({ mode, setMode, selected }) {
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
    <main style={{
      flex: 1, minWidth: 0,
      display:'flex', flexDirection:'column',
      background: 'var(--paper-2)',
    }}>
      <div style={{
        height: 44, flexShrink: 0,
        background:'var(--paper-2)', borderBottom: '0.5px solid var(--rule-2)',
        display:'flex', alignItems:'center', padding:'0 16px', gap: 10,
      }}>
        <div style={{display:'flex', gap: 2, padding: 2, background:'var(--paper-2)', borderRadius: 3}}>
          {[
            ['split','Side-by-side'],
            ['overlay','Overlay'],
            ['onion','Onion-skin'],
          ].map(([k, label]) => (
            <button key={k} onClick={() => setMode(k)} style={{
              padding:'5px 10px', fontSize:11, border:'none', cursor:'pointer',
              background: mode === k ? 'var(--paper-3)' : 'transparent',
              color: mode === k ? 'var(--ink)' : 'var(--ink-3)',
              fontWeight: mode === k ? 500 : 400,
              borderRadius: 2,
              boxShadow: mode === k ? '0 1px 2px rgba(0,0,0,.06)' : 'none',
            }}>{label}</button>
          ))}
        </div>
        <div style={{height: 18, width: 1, background:'var(--rule-2)'}} />
        <div style={{display:'flex', gap: 4, alignItems:'center'}}>
          <Toggle label="Spady" on />
          <Toggle label="Siatka" />
          <Toggle label="Linijka" on />
          <Toggle label="Etykiety" on />
        </div>
        <div style={{flex: 1}} />
        <div style={{fontFamily:"'Geist Mono', monospace", fontSize: 11, color:'var(--ink-3)', display:'flex', gap: 12}}>
          <span>218 × 142 mm</span>
          <span>·</span>
          <span>120% zoom</span>
          <span>·</span>
          <span>x 154.3 mm  y 88.7 mm</span>
        </div>
      </div>

      <div style={{flex: 1, padding: 18, overflow:'auto', position:'relative'}}>
        {mode === 'split' && (
          <div style={{display:'grid', gridTemplateColumns:'1fr 1fr', gap: 18}}>
            <ViewerPane label="A · v1.4 · źródło" variant="a" markers={aMarkers} stamp="ZATWIERDZONE" stampColor="var(--ok)" />
            <ViewerPane label="B · v1.5 · porównywany" variant="b" markers={bMarkers} stamp="WYMAGA POPRAWY" stampColor="var(--critical)" />
          </div>
        )}
        {mode === 'overlay' && <OverlayViewer />}
        {mode === 'onion' && <OnionViewer />}

        {/* selected diff inset */}
        {selected && (
          <div style={{
            marginTop: 18,
            background:'var(--paper-3)',
            border:'0.5px solid var(--rule-2)',
            borderRadius: 2,
            padding: 14,
            display:'grid', gridTemplateColumns:'auto 1fr 1fr',
            gap: 18, alignItems:'start',
          }}>
            <div style={{
              padding:'10px 14px', background: SEV[selected.sev].bg,
              borderLeft: `2px solid ${SEV[selected.sev].color}`,
            }}>
              <div style={{fontFamily:"'Geist Mono', monospace", fontSize: 18, fontWeight: 600, color: SEV[selected.sev].color}}>
                {selected.id}
              </div>
              <div style={{marginTop: 4}}><SevTag sev={selected.sev} /></div>
            </div>
            <div>
              <div style={{fontSize: 10, color:'var(--ink-3)', letterSpacing:'.08em', textTransform:'uppercase'}}>
                {CATEGORIES[selected.cat]?.label} · {selected.field}
              </div>
              <div style={{marginTop: 4, fontSize: 13, color:'var(--ink)', lineHeight: 1.4}}>{selected.note}</div>
              <div style={{marginTop: 8, fontSize: 11, color:'var(--ink-3)', fontFamily:"'Geist Mono', monospace"}}>↳ {selected.where}</div>
            </div>
            <div style={{display:'grid', gridTemplateColumns:'1fr 1fr', gap: 10}}>
              <ValueChip label="A · v1.4" value={selected.a} />
              <ValueChip label="B · v1.5" value={selected.b} diff />
            </div>
          </div>
        )}
      </div>
    </main>
  );
}

function Toggle({ label, on }) {
  return (
    <button style={{
      padding:'5px 9px', fontSize: 11, cursor:'pointer',
      background: on ? 'var(--paper-2)' : 'transparent',
      color: on ? 'var(--ink)' : 'var(--ink-3)',
      border:'0.5px solid ' + (on ? 'var(--rule-2)' : 'transparent'),
      borderRadius: 2,
      display:'inline-flex', alignItems:'center', gap: 5,
    }}>
      <span style={{
        width: 8, height: 8, borderRadius: 2,
        border: '1px solid ' + (on ? 'var(--ink-2)' : 'var(--rule-2)'),
        background: on ? 'var(--ink-2)' : 'transparent',
      }} />
      {label}
    </button>
  );
}

function ViewerPane({ label, variant, markers, stamp, stampColor }) {
  return (
    <div style={{position:'relative', background:'var(--paper-3)', padding: 12, borderRadius: 2,
      border: '0.5px solid var(--rule-2)'}}>
      <div style={{display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom: 8}}>
        <div style={{fontSize: 11, color:'var(--ink-2)', letterSpacing:'.04em', fontWeight: 500}}>{label}</div>
        <div style={{
          padding:'2px 6px', fontSize: 9, fontFamily:"'Geist Mono', monospace",
          fontWeight: 600, letterSpacing:'.08em',
          color: stampColor, border: '0.5px solid ' + stampColor,
        }}>{stamp}</div>
      </div>
      <PackArtwork variant={variant} width="100%" markers={markers} />
    </div>
  );
}

function OverlayViewer() {
  // Simple split-slider mock — show A on left and B on right with diagonal handle
  return (
    <div style={{
      position:'relative', background:'var(--paper-3)', padding: 12, borderRadius: 2,
      border: '0.5px solid var(--rule-2)', maxWidth: 760, margin: '0 auto',
    }}>
      <div style={{fontSize: 11, color:'var(--ink-3)', marginBottom: 8}}>
        Przeciągnij linię aby porównać A ↔ B
      </div>
      <div style={{position:'relative'}}>
        <PackArtwork variant="a" width="100%" />
        <div style={{
          position:'absolute', inset: 0, width:'45%', overflow:'hidden',
        }}>
          <div style={{width: '222.2%'}}>
            <PackArtwork variant="b" width="100%" />
          </div>
        </div>
        <div style={{
          position:'absolute', left: '45%', top: 0, bottom: 0, width: 2,
          background: 'var(--accent)',
        }}>
          <div style={{
            position:'absolute', top:'50%', left:'50%', transform:'translate(-50%, -50%)',
            background:'var(--accent)', color:'var(--paper)', width: 28, height: 28, borderRadius:'50%',
            display:'flex', alignItems:'center', justifyContent:'center', fontSize: 14,
          }}>↔</div>
        </div>
      </div>
    </div>
  );
}

function OnionViewer() {
  return (
    <div style={{
      position:'relative', background:'var(--paper-3)', padding: 12, borderRadius: 2,
      border: '0.5px solid var(--rule-2)', maxWidth: 760, margin: '0 auto',
    }}>
      <div style={{fontSize: 11, color:'var(--ink-3)', marginBottom: 8, display:'flex', justifyContent:'space-between'}}>
        <span>Onion-skin · A pod, B na wierzchu (60% opacity)</span>
        <span style={{fontFamily:"'Geist Mono', monospace"}}>opacity ━━━━━━━●━━━ 60%</span>
      </div>
      <div style={{position:'relative'}}>
        <PackArtwork variant="a" width="100%" />
        <div style={{position:'absolute', inset: 0, opacity: 0.6}}>
          <PackArtwork variant="b" width="100%" />
        </div>
      </div>
    </div>
  );
}

function ValueChip({ label, value, diff }) {
  return (
    <div style={{
      padding:'8px 10px', borderRadius: 2,
      background: diff ? 'var(--critical-bg)' : 'var(--paper-2)',
      borderLeft: `2px solid ${diff ? 'var(--critical)' : 'var(--ink-4)'}`,
    }}>
      <div style={{fontSize: 9, color: diff ? 'var(--critical)' : 'var(--ink-3)', letterSpacing:'.06em', textTransform:'uppercase'}}>{label}</div>
      <div style={{marginTop: 3, fontFamily:"'Geist Mono', monospace", fontSize: 11}}>{value || '—'}</div>
    </div>
  );
}

// ── right panel — diff list ─────────────────────────────────
function BRightPanel({ diffs, selected, setSelected, filter, setFilter, sel }) {
  return (
    <aside style={{
      width: 360, flexShrink: 0,
      borderLeft: '0.5px solid var(--rule-2)',
      background: 'var(--paper-2)',
      display:'flex', flexDirection:'column',
    }}>
      <div style={{padding:'14px 16px 8px'}}>
        <div style={{display:'flex', justifyContent:'space-between', alignItems:'center'}}>
          <div style={{fontWeight: 500, fontSize: 13}}>Rozbieżności</div>
          <div style={{fontFamily:"'Geist Mono', monospace", fontSize: 11, color:'var(--ink-3)'}}>
            {diffs.length} / {CMP.diffs.length}
          </div>
        </div>
      </div>
      <div style={{padding: '0 12px 8px', display:'flex', gap: 4}}>
        {[
          ['all', 'Wszystkie', CMP.diffs.length, 'var(--ink-2)'],
          ['crit','Krytyczne', CMP.summary.critical, 'var(--critical)'],
          ['warn','Ostrzeżenia',CMP.summary.warning, 'var(--warn)'],
          ['info','Info',     CMP.summary.info, 'var(--info)'],
        ].map(([k, lbl, n, c]) => (
          <button key={k} onClick={() => setFilter(k)} style={{
            flex: 1, padding: '6px 4px', fontSize: 10, cursor:'pointer',
            background: filter === k ? c : 'transparent',
            color: filter === k ? 'var(--paper)' : c,
            border: '0.5px solid ' + (filter === k ? c : 'var(--rule-2)'),
            borderRadius: 2, display:'flex', flexDirection:'column', alignItems:'center', gap: 2,
            fontFamily:"'Geist', sans-serif",
          }}>
            <span style={{fontFamily:"'Geist Mono', monospace", fontWeight: 600, fontSize: 12}}>{n}</span>
            <span style={{fontSize: 9, letterSpacing:'.04em'}}>{lbl}</span>
          </button>
        ))}
      </div>
      <div style={{flex:1, overflowY:'auto', padding:'0 6px 12px'}}>
        {diffs.map((d) => <DiffListItem key={d.id} d={d} active={d.id === selected} onClick={() => setSelected(d.id)} />)}
      </div>
      {sel && (
        <div style={{
          padding:'14px 16px', borderTop:'0.5px solid var(--rule-2)',
          background:'var(--paper-2)',
        }}>
          <div style={{fontSize: 10, color:'var(--ink-3)', letterSpacing:'.08em', textTransform:'uppercase', marginBottom: 6}}>
            Działania dla {sel.id}
          </div>
          <div style={{display:'grid', gridTemplateColumns:'1fr 1fr', gap: 6}}>
            <BButton variant="ghost">Akceptuj A</BButton>
            <BButton variant="ghost">Akceptuj B</BButton>
            <BButton variant="ghost">Komentuj</BButton>
            <BButton variant="dark">Eskaluj do QA</BButton>
          </div>
          <div style={{marginTop: 10, padding: '8px 10px', background:'var(--paper-3)', borderRadius: 2,
            border: '0.5px solid var(--rule-2)', fontSize: 11, color:'var(--ink-2)', lineHeight: 1.4}}>
            <span style={{color:'var(--ink-3)', fontFamily:"'Geist Mono', monospace", fontSize: 10}}>JZ · 17:31</span>
            <div style={{marginTop: 2}}>Sprawdzić z dostawcą — podejrzewam pomyłkę przy generowaniu kodu.</div>
          </div>
        </div>
      )}
    </aside>
  );
}

function DiffListItem({ d, active, onClick }) {
  return (
    <button onClick={onClick} style={{
      width:'100%', textAlign:'left', cursor:'pointer',
      padding: '10px 10px', marginBottom: 2,
      background: active ? 'var(--paper-3)' : 'transparent',
      border: 'none',
      borderLeft: active ? '2px solid var(--accent)' : '2px solid transparent',
      borderRadius: 0,
      display:'flex', gap: 10, alignItems:'flex-start',
      fontFamily:'inherit',
    }}>
      <span style={{
        width: 6, height: 6, borderRadius:'50%', background: SEV[d.sev].color,
        marginTop: 5, flexShrink: 0,
      }} />
      <div style={{flex: 1, minWidth: 0}}>
        <div style={{display:'flex', justifyContent:'space-between', alignItems:'baseline', gap: 6}}>
          <div style={{fontSize: 12, fontWeight: 500, color:'var(--ink)'}}>{d.field}</div>
          <div style={{fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--ink-3)'}}>{d.id}</div>
        </div>
        <div style={{fontSize: 10, color:'var(--ink-3)', letterSpacing:'.04em', marginTop: 2}}>
          {CATEGORIES[d.cat]?.label.toUpperCase()} {d.blocker ? ' · BLOKER' : ''}
        </div>
        <div style={{
          marginTop: 6, fontSize: 11, color:'var(--ink-2)', lineHeight: 1.35,
          overflow:'hidden', textOverflow:'ellipsis',
          display:'-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient:'vertical',
        }}>
          {d.note}
        </div>
      </div>
    </button>
  );
}

function BStatusBar() {
  return (
    <footer style={{
      height: 26, flexShrink: 0,
      borderTop:'0.5px solid var(--rule-2)',
      background:'var(--paper-2)',
      display:'flex', alignItems:'center', padding:'0 16px', gap: 14,
      fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--ink-3)',
    }}>
      <span>ANALIZA: 14.2 s</span>
      <span>·</span>
      <span>RENDERERA: cairo 1.18</span>
      <span>·</span>
      <span>OCR: tesseract 5.4 · PL/EN/DE</span>
      <span>·</span>
      <span>REGUŁY: 142 aktywne</span>
      <span style={{flex: 1}} />
      <span style={{color:'var(--ok)'}}>● połączono z PIM</span>
      <span>·</span>
      <span>autozapis 17:42</span>
    </footer>
  );
}

Object.assign(window, { VariantB });
