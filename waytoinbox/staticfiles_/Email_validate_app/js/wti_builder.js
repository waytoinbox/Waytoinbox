/* ══════════════════════════════════════════════════════
   wti_builder.js — WTI Email Template Builder
   v1.0 — custom drag-and-drop email builder
   ══════════════════════════════════════════════════════ */
(function (W) {
  'use strict';

  /* ── ID util ──────────────────────────────────────── */
  let _n = 0;
  function uid() { return 'w' + Date.now().toString(36) + (++_n); }

  /* ── Block catalogue ──────────────────────────────── */
  const BLOCKS = [
    { type: 'text',    label: 'Text',    icon: 'fa-paragraph' },
    { type: 'heading', label: 'Heading', icon: 'fa-heading' },
    { type: 'button',  label: 'Button',  icon: 'fa-square' },
    { type: 'image',   label: 'Image',   icon: 'fa-image' },
    { type: 'divider', label: 'Divider', icon: 'fa-minus' },
    { type: 'spacer',  label: 'Spacer',  icon: 'fa-arrows-up-down' },
    { type: 'social',  label: 'Social',  icon: 'fa-share-nodes' },
    { type: 'html',    label: 'HTML',    icon: 'fa-code' },
  ];

  /* ── Column layouts ───────────────────────────────── */
  const LAYOUTS = {
    '1':       { label: 'Full width',  fracs: [1] },
    '1-1':     { label: 'Two halves',  fracs: [.5, .5] },
    '1-1-1':   { label: 'Thirds',      fracs: [1/3, 1/3, 1/3] },
    '2-1':     { label: '2/3 + 1/3',  fracs: [2/3, 1/3] },
    '1-2':     { label: '1/3 + 2/3',  fracs: [1/3, 2/3] },
    '1-1-1-1': { label: 'Quarters',    fracs: [.25, .25, .25, .25] },
  };

  /* ── Default block factory ────────────────────────── */
  function mkBlock(type) {
    switch (type) {
      case 'text':
        return { id: uid(), type, html: '<p style="margin:0;line-height:1.6;font-size:14px;color:#333333;">Click to edit this text and make it your own.</p>' };
      case 'heading':
        return { id: uid(), type, level: 'h2', text: 'Your Heading Here', align: 'left', color: '#111111', fontSize: 26, fontWeight: 'bold' };
      case 'button':
        return { id: uid(), type, text: 'Click Here', url: '#', align: 'center', bgColor: '#009acc', textColor: '#ffffff', fontSize: 16, paddingV: 12, paddingH: 28, borderRadius: 4, bold: true };
      case 'image':
        return { id: uid(), type, src: '', alt: '', url: '', align: 'center', width: 100 };
      case 'divider':
        return { id: uid(), type, color: '#dddddd', thickness: 1, lineStyle: 'solid', marginV: 12 };
      case 'spacer':
        return { id: uid(), type, height: 24 };
      case 'social':
        return { id: uid(), type, align: 'center', size: 32, items: [
          { name: 'Facebook',  url: '#', color: '#1877F2' },
          { name: 'Twitter',   url: '#', color: '#1DA1F2' },
          { name: 'Instagram', url: '#', color: '#E4405F' },
          { name: 'LinkedIn',  url: '#', color: '#0A66C2' },
        ]};
      case 'html':
        return { id: uid(), type, html: '<p style="margin:0;color:#888;font-family:Arial,sans-serif;font-size:13px;">Custom HTML block — edit in the panel.</p>' };
      default:
        return { id: uid(), type };
    }
  }

  function mkRow(layoutId) {
    const layout = LAYOUTS[layoutId] || LAYOUTS['1'];
    return {
      id: uid(), layout: layoutId,
      bgColor: '#ffffff',
      pt: 20, pb: 20, pl: 20, pr: 20,
      cols: layout.fracs.map(f => ({ id: uid(), frac: f, bg: '', blocks: [] })),
    };
  }

  /* ══════════════════════════════════════════════════════
     WTIBuilder class
     ══════════════════════════════════════════════════════ */
  class WTIBuilder {
    constructor(el, opts) {
      this.el   = typeof el === 'string' ? document.querySelector(el) : el;
      this.opts = opts || {};

      this._state    = { global: { bgColor: '#f4f4f4', width: 600, fontFamily: 'Arial, sans-serif' }, rows: [] };
      this._undo     = [];
      this._redo     = [];
      this._sel      = null;          // { type:'block'|'row'|'col', id }
      this._drag     = null;          // current drag payload
      this._hTimer   = null;          // debounce history timer
      this._previewW = null;          // current preview width (null = use content width)

      this._build();
      this._bindGlobal();
      this._snapshot();
    }

    /* ── Public API ─────────────────────────────────── */
    load(design) {
      if (!design || typeof design !== 'object') return;
      this._state = JSON.parse(JSON.stringify(design));
      this._undo = []; this._redo = []; this._sel = null;
      this._snapshot();
      this._renderCanvas();
      this._renderProps();
      this._syncGlobalInputs();
    }

    getDesign() { return JSON.parse(JSON.stringify(this._state)); }
    getHtml()   { return this._buildEmailHtml(); }

    /* ── Build skeleton ─────────────────────────────── */
    _build() {
      this.el.innerHTML = '';
      this.el.classList.add('wtib-root');

      /* Left */
      this._left = this._el('div', 'wtib-left');
      this._left.innerHTML = this._leftHtml();

      /* Center */
      this._center = this._el('div', 'wtib-canvas-wrap');
      this._toolbar = this._el('div', 'wtib-toolbar');
      this._toolbar.innerHTML = this._toolbarHtml();
      this._scroll     = this._el('div', 'wtib-scroll');
      this._frameWrap  = this._el('div', 'wtib-frame-wrap');
      this._frame      = this._el('div', 'wtib-frame');
      this._resizeR    = this._el('div', 'wtib-resize-handle wtib-resize-r');
      this._widthBadge = this._el('span', 'wtib-width-badge');
      this._frameWrap.appendChild(this._frame);
      this._frameWrap.appendChild(this._resizeR);
      this._frameWrap.appendChild(this._widthBadge);
      this._scroll.appendChild(this._frameWrap);
      this._center.appendChild(this._toolbar);
      this._center.appendChild(this._scroll);

      /* Right */
      this._right = this._el('div', 'wtib-right');
      this._props = this._el('div', 'wtib-props');
      this._right.appendChild(this._props);

      this.el.appendChild(this._left);
      this.el.appendChild(this._center);
      this.el.appendChild(this._right);

      this._renderCanvas();
      this._renderProps();
      this._syncGlobalInputs();
    }

    _el(tag, cls) {
      const e = document.createElement(tag);
      if (cls) e.className = cls;
      return e;
    }

    /* ── Left panel HTML ────────────────────────────── */
    _leftHtml() {
      const chips = BLOCKS.map(b =>
        `<div class="wtib-chip" draggable="true" data-btype="${b.type}">
           <i class="fas ${b.icon}"></i><span>${b.label}</span>
         </div>`).join('');

      const layouts = Object.entries(LAYOUTS).map(([id, l]) =>
        `<div class="wtib-layout-chip" data-layout="${id}" title="${l.label}">
           <div class="wtib-layout-preview">${l.fracs.map(f=>`<span style="flex:${f}"></span>`).join('')}</div>
           <span>${l.label}</span>
         </div>`).join('');

      return `
        <div class="wtib-panel-hd">Blocks</div>
        <div class="wtib-block-grid">${chips}</div>
        <div class="wtib-panel-hd">Add Row</div>
        <div class="wtib-layout-list">${layouts}</div>
        <div class="wtib-panel-hd">Global Settings</div>
        <div class="wtib-global-opts">
          <div class="wtib-prop-row row">
            <label>Background</label>
            <input type="color" data-g="bgColor" value="#f4f4f4">
          </div>
          <div class="wtib-prop-row">
            <label>Content Width (px)</label>
            <input type="number" data-g="width" value="600" min="400" max="900" step="10">
          </div>
          <div class="wtib-prop-row">
            <label>Font Family</label>
            <select data-g="fontFamily">
              <option value="Arial, sans-serif">Arial</option>
              <option value="'Helvetica Neue', Helvetica, sans-serif">Helvetica</option>
              <option value="Georgia, serif">Georgia</option>
              <option value="'Times New Roman', Times, serif">Times New Roman</option>
              <option value="Verdana, sans-serif">Verdana</option>
              <option value="Tahoma, sans-serif">Tahoma</option>
            </select>
          </div>
        </div>`;
    }

    /* ── Toolbar HTML ───────────────────────────────── */
    _toolbarHtml() {
      return `
        <div class="wtib-toolbar-group">
          <button class="wtib-tb-btn" id="wtibUndo" title="Undo (Ctrl+Z)"><i class="fas fa-rotate-left"></i></button>
          <button class="wtib-tb-btn" id="wtibRedo" title="Redo (Ctrl+Y)"><i class="fas fa-rotate-right"></i></button>
        </div>
        <div class="wtib-toolbar-group">
          <span class="wtib-preview-label">Preview:</span>
          <button class="wtib-tb-btn active" data-preview="desktop" title="Desktop view"><i class="fas fa-desktop"></i> Desktop</button>
          <button class="wtib-tb-btn"        data-preview="mobile"  title="Mobile view"><i class="fas fa-mobile-screen"></i> Mobile</button>
          <div class="wtib-tb-sep"></div>
          <button class="wtib-tb-btn wtib-fp-only" id="wtibSaveBtn" title="Save template"><i class="fas fa-save"></i> Save</button>
          <button class="wtib-tb-btn" id="wtibFullPage" title="Toggle full-page mode (F11)"><i class="fas fa-expand"></i> Full Page</button>
        </div>`;
    }

    /* ── Preview width ──────────────────────────────── */
    _setPreviewWidth(w) {
      const desktopW = this._state.global.width || 600;
      const clamped  = Math.max(320, Math.min(this._scroll.offsetWidth - 48, w));
      this._previewW = clamped;
      this._frame.style.width    = clamped + 'px';
      this._frame.style.maxWidth = clamped + 'px';
      this._frameWrap.style.width = clamped + 'px';
      this._widthBadge.textContent = Math.round(clamped) + 'px';
      this._toolbar.querySelectorAll('[data-preview]').forEach(b => {
        if      (b.dataset.preview === 'desktop') b.classList.toggle('active', Math.round(clamped) >= desktopW);
        else if (b.dataset.preview === 'mobile')  b.classList.toggle('active', Math.round(clamped) <= 375);
        else b.classList.remove('active');
      });
    }

    /* ── Canvas render ──────────────────────────────── */
    _renderCanvas() {
      const savedScroll = this._scroll.scrollTop;
      const g = this._state.global;
      this._frame.innerHTML = '';
      /* Preserve current preview width — only reset to content width on first render */
      this._setPreviewWidth(this._previewW !== null ? this._previewW : (g.width || 600));
      this._scroll.style.background = g.bgColor || '#f4f4f4';

      if (!this._state.rows.length) {
        const empty = this._el('div', 'wtib-empty');
        empty.innerHTML = `<i class="fas fa-inbox"></i><p>Drag a block from the left panel,<br>or click a row layout to start.</p>`;
        this._bindDrop(empty, null, null, 0);
        this._frame.appendChild(empty);
        return;
      }

      this._state.rows.forEach((row) => {
        this._frame.appendChild(this._buildRowEl(row));
      });
      this._scroll.scrollTop = savedScroll;
    }

    /* ── Row element ────────────────────────────────── */
    _buildRowEl(row) {
      const wrap = this._el('div', 'wtib-row-wrap');
      wrap.dataset.rowId = row.id;
      if (this._sel?.type === 'row' && this._sel.id === row.id) wrap.classList.add('selected');

      /* Row controls */
      const ctrl = this._el('div', 'wtib-row-ctrl');
      ctrl.innerHTML = `
        <span class="wtib-row-badge">Row</span>
        <div class="wtib-row-ctrl-spacer"></div>
        <button class="wtib-icon-btn grab" data-rh="${row.id}" title="Drag to reorder"><i class="fas fa-grip-lines"></i></button>
        <button class="wtib-icon-btn" data-ra="up"  data-rid="${row.id}" title="Move up"><i class="fas fa-chevron-up"></i></button>
        <button class="wtib-icon-btn" data-ra="dn"  data-rid="${row.id}" title="Move down"><i class="fas fa-chevron-down"></i></button>
        <button class="wtib-icon-btn" data-ra="dup" data-rid="${row.id}" title="Duplicate"><i class="fas fa-copy"></i></button>
        <button class="wtib-icon-btn danger" data-ra="del" data-rid="${row.id}" title="Delete row"><i class="fas fa-trash"></i></button>`;
      wrap.appendChild(ctrl);

      /* Row inner */
      const rowEl = this._el('div', 'wtib-row');
      rowEl.dataset.rowId = row.id;
      rowEl.style.cssText = `background:${row.bgColor||'#fff'};padding:${row.pt||0}px ${row.pr||0}px ${row.pb||0}px ${row.pl||0}px;`;
      row.cols.forEach(col => rowEl.appendChild(this._buildColEl(col, row.id)));
      wrap.appendChild(rowEl);

      /* Click to select row */
      wrap.addEventListener('click', e => {
        if (e.target.closest('[data-ra],[data-rh],[data-ba],[data-btype],.wtib-col,.wtib-block')) return;
        this._select('row', row.id);
      });

      /* Row drag handle */
      const handle = ctrl.querySelector('[data-rh]');
      handle.addEventListener('mousedown', () => { wrap.draggable = true; });
      document.addEventListener('mouseup', () => { wrap.draggable = false; }, { once: false });

      wrap.addEventListener('dragstart', e => {
        if (!wrap.draggable) { e.preventDefault(); return; }
        this._drag = { src: 'row', rowId: row.id };
        e.dataTransfer.effectAllowed = 'move';
        setTimeout(() => wrap.classList.add('dragging'), 0);
      });
      wrap.addEventListener('dragend', () => {
        wrap.draggable = false; wrap.classList.remove('dragging');
        this._frame.querySelectorAll('.drag-over-top,.drag-over-bottom').forEach(el => el.classList.remove('drag-over-top','drag-over-bottom'));
        this._drag = null;
      });
      wrap.addEventListener('dragover', e => {
        if (this._drag?.src !== 'row') return;
        e.preventDefault();
        const mid = wrap.getBoundingClientRect().top + wrap.offsetHeight / 2;
        wrap.classList.toggle('drag-over-top',    e.clientY < mid);
        wrap.classList.toggle('drag-over-bottom', e.clientY >= mid);
      });
      wrap.addEventListener('dragleave', () => wrap.classList.remove('drag-over-top','drag-over-bottom'));
      wrap.addEventListener('drop', e => {
        if (this._drag?.src !== 'row') return;
        e.preventDefault();
        const mid = wrap.getBoundingClientRect().top + wrap.offsetHeight / 2;
        this._moveRow(this._drag.rowId, row.id, e.clientY < mid);
        wrap.classList.remove('drag-over-top','drag-over-bottom');
      });

      return wrap;
    }

    /* ── Column element ─────────────────────────────── */
    _buildColEl(col, rowId) {
      const el = this._el('div', 'wtib-col');
      el.dataset.colId = col.id;
      el.style.flex = col.frac;
      if (col.bg) el.style.background = col.bg;
      if (this._sel?.type === 'col' && this._sel.id === col.id) el.classList.add('selected');

      if (!col.blocks.length) {
        const ph = this._el('div', 'wtib-col-ph');
        ph.innerHTML = '<i class="fas fa-plus"></i>';
        el.appendChild(ph);
      } else {
        col.blocks.forEach(b => el.appendChild(this._buildBlockEl(b, col.id)));
      }

      /* Drop zone */
      this._bindDrop(el, col.id, rowId, null);

      el.addEventListener('click', e => {
        if (e.target.closest('.wtib-block')) return;
        e.stopPropagation();
        this._select('col', col.id);
      });

      return el;
    }

    /* ── Block element ──────────────────────────────── */
    _buildBlockEl(block, colId) {
      const wrap = this._el('div', 'wtib-block');
      wrap.dataset.blockId = block.id;
      wrap.dataset.colId   = colId;
      wrap.draggable = true;
      if (this._sel?.type === 'block' && this._sel.id === block.id) wrap.classList.add('selected');

      /* Block toolbar */
      const ctrl = this._el('div', 'wtib-block-ctrl');
      ctrl.innerHTML = `
        <button class="wtib-icon-btn" data-ba="up"  data-bid="${block.id}" title="Move up"><i class="fas fa-chevron-up"></i></button>
        <button class="wtib-icon-btn" data-ba="dn"  data-bid="${block.id}" title="Move down"><i class="fas fa-chevron-down"></i></button>
        <button class="wtib-icon-btn" data-ba="dup" data-bid="${block.id}" title="Duplicate"><i class="fas fa-copy"></i></button>
        <button class="wtib-icon-btn danger" data-ba="del" data-bid="${block.id}" title="Delete"><i class="fas fa-trash"></i></button>`;
      wrap.appendChild(ctrl);

      /* Block content */
      const inner = this._el('div', 'wtib-block-inner');
      inner.innerHTML = this._blockPreviewHtml(block);
      wrap.appendChild(inner);

      /* Select on click */
      wrap.addEventListener('click', e => {
        if (e.target.closest('[data-ba]')) return; // let action buttons bubble to _frame
        e.stopPropagation();
        this._select('block', block.id);
      });

      /* Double-click to edit text inline */
      if (block.type === 'text' || block.type === 'html') {
        wrap.addEventListener('dblclick', e => {
          if (e.target.closest('[data-ba]')) return;
          const editable = inner.querySelector('.wtib-editable');
          if (!editable) return;
          editable.contentEditable = 'true';
          editable.classList.add('wtib-editing');
          editable.focus();
          const done = () => {
            editable.contentEditable = 'false';
            editable.classList.remove('wtib-editing');
            block.html = editable.innerHTML;
            this._snapshot();
          };
          editable.addEventListener('blur', done, { once: true });
        });
      }
      if (block.type === 'heading') {
        wrap.addEventListener('dblclick', e => {
          if (e.target.closest('[data-ba]')) return;
          const editable = inner.querySelector('.wtib-editable');
          if (!editable) return;
          editable.contentEditable = 'true';
          editable.classList.add('wtib-editing');
          editable.focus();
          const done = () => {
            editable.contentEditable = 'false';
            editable.classList.remove('wtib-editing');
            block.text = editable.textContent;
            this._snapshot();
          };
          editable.addEventListener('blur', done, { once: true });
        });
      }

      /* Block drag */
      wrap.addEventListener('dragstart', e => {
        e.stopPropagation();
        this._drag = { src: 'block', blockId: block.id, colId };
        e.dataTransfer.effectAllowed = 'move';
        setTimeout(() => wrap.classList.add('dragging'), 0);
      });
      wrap.addEventListener('dragend', () => {
        wrap.classList.remove('dragging');
        this._frame.querySelectorAll('.wtib-drop-line').forEach(d => d.remove());
        this._frame.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
        this._drag = null;
      });

      return wrap;
    }

    /* ── Block preview HTML ─────────────────────────── */
    _blockPreviewHtml(block) {
      const ff = this._state.global.fontFamily || 'Arial, sans-serif';
      switch (block.type) {
        case 'text':
          return `<div class="wtib-editable" style="font-family:${ff};padding:4px 0;">${block.html || ''}</div>`;

        case 'heading': {
          const tag = block.level || 'h2';
          return `<${tag} class="wtib-editable" style="margin:0;padding:4px 0;font-family:${ff};font-size:${block.fontSize||26}px;color:${block.color||'#111'};font-weight:${block.fontWeight||'bold'};text-align:${block.align||'left'};">${block.text||''}</${tag}>`;
        }

        case 'button': {
          const s = `display:inline-block;font-family:${ff};background:${block.bgColor||'#009acc'};color:${block.textColor||'#fff'};font-size:${block.fontSize||16}px;font-weight:${block.bold?'bold':'normal'};padding:${block.paddingV||12}px ${block.paddingH||28}px;border-radius:${block.borderRadius||4}px;text-decoration:none;cursor:pointer;`;
          return `<div style="text-align:${block.align||'center'};padding:6px 0;"><a style="${s}">${block.text||'Button'}</a></div>`;
        }

        case 'image':
          if (!block.src) return `<div class="wtib-img-ph"><div class="wtib-img-ph-inner"><i class="fas fa-image"></i><span>Set image URL in the panel →</span></div></div>`;
          return `<div style="text-align:${block.align||'center'};padding:4px 0;"><img src="${block.src}" alt="${this._esc(block.alt||'')}" style="display:inline-block;max-width:100%;width:${block.width||100}%;height:auto;"></div>`;

        case 'divider':
          return `<div style="padding:${block.marginV||12}px 0;"><hr style="border:0;border-top:${block.thickness||1}px ${block.lineStyle||'solid'} ${block.color||'#ddd'};margin:0;"></div>`;

        case 'spacer':
          return `<div style="height:${block.height||24}px;background:repeating-linear-gradient(45deg,rgba(0,153,204,.07) 0,rgba(0,153,204,.07) 2px,transparent 2px,transparent 8px);"></div>`;

        case 'social': {
          const icons = (block.items||[]).map(n => {
            const sz = block.size || 32;
            return `<span style="display:inline-block;width:${sz}px;height:${sz}px;background:${n.color||'#ccc'};border-radius:50%;line-height:${sz}px;text-align:center;color:#fff;font-weight:700;font-size:${Math.floor(sz*.38)}px;font-family:${ff};margin:0 4px;">${n.name.slice(0,2).toUpperCase()}</span>`;
          }).join('');
          return `<div style="text-align:${block.align||'center'};padding:6px 0;">${icons}</div>`;
        }

        case 'html':
          return `<div class="wtib-editable">${block.html||''}</div>`;

        default:
          return `<div style="padding:8px;color:#aaa;font-size:12px;">[${block.type}]</div>`;
      }
    }

    /* ── Drop zone binding ──────────────────────────── */
    _bindDrop(el, colId, rowId, forceIdx) {
      el.addEventListener('dragover', e => {
        if (!this._drag || this._drag.src === 'row') return;
        e.preventDefault(); e.stopPropagation();
        el.classList.add('drag-over');
        /* show drop line */
        el.querySelectorAll('.wtib-drop-line').forEach(d => d.remove());
        const idx = this._dropIndex(el, e.clientY);
        const blocks = el.querySelectorAll('.wtib-block');
        const line = this._el('div', 'wtib-drop-line');
        if (idx < blocks.length) el.insertBefore(line, blocks[idx]);
        else el.appendChild(line);
      });
      el.addEventListener('dragleave', e => {
        if (!el.contains(e.relatedTarget)) {
          el.classList.remove('drag-over');
          el.querySelectorAll('.wtib-drop-line').forEach(d => d.remove());
        }
      });
      el.addEventListener('drop', e => {
        e.preventDefault(); e.stopPropagation();
        el.classList.remove('drag-over');
        el.querySelectorAll('.wtib-drop-line').forEach(d => d.remove());
        const idx = forceIdx !== null ? forceIdx : this._dropIndex(el, e.clientY);
        this._handleDrop(colId, rowId, idx);
      });
    }

    _dropIndex(colEl, mouseY) {
      const blocks = Array.from(colEl.querySelectorAll('.wtib-block'));
      for (let i = 0; i < blocks.length; i++) {
        const r = blocks[i].getBoundingClientRect();
        if (mouseY < r.top + r.height / 2) return i;
      }
      return blocks.length;
    }

    _handleDrop(colId, rowId, insertIdx) {
      const d = this._drag;
      if (!d) return;

      if (d.src === 'panel') {
        /* New block dropped from left panel */
        let targetColId = colId;
        if (!targetColId) {
          /* Dropped on empty canvas — create a full-width row */
          const row = mkRow('1');
          this._state.rows.push(row);
          targetColId = row.cols[0].id;
          insertIdx = 0;
        }
        const col = this._findCol(targetColId);
        if (!col) return;
        const block = mkBlock(d.btype);
        col.blocks.splice(insertIdx, 0, block);
        this._sel = { type: 'block', id: block.id };

      } else if (d.src === 'block') {
        /* Existing block moved */
        let targetColId = colId;
        if (!targetColId) {
          const row = mkRow('1');
          this._state.rows.push(row);
          targetColId = row.cols[0].id;
          insertIdx = 0;
        }
        const srcCol = this._findCol(d.colId);
        const dstCol = this._findCol(targetColId);
        if (!srcCol || !dstCol) return;
        const bi = srcCol.blocks.findIndex(b => b.id === d.blockId);
        if (bi < 0) return;
        const [block] = srcCol.blocks.splice(bi, 1);
        const adj = (srcCol === dstCol && bi < insertIdx) ? insertIdx - 1 : insertIdx;
        dstCol.blocks.splice(adj, 0, block);
        this._sel = { type: 'block', id: block.id };

      } else if (d.src === 'layout') {
        /* New row from layout chip dropped on empty canvas */
        const row = mkRow(d.layoutId);
        this._state.rows.push(row);
        this._sel = { type: 'row', id: row.id };
      }

      this._snapshot();
      this._renderCanvas();
      this._renderProps();
    }

    _moveRow(fromId, toId, before) {
      const rows = this._state.rows;
      const fi = rows.findIndex(r => r.id === fromId);
      const ti = rows.findIndex(r => r.id === toId);
      if (fi < 0 || ti < 0 || fi === ti) return;
      const [row] = rows.splice(fi, 1);
      const newTi = rows.findIndex(r => r.id === toId);
      rows.splice(before ? newTi : newTi + 1, 0, row);
      this._snapshot();
      this._renderCanvas();
    }

    /* ── Selection ──────────────────────────────────── */
    _select(type, id) {
      this._sel = { type, id };
      this._renderCanvas();
      this._renderProps();
    }

    /* ── Properties panel ───────────────────────────── */
    _renderProps() {
      this._props.innerHTML = '';
      if (!this._sel) {
        this._props.innerHTML = `<div class="wtib-props-empty"><i class="fas fa-arrow-pointer"></i><p>Click any element on the canvas to edit its properties.</p></div>`;
        return;
      }
      if (this._sel.type === 'block') {
        const b = this._findBlock(this._sel.id);
        if (b) this._blockProps(b);
      } else if (this._sel.type === 'row') {
        const r = this._findRow(this._sel.id);
        if (r) this._rowProps(r);
      } else if (this._sel.type === 'col') {
        const c = this._findCol(this._sel.id);
        if (c) this._colProps(c);
      }
    }

    _rowProps(row) {
      this._props.innerHTML = `
        <div class="wtib-props-title"><i class="fas fa-table-columns"></i> Row Settings</div>
        <div class="wtib-prop-group">
          <div class="wtib-prop-row row">
            <label>Background</label>
            <input type="color" data-rp="${row.id}" data-k="bgColor" value="${row.bgColor||'#ffffff'}">
          </div>
          <div>
            <label class="wtib-prop-row" style="margin-bottom:5px;font-family:var(--font-head);font-size:10.5px;font-weight:700;color:var(--ink-3);">Padding (px)</label>
            <div class="wtib-prop-2col">
              <div class="wtib-prop-row"><label>Top</label><input type="number" data-rp="${row.id}" data-k="pt" value="${row.pt||0}" min="0" max="120"></div>
              <div class="wtib-prop-row"><label>Bottom</label><input type="number" data-rp="${row.id}" data-k="pb" value="${row.pb||0}" min="0" max="120"></div>
              <div class="wtib-prop-row"><label>Left</label><input type="number" data-rp="${row.id}" data-k="pl" value="${row.pl||0}" min="0" max="120"></div>
              <div class="wtib-prop-row"><label>Right</label><input type="number" data-rp="${row.id}" data-k="pr" value="${row.pr||0}" min="0" max="120"></div>
            </div>
          </div>
        </div>`;
      this._bindProps();
    }

    _colProps(col) {
      this._props.innerHTML = `
        <div class="wtib-props-title"><i class="fas fa-columns"></i> Column Settings</div>
        <div class="wtib-prop-group">
          <div class="wtib-prop-row row">
            <label>Background</label>
            <input type="color" data-cp="${col.id}" data-k="bg" value="${col.bg||'#ffffff'}">
          </div>
        </div>`;
      this._bindProps();
    }

    _blockProps(block) {
      let html = `<div class="wtib-props-title"><i class="fas fa-pen-to-square"></i> ${block.type.charAt(0).toUpperCase()+block.type.slice(1)}</div>`;
      const B = block.id;

      switch (block.type) {
        case 'text':
          html += `<div class="wtib-prop-group"><p class="wtib-hint"><i class="fas fa-hand-pointer"></i>Double-click the block on the canvas to edit text directly.</p></div>`;
          break;

        case 'heading':
          html += `<div class="wtib-prop-group">
            <div class="wtib-prop-row"><label>Text</label><input type="text" data-bp="${B}" data-k="text" value="${this._esc(block.text||'')}"></div>
            <div class="wtib-prop-row"><label>Tag</label>
              <select data-bp="${B}" data-k="level">${['h1','h2','h3','h4'].map(t=>`<option${block.level===t?' selected':''}>${t}</option>`).join('')}</select></div>
            <div class="wtib-prop-row"><label>Align</label>${this._alignBtns(B,'align',block.align||'left')}</div>
            <div class="wtib-prop-row row"><label>Color</label><input type="color" data-bp="${B}" data-k="color" value="${block.color||'#111111'}"></div>
            <div class="wtib-prop-row"><label>Font Size (px)</label><input type="number" data-bp="${B}" data-k="fontSize" value="${block.fontSize||26}" min="8" max="96"></div>
          </div>`;
          break;

        case 'button':
          html += `<div class="wtib-prop-group">
            <div class="wtib-prop-row"><label>Label</label><input type="text" data-bp="${B}" data-k="text" value="${this._esc(block.text||'')}"></div>
            <div class="wtib-prop-row"><label>URL</label><input type="url" data-bp="${B}" data-k="url" value="${this._esc(block.url||'#')}"></div>
            <div class="wtib-prop-row"><label>Alignment</label>${this._alignBtns(B,'align',block.align||'center')}</div>
          </div>
          <div class="wtib-prop-group">
            <div class="wtib-prop-row row"><label>Background</label><input type="color" data-bp="${B}" data-k="bgColor" value="${block.bgColor||'#009acc'}"></div>
            <div class="wtib-prop-row row"><label>Text Color</label><input type="color" data-bp="${B}" data-k="textColor" value="${block.textColor||'#ffffff'}"></div>
            <div class="wtib-prop-row"><label>Font Size (px)</label><input type="number" data-bp="${B}" data-k="fontSize" value="${block.fontSize||16}" min="10" max="40"></div>
          </div>
          <div class="wtib-prop-group">
            <div class="wtib-prop-2col">
              <div class="wtib-prop-row"><label>Padding H</label><input type="number" data-bp="${B}" data-k="paddingH" value="${block.paddingH||28}" min="0" max="100"></div>
              <div class="wtib-prop-row"><label>Padding V</label><input type="number" data-bp="${B}" data-k="paddingV" value="${block.paddingV||12}" min="0" max="80"></div>
              <div class="wtib-prop-row"><label>Radius (px)</label><input type="number" data-bp="${B}" data-k="borderRadius" value="${block.borderRadius||4}" min="0" max="60"></div>
            </div>
          </div>`;
          break;

        case 'image':
          html += `<div class="wtib-prop-group">
            <div class="wtib-prop-row"><label>Image URL</label><input type="url" data-bp="${B}" data-k="src" value="${this._esc(block.src||'')}" placeholder="https://..."></div>
            <div class="wtib-prop-row"><label>Alt Text</label><input type="text" data-bp="${B}" data-k="alt" value="${this._esc(block.alt||'')}"></div>
            <div class="wtib-prop-row"><label>Link URL</label><input type="url" data-bp="${B}" data-k="url" value="${this._esc(block.url||'')}" placeholder="Optional link"></div>
          </div>
          <div class="wtib-prop-group">
            <div class="wtib-prop-row"><label>Width (%)</label><input type="number" data-bp="${B}" data-k="width" value="${block.width||100}" min="10" max="100"></div>
            <div class="wtib-prop-row"><label>Align</label>${this._alignBtns(B,'align',block.align||'center')}</div>
          </div>`;
          break;

        case 'divider':
          html += `<div class="wtib-prop-group">
            <div class="wtib-prop-row row"><label>Color</label><input type="color" data-bp="${B}" data-k="color" value="${block.color||'#dddddd'}"></div>
            <div class="wtib-prop-row"><label>Thickness (px)</label><input type="number" data-bp="${B}" data-k="thickness" value="${block.thickness||1}" min="1" max="10"></div>
            <div class="wtib-prop-row"><label>Style</label>
              <select data-bp="${B}" data-k="lineStyle">${['solid','dashed','dotted'].map(s=>`<option${block.lineStyle===s?' selected':''}>${s}</option>`).join('')}</select></div>
            <div class="wtib-prop-row"><label>Vertical Spacing (px)</label><input type="number" data-bp="${B}" data-k="marginV" value="${block.marginV||12}" min="0" max="80"></div>
          </div>`;
          break;

        case 'spacer':
          html += `<div class="wtib-prop-group">
            <div class="wtib-prop-row"><label>Height (px)</label><input type="number" data-bp="${B}" data-k="height" value="${block.height||24}" min="4" max="200"></div>
          </div>`;
          break;

        case 'social':
          html += `<div class="wtib-prop-group">
            <div class="wtib-prop-row"><label>Align</label>${this._alignBtns(B,'align',block.align||'center')}</div>
            <div class="wtib-prop-row"><label>Icon Size (px)</label><input type="number" data-bp="${B}" data-k="size" value="${block.size||32}" min="16" max="64"></div>
          </div>
          <div class="wtib-prop-group">
            <label style="font-family:var(--font-head);font-size:10.5px;font-weight:700;color:var(--ink-3);display:block;margin-bottom:7px;">Network Links</label>
            ${(block.items||[]).map((n,i)=>`
              <div class="wtib-social-item">
                <span class="wtib-social-dot" style="background:${n.color}">${n.name.slice(0,2).toUpperCase()}</span>
                <span class="wtib-social-lbl">${n.name}</span>
                <input type="url" data-soc="${B}" data-si="${i}" value="${this._esc(n.url||'#')}" placeholder="URL">
              </div>`).join('')}
          </div>`;
          break;

        case 'html':
          html += `<div class="wtib-prop-group">
            <div class="wtib-prop-row"><label>HTML Code</label><textarea data-bp="${B}" data-k="html">${this._esc(block.html||'')}</textarea></div>
            <p class="wtib-hint"><i class="fas fa-info-circle"></i> Or double-click the block to edit inline.</p>
          </div>`;
          break;
      }

      this._props.innerHTML = html;
      this._bindProps();
    }

    _alignBtns(blockId, key, current) {
      return `<div class="wtib-align-row">
        ${['left','center','right'].map(a=>`<button class="wtib-align-btn${current===a?' active':''}" data-ab="${blockId}" data-ak="${key}" data-av="${a}"><i class="fas fa-align-${a}"></i></button>`).join('')}
      </div>`;
    }

    _bindProps() {
      /* Block props */
      this._props.querySelectorAll('[data-bp]').forEach(inp => {
        const fn = () => {
          const block = this._findBlock(inp.dataset.bp);
          if (!block) return;
          block[inp.dataset.k] = inp.type === 'number' ? +inp.value : inp.value;
          this._refreshBlock(block.id);
          inp.type === 'color' ? this._snapshot() : this._schedSnap();
        };
        inp.addEventListener('input', fn);
        if (inp.type === 'color') inp.addEventListener('change', fn);
      });

      /* Align buttons */
      this._props.querySelectorAll('[data-ab]').forEach(btn => {
        btn.addEventListener('click', () => {
          const block = this._findBlock(btn.dataset.ab);
          if (!block) return;
          block[btn.dataset.ak] = btn.dataset.av;
          this._refreshBlock(block.id);
          this._snapshot();
          this._props.querySelectorAll(`[data-ab="${btn.dataset.ab}"][data-ak="${btn.dataset.ak}"]`)
            .forEach(b => b.classList.toggle('active', b.dataset.av === btn.dataset.av));
        });
      });

      /* Social URLs */
      this._props.querySelectorAll('[data-soc]').forEach(inp => {
        inp.addEventListener('input', () => {
          const block = this._findBlock(inp.dataset.soc);
          if (block && block.items && block.items[+inp.dataset.si]) {
            block.items[+inp.dataset.si].url = inp.value;
            this._refreshBlock(block.id);
          }
        });
      });

      /* Row props */
      this._props.querySelectorAll('[data-rp]').forEach(inp => {
        const fn = () => {
          const row = this._findRow(inp.dataset.rp);
          if (!row) return;
          row[inp.dataset.k] = inp.type === 'number' ? +inp.value : inp.value;
          /* live update the row DOM */
          const rowEl = this._frame.querySelector(`.wtib-row[data-row-id="${row.id}"]`);
          if (rowEl) rowEl.style.cssText = `background:${row.bgColor||'#fff'};padding:${row.pt||0}px ${row.pr||0}px ${row.pb||0}px ${row.pl||0}px;`;
          inp.type === 'color' ? this._snapshot() : this._schedSnap();
        };
        inp.addEventListener('input', fn);
        if (inp.type === 'color') inp.addEventListener('change', fn);
      });

      /* Column props */
      this._props.querySelectorAll('[data-cp]').forEach(inp => {
        const fn = () => {
          const col = this._findCol(inp.dataset.cp);
          if (!col) return;
          col[inp.dataset.k] = inp.value;
          const colEl = this._frame.querySelector(`[data-col-id="${col.id}"]`);
          if (colEl) colEl.style.background = inp.value;
          this._snapshot();
        };
        inp.addEventListener('input', fn);
        inp.addEventListener('change', fn);
      });
    }

    /* Refresh only the block's inner HTML without full canvas re-render */
    _refreshBlock(blockId) {
      const block = this._findBlock(blockId);
      if (!block) return;
      const el = this._frame.querySelector(`[data-block-id="${blockId}"] .wtib-block-inner`);
      if (el) el.innerHTML = this._blockPreviewHtml(block);
    }

    /* ── Global settings ────────────────────────────── */
    _syncGlobalInputs() {
      const g = this._state.global;
      const query = k => this._left.querySelector(`[data-g="${k}"]`);
      const bg = query('bgColor'); if (bg) bg.value = g.bgColor || '#f4f4f4';
      const w  = query('width');   if (w)  w.value  = g.width   || 600;
      const ff = query('fontFamily');
      if (ff) { Array.from(ff.options).forEach(o => { o.selected = o.value === (g.fontFamily||'Arial, sans-serif'); }); }
    }

    /* ── Row / block action buttons ─────────────────── */
    _rowAction(rowId, action) {
      const rows = this._state.rows;
      const i = rows.findIndex(r => r.id === rowId);
      if (i < 0) return;
      if (action === 'del') {
        rows.splice(i, 1);
        if (this._sel?.id === rowId) this._sel = null;
      } else if (action === 'dup') {
        const clone = JSON.parse(JSON.stringify(rows[i]));
        clone.id = uid(); clone.cols.forEach(c => { c.id = uid(); c.blocks.forEach(b => { b.id = uid(); }); });
        rows.splice(i + 1, 0, clone);
      } else if (action === 'up' && i > 0) {
        [rows[i-1], rows[i]] = [rows[i], rows[i-1]];
      } else if (action === 'dn' && i < rows.length - 1) {
        [rows[i], rows[i+1]] = [rows[i+1], rows[i]];
      }
      this._snapshot(); this._renderCanvas(); this._renderProps();
    }

    _blockAction(blockId, action) {
      const col = this._findBlockCol(blockId);
      if (!col) return;
      const i = col.blocks.findIndex(b => b.id === blockId);
      if (i < 0) return;
      if (action === 'del') {
        col.blocks.splice(i, 1);
        if (this._sel?.id === blockId) this._sel = null;
      } else if (action === 'dup') {
        const clone = JSON.parse(JSON.stringify(col.blocks[i]));
        clone.id = uid();
        col.blocks.splice(i + 1, 0, clone);
        this._sel = { type: 'block', id: clone.id };
      } else if (action === 'up' && i > 0) {
        [col.blocks[i-1], col.blocks[i]] = [col.blocks[i], col.blocks[i-1]];
      } else if (action === 'dn' && i < col.blocks.length - 1) {
        [col.blocks[i], col.blocks[i+1]] = [col.blocks[i+1], col.blocks[i]];
      }
      this._snapshot(); this._renderCanvas(); this._renderProps();
    }

    /* ── Global event binding ───────────────────────── */
    _bindGlobal() {
      /* Left panel — block chips drag */
      this._left.addEventListener('dragstart', e => {
        const chip = e.target.closest('[data-btype]');
        if (chip) { this._drag = { src: 'panel', btype: chip.dataset.btype }; e.dataTransfer.effectAllowed = 'copy'; }
      });

      /* Left panel — layout chips click */
      this._left.addEventListener('click', e => {
        const chip = e.target.closest('[data-layout]');
        if (chip) {
          const row = mkRow(chip.dataset.layout);
          this._state.rows.push(row);
          this._sel = { type: 'row', id: row.id };
          this._snapshot(); this._renderCanvas(); this._renderProps();
        }
      });

      /* Global settings inputs */
      this._left.addEventListener('input', e => {
        const inp = e.target.closest('[data-g]');
        if (!inp) return;
        const k = inp.dataset.g;
        this._state.global[k] = inp.type === 'number' ? +inp.value : inp.value;
        if (k === 'bgColor') this._scroll.style.background = inp.value;
        if (k === 'width') { this._frame.style.width = inp.value + 'px'; this._frame.style.maxWidth = inp.value + 'px'; }
        if (k === 'fontFamily') this._renderCanvas();
        this._schedSnap();
      });

      /* Canvas — click away deselects */
      this._scroll.addEventListener('click', e => {
        if (e.target === this._scroll || e.target === this._frame) {
          this._sel = null; this._renderCanvas(); this._renderProps();
        }
      });

      /* Canvas — delegated row/block action buttons */
      this._frame.addEventListener('click', e => {
        const ra = e.target.closest('[data-ra]');
        if (ra) { e.stopPropagation(); this._rowAction(ra.dataset.rid, ra.dataset.ra); return; }
        const ba = e.target.closest('[data-ba]');
        if (ba) { e.stopPropagation(); this._blockAction(ba.dataset.bid, ba.dataset.ba); return; }
      });

      /* Toolbar — undo / redo / preview / full-page */
      this._toolbar.addEventListener('click', e => {
        const u = e.target.closest('#wtibUndo'); if (u) { this.undo(); return; }
        const r = e.target.closest('#wtibRedo'); if (r) { this.redo(); return; }

        const fp = e.target.closest('#wtibFullPage');
        if (fp) { this._toggleFullPage(fp); return; }

        const p = e.target.closest('[data-preview]');
        if (p) {
          const w = p.dataset.preview === 'mobile' ? 375 : (this._state.global.width || 600);
          this._setPreviewWidth(w);
        }
      });

      /* Resize handle drag */
      let _dragging = false, _dragX0 = 0, _dragW0 = 0;
      this._resizeR.addEventListener('mousedown', e => {
        _dragging = true;
        _dragX0   = e.clientX;
        _dragW0   = this._frameWrap.offsetWidth;
        document.body.style.cursor     = 'ew-resize';
        document.body.style.userSelect = 'none';
        e.preventDefault();
      });
      document.addEventListener('mousemove', e => {
        if (!_dragging) return;
        this._setPreviewWidth(_dragW0 + (e.clientX - _dragX0));
      });
      document.addEventListener('mouseup', () => {
        if (!_dragging) return;
        _dragging = false;
        document.body.style.cursor     = '';
        document.body.style.userSelect = '';
      });

      /* Keyboard shortcuts */
      document.addEventListener('keydown', e => {
        if (e.key === 'F11') { e.preventDefault(); const btn = document.getElementById('wtibFullPage'); if (btn) this._toggleFullPage(btn); return; }
        if (e.ctrlKey && e.key === 'z') { e.preventDefault(); this.undo(); return; }
        if (e.ctrlKey && e.key === 'y') { e.preventDefault(); this.redo(); return; }
        if ((e.key === 'Delete' || e.key === 'Backspace') && this._sel) {
          const active = document.activeElement;
          if (active && (active.isContentEditable || ['INPUT','TEXTAREA','SELECT'].includes(active.tagName))) return;
          if (this._sel.type === 'block') this._blockAction(this._sel.id, 'del');
          else if (this._sel.type === 'row')  this._rowAction(this._sel.id, 'del');
        }
      });
    }

    /* ── History (undo/redo) ────────────────────────── */
    _snapshot() {
      clearTimeout(this._hTimer);
      this._undo.push(JSON.stringify(this._state));
      if (this._undo.length > 60) this._undo.shift();
      this._redo = [];
      this._syncHistoryBtns();
    }
    _schedSnap() {
      clearTimeout(this._hTimer);
      this._hTimer = setTimeout(() => this._snapshot(), 700);
    }

    undo() {
      if (this._undo.length < 2) return;
      this._redo.push(this._undo.pop());
      this._state = JSON.parse(this._undo[this._undo.length - 1]);
      this._renderCanvas(); this._renderProps(); this._syncHistoryBtns();
    }
    redo() {
      if (!this._redo.length) return;
      const s = this._redo.pop();
      this._undo.push(s);
      this._state = JSON.parse(s);
      this._renderCanvas(); this._renderProps(); this._syncHistoryBtns();
    }
    _syncHistoryBtns() {
      const u = document.getElementById('wtibUndo'); if (u) u.disabled = this._undo.length < 2;
      const r = document.getElementById('wtibRedo'); if (r) r.disabled = !this._redo.length;
    }

    /* ── Finders ────────────────────────────────────── */
    _findRow(id)   { return this._state.rows.find(r => r.id === id); }
    _findCol(id)   { for (const r of this._state.rows) { const c = r.cols.find(c => c.id === id); if (c) return c; } return null; }
    _findBlock(id) { for (const r of this._state.rows) for (const c of r.cols) { const b = c.blocks.find(b => b.id === id); if (b) return b; } return null; }
    _findBlockCol(blockId) { for (const r of this._state.rows) for (const c of r.cols) { if (c.blocks.some(b => b.id === blockId)) return c; } return null; }

    /* ── HTML escape ────────────────────────────────── */
    _esc(s) { return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

    /* ── Full-page toggle ───────────────────────────── */
    _toggleFullPage(btn) {
      const params = new URLSearchParams(window.location.search);
      if (params.get('fp') === '1') {
        /* Separate-tab mode (opened from Campaign) — close the tab */
        window.close();
        return;
      }
      /* In-page toggle — preserves all elements, no new tab */
      const active = document.body.classList.toggle('wtib-fp');
      btn.classList.toggle('fp-active', active);
      btn.innerHTML = active
        ? '<i class="fas fa-compress"></i> Exit Full Page'
        : '<i class="fas fa-expand"></i> Full Page';
      btn.title = active ? 'Exit full-page mode (F11)' : 'Toggle full-page mode (F11)';
    }

    /* Called externally when ?fp=1 is detected in the URL */
    activateFullPage() {
      document.body.classList.add('wtib-fp');
      const btn = document.getElementById('wtibFullPage');
      if (!btn) return;
      btn.classList.add('fp-active');
      btn.innerHTML = '<i class="fas fa-xmark"></i> Close Tab';
      btn.title = 'Close this full-page tab';
    }

    /* ── Email HTML export ──────────────────────────── */
    _buildEmailHtml() {
      const g  = this._state.global;
      const w  = g.width  || 600;
      const ff = g.fontFamily || 'Arial, sans-serif';
      const bg = g.bgColor   || '#f4f4f4';

      const rowsHtml = this._state.rows.map(row => {
        const rBg = row.bgColor || '#ffffff';
        const cols = row.cols.map(col => {
          const cw = Math.round(col.frac * w);
          const cBg = col.bg ? `background:${col.bg};` : '';
          const blocks = col.blocks.map(b => this._blockEmail(b, ff)).join('');
          return `<td class="email-col" width="${cw}" valign="top" style="width:${cw}px;${cBg}vertical-align:top;">${blocks}</td>`;
        }).join('');
        return `
<table role="presentation" width="${w}" cellpadding="0" cellspacing="0" border="0" align="center" class="email-row" style="width:${w}px;max-width:${w}px;background:${rBg};">
  <tr><td style="padding:${row.pt||0}px ${row.pr||0}px ${row.pb||0}px ${row.pl||0}px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>${cols}</tr></table>
  </td></tr>
</table>`;
      }).join('\n');

      return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <title>Email</title>
  <style>
    body,html{margin:0;padding:0;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;}
    img{border:0;outline:none;text-decoration:none;display:block;}
    table{border-collapse:collapse;}
    a{color:inherit;}
    @media only screen and (max-width:600px){
      .email-row{width:100%!important;}
      .email-col{width:100%!important;display:block!important;}
    }
  </style>
</head>
<body style="margin:0;padding:0;background-color:${bg};font-family:${ff};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:${bg};">
  <tr><td align="center" style="padding:24px 0;">
    ${rowsHtml}
  </td></tr>
</table>
</body>
</html>`;
    }

    _blockEmail(block, ff) {
      switch (block.type) {
        case 'text':
          return `<div style="font-family:${ff};">${block.html||''}</div>`;

        case 'heading': {
          const tag = block.level || 'h2';
          return `<${tag} style="margin:0;padding:4px 0;font-family:${ff};font-size:${block.fontSize||26}px;color:${block.color||'#111'};font-weight:${block.fontWeight||'bold'};text-align:${block.align||'left'};">${block.text||''}</${tag}>`;
        }

        case 'button': {
          const align = block.align || 'center';
          const bg    = block.bgColor    || '#009acc';
          const tc    = block.textColor  || '#fff';
          const fs    = block.fontSize   || 16;
          const pv    = block.paddingV   || 12;
          const ph    = block.paddingH   || 28;
          const br    = block.borderRadius || 4;
          const fw    = block.bold ? 'bold' : 'normal';
          const btnStyle = `display:inline-block;font-family:${ff};background-color:${bg};color:${tc};font-size:${fs}px;font-weight:${fw};padding:${pv}px ${ph}px;border-radius:${br}px;text-decoration:none;`;
          return `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td align="${align}" style="padding:8px 0;">
    <a href="${block.url||'#'}" style="${btnStyle}" target="_blank">${block.text||'Button'}</a>
  </td></tr>
</table>`;
        }

        case 'image': {
          if (!block.src) return '';
          const align = block.align || 'center';
          const img = `<img src="${block.src}" alt="${this._esc(block.alt||'')}" width="${block.width||100}%" style="display:block;max-width:100%;width:${block.width||100}%;height:auto;border:0;">`;
          return `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td align="${align}" style="padding:4px 0;">${block.url ? `<a href="${block.url}" style="border:0;text-decoration:none;">${img}</a>` : img}</td></tr>
</table>`;
        }

        case 'divider':
          return `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td style="padding:${block.marginV||12}px 0;font-size:0;line-height:0;border-top:${block.thickness||1}px ${block.lineStyle||'solid'} ${block.color||'#ddd'};"></td></tr>
</table>`;

        case 'spacer':
          return `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td height="${block.height||24}" style="height:${block.height||24}px;font-size:0;line-height:0;">&nbsp;</td></tr>
</table>`;

        case 'social': {
          const sz = block.size || 32;
          const cells = (block.items||[]).map(n =>
            `<td style="padding:0 4px;">
  <a href="${n.url||'#'}" target="_blank" title="${n.name}" style="text-decoration:none;border:0;">
    <div style="width:${sz}px;height:${sz}px;background:${n.color||'#ccc'};border-radius:50%;text-align:center;line-height:${sz}px;color:#fff;font-weight:bold;font-size:${Math.floor(sz*.38)}px;font-family:${ff};">${n.name.slice(0,2).toUpperCase()}</div>
  </a>
</td>`).join('');
          return `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td align="${block.align||'center'}" style="padding:8px 0;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>${cells}</tr></table>
  </td></tr>
</table>`;
        }

        case 'html':
          return block.html || '';

        default: return '';
      }
    }
  }

  /* ── Export ─────────────────────────────────────── */
  W.WTIBuilder = WTIBuilder;

})(window);
