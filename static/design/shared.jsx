// Shared data + small UI atoms used across all 3 variants.

// ── COMPARISON DATA ──────────────────────────────────────────────
// One canonical fixture so all three variants display the same case.
const CMP = {
  no: '#08',
  date: '20.05.2026',
  time: '17:42',
  org: 'ACME sp. z o.o.',
  product: 'easyCARE Sterile Gauze Swabs · 200 szt.',
  sku: 'ZAY-easyCARE(200)-BNPFE',
  ean: '5900010822690',
  ref: 'INVS2603110009',
  fileA: {
    name: 'ZAY-easyCARE(200)-BNPFE-260320.pdf',
    version: 'v1.4',
    date: '24.02.2026',
    author: 'A. Kowalska',
    pages: 1,
    size: '4.2 MB',
    dim: '218 × 142 mm',
    bleed: '3 mm',
    colors: '4C + Pantone 286 C',
    fonts: 'Helvetica Neue, Arial',
  },
  fileB: {
    name: 'ZAY-easyCARE(200)-BNPFE-260320-rev.pdf',
    version: 'v1.5',
    date: '19.05.2026',
    author: 'M. Nowak (dostawca: PrintHub Wuhan)',
    pages: 1,
    size: '4.5 MB',
    dim: '218 × 142 mm',
    bleed: '2 mm',
    colors: '4C + Pantone 286 C',
    fonts: 'Helvetica Neue, Arial, Liberation Sans',
  },
  summary: { critical: 5, warning: 8, info: 4, ok: 17, items: 34 },
  modules: [
    { id: 'visual',  name: 'Diff wizualny',          status: 'fail', crit: 2, warn: 3, ok: 11, note: '2 elementy przesunięte > 2 mm' },
    { id: 'text',    name: 'Tekst regulacyjny',      status: 'fail', crit: 1, warn: 1, ok: 6,  note: 'Brak ostrzeżenia "do użytku zewnętrznego"' },
    { id: 'codes',   name: 'EAN / LOT / GTIN',       status: 'fail', crit: 1, warn: 0, ok: 4,  note: 'Niezgodność EAN' },
    { id: 'symbols', name: 'Symbole CE / piktogramy',status: 'warn', crit: 0, warn: 1, ok: 9,  note: 'CE przesunięte 2.3 mm' },
    { id: 'lang',    name: 'Wersje językowe',        status: 'fail', crit: 1, warn: 0, ok: 2,  note: 'Brak wersji DE' },
    { id: 'color',   name: 'Kolorystyka / Pantone',  status: 'warn', crit: 0, warn: 1, ok: 3,  note: 'ΔE 4.2 vs próbka' },
    { id: 'type',    name: 'Typografia / czytelność',status: 'warn', crit: 0, warn: 2, ok: 4,  note: 'Instrukcja < 7 pt' },
    { id: 'meta',    name: 'Metadane PDF',           status: 'info', crit: 0, warn: 0, ok: 2,  note: 'Inny autor i edytor' },
  ],
  diffs: [
    { id: 'D-01', sev: 'crit', cat: 'codes',   field: 'EAN-13',
      a: '5900010822690', b: '5900010822960',
      note: 'Zamiana cyfr na pozycji 11–12 (69 ↔ 96). Walidacja sumy kontrolnej: B niepoprawne.',
      where: 'Spód, oś X 162 mm, Y 12 mm',
      blocker: true },
    { id: 'D-02', sev: 'crit', cat: 'text',    field: 'Ostrzeżenie',
      a: 'Wyłącznie do użytku zewnętrznego. Nie spożywać.', b: '—',
      note: 'Wymagane zgodnie z PN-EN 15986. Usunięcie blokuje rejestrację.',
      where: 'Front, blok ostrzeżeń (R3)',
      blocker: true },
    { id: 'D-03', sev: 'crit', cat: 'lang',    field: 'Wersja językowa DE',
      a: 'obecna (Deutsch)', b: 'usunięta',
      note: 'Wymagana dla rynku DACH. Sprawdź pole "Markets" w PIM.',
      where: 'Tył, kolumna 2',
      blocker: true },
    { id: 'D-04', sev: 'crit', cat: 'codes',   field: 'Format kodu LOT',
      a: 'LOT 240520', b: 'LOT/24/05/20',
      note: 'Niezgodne ze standardem GS1 zatwierdzonym przez QA (2024-11).',
      where: 'Spód, R4',
      blocker: false },
    { id: 'D-05', sev: 'crit', cat: 'visual',  field: 'Logo ACME',
      a: 'szer. 38 mm, lewy-górny', b: 'szer. 32 mm, przesunięte 4 mm w prawo',
      note: 'Naruszenie brand guidelines — minimalny rozmiar i pole ochronne.',
      where: 'Front, R1',
      blocker: false },

    { id: 'D-06', sev: 'warn', cat: 'visual',  field: 'Symbol CE',
      a: 'X 192 mm, Y 8 mm', b: 'X 194.3 mm, Y 8 mm',
      note: 'Przesunięcie 2.3 mm — w granicach tolerancji druku, ale poza siatką.',
      where: 'Tył, R5', blocker: false },
    { id: 'D-07', sev: 'warn', cat: 'type',    field: 'Wielkość czcionki — instrukcja',
      a: '7.0 pt', b: '6.5 pt',
      note: 'Poniżej minimum 7 pt wg dyrektywy MDR — sprawdź renderowanie.',
      where: 'Tył, kolumna 1', blocker: false },
    { id: 'D-08', sev: 'warn', cat: 'visual',  field: 'Spad / bleed',
      a: '3 mm', b: '2 mm',
      note: 'Drukarnia wymaga 3 mm dla maszyny offsetowej KBA Rapida.',
      where: 'Cały arkusz', blocker: false },
    { id: 'D-09', sev: 'warn', cat: 'color',   field: 'Pantone 286 C',
      a: 'ΔE 0.4 vs próbka', b: 'ΔE 4.2 vs próbka',
      note: 'Powyżej akceptowalnego progu ΔE = 2.0.',
      where: 'Pasek nagłówka', blocker: false },
    { id: 'D-10', sev: 'warn', cat: 'visual',  field: 'Pole "Data ważności"',
      a: 'Y 118 mm', b: 'Y 122 mm',
      note: 'Przesunięcie 4 mm w dół; koliduje z liniami cięcia.',
      where: 'Spód, R4', blocker: false },
    { id: 'D-11', sev: 'warn', cat: 'symbols', field: 'Piktogram "trzymać z dala od słońca"',
      a: 'wektor', b: 'rastrowy 220 dpi',
      note: 'Rekomendowane 300 dpi lub wektor — możliwa pikseloza w druku.',
      where: 'Spód, R5', blocker: false },
    { id: 'D-12', sev: 'warn', cat: 'type',    field: 'Czcionka dodana',
      a: '—', b: 'Liberation Sans (substytut)',
      note: 'Drukarnia może nie mieć tej rodziny — zaembedduj fonty.',
      where: 'Metadane', blocker: false },
    { id: 'D-13', sev: 'warn', cat: 'text',    field: 'Numer rejestracyjny',
      a: 'PL/CA01/0258/26', b: 'PL/CA01/0258/2026',
      note: 'Format roku — zatwierdzić z działem regulacyjnym.',
      where: 'Tył, R6', blocker: false },

    { id: 'D-14', sev: 'info', cat: 'meta',    field: 'Autor PDF',
      a: 'A. Kowalska', b: 'M. Nowak (PrintHub Wuhan)',
      note: 'Plik B przygotowany przez dostawcę zewnętrznego.',
      where: 'Metadane', blocker: false },
    { id: 'D-15', sev: 'info', cat: 'meta',    field: 'Producent PDF',
      a: 'Adobe InDesign 19.5', b: 'Adobe Acrobat 24.2 (Distiller)',
      note: '', where: 'Metadane', blocker: false },
    { id: 'D-16', sev: 'info', cat: 'visual',  field: 'Znaki cięcia',
      a: 'standardowe', b: 'standardowe + japońskie',
      note: 'Dodatkowe — bez wpływu na druk PL.',
      where: 'Cały arkusz', blocker: false },
    { id: 'D-17', sev: 'info', cat: 'visual',  field: 'Profil ICC',
      a: 'ISO Coated v2 (ECI)', b: 'ISO Coated v2 300% (ECI)',
      note: '', where: 'Metadane', blocker: false },
  ],
};

