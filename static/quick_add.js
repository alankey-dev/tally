(() => {
  const input = document.querySelector('#quick-query');
  const results = document.querySelector('#quick-results');
  const status = document.querySelector('#quick-status');
  const quantity = document.querySelector('.quantity-input');
  const photo = document.querySelector('#scan-photo');
  const labelStart = /^(\[\)>|\{pbn:|\{pc:|QTY:)/;
  const insertAtCursor = (text) => input.setRangeText(text, input.selectionStart, input.selectionEnd, 'end');
  if (input) {
    // Wedge scanners send GS, RS and EOT as Ctrl key presses. Keep them out of the browser's shortcuts.
    input.addEventListener('keydown', (event) => {
      if (!event.ctrlKey || event.altKey || event.metaKey) return;
      if (event.code === 'BracketRight') insertAtCursor('\x1d');
      else if (event.code === 'Digit6') insertAtCursor('\x1e');
      else if (event.code !== 'KeyD') return;
      event.preventDefault();
    });
    // A bag scanned into the quantity box goes to the search box instead.
    let lastKey = 0;
    let run = 0;
    quantity?.addEventListener('keydown', (event) => {
      if (event.ctrlKey || event.altKey || event.metaKey || event.key.length !== 1) return;
      run = event.timeStamp - lastKey < 30 ? run + 1 : 1;
      lastKey = event.timeStamp;
      if (!(run === 1 && !quantity.value && '[{'.includes(event.key)) && run <= 4) return;
      event.preventDefault();
      input.value = quantity.value + event.key;
      quantity.value = '';
      input.focus();
    });
  }
  if (photo && input) {
    const load = (src) => new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = src;
      script.onload = resolve;
      script.onerror = reject;
      document.head.append(script);
    });
    const decode = async (canvas) => {
      const formats = window.BarcodeDetector ? await BarcodeDetector.getSupportedFormats() : [];
      if (formats.includes('data_matrix') && formats.includes('qr_code')) {
        const codes = await new BarcodeDetector({formats: ['data_matrix', 'qr_code']}).detect(canvas);
        return codes[0]?.rawValue || '';
      }
      try {
        if (!window.ZXingWASM) await load(photo.dataset.loader);
        ZXingWASM.setZXingModuleOverrides({locateFile: (path, prefix) => path.endsWith('.wasm') ? photo.dataset.wasm : prefix + path});
      } catch (error) {
        throw new Error('loader');
      }
      const image = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height);
      const found = await ZXingWASM.readBarcodes(image, {formats: ['DataMatrix', 'QRCode'], tryRotate: true});
      return found.find((code) => code.isValid)?.text || '';
    };
    photo.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      photo.click();
    });
    photo.addEventListener('change', async () => {
      const file = photo.files[0];
      if (!file) return;
      status.textContent = 'Reading the label…';
      try {
        const bitmap = await createImageBitmap(file);
        const scale = Math.min(1, 2000 / Math.max(bitmap.width, bitmap.height));
        const canvas = document.createElement('canvas');
        canvas.width = Math.round(bitmap.width * scale);
        canvas.height = Math.round(bitmap.height * scale);
        canvas.getContext('2d', {willReadFrequently: true}).drawImage(bitmap, 0, 0, canvas.width, canvas.height);
        const text = await decode(canvas);
        if (!text) {
          status.textContent = 'No code found. Try closer, in better light.';
          return;
        }
        input.value = text;
        status.textContent = '';
        input.form.requestSubmit();
      } catch (error) {
        status.textContent = error.message === 'loader' ? 'Could not load the scanner. Type the part number instead.' : 'No code found. Try closer, in better light.';
      } finally {
        photo.value = '';
      }
    });
  }
  if (!input || !results) return;
  results.addEventListener('click', async (event) => {
    const link = event.target.closest('[data-provider-search]');
    if (!link) return;
    event.preventDefault();
    status.textContent = 'Searching providers…';
    try {
      const response = await fetch(link.href + '&fragment=1', {cache: 'no-store'});
      if (!response.ok) throw new Error('Search failed');
      results.innerHTML = await response.text();
      status.textContent = '';
    } catch (error) {
      status.textContent = 'Could not search providers. Try again.';
    }
  });
  let timer;
  let controller;
  let revision = 0;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    controller?.abort();
    const current = ++revision;
    const query = input.value.trim();
    results.replaceChildren();
    status.textContent = '';
    if (query.length < 2 || labelStart.test(query)) return;
    timer = setTimeout(async () => {
      controller = new AbortController();
      status.textContent = 'Finding matches…';
      try {
        const response = await fetch('/quick-add?fragment=1&q=' + encodeURIComponent(query), {signal: controller.signal, cache: 'no-store'});
        if (!response.ok) throw new Error('Search failed');
        const html = await response.text();
        if (current !== revision) return;
        results.innerHTML = html;
        status.textContent = '';
      } catch (error) {
        if (current === revision && error.name !== 'AbortError') status.textContent = 'Could not search. Tap Find to retry.';
      }
    }, 160);
  });
})();
