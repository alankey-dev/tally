// House shell behaviour, lifted from Tally: light/dark theme toggle and the mobile menu sheet.
// Pair with the inline <head> script in index.html so the saved theme applies before first paint.
(() => {
  const THEME_KEY = 'site-theme';
  const root = document.documentElement;
  const systemDark = matchMedia('(prefers-color-scheme: dark)');
  const stored = () => { try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; } };
  const store = value => { try { value ? localStorage.setItem(THEME_KEY, value) : localStorage.removeItem(THEME_KEY); } catch (e) {} };
  const currentTheme = () => root.dataset.theme || (systemDark.matches ? 'dark' : 'light');

  const applyTheme = choice => {
    if (choice === 'light' || choice === 'dark') { root.dataset.theme = choice; store(choice); }
    else { delete root.dataset.theme; store(null); }
    document.querySelectorAll('[data-theme-label]').forEach(label => label.textContent = currentTheme() === 'dark' ? 'Dark theme' : 'Light theme');
    document.querySelectorAll('[data-theme-choice]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.themeChoice === (stored() || 'system'))));
  };
  applyTheme(stored());
  document.querySelectorAll('[data-theme-toggle]').forEach(button => button.addEventListener('click', () => applyTheme(currentTheme() === 'dark' ? 'light' : 'dark')));
  document.querySelectorAll('[data-theme-choice]').forEach(button => button.addEventListener('click', () => applyTheme(button.dataset.themeChoice)));
  systemDark.addEventListener('change', () => applyTheme(stored()));

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
})();
