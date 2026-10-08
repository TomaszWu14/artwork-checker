// ── VARIANT D ──────────────────────────────────────────────
// Annotated artwork zoom. One big artwork preview takes center stage,
// numbered pins drop onto the design with floating comment cards in
// the margin (Figma-like review mode). Toolbar lets the reviewer switch
// between A, B, "diff only" and overlay.
//
// Sized 1280 × 1100.

function VariantD() {
  const [side, setSide] = React.useState('B'); // A | B | diff
  // Pin positions — match the diffs that have a spatial location
  const pins = [
    { id:'D-01', sev:'crit', x: 84, y: 80, n: 1 },
    { id:'D-02', sev:'crit', x: 23, y: 47, n: 2 },
    { id:'D-03', sev:'crit', x: 23, y: 77, n: 3 },
    { id:'D-04', sev:'crit', x: 11, y: 80, n: 4 },
    { id:'D-05', sev:'crit', x: 9,  y: 11, n: 5 },
    { id:'D-06', sev:'warn', x: 91, y: 9,  n: 6 },
    { id:'D-09', sev:'warn', x: 50, y: 5,  n: 7 },
    { id:'D-10', sev:'warn', x: 36, y: 82, n: 8 },
  ];

  return (
    <div style={{
      width: 1280, minHeight: 1100,
      background: 'var(--paper)',
      fontFamily:"'Geist', sans-serif", color:'var(--ink)',
      display:'flex', flexDirection:'column',
    }}>
      <DTopBar side={side} setSide={setSide} />
      <div style={{
        flex: 1, display:'grid', gridTemplateColumns:'1fr 320px',
        gap: 0,
      }}>
        <DCanvas side={side} pins={pins} />
        <DPinsList pins={pins} />
      </div>
    </div>
  );
}

function DTopBar({ side, setSide }) {
  return (
    <div style={{
      padding:'14px 28px',
      borderBottom:'0.5px solid var(--rule-2)',
      background:'var(--paper-2)',
      display:'flex', alignItems:'center', gap: 16,
    }}>
      <div style={{display:'flex', alignItems:'center', gap:10}}>
        <AcmeWordmark size={12} />
        <span style={{color:'var(--ink-4)'}}>/</span>
        <span style={{fontSize: 12, fontWeight: 500}}>Annotated review</span>
      </div>
      <span style={{
        fontFamily:"'Geist Mono', monospace", fontSize: 11, color:'var(--ink-3)',
      }}>
        {CMP.sku} · porównanie {CMP.no}
      </span>
      <div style={{flex:1}} />

      <div style={{display:'flex', gap:2, padding: 2, background:'var(--paper)', borderRadius: 4, border:'0.5px solid var(--rule-2)'}}>
        {['A','B','diff','overlay'].map((s) => (
          <button key={s} onClick={() => setSide(s)} style={{
            padding:'5px 12px', fontSize: 11, cursor:'pointer',
            background: side === s ? 'var(--paper-3)' : 'transparent',
            color: side === s ? 'var(--ink)' : 'var(--ink-3)',
            border:'none', borderRadius: 3,
            fontFamily:"'Geist', sans-serif", fontWeight: side === s ? 500 : 400,
          }}>{s === 'A' ? 'A · v1.4' : s === 'B' ? 'B · v1.5' : s === 'diff' ? 'tylko różnice' : 'overlay'}</button>
        ))}
      </div>

      <div style={{display:'flex', alignItems:'center', gap: 8, padding:'5px 10px', background:'var(--paper)', borderRadius: 3, border:'0.5px solid var(--rule-2)'}}>
        <span style={{fontFamily:"'Geist Mono', monospace", fontSize: 11, color:'var(--ink-3)'}}>zoom</span>
        <span style={{fontFamily:"'Geist Mono', monospace", fontSize: 11, color:'var(--ink)'}}>185%</span>
        <span style={{color:'var(--rule-3)'}}>·</span>
        <span style={{fontFamily:"'Geist Mono', monospace", fontSize: 11, color:'var(--ink-3)'}}>200 DPI</span>
      </div>

      <button style={{
        padding:'7px 14px', background:'var(--accent)', color:'var(--paper)',
        border:'none', cursor:'pointer', borderRadius: 3,
        fontSize: 12, fontWeight: 500,
      }}>Rozwiąż wszystkie ({pinCount('warn')})</button>
    </div>
  );
}

function pinCount(sev) {
  return CMP.diffs.filter((d) => d.sev === sev).length;
}

