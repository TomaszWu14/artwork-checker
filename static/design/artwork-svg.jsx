// Stylized packaging artwork preview.
// Renders the "easyCARE" gauze pack mockup as inline SVG so we can
// place diff markers, callouts, and "before/after" overlays on top of it.
//
// <PackArtwork variant="a" | "b" width={...} markers={[{id, sev, x, y, w, h}]} />

function PackArtwork({ variant='a', width=520, markers=[], showGrid=false, showBleed=true }) {
  const W = 540, H = 340;   // viewBox proportions ~ 218x142mm pack
  const isA = variant === 'a';
  // Differences encoded between A and B:
  //  - Logo size & position (D-05)
  //  - CE position (D-06)
  //  - EAN value & layout (D-01)
  //  - "Wyłącznie do użytku zewnętrznego" text presence (D-02)
  //  - DE language column presence (D-03)
  //  - LOT format (D-04)
  //  - Bleed marker (D-08)
  //  - Pantone bar saturation (D-09)
  //  - "Data ważności" Y position (D-10)

  const teal = isA ? '#1f5b75' : '#2d6e8a'; // slight ΔE drift on B
  const logoW = isA ? 84 : 70;
  const logoX = isA ? 22 : 28;
  const ceX = isA ? 460 : 466;
  const expY = isA ? 296 : 304;
  const bleed = isA ? 8 : 4;

  return (
    <div style={{position:'relative', width, aspectRatio: `${W}/${H}`, userSelect:'none'}}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height="100%"
           style={{display:'block', background:'#fdfcf7', borderRadius:2}}>
        {/* paper edge */}
        <rect x="0" y="0" width={W} height={H} fill="#fdfcf7" />
        {/* bleed area */}
        {showBleed && (
          <rect x={bleed} y={bleed} width={W-bleed*2} height={H-bleed*2}
                fill="none" stroke="#d1cdbe" strokeDasharray="2 2" strokeWidth="0.5" />
        )}
        {/* color band */}
        <rect x="0" y="0" width={W} height="46" fill={teal} />
        {/* sub-band */}
        <rect x="0" y="46" width={W} height="6" fill={isA ? '#163f54' : '#1a5570'} />

        {/* Logo box */}
        <g transform={`translate(${logoX}, 12)`}>
          <rect width={logoW*0.28} height="22" fill="#fdfcf7" rx="1" />
          <text x={logoW*0.14} y="17" fontFamily="Geist, sans-serif" fontWeight="700"
                fontSize="14" fill={teal} textAnchor="middle"
                letterSpacing="-0.5">Z</text>
          <text x={logoW*0.28 + 6} y="17" fontFamily="Geist, sans-serif" fontWeight="600"
                fontSize="12" fill="#fdfcf7" letterSpacing=".02em">ACME</text>
        </g>

        {/* product name */}
        <text x={W/2} y="36" textAnchor="middle"
              fontFamily="Newsreader, serif" fontStyle="italic" fontSize="18" fill="#fdfcf7"
              letterSpacing=".01em">easyCARE</text>

        {/* product subtitle inside body */}
        <text x="22" y="78" fontFamily="Geist, sans-serif" fontWeight="600"
              fontSize="11" fill="#14130f" letterSpacing=".02em">
          Jałowe gaziki / Sterile gauze swabs
        </text>
        <text x="22" y="92" fontFamily="Geist Mono, monospace"
              fontSize="9" fill="#6e6c63">
          7.5 × 7.5 cm · 8 warstw · 200 szt.
        </text>

        {/* description box */}
        <g transform="translate(22, 108)">
          <line x1="0" y1="0" x2="240" y2="0" stroke="#e2dfd4" strokeWidth="0.5" />
          {/* PL */}
          <text y="14" fontFamily="Geist, sans-serif" fontWeight="600" fontSize="7" fill="#14130f">PL</text>
          <text y="26" fontFamily="Geist, sans-serif" fontSize="6" fill="#3b3a35">
            Sterylne kompresy gazowe z włókna bawełnianego.
          </text>
          <text y="35" fontFamily="Geist, sans-serif" fontSize="6" fill="#3b3a35">
            Do opatrywania ran i absorpcji wysięków.
          </text>
          {/* Warning line — only A has full text */}
          <text y="48" fontFamily="Geist, sans-serif" fontSize="6"
                fill={isA ? '#b13a2f' : '#3b3a35'}
                fontWeight={isA ? '600' : '400'}>
            {isA
              ? '⚠ Wyłącznie do użytku zewnętrznego. Nie spożywać.'
              : 'Nie stosować po upływie terminu ważności.'}
          </text>

          {/* EN */}
          <text y="64" fontFamily="Geist, sans-serif" fontWeight="600" fontSize="7" fill="#14130f">EN</text>
          <text y="76" fontFamily="Geist, sans-serif" fontSize="6" fill="#3b3a35">
            Sterile cotton gauze swabs for wound care.
          </text>

          {/* DE — only present in A */}
          {isA && (
            <>
              <text y="92" fontFamily="Geist, sans-serif" fontWeight="600" fontSize="7" fill="#14130f">DE</text>
              <text y="104" fontFamily="Geist, sans-serif" fontSize="6" fill="#3b3a35">
                Sterile Baumwoll-Gazetupfer für die Wundversorgung.
              </text>
            </>
          )}
        </g>

        {/* RIGHT column — product image placeholder */}
        <g transform="translate(290, 70)">
          <rect width="230" height="150" fill="#f6f3ea" stroke="#e2dfd4" strokeWidth="0.5" rx="2" />
          {/* faux product silhouette */}
          <g transform="translate(40, 30)">
            <rect width="150" height="90" rx="6" fill="#fdfcf7" stroke="#d1cdbe" strokeWidth="0.5" />
            <g opacity=".4">
              {Array.from({length: 8}).map((_, i) => (
                <line key={i} x1="14" y1={20 + i*8} x2="136" y2={20 + i*8}
                      stroke={teal} strokeWidth="0.5" />
              ))}
            </g>
            <text x="75" y="56" textAnchor="middle"
                  fontFamily="Geist Mono, monospace" fontSize="6" fill="#a3a097">
              [ product photo ]
            </text>
          </g>
        </g>

        {/* bottom band — codes & dates */}
        <g transform={`translate(22, 248)`}>
          <line x1="0" y1="0" x2={W-44} y2="0" stroke="#e2dfd4" strokeWidth="0.5" />

          {/* LOT — different format */}
          <text x="0" y="14" fontFamily="Geist, sans-serif" fontWeight="600"
                fontSize="7" fill="#6e6c63" letterSpacing=".06em">LOT</text>
          <text x="0" y="26" fontFamily="Geist Mono, monospace" fontSize="9" fill="#14130f">
            {isA ? 'LOT 240520' : 'LOT/24/05/20'}
          </text>

          {/* REF */}
          <text x="86" y="14" fontFamily="Geist, sans-serif" fontWeight="600"
                fontSize="7" fill="#6e6c63" letterSpacing=".06em">REF</text>
          <text x="86" y="26" fontFamily="Geist Mono, monospace" fontSize="9" fill="#14130f">
            ZAY-EC-200
          </text>

          {/* expiry — shifted Y on B */}
          <text x="172" y={isA ? 14 : 18} fontFamily="Geist, sans-serif" fontWeight="600"
                fontSize="7" fill="#6e6c63" letterSpacing=".06em">EXP</text>
          <text x="172" y={isA ? 26 : 32} fontFamily="Geist Mono, monospace" fontSize="9" fill="#14130f">
            2029-03
          </text>
        </g>

        {/* EAN-13 barcode (stylized) */}
        <g transform={`translate(${W-150}, 256)`}>
          <rect width="130" height="44" fill="#fdfcf7" />
          {/* fake bars */}
          {Array.from({length: 38}).map((_, i) => {
            const x = 4 + i * 3.2;
            const h = (i % 3 === 0) ? 32 : 28;
            return <rect key={i} x={x} y="4" width={(i%2===0)?1.4:0.8} height={h} fill="#14130f" />;
          })}
          <text x="65" y="42" textAnchor="middle"
                fontFamily="Geist Mono, monospace" fontSize="7" fill="#14130f">
            {isA ? '5 907996 822690' : '5 907996 822960'}
          </text>
        </g>

        {/* CE mark */}
        <g transform={`translate(${ceX}, 16)`}>
          <text fontFamily="Geist, sans-serif" fontWeight="700" fontSize="14" fill="#fdfcf7">CE</text>
          <text x="0" y="22" fontFamily="Geist Mono, monospace" fontSize="6" fill="#fdfcf7">2274</text>
        </g>

        {/* pictograms (sterile / single-use / temp / no sun) */}
        <g transform="translate(290, 230)" fontFamily="Geist Mono, monospace" fontSize="8" fill="#14130f">
          <g><circle cx="6" cy="6" r="5.5" fill="none" stroke="#14130f" strokeWidth="0.7" /><text x="6" y="9" textAnchor="middle">R</text></g>
          <g transform="translate(22,0)"><rect width="12" height="12" fill="none" stroke="#14130f" strokeWidth="0.7" /><text x="6" y="9" textAnchor="middle">2</text></g>
          <g transform="translate(44,0)"><circle cx="6" cy="6" r="5.5" fill="none" stroke="#14130f" strokeWidth="0.7" /><text x="6" y="9" textAnchor="middle">☼</text></g>
          <g transform="translate(66,0)"><rect width="12" height="12" fill="none" stroke="#14130f" strokeWidth="0.7" /><text x="6" y="9" textAnchor="middle">↕</text></g>
        </g>

        {/* registration number */}
        <text x="22" y="328" fontFamily="Geist Mono, monospace" fontSize="6" fill="#6e6c63">
          {isA ? 'PL/CA01/0258/26' : 'PL/CA01/0258/2026'}
        </text>

        {/* grid overlay */}
        {showGrid && (
          <g opacity=".15">
            {Array.from({length: Math.floor(W/20)}).map((_,i)=>(
              <line key={'v'+i} x1={i*20} y1={0} x2={i*20} y2={H} stroke="#2a5c80" strokeWidth="0.3"/>
            ))}
            {Array.from({length: Math.floor(H/20)}).map((_,i)=>(
              <line key={'h'+i} x1={0} y1={i*20} x2={W} y2={i*20} stroke="#2a5c80" strokeWidth="0.3"/>
            ))}
          </g>
        )}
      </svg>

      {/* diff markers overlaid */}
      {markers.map((m) => (
        <Marker key={m.id} {...m} />
      ))}
    </div>
  );
}