const CATEGORIES = {
  visual:  { label: 'Wizualne',     icon: '◐' },
  text:    { label: 'Tekst',        icon: '¶' },
  codes:   { label: 'Kody',         icon: '▦' },
  symbols: { label: 'Symbole',      icon: '⊕' },
  lang:    { label: 'Języki',       icon: 'Aa' },
  color:   { label: 'Kolor',        icon: '◍' },
  type:    { label: 'Typografia',   icon: 'T' },
  meta:    { label: 'Metadane',     icon: 'ⓘ' },
};

const SEV = {
  crit: { label: 'Krytyczny',  short: 'CRIT', color: 'var(--critical)', bg: 'var(--critical-bg)', dot: '●' },
  warn: { label: 'Ostrzeżenie',short: 'WARN', color: 'var(--warn)',     bg: 'var(--warn-bg)',     dot: '●' },
  info: { label: 'Informacja', short: 'INFO', color: 'var(--info)',     bg: 'var(--info-bg)',     dot: '●' },
  ok:   { label: 'Zgodne',     short: 'OK',   color: 'var(--ok)',       bg: 'var(--ok-bg)',       dot: '●' },
};

// ── Atoms ────────────────────────────────────────────────────────
function SevTag({ sev, size='sm' }) {
  const s = SEV[sev];
  const pad = size === 'sm' ? '2px 7px' : '4px 9px';
  const fs = size === 'sm' ? 10 : 11;
  return (
    <span style={{
      display:'inline-flex', alignItems:'center', gap:5,
      padding: pad, fontSize: fs, fontFamily:"'Geist Mono', monospace",
      fontWeight: 600, letterSpacing: '.04em',
      color: s.color, background: s.bg,
      borderRadius: 3,
    }}>
      <span style={{fontSize: 8, lineHeight: 1, color: s.color}}>●</span>
      {s.short}
    </span>
  );
}

