// ── VARIANT E ──────────────────────────────────────────────
// Exec summary / email-style single-pager. Mounted in an email-client
// chrome (subject line, From/To/CC, send actions). The report inside is
// dense but readable — designed for stakeholders who want one screen,
// not the whole audit trail.
//
// Sized 920 × 1320.

function VariantE() {
  return (
    <div style={{
      width: 920, minHeight: 1320,
      background:'var(--paper)',
      fontFamily:"'Geist', sans-serif", color:'var(--ink)',
      display:'flex', flexDirection:'column',
    }}>
      <EMailChrome />
      <EReportBody />
    </div>
  );
}

function EMailChrome() {
  return (
    <header style={{
      background:'var(--paper-2)',
      borderBottom:'0.5px solid var(--rule-2)',
      padding: '14px 28px 12px',
    }}>
      <div style={{display:'flex', alignItems:'center', gap: 14}}>
        <span style={{
          padding: '3px 8px',
          background:'var(--paper)', border:'0.5px solid var(--rule-2)',
          fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--ink-3)',
          letterSpacing:'.06em', borderRadius: 2,
        }}>SKRZYNKA</span>
        <span style={{fontSize: 12, color:'var(--ink-3)'}}>artcompare@example.com</span>
        <span style={{flex: 1}} />
        <span style={{fontFamily:"'Geist Mono', monospace", fontSize: 11, color:'var(--ink-4)'}}>{CMP.date} · {CMP.time}</span>
      </div>
      <h1 style={{
        margin: '14px 0 4px',
        fontFamily:"'Newsreader', serif", fontWeight: 500,
        fontSize: 28, letterSpacing:'-.01em', lineHeight: 1.15,
      }}>
        Re: <span style={{fontStyle:'italic', color:'var(--ink-2)'}}>easyCARE 200 · gaziki sterylne</span>
        <br/>
        <span style={{color:'var(--critical)'}}>5 krytycznych</span> rozbieżności w v1.5
      </h1>
      <div style={{display:'grid', gridTemplateColumns:'80px 1fr', gap:'4px 14px', marginTop: 14, fontSize: 12}}>
        <div style={{color:'var(--ink-3)'}}>Od</div>
        <div>ArtCompare &lt;artcompare@example.com&gt;</div>
        <div style={{color:'var(--ink-3)'}}>Do</div>
        <div>
          <Chip name="J. Zielińska · QA Lead" />
          <Chip name="A. Kowalska · Brand" />
          <Chip name="P. Wojtas · Regulatory" />
        </div>
        <div style={{color:'var(--ink-3)'}}>DW</div>
        <div>
          <Chip name="M. Nowak · PrintHub Wuhan (dostawca)" />
          <Chip name="T. Bąk · DTP" />
        </div>
        <div style={{color:'var(--ink-3)'}}>Załączniki</div>
        <div style={{display:'flex', gap: 8, flexWrap:'wrap'}}>
          <Attachment label="raport_28.pdf" size="412 KB" />
          <Attachment label="diff.zip" size="2.4 MB" />
          <Attachment label="v1.5_marked.pdf" size="6.1 MB" />
        </div>
      </div>
    </header>
  );
}

function Chip({ name }) {
  return (
    <span style={{
      display:'inline-block', padding:'2px 8px', marginRight: 6, marginBottom: 2,
      background:'var(--paper-3)', borderRadius: 12, fontSize: 11, color:'var(--ink-2)',
    }}>{name}</span>
  );
}

function Attachment({ label, size }) {
  return (
    <span style={{
      display:'inline-flex', alignItems:'center', gap: 6,
      padding:'4px 10px', background:'var(--paper)', border:'0.5px solid var(--rule-2)',
      borderRadius: 2, fontSize: 11, color:'var(--ink-2)',
    }}>
      <span style={{fontFamily:"'Geist Mono', monospace", color:'var(--accent)'}}>▎</span>
      {label}
      <span style={{color:'var(--ink-4)', fontFamily:"'Geist Mono', monospace", fontSize: 10}}>{size}</span>
    </span>
  );
}

function EReportBody() {
  return (
    <main style={{padding: '24px 28px 32px'}}>
      <EOpening />
      <EVerdictStrip />
      <EThumbnails />
      <ETopFindings />
      <EByModule />
      <ECTA />
      <EFooter />
    </main>
  );
}

