// Main app — assembles the design canvas with all variants.

function App() {
  return (
    <DesignCanvas
      title="ArtCompare · raport porównania artworków opakowań"
      subtitle="5 wariantów · case: ZAY-easyCARE(200)-BNPFE-260320 v1.4 → v1.5">

      <DCSection
        id="paper"
        title="A · Raport audytowy (print-grade)"
        subtitle="Ewolucja DocCompare v6 — A4 portrait, do druku i archiwum. Gęsta typografia, podgląd artworków inline.">
        <DCArtboard id="paper-1" label="A · raport audytowy"
          width={920} height={1640}>
          <VariantA />
        </DCArtboard>
      </DCSection>

      <DCSection
        id="workspace"
        title="B · Workspace inspekcji"
        subtitle="Aplikacja webowa. Tryby diff (side-by-side / overlay / onion), lista rozbieżności z akcjami, komentarze.">
        <DCArtboard id="workspace-1" label="B · workspace inspekcji"
          width={1440} height={900}>
          <VariantB />
        </DCArtboard>
      </DCSection>

      <DCSection
        id="board"
        title="C · Review board"
        subtitle="Kanban rozbieżności. Każda jako karta z odpowiedzialną osobą, statusem przeglądu i komentarzami.">
        <DCArtboard id="board-1" label="C · review board"
          width={1280} height={1100}>
          <VariantC />
        </DCArtboard>
      </DCSection>

      <DCSection
        id="annotated"
        title="D · Annotated zoom"
        subtitle="Jeden duży podgląd artworku z pinami komentarzy w stylu Figmy. Tryby A / B / tylko różnice / overlay.">
        <DCArtboard id="annotated-1" label="D · annotated zoom"
          width={1280} height={1100}>
          <VariantD />
        </DCArtboard>
      </DCSection>

      <DCSection
        id="email"
        title="E · Email / exec summary"
        subtitle="Jednostronicowe podsumowanie do wysyłki interesariuszom. Werdykt, top-5 ustaleń, status modułów, akcje.">
        <DCArtboard id="email-1" label="E · email summary"
          width={920} height={1480}>
          <VariantE />
        </DCArtboard>
      </DCSection>

    </DesignCanvas>
  );
}

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<App />);