function Rule({ vertical, color='var(--rule)', style={} }) {
  return vertical
    ? <div style={{width:1, alignSelf:'stretch', background: color, ...style}} />
    : <div style={{height:1, width:'100%', background: color, ...style}} />;
}

function KV({ k, v, mono, style={} }) {
  return (
    <div style={{display:'flex', alignItems:'baseline', gap:8, ...style}}>
      <div style={{fontSize:10, color:'var(--ink-3)', letterSpacing:'.06em', textTransform:'uppercase', minWidth:80}}>{k}</div>
      <div style={{
        fontSize:12, color:'var(--ink)',
        fontFamily: mono ? "'Geist Mono', monospace" : 'inherit',
      }}>{v}</div>
    </div>
  );
}

// Brand header used in print/report-style variant
function AcmeWordmark({ color='var(--ink)', size=14 }) {
  return (
    <div style={{display:'inline-flex', alignItems:'center', gap:8, fontFamily:"'Geist', sans-serif", color}}>
      <div style={{
        width: size+4, height: size+4, borderRadius: 2,
        background: 'var(--accent)', display:'flex', alignItems:'center', justifyContent:'center',
        color:'#0d1b2c', fontWeight:700, fontSize: size-2, letterSpacing:'-.02em'
      }}>A</div>
      <div style={{fontSize: size, fontWeight: 600, letterSpacing:'-.01em'}}>ACME</div>
    </div>
  );
}

Object.assign(window, { CMP, CATEGORIES, SEV, SevTag, Rule, KV, AcmeWordmark });
