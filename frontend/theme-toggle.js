const THEME_ATTRIBUTE = 'data-theme';
const THEME_STORAGE_KEY = 'theme-preference';
const themeToggleButton = document.querySelector('#theme_toggle');

function applyThemePreference(themePreference) {
  if (themePreference === 'light' || themePreference === 'dark') {
    document.body.setAttribute(THEME_ATTRIBUTE, themePreference);
  } else {
    document.body.removeAttribute(THEME_ATTRIBUTE);
  }
}

function updateThemeToggleLabel(themePreference) {
  if (!themeToggleButton) {
    return;
  }

  const displayMap = {
    auto: 'Auto',
    light: 'Light',
    dark: 'Dark'
  };

  themeToggleButton.textContent = `Theme: ${displayMap[themePreference]}`;
}

function getSavedThemePreference() {
  const savedPreference = localStorage.getItem(THEME_STORAGE_KEY);
  return savedPreference === 'light' || savedPreference === 'dark' ? savedPreference : 'auto';
}

function setThemePreference(themePreference) {
  if (themePreference === 'auto') {
    localStorage.removeItem(THEME_STORAGE_KEY);
  } else {
    localStorage.setItem(THEME_STORAGE_KEY, themePreference);
  }

  applyThemePreference(themePreference);
  updateThemeToggleLabel(themePreference);
}

const initialThemePreference = getSavedThemePreference();
setThemePreference(initialThemePreference);

themeToggleButton?.addEventListener('click', () => {
  const currentThemePreference = getSavedThemePreference();
  const nextThemePreference = currentThemePreference === 'auto'
    ? 'light'
    : currentThemePreference === 'light'
      ? 'dark'
      : 'auto';

  setThemePreference(nextThemePreference);
});
