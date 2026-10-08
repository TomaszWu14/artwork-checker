// ── VARIANT C ──────────────────────────────────────────────
// Review board. The diff list as a kanban with signoff lanes:
//   "Do przeglądu" → "W trakcie" → "Do poprawy u dostawcy" → "Zaakceptowane"
//
// Each card carries severity, location, A↔B values, owner, comments.
// Top of board: hero strip with miniature of both artworks + KPIs.
//
// Sized 1280 × 900.

const vcDims = { W: 1280, H: 900 };

const LANES = [
  { id: 'todo',      title: 'Do przeglądu',           color: 'var(--ink-3)',  bg: 'var(--paper-2)' },
  { id: 'doing',     title: 'W trakcie',              color: 'var(--info)',   bg: 'var(--info-bg)' },
  { id: 'supplier',  title: 'Do poprawy u dostawcy',  color: 'var(--warn)',   bg: 'var(--warn-bg)' },
  { id: 'done',      title: 'Zaakceptowane',          color: 'var(--ok)',     bg: 'var(--ok-bg)' },
];

// Assign each diff to a lane (deterministic, not random)
const LANE_OF = {
  'D-01':'supplier','D-02':'supplier','D-03':'supplier',
  'D-04':'doing','D-05':'doing',
  'D-06':'todo','D-07':'todo','D-08':'todo','D-09':'doing','D-10':'todo',
  'D-11':'todo','D-12':'done','D-13':'doing',
  'D-14':'done','D-15':'done','D-16':'done','D-17':'done',
};

const OWNERS = {
  'D-01':{ name:'M. Nowak', role:'dostawca', tone:'warn' },
  'D-02':{ name:'J. Zielińska', role:'QA',  tone:'ok' },
  'D-03':{ name:'P. Wojtas', role:'Reg.', tone:'ink' },
  'D-04':{ name:'M. Nowak', role:'dostawca', tone:'warn' },
  'D-05':{ name:'A. Kowalska', role:'Brand', tone:'ink' },
  'D-06':{ name:'A. Kowalska', role:'Brand', tone:'ink' },
  'D-07':{ name:'J. Zielińska', role:'QA',  tone:'ok' },
  'D-08':{ name:'T. Bąk', role:'DTP', tone:'ink' },
  'D-09':{ name:'A. Kowalska', role:'Brand', tone:'ink' },
  'D-10':{ name:'T. Bąk', role:'DTP', tone:'ink' },
  'D-11':{ name:'T. Bąk', role:'DTP', tone:'ink' },
  'D-12':{ name:'T. Bąk', role:'DTP', tone:'ink' },
  'D-13':{ name:'P. Wojtas', role:'Reg.', tone:'ink' },
  'D-14':{ name:'J. Zielińska', role:'QA',  tone:'ok' },
  'D-15':{ name:'—', role:'auto', tone:'ink' },
  'D-16':{ name:'—', role:'auto', tone:'ink' },
  'D-17':{ name:'—', role:'auto', tone:'ink' },
};

const COMMENTS = {
  'D-01': 2, 'D-02': 4, 'D-03': 1, 'D-05': 1, 'D-09': 2,
};

function VariantC() {
  return (
    <div style={{
      width: vcDims.W, minHeight: vcDims.H,
      background: 'var(--paper-2)',
      fontFamily: "'Geist', sans-serif", color: 'var(--ink)', fontSize: 13,
      display: 'flex', flexDirection: 'column',
    }}>
      <CHeader />
      <CHero />
      <CBoard />
    </div>
  );
}

function CHeader() {
  return (
    <header style={{
      padding:'16px 28px',
      background:'var(--paper-2)',
      borderBottom:'0.5px solid var(--rule-2)',
      display:'flex', alignItems:'center', gap: 14,
    }}>
      <AcmeWordmark size={13} />
      <span style={{color:'var(--ink-4)'}}>/</span>
      <span style={{fontWeight: 500}}>ArtCompare</span>
      <span style={{color:'var(--ink-4)'}}>/</span>
      <span style={{fontFamily:"'Geist Mono', monospace", color:'var(--ink-3)', fontSize: 12}}>{CMP.no}</span>
      <span style={{fontWeight: 500}}>· {CMP.product}</span>
      <div style={{flex: 1}} />
      <div style={{display:'flex', alignItems:'center', gap: 8, fontSize: 11, color:'var(--ink-3)'}}>
        <span>Otwarte</span>
        <span style={{fontFamily:"'Geist Mono', monospace", color:'var(--ink)', fontWeight: 500}}>{CMP.summary.critical + CMP.summary.warning + CMP.summary.info - 5}</span>
        <span>· Zaakceptowane</span>
        <span style={{fontFamily:"'Geist Mono', monospace", color:'var(--ok)', fontWeight: 500}}>5</span>
        <span>· Czas przeglądu</span>
        <span style={{fontFamily:"'Geist Mono', monospace", color:'var(--ink)', fontWeight: 500}}>2 dni 4 h</span>
      </div>
      <button style={{
        padding:'7px 14px', background:'var(--accent)', color:'var(--paper)',
        border:'none', cursor:'pointer', borderRadius: 2,
        fontFamily:"'Geist', sans-serif", fontSize: 12, fontWeight: 500,
      }}>Zaakceptuj wszystko + odeślij</button>
    </header>
  );
}