function DCanvas({ side, pins }) {
  // The artwork is displayed at scale; pins are absolutely positioned
  // relative to the same coord system as the PackArtwork SVG viewBox.
  const showA = side === 'A' || side === 'overlay';
  const showB = side === 'B' || side === 'overlay' || side === 'diff';

  return (
    <div style={{
      position:'relative',
      padding: 36,
      background: 'var(--paper)',
      // grid pattern background
      backgroundImage:
        'radial-gradient(circle at 1px 1px, rgba(255,255,255,.04) 1px, transparent 0)',
      backgroundSize: '24px 24px',
      overflow:'hidden',
    }}>
      <div style={{
        position:'relative',
        maxWidth: 780, margin: '0 auto',
        boxShadow: '0 30px 80px rgba(0,0,0,.4), 0 0 0 1px var(--rule-2)',
      }}>
        {showA && (
          <div style={{position: showB && side === 'overlay' ? 'absolute' : 'relative', inset: 0, opacity: side === 'overlay' ? 0.45 : 1}}>
            <PackArtwork variant="a" width="100%" showBleed={true} />
          </div>
        )}
        {showB && side !== 'overlay' && (
          <PackArtwork variant="b" width="100%" showBleed={true} />
        )}
        {showB && side === 'overlay' && (
          <div style={{opacity: 0.7}}>
            <PackArtwork variant="b" width="100%" showBleed={true} />
          </div>
        )}

        {/* Pins overlay */}
        <div style={{position:'absolute', inset: 0, pointerEvents:'none'}}>
          {pins.map((p) => <Pin key={p.id} {...p} />)}
        </div>
      </div>

      {/* legend bottom-left */}
      <div style={{
        position:'absolute', bottom: 16, left: 28,
        display:'flex', gap: 12,
        fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--ink-3)',
      }}>
        <Legend color="var(--critical)" label="krytyczne" />
        <Legend color="var(--warn)" label="ostrzeżenia" />
        <Legend color="var(--info)" label="info" />
        <span style={{marginLeft: 6}}>· 218 × 142 mm @ 200 DPI</span>
      </div>

      {/* scale bar bottom-right */}
      <div style={{
        position:'absolute', bottom: 16, right: 28,
        display:'flex', alignItems:'center', gap:8,
        fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--ink-3)',
      }}>
        <span>0</span>
        <span style={{display:'inline-block', width: 80, height: 1, background:'var(--ink-3)', position:'relative'}}>
          <span style={{position:'absolute', left:0, top:-3, width:1, height:7, background:'var(--ink-3)'}} />
          <span style={{position:'absolute', left:'50%', top:-3, width:1, height:7, background:'var(--ink-3)'}} />
          <span style={{position:'absolute', right:0, top:-3, width:1, height:7, background:'var(--ink-3)'}} />
        </span>
        <span>20 mm</span>
      </div>
    </div>
  );
}

function Legend({ color, label }) {
  return (
    <span style={{display:'inline-flex', alignItems:'center', gap: 5}}>
      <span style={{width: 8, height: 8, borderRadius:'50%', background: color, display:'inline-block'}} />
      <span style={{letterSpacing:'.06em'}}>{label}</span>
    </span>
  );
}

function Pin({ id, sev, x, y, n }) {
  const c = sev === 'crit' ? 'var(--critical)' : sev === 'warn' ? 'var(--warn)' : 'var(--info)';
  return (
    <div style={{
      position:'absolute', left:`${x}%`, top:`${y}%`,
      transform:'translate(-50%, -100%)',
      pointerEvents:'auto',
    }}>
      <div style={{
        width: 24, height: 24, borderRadius:'50% 50% 50% 2px',
        transform:'rotate(-45deg)',
        background: c, color:'var(--paper)',
        boxShadow:`0 2px 6px rgba(0,0,0,.4), 0 0 0 2px var(--paper)`,
        display:'flex', alignItems:'center', justifyContent:'center',
        fontFamily:"'Geist Mono', monospace", fontSize: 10, fontWeight: 700,
      }}>
        <span style={{transform:'rotate(45deg)'}}>{n}</span>
      </div>
    </div>
  );
}

