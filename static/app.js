(() => {
  const guidedForm = document.querySelector('#guided-item-form');
  const familySelect = document.querySelector('#item-family');
  const itemName = document.querySelector('#item-name');
  if (guidedForm && familySelect && itemName) {
    let familyWasChosen = false;
    const showFamily = (family, chooseHome = true) => {
      document.querySelectorAll('.guided-fields').forEach(section => {
        const active = section.dataset.family === family;
        section.hidden = !active;
        section.querySelectorAll('input,select').forEach(field => field.disabled = !active);
      });
      if (chooseHome) {
        const location = document.querySelector('#location-select');
        const code = familySelect.selectedOptions[0]?.dataset.home;
        if (location && code && !location.value) {
          const option = Array.from(location.options).find(candidate => candidate.dataset.code === code);
          if (option) location.value = option.value;
        }
      }
    };
    showFamily(familySelect.value, false);
    familySelect.addEventListener('change', () => { familyWasChosen = true; showFamily(familySelect.value); });

    const broadNames = new Set(['capacitor', 'resistor', 'led', 'connector', 'sensor', 'module', 'microcontroller', 'esp32']);
    let generatedName = broadNames.has(itemName.value.trim().toLowerCase());
    itemName.addEventListener('input', event => {
      if (event.isTrusted) generatedName = false;
    });
    const valueFor = key => guidedForm.querySelector(`.guided-fields:not([hidden]) [data-attribute="${key}"]`)?.value.trim() || '';
    const refreshName = () => {
      if (!generatedName) return;
      const family = familySelect.value;
      const names = {
        capacitor: [valueFor('value'), valueFor('value_unit'), valueFor('capacitor_type'), 'capacitor'],
        resistor: [valueFor('value'), valueFor('value_unit'), valueFor('tolerance'), valueFor('power'), 'resistor'],
        microcontroller: [guidedForm.querySelector('#item-manufacturer')?.value.trim() || '', valueFor('variant'), valueFor('chip')],
        led: [valueFor('colour'), valueFor('package'), valueFor('led_type'), 'LED'],
        connector: [valueFor('series'), valueFor('positions') && `${valueFor('positions')}-pin`, valueFor('gender'), 'connector'],
        sensor: [valueFor('model'), valueFor('measures'), 'sensor'],
        module: [valueFor('model'), valueFor('function'), 'module']
      };
      const nextName = (names[family] || []).filter(Boolean).join(' ');
      if (nextName) itemName.value = nextName;
    };
    guidedForm.querySelector('#item-manufacturer')?.addEventListener('input', refreshName);
    guidedForm.querySelectorAll('[data-attribute]').forEach(field => field.addEventListener('input', refreshName));
    familySelect.addEventListener('change', () => {
      generatedName = broadNames.has(itemName.value.trim().toLowerCase()) || generatedName;
      refreshName();
    });
    let guideTimer;
    itemName.addEventListener('input', () => {
      clearTimeout(guideTimer);
      if (familyWasChosen || itemName.value.trim().length < 3) return;
      guideTimer = setTimeout(async () => {
        const guide = await (await fetch('/api/item-guide?q=' + encodeURIComponent(itemName.value))).json();
        familySelect.value = guide.family;
        showFamily(guide.family);
      }, 180);
    });
  }
  const locationFilter = document.querySelector('#location-filter');
  const locationSelect = document.querySelector('#location-select');
  if (locationFilter && locationSelect) {
    const options = Array.from(locationSelect.options, option => option.cloneNode(true));
    locationFilter.addEventListener('input', () => {
      const selected = locationSelect.value;
      const query = locationFilter.value.trim().toLowerCase();
      locationSelect.replaceChildren(...options.filter(option => !option.value || option.textContent.toLowerCase().includes(query)).map(option => option.cloneNode(true)));
      locationSelect.value = Array.from(locationSelect.options).some(option => option.value === selected) ? selected : '';
    });
  }
  const search = document.querySelector('#inventory-search');
  const suggestions = document.querySelector('#search-suggestions');
  let searchTimer;

  if (search && suggestions) {
    search.addEventListener('input', () => {
      clearTimeout(searchTimer);
      const query = search.value.trim();
      if (query.length < 2) { suggestions.hidden = true; return; }
      searchTimer = setTimeout(async () => {
        const response = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
        const results = await response.json();
        suggestions.replaceChildren(...results.map(result => {
          const link = document.createElement('a');
          link.href = `/items/${result.id}`;
          link.innerHTML = `<strong>${escapeHtml(result.name)}</strong><span>${escapeHtml(result.code)} · ${escapeHtml(result.label)}</span>`;
          return link;
        }));
        suggestions.hidden = results.length === 0;
      }, 120);
    });
    document.addEventListener('click', event => { if (!event.target.closest('.search-field')) suggestions.hidden = true; });
  }

  function formatQuantity(value) {
    const number = Number(value);
    return Number.isFinite(number) ? String(Number(number.toFixed(3))) : String(value);
  }

  function escapeHtml(value) {
    const node = document.createElement('span'); node.textContent = value || ''; return node.innerHTML;
  }

  if (document.body.dataset.live === 'true') {
    let currentVersion;
    const checkForUpdates = async () => {
      if (document.hidden) return;
      try {
        const {version} = await (await fetch('/api/version', {cache: 'no-store'})).json();
        if (currentVersion && currentVersion !== version) {
          if (document.activeElement.matches('input, textarea, select') || [...document.querySelectorAll('input[type=file]')].some((input) => input.files.length) || document.querySelector('dialog[open]')) return;
          window.location.reload();
        }
        currentVersion = version;
      } catch (_) { /* The page remains usable while the server reconnects. */ }
    };
    checkForUpdates();
    setInterval(checkForUpdates, 5000);
  }

  // Build planner: the need and shortfall of each line follow the number of builds as it is typed.
  const planner = document.querySelector('[data-build-planner]');
  if (planner) {
    const wanted = planner.querySelector('[name="quantity"]');
    const build = planner.querySelector('button.primary');
    const update = () => {
      const typed = Number(wanted.value);
      const count = Number.isInteger(typed) && typed >= 1 && typed <= Number(wanted.max) ? typed : 1;
      const lines = document.querySelectorAll('[data-line]');
      let short = false;
      document.querySelectorAll('[data-build-count]').forEach(node => node.textContent = `×${count}`);
      lines.forEach(row => {
        const need = Number(row.dataset.perBuild) * count;
        const missing = need - Number(row.dataset.onHand);
        if (missing > 1e-9) short = true;
        row.querySelector('[data-need]').textContent = `${formatQuantity(need)} ${row.dataset.unit}`;
        row.querySelector('[data-status]').innerHTML = missing > 1e-9 ? `<span class="badge danger">Short ${formatQuantity(missing)}</span>` : '<span class="badge neutral">Covered</span>';
      });
      build.disabled = short || !lines.length;
      planner.querySelectorAll('[data-order-shortages]').forEach(button => button.disabled = !short);
    };
    wanted.addEventListener('input', update);
    // Browsers restore a typed count without an input event on reload or history navigation.
    window.addEventListener('pageshow', update);
    update();
  }

  // Copy button for the Mouser text: shown only where the clipboard API exists (not on plain HTTP).
  document.querySelectorAll('[data-copy]').forEach(button => {
    if (!(window.isSecureContext && navigator.clipboard)) return;
    button.hidden = false;
    button.addEventListener('click', () => navigator.clipboard.writeText(document.querySelector(button.dataset.copy).value));
  });

  const menuToggle = document.querySelector('[data-menu-toggle]');
  const sheet = document.querySelector('[data-menu-sheet]');
  if (menuToggle && sheet) {
    const setOpen = open => {
      sheet.hidden = !open;
      menuToggle.setAttribute('aria-expanded', String(open));
      if (open) sheet.querySelector('a')?.focus();
    };
    menuToggle.addEventListener('click', () => setOpen(sheet.hidden));
    sheet.querySelector('[data-menu-close]')?.addEventListener('click', () => { setOpen(false); menuToggle.focus(); });
    sheet.addEventListener('click', event => { if (event.target === sheet) setOpen(false); });
    document.addEventListener('keydown', event => { if (event.key === 'Escape' && !sheet.hidden) { setOpen(false); menuToggle.focus(); } });
  }

  // Theme: an explicit light/dark choice is stored; with no choice the system preference applies.
  const root = document.documentElement;
  const systemDark = window.matchMedia('(prefers-color-scheme: dark)');
  const currentTheme = () => root.dataset.theme || (systemDark.matches ? 'dark' : 'light');
  const applyTheme = choice => {
    if (choice === 'light' || choice === 'dark') { root.dataset.theme = choice; localStorage.setItem('tally-theme', choice); }
    else { delete root.dataset.theme; localStorage.removeItem('tally-theme'); }
    document.querySelectorAll('[data-theme-label]').forEach(label => label.textContent = currentTheme() === 'dark' ? 'Dark theme' : 'Light theme');
    document.querySelectorAll('[data-theme-choice]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.themeChoice === (localStorage.getItem('tally-theme') || 'system'))));
  };
  applyTheme(localStorage.getItem('tally-theme'));
  document.querySelectorAll('[data-theme-toggle]').forEach(button => button.addEventListener('click', () => applyTheme(currentTheme() === 'dark' ? 'light' : 'dark')));
  document.querySelectorAll('[data-theme-choice]').forEach(button => button.addEventListener('click', () => applyTheme(button.dataset.themeChoice)));
  systemDark.addEventListener('change', () => applyTheme(localStorage.getItem('tally-theme')));

  const imageDialog = document.querySelector('#component-image-dialog');
  if (imageDialog) {
    const image = imageDialog.querySelector('img');
    const caption = imageDialog.querySelector('p');
    document.querySelectorAll('[data-image-open]').forEach(button => button.addEventListener('click', () => {
      if (!button.dataset.imageSrc) return;
      image.src = button.dataset.imageSrc; image.alt = button.dataset.imageLabel; caption.textContent = button.dataset.imageLabel;
      imageDialog.showModal();
    }));
    imageDialog.querySelector('[data-image-close]').addEventListener('click', () => imageDialog.close());
  }

  const checkout = document.querySelector('[data-stock-checkout]');
  if (checkout) {
    const lines = checkout.querySelector('[data-cart-lines]');
    const input = checkout.querySelector('[name="cart"]');
    const count = checkout.querySelector('[data-cart-count]');
    const fab = document.querySelector('[data-checkout-fab]');
    if (fab) fab.addEventListener('click', () => checkout.scrollIntoView({behavior: 'smooth', block: 'start'}));
    let cart = [];
    const renderCart = () => {
      input.value = JSON.stringify(cart.map(({id, quantity}) => ({id, quantity})));
      count.textContent = `${cart.length} item${cart.length === 1 ? '' : 's'}`;
      if (fab) { fab.hidden = !cart.length; fab.querySelector('[data-cart-count]').textContent = count.textContent; }
      if (!cart.length) { lines.innerHTML = '<p class="empty">Add components from the list.</p>'; return; }
      lines.replaceChildren(...cart.map(line => {
        const row = document.createElement('div'); row.className = 'checkout-line';
        row.innerHTML = `<span><strong>${escapeHtml(line.name)}</strong><small>${formatQuantity(line.available)} ${escapeHtml(line.unit)} available</small></span><input aria-label="Quantity for ${escapeHtml(line.name)}" type="number" min="0.01" step="any" value="${line.quantity}"><button type="button" class="button quiet" aria-label="Remove ${escapeHtml(line.name)}"><svg class="icon small" aria-hidden="true"><use href="#i-close"/></svg></button>`;
        row.querySelector('input').addEventListener('input', event => { line.quantity = Number(event.target.value); renderCart(); });
        row.querySelector('button').addEventListener('click', () => { cart = cart.filter(candidate => candidate.id !== line.id); renderCart(); });
        return row;
      }));
    };
    document.querySelectorAll('[data-cart-add]').forEach(button => button.addEventListener('click', () => {
      const id = Number(button.dataset.id); const existing = cart.find(line => line.id === id);
      if (existing) existing.quantity += 1;
      else cart.push({id, name: button.dataset.name, unit: button.dataset.unit, available: Number(button.dataset.available), quantity: 1});
      renderCart();
    }));
    const selectedId = Number(checkout.dataset.selectedId);
    if (selectedId) document.querySelector(`[data-cart-add][data-id="${selectedId}"]`)?.click();
    checkout.querySelector('form').addEventListener('submit', event => {
      if (!cart.length || cart.some(line => !Number.isFinite(line.quantity) || line.quantity <= 0)) { event.preventDefault(); alert('Add components and enter a quantity greater than zero.'); }
    });
    renderCart();
  }

  if (document.body.hasAttribute('data-stocktake')) {
    document.querySelectorAll('tr[data-expected]').forEach(row => {
      const field = row.querySelector('.count-input'); const tick = row.querySelector('input[type="checkbox"]'); const expected = Number(row.dataset.expected);
      const show = () => {
        const out = row.querySelector('[data-variance]'); const diff = Number(field.value) - expected;
        if (field.value === '' || !Number.isFinite(diff) || Math.abs(diff) < 1e-9) { out.innerHTML = ''; return; }
        out.innerHTML = `<span class="badge ${diff > 0 ? 'neutral' : 'warn'}">${diff > 0 ? '+' : '-'}${formatQuantity(Math.abs(diff))}</span>`;
      };
      tick.addEventListener('change', () => { field.readOnly = tick.checked; if (tick.checked) field.value = row.dataset.expected; show(); });
      field.addEventListener('input', show);
    });
    document.querySelector('form[method="post"]')?.addEventListener('submit', event => {
      const bad = [...document.querySelectorAll('.count-input')].filter(field => field.validity.badInput);
      bad.forEach(field => field.setAttribute('aria-invalid', 'true'));
      if (bad.length) { event.preventDefault(); bad[0].focus(); }
    });
  }

  document.querySelectorAll('form[data-max-bytes]').forEach(form => form.addEventListener('submit', event => {
    const file = form.querySelector('input[type="file"]').files[0];
    const limit = Number(form.dataset.maxBytes);
    if (!file || file.size <= limit) return;
    event.preventDefault();
    let note = form.querySelector('[data-size-error]');
    if (!note) { note = document.createElement('small'); note.className = 'danger-text'; note.dataset.sizeError = ''; note.setAttribute('role', 'alert'); form.querySelector('input[type="file"]').after(note); }
    note.textContent = `Files can be up to ${Math.round(limit / 1048576)} MB.`;
  }));
})();