function CHero() {
  return (
    <section style={{
      padding:'24px 28px',
      background:'var(--paper-tint)',
      borderBottom:'0.5px solid var(--rule-2)',
      display:'grid', gridTemplateColumns:'1fr 380px',
      gap: 32, alignItems:'start',
    }}>
      <div>
        <div style={{fontSize: 10, color:'var(--ink-3)', letterSpacing:'.1em', textTransform:'uppercase'}}>
          Porównanie wersji
        </div>
        <h1 style={{
          margin:'8px 0 4px', fontFamily:"'Newsreader', serif", fontWeight: 500,
          fontSize: 30, letterSpacing:'-.01em',
        }}>
          v1.4 <span style={{color:'var(--ink-4)', fontSize:24}}>→</span> v1.5
          <span style={{fontStyle:'italic', color:'var(--ink-3)', marginLeft: 12, fontSize: 18}}>
            rev. dostawcy
          </span>
        </h1>
        <div style={{fontFamily:"'Geist Mono', monospace", fontSize: 11, color:'var(--ink-3)', marginTop: 4}}>
          {CMP.sku} · EAN {CMP.ean} · {CMP.date}
        </div>

        <div style={{
          marginTop: 18, display:'grid', gridTemplateColumns:'repeat(4, 1fr)',
          gap: 10,
        }}>
          <Kpi n={CMP.summary.critical} label="Krytyczne" sub="3 blokujące" color="var(--critical)" />
          <Kpi n={CMP.summary.warning} label="Ostrzeżenia" sub="0 blokujące" color="var(--warn)" />
          <Kpi n={CMP.summary.ok} label="Zgodne" sub="auto-OK" color="var(--ok)" />
          <Kpi n={`${Math.round(CMP.summary.ok / (CMP.summary.ok + CMP.summary.critical + CMP.summary.warning + CMP.summary.info) * 100)}%`} label="Zgodność" sub="aktualna" color="var(--ink)" />
        </div>
      </div>

      <div style={{display:'grid', gridTemplateColumns:'1fr 1fr', gap: 8}}>
        <ThumbCard variant="a" label="A · v1.4" date={CMP.fileA.date} />
        <ThumbCard variant="b" label="B · v1.5" date={CMP.fileB.date} flagged />
      </div>
    </section>
  );
}

function Kpi({ n, label, sub, color }) {
  return (
    <div style={{
      padding:'14px 16px',
      background:'var(--paper-2)',
      borderTop: `2px solid ${color}`,
      borderRadius: 0,
    }}>
      <div style={{
        fontFamily:"'Geist Mono', monospace", fontSize: 32, fontWeight: 500,
        color, lineHeight: 1, fontVariantNumeric:'tabular-nums',
      }}>{n}</div>
      <div style={{marginTop: 6, fontSize: 12, fontWeight: 500, color:'var(--ink)'}}>{label}</div>
      <div style={{fontSize: 10, color:'var(--ink-3)', marginTop: 1}}>{sub}</div>
    </div>
  );
}

function ThumbCard({ variant, label, date, flagged }) {
  return (
    <div style={{
      position:'relative', background:'var(--paper-3)', borderRadius: 2,
      border:'0.5px solid var(--rule-2)', overflow:'hidden',
    }}>
      <div style={{padding:'6px 10px', display:'flex', justifyContent:'space-between', alignItems:'center', borderBottom:'0.5px solid var(--rule-2)'}}>
        <span style={{fontSize: 10, fontFamily:"'Geist Mono', monospace", color:'var(--ink-2)'}}>{label}</span>
        <span style={{fontSize: 10, color:'var(--ink-4)', fontFamily:"'Geist Mono', monospace"}}>{date}</span>
      </div>
      <div style={{padding: 6}}>
        <PackArtwork variant={variant} width="100%" showBleed={false} />
      </div>
      {flagged && (
        <div style={{
          position:'absolute', top: 28, right: 6,
          padding:'2px 6px', background:'var(--critical)', color:'var(--paper)',
          fontFamily:"'Geist Mono', monospace", fontSize: 9, letterSpacing:'.06em',
        }}>5 KRYT.</div>
      )}
    </div>
  );
}

function CBoard() {
  return (
    <div style={{
      padding:'20px 28px 28px',
      flex: 1,
      display:'grid', gridTemplateColumns:'repeat(4, 1fr)', gap: 14,
    }}>
      {LANES.map((lane) => (
        <BoardColumn key={lane.id} lane={lane}
          diffs={CMP.diffs.filter((d) => LANE_OF[d.id] === lane.id)} />
      ))}
    </div>
  );
}