function Marker({ id, sev='warn', x, y, w=24, h=18, label, side='right' }) {
  const color = SEV[sev].color;
  const bg = SEV[sev].bg;
  return (
    <div style={{
      position:'absolute',
      left: `${x}%`, top:`${y}%`,
      width:`${w}%`, height:`${h}%`,
      border:`1.5px solid ${color}`,
      borderRadius: 2,
      background: `${color}10`,
      pointerEvents:'none',
    }}>
      {label && (
        <div style={{
          position:'absolute',
          [side === 'right' ? 'left' : 'right']: '100%',
          [side === 'right' ? 'marginLeft' : 'marginRight']: 6,
          top:'50%', transform:'translateY(-50%)',
          background: color, color:'#fdfcf7',
          fontFamily:"'Geist Mono', monospace", fontSize: 9, fontWeight: 600,
          padding:'2px 5px', borderRadius: 2, whiteSpace:'nowrap',
        }}>{id}</div>
      )}
    </div>
  );
}

// Side-by-side comparison helper
function ComparePair({ width=460, markers={a:[], b:[]}, showBleed=true }) {
  return (
    <div style={{display:'grid', gridTemplateColumns:'1fr 1fr', gap: 16}}>
      <div>
        <div style={{display:'flex', justifyContent:'space-between', alignItems:'baseline', marginBottom: 6}}>
          <div style={{fontSize:10, color:'var(--ink-3)', letterSpacing:'.08em', textTransform:'uppercase'}}>A · v1.4</div>
          <div style={{fontSize:10, color:'var(--ink-4)', fontFamily:"'Geist Mono', monospace"}}>{CMP.fileA.date}</div>
        </div>
        <PackArtwork variant="a" width={width} markers={markers.a} showBleed={showBleed} />
      </div>
      <div>
        <div style={{display:'flex', justifyContent:'space-between', alignItems:'baseline', marginBottom: 6}}>
          <div style={{fontSize:10, color:'var(--ink-3)', letterSpacing:'.08em', textTransform:'uppercase'}}>B · v1.5</div>
          <div style={{fontSize:10, color:'var(--ink-4)', fontFamily:"'Geist Mono', monospace"}}>{CMP.fileB.date}</div>
        </div>
        <PackArtwork variant="b" width={width} markers={markers.b} showBleed={showBleed} />
      </div>
    </div>
  );
}

Object.assign(window, { PackArtwork, Marker, ComparePair });