function DPinsList({ pins }) {
  return (
    <aside style={{
      borderLeft:'0.5px solid var(--rule-2)',
      background:'var(--paper-2)',
      padding: '20px 18px',
      display:'flex', flexDirection:'column', gap: 10,
      overflowY:'auto',
    }}>
      <div style={{display:'flex', alignItems:'baseline', justifyContent:'space-between', marginBottom: 4}}>
        <div style={{fontSize: 12, fontWeight: 500}}>Piny komentarzy</div>
        <div style={{fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--ink-3)'}}>{pins.length}</div>
      </div>
      {pins.map((p) => {
        const d = CMP.diffs.find((x) => x.id === p.id);
        return <PinCard key={p.id} pin={p} d={d} />;
      })}
    </aside>
  );
}

function PinCard({ pin, d }) {
  const c = pin.sev === 'crit' ? 'var(--critical)' : pin.sev === 'warn' ? 'var(--warn)' : 'var(--info)';
  // pretend comments
  const commentsByPin = {
    'D-01': [
      { who:'JZ', when:'17:31', text:'EAN nie waliduje sumy kontrolnej. Sprawdzić z dostawcą.' },
      { who:'PW', when:'17:55', text:'Wymaga ponownego druku — blokuje rejestrację.' },
    ],
    'D-02': [{ who:'PW', when:'17:38', text:'MDR Art. 23 ust. 4 — bez tej linii nie wpuścimy do obrotu.' }],
    'D-03': [{ who:'JZ', when:'17:42', text:'Rynek DACH jest aktywny w PIM, sprawdzam u marketingu.' }],
    'D-05': [{ who:'AK', when:'17:14', text:'Sprawdzić pole ochronne loga — minimum 8 mm wokół.' }],
    'D-09': [
      { who:'AK', when:'16:58', text:'ΔE 4.2 to za dużo. Wymaga próby drukarskiej.' },
      { who:'TB', when:'17:05', text:'Mogę przeliczyć z profilu Coated v2 300% — wraca w pn.' },
    ],
  };
  const comments = commentsByPin[d.id] || [];
  return (
    <div style={{
      background:'var(--paper)',
      borderRadius: 3,
      border:'0.5px solid var(--rule-2)',
      borderLeft: `2px solid ${c}`,
      padding: '10px 12px',
    }}>
      <div style={{display:'flex', alignItems:'center', gap: 8}}>
        <span style={{
          width: 18, height: 18, borderRadius:'50%', background: c, color:'var(--paper)',
          fontFamily:"'Geist Mono', monospace", fontSize: 10, fontWeight: 700,
          display:'inline-flex', alignItems:'center', justifyContent:'center',
        }}>{pin.n}</span>
        <span style={{fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--ink-3)'}}>{d.id}</span>
        <span style={{flex:1}} />
        {d.blocker && <span style={{padding:'1px 4px', fontFamily:"'Geist Mono', monospace", fontSize: 8, background:'var(--critical)', color:'var(--paper)', letterSpacing:'.08em'}}>BLOKER</span>}
      </div>
      <div style={{marginTop: 6, fontSize: 12, fontWeight: 500}}>{d.field}</div>
      <div style={{
        marginTop: 4, fontSize: 11, color:'var(--ink-2)', lineHeight: 1.4,
        overflow:'hidden', textOverflow:'ellipsis',
        display:'-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient:'vertical',
      }}>{d.note}</div>
      <div style={{marginTop: 6, fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--ink-4)'}}>
        ↳ {d.where}
      </div>

      {comments.length > 0 && (
        <div style={{marginTop: 8, paddingTop: 8, borderTop: '0.5px dashed var(--rule-2)', display:'flex', flexDirection:'column', gap: 6}}>
          {comments.map((cm, i) => (
            <div key={i} style={{display:'flex', gap: 6}}>
              <div style={{
                width: 18, height: 18, borderRadius:'50%', background:'var(--rule-3)', color:'var(--ink)',
                fontSize: 9, fontWeight: 600, fontFamily:"'Geist Mono', monospace",
                display:'flex', alignItems:'center', justifyContent:'center', flexShrink: 0,
              }}>{cm.who}</div>
              <div style={{flex: 1, minWidth: 0}}>
                <div style={{display:'flex', gap: 6, alignItems:'baseline'}}>
                  <span style={{fontSize: 10, fontWeight: 500, color:'var(--ink-2)'}}>{cm.who}</span>
                  <span style={{fontFamily:"'Geist Mono', monospace", fontSize: 9, color:'var(--ink-4)'}}>{cm.when}</span>
                </div>
                <div style={{fontSize: 11, color:'var(--ink-2)', lineHeight: 1.4, marginTop: 1}}>{cm.text}</div>
              </div>
            </div>
          ))}
          <div style={{
            marginTop: 2,
            display:'flex', alignItems:'center', gap: 6,
            padding:'6px 8px', borderRadius: 3,
            background:'var(--paper-2)', fontSize: 11, color:'var(--ink-4)',
          }}>
            <span>＋</span>
            <span style={{fontStyle:'italic'}}>odpowiedz…</span>
          </div>
        </div>
      )}
    </div>
  );
}

Object.assign(window, { VariantD });