function BoardColumn({ lane, diffs }) {
  return (
    <div style={{display:'flex', flexDirection:'column', minWidth: 0}}>
      <div style={{
        display:'flex', alignItems:'center', justifyContent:'space-between',
        padding:'8px 10px', marginBottom: 10,
        borderTop:`2px solid ${lane.color}`, background:'var(--paper-2)',
      }}>
        <div style={{display:'flex', alignItems:'center', gap: 8}}>
          <span style={{
            width: 6, height: 6, borderRadius:'50%', background: lane.color,
          }} />
          <span style={{fontSize: 12, fontWeight: 500}}>{lane.title}</span>
        </div>
        <span style={{
          fontFamily:"'Geist Mono', monospace", fontSize: 11,
          color: lane.color, fontWeight: 500,
        }}>{diffs.length}</span>
      </div>
      <div style={{display:'flex', flexDirection:'column', gap: 8}}>
        {diffs.map((d) => <DiffCard key={d.id} d={d} />)}
        {diffs.length === 0 && (
          <div style={{
            padding:'18px', fontSize: 11, color:'var(--ink-4)',
            textAlign:'center', fontStyle:'italic',
            border:'0.5px dashed var(--rule-2)', borderRadius: 2,
          }}>brak pozycji</div>
        )}
      </div>
    </div>
  );
}

function DiffCard({ d }) {
  const owner = OWNERS[d.id];
  const comments = COMMENTS[d.id] || 0;
  return (
    <article style={{
      background:'var(--paper-3)',
      borderRadius: 2,
      border: '0.5px solid var(--rule-2)',
      overflow:'hidden',
    }}>
      {/* tiny sev rail */}
      <div style={{height: 3, background: SEV[d.sev].color}} />

      <div style={{padding: '10px 12px'}}>
        <div style={{display:'flex', justifyContent:'space-between', alignItems:'center'}}>
          <span style={{
            fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--ink-3)',
            letterSpacing:'.04em',
          }}>{d.id} · {CATEGORIES[d.cat]?.label.toUpperCase()}</span>
          {d.blocker && (
            <span style={{
              padding:'1px 4px', fontFamily:"'Geist Mono', monospace", fontSize: 8,
              background:'var(--critical)', color:'var(--paper)', letterSpacing:'.08em',
            }}>BLOKER</span>
          )}
        </div>
        <div style={{marginTop: 6, fontWeight: 500, fontSize: 13, lineHeight: 1.3}}>
          {d.field}
        </div>
        <div style={{marginTop: 4, fontSize: 11, color:'var(--ink-2)', lineHeight: 1.4,
          overflow:'hidden', textOverflow:'ellipsis',
          display:'-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient:'vertical',
        }}>
          {d.note}
        </div>

        <div style={{marginTop: 8, display:'grid', gridTemplateColumns:'1fr 1fr', gap: 4}}>
          <MiniValue label="A" value={d.a} />
          <MiniValue label="B" value={d.b} diff />
        </div>

        <div style={{marginTop: 10, display:'flex', alignItems:'center', gap: 8}}>
          <Avatar2 initials={owner.name.split(' ').map(s=>s[0]).join('')} tone={owner.tone} />
          <div style={{flex: 1, minWidth: 0}}>
            <div style={{fontSize: 11, color:'var(--ink-2)', whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis'}}>{owner.name}</div>
            <div style={{fontSize: 9, color:'var(--ink-4)', letterSpacing:'.04em'}}>{owner.role.toUpperCase()}</div>
          </div>
          {comments > 0 && (
            <div style={{
              display:'inline-flex', alignItems:'center', gap: 3,
              fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--ink-3)',
            }}>
              <span>{'❝'}</span>{comments}
            </div>
          )}
          <div style={{
            fontFamily:"'Geist Mono', monospace", fontSize: 10, color:'var(--ink-4)',
            whiteSpace:'nowrap',
          }}>{d.where.split(',')[0]}</div>
        </div>
      </div>
    </article>
  );
}

function MiniValue({ label, value, diff }) {
  return (
    <div style={{
      padding:'5px 7px', borderRadius: 2,
      background: diff ? 'var(--critical-bg)' : 'var(--paper-2)',
    }}>
      <div style={{
        fontSize: 8, color: diff ? 'var(--critical)' : 'var(--ink-4)',
        letterSpacing:'.08em', fontFamily:"'Geist Mono', monospace",
      }}>{label}</div>
      <div style={{
        marginTop: 1, fontFamily:"'Geist Mono', monospace", fontSize: 10,
        color:'var(--ink)', lineHeight: 1.25,
        overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap',
      }}>{value || '—'}</div>
    </div>
  );
}

function Avatar2({ initials, tone }) {
  const colors = {
    ink: { bg:'var(--rule-3)', fg:'var(--ink)' },
    warn:{ bg:'var(--warn)', fg:'var(--paper)' },
    ok:  { bg:'var(--ok)', fg:'var(--paper)' },
  }[tone];
  return (
    <div style={{
      width: 22, height: 22, borderRadius:'50%', background: colors.bg, color: colors.fg,
      display:'flex', alignItems:'center', justifyContent:'center', flexShrink: 0,
      fontSize: 9, fontWeight: 600, fontFamily:"'Geist Mono', monospace",
    }}>{initials}</div>
  );
}

Object.assign(window, { VariantC });
