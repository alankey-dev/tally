(() => {
  const input = document.querySelector('#quick-query');
  const results = document.querySelector('#quick-results');
  const status = document.querySelector('#quick-status');
  if (!input || !results) return;
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
    if (query.length < 2) return;
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
