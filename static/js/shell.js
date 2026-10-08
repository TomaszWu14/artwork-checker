/* ===========================================================================
   DocCompare v6 — minimal Alpine shell for sidebar state
   =========================================================================== */

function dcShell() {
  return {
    rail:       false,
    mobileOpen: false,
    collapsed:  {},

    init() {
      try { this.rail = localStorage.getItem('dc_rail') === '1'; } catch(e) {}
      try { this.collapsed = JSON.parse(localStorage.getItem('dc_sections') || '{}'); } catch(e) {}
      this.$nextTick(() => { if (window.lucide) lucide.createIcons(); });
      this.$watch('rail', v => { try { localStorage.setItem('dc_rail', v ? '1' : '0'); } catch(e) {}
        this.$nextTick(() => { if (window.lucide) lucide.createIcons(); }); });

      // Keyboard shortcuts
      const self = this;
      document.addEventListener('keydown', function(e) {
        // Ignore when typing in inputs/textareas
        const tag = (e.target || {}).tagName || '';
        if (['INPUT','TEXTAREA','SELECT'].includes(tag)) return;

        // [ → toggle sidebar rail
        if (e.key === '[' && !e.ctrlKey && !e.metaKey && !e.altKey) {
          e.preventDefault();
          self.toggleRail();
        }
        // Escape → close mobile sidebar
        if (e.key === 'Escape' && self.mobileOpen) {
          self.mobileOpen = false;
        }
      });
    },

    toggleRail()      { this.rail = !this.rail; },
    toggleSection(id) {
      this.collapsed = { ...this.collapsed, [id]: !this.collapsed[id] };
      try { localStorage.setItem('dc_sections', JSON.stringify(this.collapsed)); } catch(e) {}
    },
    isCollapsed(id) { return !!this.collapsed[id]; },

    icons() { if (window.lucide) window.lucide.createIcons(); },
  };
}