function EOpening() {
  return (
    <section style={{marginBottom: 24}}>
      <p style={{margin: '0 0 10px', fontSize: 14, lineHeight: 1.6, color:'var(--ink)'}}>
        Cześć zespole — automatyczne porównanie <span style={{fontFamily:"'Geist Mono', monospace", color:'var(--accent-2)'}}>{CMP.sku}-260320</span> v1.4 vs v1.5 (rev. dostawcy) wykryło
        <span style={{color:'var(--critical)', fontWeight: 500}}> {CMP.summary.critical} krytycznych</span> rozbieżności, w tym <strong style={{color:'var(--ink)', fontWeight: 600}}>3 blokujące druk</strong>.
      </p>
      <p style={{margin: 0, fontSize: 13, lineHeight: 1.6, color:'var(--ink-2)'}}>
        Najpoważniejsze: niezgodny <strong>kod EAN-13</strong> (zamiana cyfr, suma kontrolna niepoprawna),
        usunięty wymagany <strong>znak ostrzegawczy MDR</strong> i brak <strong>wersji DE</strong>.
        Rekomendowane: zatrzymanie wysyłki do drukarni, eskalacja do Reg. + QA, korekta u dostawcy w 24 h.
      </p>
    </section>
  );
}

function EVerdictStrip() {
  const totals = [
    { n: CMP.summary.critical, label:'Krytyczne',  c:'var(--critical)' },
    { n: CMP.summary.warning,  label:'Ostrzeżenia',c:'var(--warn)' },
    { n: CMP.summary.info,     label:'Informacja', c:'var(--info)' },
    { n: CMP.summary.ok,       label:'Zgodne',     c:'var(--ok)' },
  ];
  return (
    <section style={{
      marginBottom: 24,
      display:'grid', gridTemplateColumns:'1fr 1fr', gap: 12,
    }}>
      <div style={{
        padding: '16px 18px',
        background:'var(--critical-bg)',
        borderLeft:'2px solid var(--critical)',
        borderRadius: 2,
        display:'flex', alignItems:'center', gap: 14,
      }}>
        <div style={{
          fontFamily:"'Geist Mono', monospace",
          fontSize: 11, fontWeight: 600, letterSpacing:'.1em',
          color:'var(--critical)', padding:'4px 8px',
          background:'var(--paper-2)', borderRadius: 2,
        }}>WERDYKT</div>
        <div>
          <div style={{fontFamily:"'Newsreader', serif", fontStyle:'italic', fontSize: 18, color:'var(--ink)'}}>
            Nie dopuszczać do druku
          </div>
          <div style={{fontSize: 11, color:'var(--ink-2)', marginTop: 2}}>
            3 blokery · oczekuje na korektę dostawcy
          </div>
        </div>
      </div>
      <div style={{
        padding: '12px 14px',
        background:'var(--paper-2)',
        borderRadius: 2,
        display:'grid', gridTemplateColumns:'repeat(4, 1fr)', gap: 6,
      }}>
        {totals.map((t) => (
          <div key={t.label} style={{textAlign:'center'}}>
            <div style={{
              fontFamily:"'Geist Mono', monospace", fontSize: 22, fontWeight: 600,
              color: t.c, lineHeight: 1,
            }}>{t.n}</div>
            <div style={{
              fontSize: 9, color:'var(--ink-3)',
              letterSpacing:'.08em', textTransform:'uppercase', marginTop: 5,
            }}>{t.label}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

function EThumbnails() {
  return (
    <section style={{marginBottom: 24}}>
      <SubHead title="Wersje" no="01" />
      <div style={{display:'grid', gridTemplateColumns:'1fr 1fr', gap: 14}}>
        <ThumbBlock variant="a" label="A · v1.4 · źródło · ZATWIERDZONE 24.02" stamp="OK" stampColor="var(--ok)" />
        <ThumbBlock variant="b" label="B · v1.5 · rev. dostawcy · 19.05" stamp="5 KRYT." stampColor="var(--critical)" flagged />
      </div>
    </section>
  );
}

function ThumbBlock({ variant, label, stamp, stampColor, flagged }) {
  return (
    <div style={{
      background:'var(--paper-2)',
      borderRadius: 2,
      border: flagged ? '1px solid var(--critical)' : '0.5px solid var(--rule-2)',
      padding: 10,
    }}>
      <div style={{display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom: 8}}>
        <div style={{fontSize: 10, color:'var(--ink-3)', letterSpacing:'.04em'}}>{label}</div>
        <div style={{
          padding:'2px 6px', fontFamily:"'Geist Mono', monospace", fontSize: 9, fontWeight: 600,
          letterSpacing:'.08em', color: stampColor, border:`0.5px solid ${stampColor}`,
        }}>{stamp}</div>
      </div>
      <PackArtwork variant={variant} width="100%" showBleed={false} />
    </div>
  );
}

function SubHead({ title, no }) {
  return (
    <div style={{
      display:'flex', alignItems:'baseline', gap: 10, marginBottom: 12,
    }}>
      <span style={{
        fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--accent)',
        letterSpacing:'.1em',
      }}>{no}</span>
      <h2 style={{margin: 0, fontFamily:"'Newsreader', serif", fontWeight: 500, fontSize: 19, letterSpacing:'-.01em'}}>{title}</h2>
      <div style={{flex: 1, height: 1, background:'var(--rule-2)', alignSelf:'center', marginLeft: 6}} />
    </div>
  );
}

function ETopFindings() {
  const crits = CMP.diffs.filter((d) => d.sev === 'crit');
  return (
    <section style={{marginBottom: 24}}>
      <SubHead title="Krytyczne ustalenia" no="02" />
      <div style={{display:'flex', flexDirection:'column', gap: 8}}>
        {crits.map((d, i) => <ETopRow key={d.id} d={d} idx={i+1} />)}
      </div>
    </section>
  );
}

function ETopRow({ d, idx }) {
  return (
    <div style={{
      display:'grid', gridTemplateColumns:'28px 1fr 220px',
      gap: 14, alignItems:'flex-start',
      padding: '12px 14px',
      background:'var(--paper-2)',
      borderLeft:'2px solid var(--critical)',
      borderRadius: 2,
    }}>
      <div style={{
        fontFamily:"'Geist Mono', monospace", fontSize: 18, fontWeight: 500,
        color:'var(--critical)', lineHeight: 1,
      }}>{String(idx).padStart(2, '0')}</div>
      <div>
        <div style={{
          display:'flex', alignItems:'center', gap: 8, fontSize: 10,
          color:'var(--ink-3)', letterSpacing:'.04em', textTransform:'uppercase',
        }}>
          <span style={{fontFamily:"'Geist Mono', monospace", color:'var(--ink-2)'}}>{d.id}</span>
          <span>·</span>
          <span>{CATEGORIES[d.cat]?.label}</span>
          {d.blocker && <>
            <span>·</span>
            <span style={{
              padding:'1px 5px', background:'var(--critical)', color:'var(--paper)',
              fontFamily:"'Geist Mono', monospace", fontSize: 9, letterSpacing:'.08em',
            }}>BLOKER</span>
          </>}
        </div>
        <div style={{marginTop: 4, fontSize: 14, fontWeight: 500, color:'var(--ink)'}}>{d.field}</div>
        <div style={{marginTop: 4, fontSize: 12, color:'var(--ink-2)', lineHeight: 1.45}}>{d.note}</div>
      </div>
      <div style={{display:'flex', flexDirection:'column', gap: 4}}>
        <div style={{fontSize: 9, color:'var(--ink-3)', letterSpacing:'.06em', textTransform:'uppercase'}}>A → B</div>
        <div style={{
          fontFamily:"'Geist Mono', monospace", fontSize: 11, color:'var(--ink-2)',
          padding:'4px 7px', background:'var(--paper)', borderRadius: 2,
          whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis',
        }}>{d.a || '—'}</div>
        <div style={{
          fontFamily:"'Geist Mono', monospace", fontSize: 11, color:'var(--critical)',
          padding:'4px 7px', background:'var(--critical-bg)', borderRadius: 2,
          whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis',
        }}>{d.b || '—'}</div>
      </div>
    </div>
  );
}

function EByModule() {
  return (
    <section style={{marginBottom: 24}}>
      <SubHead title="Status modułów" no="03" />
      <div style={{
        display:'grid', gridTemplateColumns:'repeat(4, 1fr)', gap: 8,
      }}>
        {CMP.modules.map((m) => {
          const sevKey = m.status === 'fail' ? 'crit' : m.status === 'warn' ? 'warn' : m.status === 'info' ? 'info' : 'ok';
          return (
            <div key={m.id} style={{
              padding:'10px 12px',
              background:'var(--paper-2)',
              borderRadius: 2,
              borderTop: `2px solid ${SEV[sevKey].color}`,
            }}>
              <div style={{display:'flex', justifyContent:'space-between', alignItems:'center'}}>
                <span style={{fontFamily:"'Geist Mono', monospace", color:'var(--ink-3)', fontSize: 11}}>
                  {CATEGORIES[m.id]?.icon}
                </span>
                <SevTag sev={sevKey} />
              </div>
              <div style={{marginTop: 6, fontSize: 11, fontWeight: 500, lineHeight: 1.3}}>{m.name}</div>
              <div style={{
                marginTop: 6, display:'flex', gap: 6, fontFamily:"'Geist Mono', monospace", fontSize: 10,
              }}>
                {m.crit > 0 && <span style={{color:'var(--critical)'}}>{m.crit}c</span>}
                {m.warn > 0 && <span style={{color:'var(--warn)'}}>{m.warn}w</span>}
                <span style={{color:'var(--ok)'}}>{m.ok}✓</span>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function ECTA() {
  return (
    <section style={{
      marginBottom: 24,
      padding:'18px 22px',
      background:'var(--paper-2)',
      borderRadius: 2,
      border:'0.5px solid var(--rule-2)',
    }}>
      <div style={{fontSize: 11, color:'var(--ink-3)', letterSpacing:'.08em', textTransform:'uppercase', marginBottom: 8}}>
        Rekomendowane działania
      </div>
      <ol style={{margin: 0, paddingLeft: 18, fontSize: 13, color:'var(--ink-2)', lineHeight: 1.7}}>
        <li><strong style={{color:'var(--ink)'}}>QA</strong> · zatrzymać release v1.5, oznaczyć blokery</li>
        <li><strong style={{color:'var(--ink)'}}>Regulatory</strong> · zweryfikować braki MDR (warning + wersja DE)</li>
        <li><strong style={{color:'var(--ink)'}}>Brand</strong> · sprawdzić pole ochronne loga i ΔE Pantone</li>
        <li><strong style={{color:'var(--ink)'}}>Dostawca</strong> · korekta pliku do <span style={{fontFamily:"'Geist Mono', monospace", color:'var(--ink)'}}>22.05 16:00</span></li>
      </ol>
      <div style={{
        marginTop: 16, display:'flex', gap: 8, alignItems:'center',
      }}>
        <button style={{
          padding:'8px 14px', background:'var(--accent)', color:'var(--paper)',
          border:'none', borderRadius: 2, fontFamily:"'Geist', sans-serif",
          fontSize: 12, fontWeight: 500, cursor:'pointer',
        }}>Otwórz w ArtCompare ›</button>
        <button style={{
          padding:'8px 14px', background:'transparent', color:'var(--ink-2)',
          border:'0.5px solid var(--rule-3)', borderRadius: 2,
          fontFamily:"'Geist', sans-serif", fontSize: 12, fontWeight: 500, cursor:'pointer',
        }}>Pobierz raport</button>
        <button style={{
          padding:'8px 14px', background:'transparent', color:'var(--ink-2)',
          border:'0.5px solid var(--rule-3)', borderRadius: 2,
          fontFamily:"'Geist', sans-serif", fontSize: 12, fontWeight: 500, cursor:'pointer',
        }}>Eskaluj do dyr. QA</button>
        <span style={{flex: 1}} />
        <span style={{fontSize: 11, color:'var(--ink-4)', fontStyle:'italic'}}>raport wygenerowany automatycznie</span>
      </div>
    </section>
  );
}

function EFooter() {
  return (
    <footer style={{
      paddingTop: 16, borderTop: '0.5px solid var(--rule-2)',
      fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--ink-4)',
      display:'flex', justifyContent:'space-between',
    }}>
      <span>ArtCompare v1.0 · ACME sp. z o.o.</span>
      <span>{CMP.date} · {CMP.time} · porównanie {CMP.no}</span>
    </footer>
  );
}

Object.assign(window, { VariantE });
