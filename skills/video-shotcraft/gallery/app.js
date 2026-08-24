const translations = window.GALLERY_I18N;
const staticGallery = true;
const libraryEndpoint = staticGallery ? './api/library.json' : '/api/library';
const savedLanguage = (() => {
  try {
    return localStorage.getItem('video-shot-gallery-language');
  } catch {
    return null;
  }
})();
const savedTheme = (() => {
  try {
    const value = localStorage.getItem('video-shot-gallery-theme');
    return ['system', 'light', 'dark'].includes(value) ? value : 'system';
  } catch {
    return 'system';
  }
})();

const state = {
  library: null,
  query: '',
  filter: 'all',
  revision: '',
  hasLoaded: false,
  language: savedLanguage === 'zh' ? 'zh' : 'en',
  theme: savedTheme,
  selectedStyles: {},
  selectedCards: new Set(),
};

const elements = {
  cardCount: document.querySelector('#cardCount'),
  clearFilters: document.querySelector('#clearFilters'),
  emptyState: document.querySelector('#emptyState'),
  filters: document.querySelector('#filters'),
  languageSwitch: document.querySelector('#languageSwitch'),
  library: document.querySelector('#library'),
  pageTitle: document.querySelector('#pageTitle'),
  previewCount: document.querySelector('#previewCount'),
  searchInput: document.querySelector('#searchInput'),
  styleCount: document.querySelector('#styleCount'),
  syncStatus: document.querySelector('#syncStatus'),
  themeSwitch: document.querySelector('#themeSwitch'),
  toast: document.querySelector('#toast'),
  selectionBar: document.querySelector('#selectionBar'),
  selectionCount: document.querySelector('#selectionCount'),
  copySelected: document.querySelector('#copySelected'),
  clearSelected: document.querySelector('#clearSelected'),
};

const escapeHtml = (value = '') => String(value).replace(/[&<>'"]/g, (character) => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  "'": '&#039;',
  '"': '&quot;',
}[character]));

const text = (key) => translations.ui[state.language][key] || key;
const cardName = (card) => state.language === 'zh'
  ? translations.cardsZh[card.name] || card.name
  : card.name;
const styleName = (style) => state.language === 'zh'
  ? translations.stylesZh[style.key] || style.label
  : style.key;
const cardDescription = (card) => state.language === 'zh'
  ? card.summary || card.intention
  : translations.cardsEn[card.name] || card.name.split('-').join(' ');

function durationText(value = '') {
  if (state.language === 'zh' || !value) return value || '-';
  if (/^n\/a/i.test(value)) return 'N/A';
  const normalized = value.replace(/[—–]/g, '-');
  const seconds = normalized.match(/~?\s*\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?s/i)?.[0]?.replace(/\s/g, '');
  const frames = normalized.match(/[≥~]?\s*\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?f/i)?.[0]?.replace(/\s/g, '');
  if (seconds && frames) return `${seconds.startsWith('~') ? '' : '~'}${seconds} (${frames})`;
  return seconds || frames || 'Technique';
}

function energyText(value = '') {
  if (state.language === 'zh' || !value) return value || '-';
  if (/^n\/a/i.test(value)) return 'N/A';
  if (value.includes('峰值')) return 'Peak';
  if (value.includes('低开缓升')) return 'Low to medium';
  if (value.includes('高开中收')) return 'High to medium';
  if (value.includes('中高')) return 'Medium-high';
  if (value.includes('低中') || value.includes('中低')) return 'Low-medium';
  if (value.includes('高')) return 'High';
  if (value.includes('中')) return 'Medium';
  if (value.includes('低')) return 'Low';
  return 'Variable';
}

function resolveTheme(choice = state.theme) {
  if (choice !== 'system') return choice;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function applyTheme() {
  const resolved = resolveTheme();
  document.documentElement.dataset.theme = resolved;
  document.documentElement.dataset.themeChoice = state.theme;
  document.documentElement.style.colorScheme = resolved;
  elements.themeSwitch.setAttribute('aria-label', text('themeLabel'));
  elements.themeSwitch.querySelectorAll('[data-theme]').forEach((button) => {
    const active = button.dataset.theme === state.theme;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
    button.textContent = text(`theme${button.dataset.theme[0].toUpperCase()}${button.dataset.theme.slice(1)}`);
  });
}

function applyLanguage() {
  document.documentElement.lang = state.language === 'zh' ? 'zh-CN' : 'en';
  document.title = text('documentTitle');
  elements.pageTitle.textContent = text('title');
  document.querySelectorAll('[data-i18n]').forEach((node) => {
    node.textContent = text(node.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-aria-label]').forEach((node) => {
    node.setAttribute('aria-label', text(node.dataset.i18nAriaLabel));
  });
  elements.searchInput.placeholder = text('searchPlaceholder');
  elements.languageSwitch.setAttribute('aria-label', state.language === 'zh' ? '语言' : 'Language');
  elements.languageSwitch.querySelectorAll('[data-language]').forEach((button) => {
    const active = button.dataset.language === state.language;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  applyTheme();
  updateSelectionBar();
  setSyncStatus('ready', state.hasLoaded ? text('synced') : text('scanning'));
}

function mediaMarkup(style, cardIndex) {
  const title = escapeHtml(styleName(style));
  if (!style.media) {
    return `
      <div class="preview preview-missing">
        <span class="missing-glyph" aria-hidden="true"></span>
        <p>${escapeHtml(text('noSample'))}</p>
        <small>${escapeHtml(text('noSampleHint'))}</small>
      </div>`;
  }

  if (style.media.type === 'gif') {
    return `
      <figure class="preview">
        <img class="lazy-media" data-src="${style.media.url}" alt="${title}" loading="lazy">
      </figure>`;
  }

  return `
    <figure class="preview">
      <video class="lazy-media" data-src="${style.media.url}" muted loop playsinline preload="none"
        aria-label="${title}" data-key="${cardIndex}"></video>
      <button class="video-toggle" type="button" aria-label="${escapeHtml(text('pause'))} ${title}" data-video-key="${cardIndex}">
        <span aria-hidden="true"></span>
      </button>
    </figure>`;
}

function styleSelectorMarkup(card, selectedIndex) {
  return `
    <div class="style-selector" role="group" aria-label="${escapeHtml(text('style'))}">
      ${card.styles.map((style, index) => `
        <button type="button" class="style-option${index === selectedIndex ? ' is-active' : ''}"
          data-card-name="${escapeHtml(card.name)}" data-style-index="${index}"
          aria-pressed="${index === selectedIndex}">
          <span>${escapeHtml(styleName(style))}</span>
          ${style.media ? '' : '<i aria-hidden="true"></i>'}
        </button>`).join('')}
    </div>`;
}

function cardMarkup(card, cardIndex) {
  const selectedIndex = Math.min(state.selectedStyles[card.name] || 0, card.styles.length - 1);
  const style = card.styles[selectedIndex];
  const encodedSource = card.source.split('/').map(encodeURIComponent).join('/');
  const sourceUrl = card.sourceUrl || `/source/${encodedSource}`;
  const previewed = card.styles.filter((item) => item.media).length;
  const title = cardName(card);
  const subtitle = state.language === 'zh' ? card.name : '';

  const isSelected = state.selectedCards.has(card.name);

  return `
    <article class="shot-card${isSelected ? ' is-selected' : ''}" id="${escapeHtml(card.name)}">
      ${mediaMarkup(style, cardIndex)}
      <div class="card-body">
        <div class="card-title-row">
          <div class="card-title">
            <h2>${escapeHtml(title)}</h2>
            ${subtitle ? `<p>${escapeHtml(subtitle)}</p>` : ''}
          </div>
          <div class="card-actions">
            <button type="button" class="select-button${isSelected ? ' is-selected' : ''}" data-select-name="${escapeHtml(card.name)}"
              aria-pressed="${isSelected}" aria-label="${escapeHtml(text('select'))} ${escapeHtml(card.name)}">
              <span class="select-check" aria-hidden="true"></span>${escapeHtml(isSelected ? text('selected') : text('select'))}
            </button>
            <button type="button" class="copy-button" data-copy-name="${escapeHtml(card.name)}" aria-label="${escapeHtml(text('copy'))} ${escapeHtml(card.name)}">${escapeHtml(text('copy'))}</button>
            <a class="source-link" href="${escapeHtml(sourceUrl)}" target="_blank" rel="noreferrer">${escapeHtml(text('source'))}</a>
          </div>
        </div>

        <div class="current-style">
          <span>${escapeHtml(text('style'))} ${String(selectedIndex + 1).padStart(2, '0')}</span>
          <strong>${escapeHtml(styleName(style))}</strong>
          ${state.language === 'zh' ? `<code>${escapeHtml(style.key)}</code>` : ''}
        </div>

        <p class="summary">${escapeHtml(cardDescription(card))}</p>
        ${styleSelectorMarkup(card, selectedIndex)}

        <dl class="card-meta">
          <div><dt>${escapeHtml(text('duration'))}</dt><dd>${escapeHtml(durationText(card.duration))}</dd></div>
          <div><dt>${escapeHtml(text('energy'))}</dt><dd>${escapeHtml(energyText(card.energy))}</dd></div>
          <div><dt>${escapeHtml(text('sample'))}</dt><dd>${previewed}/${card.styles.length}</dd></div>
        </dl>
      </div>
    </article>`;
}

function cardMatches(card) {
  const searchable = [
    card.name,
    translations.cardsZh[card.name],
    translations.cardsEn[card.name],
    card.summary,
    card.use,
    card.duration,
    card.energy,
    card.intention,
    ...card.styles.flatMap((style) => [
      style.key,
      style.label,
      translations.stylesZh[style.key],
      style.description,
      style.use,
    ]),
  ].join(' ').toLowerCase();

  if (state.query && !searchable.includes(state.query.toLowerCase())) return false;
  const energy = card.energy || '';
  if (state.filter === 'high' && !energy.includes('高')) return false;
  if (state.filter === 'medium' && !energy.includes('中')) return false;
  if (state.filter === 'low' && !energy.includes('低')) return false;
  if (state.filter === 'missing' && card.styles.every((style) => style.media)) return false;
  return true;
}

function render() {
  if (!state.library) return;
  const cards = state.library.cards.filter(cardMatches);
  elements.library.innerHTML = cards.map(cardMarkup).join('');
  elements.library.setAttribute('aria-busy', 'false');
  elements.emptyState.hidden = cards.length > 0;
  observeMedia();
}

let mediaObserver;
function observeMedia() {
  mediaObserver?.disconnect();
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  mediaObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      const media = entry.target;
      if (entry.isIntersecting) {
        if (!media.src && media.dataset.src) media.src = media.dataset.src;
        if (media.tagName === 'VIDEO' && !reduceMotion && entry.intersectionRatio > 0.55) {
          media.play().catch(() => {});
        }
      } else if (media.tagName === 'VIDEO') {
        media.pause();
      }
    });
  }, {rootMargin: '320px 0px', threshold: [0, 0.55]});

  document.querySelectorAll('.lazy-media').forEach((media) => mediaObserver.observe(media));
}

function setSyncStatus(mode, label) {
  elements.syncStatus.dataset.state = mode;
  elements.syncStatus.lastElementChild.textContent = label;
}

let toastTimer;
function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add('is-visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => elements.toast.classList.remove('is-visible'), 2600);
}

async function loadLibrary({silent = false} = {}) {
  if (!silent) setSyncStatus('loading', text('scanning'));
  try {
    const response = await fetch(libraryEndpoint, {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const library = await response.json();
    const changed = state.hasLoaded && library.revision !== state.revision;
    state.library = library;
    state.revision = library.revision;
    state.hasLoaded = true;

    elements.cardCount.textContent = library.stats.cardCount;
    elements.styleCount.textContent = library.stats.styleCount;
    elements.previewCount.textContent = library.stats.previewCount;
    setSyncStatus('ready', text('synced'));
    if (changed) {
      render();
      showToast(text('updated'));
    } else if (!silent) {
      render();
    }
  } catch (error) {
    console.error(error);
    setSyncStatus('error', text('failed'));
    if (!state.hasLoaded) {
      elements.library.innerHTML = `
        <div class="load-error">
          <p>${escapeHtml(text('loadFailed'))}</p>
          <button type="button" id="retryLoad">${escapeHtml(text('retry'))}</button>
        </div>`;
      document.querySelector('#retryLoad')?.addEventListener('click', () => loadLibrary());
    }
  }
}

elements.searchInput.addEventListener('input', (event) => {
  state.query = event.target.value.trim();
  render();
});

elements.filters.addEventListener('click', (event) => {
  const button = event.target.closest('[data-filter]');
  if (!button) return;
  state.filter = button.dataset.filter;
  elements.filters.querySelectorAll('[data-filter]').forEach((item) => {
    item.classList.toggle('is-active', item === button);
    item.setAttribute('aria-pressed', String(item === button));
  });
  render();
});

elements.languageSwitch.addEventListener('click', (event) => {
  const button = event.target.closest('[data-language]');
  if (!button || button.dataset.language === state.language) return;
  state.language = button.dataset.language;
  try {
    localStorage.setItem('video-shot-gallery-language', state.language);
  } catch {}
  applyLanguage();
  render();
});

elements.themeSwitch.addEventListener('click', (event) => {
  const button = event.target.closest('[data-theme]');
  if (!button || button.dataset.theme === state.theme) return;
  state.theme = button.dataset.theme;
  try {
    localStorage.setItem('video-shot-gallery-theme', state.theme);
  } catch {}
  applyTheme();
});

window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if (state.theme === 'system') applyTheme();
});

function cardCopyValue(cardName) {
  const card = state.library?.cards.find((item) => item.name === cardName);
  const selectedIndex = Math.min(state.selectedStyles[cardName] || 0, (card?.styles.length || 1) - 1);
  const styleKey = card && card.styles.length > 1 ? card.styles[selectedIndex]?.key : '';
  return styleKey && styleKey !== cardName ? `${cardName} · ${styleKey}` : cardName;
}

async function copyText(value, toastLabel = value) {
  try {
    await navigator.clipboard.writeText(value);
    showToast(`${text('copied')}${toastLabel}`);
  } catch {
    try {
      const helper = document.createElement('textarea');
      helper.value = value;
      helper.style.position = 'fixed';
      helper.style.opacity = '0';
      document.body.append(helper);
      helper.select();
      document.execCommand('copy');
      helper.remove();
      showToast(`${text('copied')}${toastLabel}`);
    } catch {
      showToast(text('copyFailed'));
    }
  }
}

const copyCardName = (cardName) => copyText(cardCopyValue(cardName));

function updateSelectionBar() {
  const count = state.selectedCards.size;
  elements.selectionBar.hidden = count === 0;
  elements.selectionCount.textContent = text('selectedCount').replace('{n}', count);
  elements.copySelected.textContent = text('copySelected');
  elements.clearSelected.textContent = text('clearSelected');
}

function toggleCardSelection(cardName) {
  if (state.selectedCards.has(cardName)) {
    state.selectedCards.delete(cardName);
  } else {
    state.selectedCards.add(cardName);
  }
  updateSelectionBar();
  render();
}

function copySelectedCards() {
  if (!state.selectedCards.size) return;
  // 按当前库顺序输出，保持稳定；含多式卡的当前所选式
  const ordered = (state.library?.cards || [])
    .filter((card) => state.selectedCards.has(card.name))
    .map((card) => cardCopyValue(card.name));
  const value = ordered.join('，');
  copyText(value, text('selectedCount').replace('{n}', ordered.length));
}

elements.library.addEventListener('click', (event) => {
  const selectButton = event.target.closest('[data-select-name]');
  if (selectButton) {
    toggleCardSelection(selectButton.dataset.selectName);
    return;
  }

  const copyButton = event.target.closest('[data-copy-name]');
  if (copyButton) {
    copyCardName(copyButton.dataset.copyName);
    return;
  }

  const styleButton = event.target.closest('[data-style-index]');
  if (styleButton) {
    state.selectedStyles[styleButton.dataset.cardName] = Number(styleButton.dataset.styleIndex);
    render();
    return;
  }

  const toggle = event.target.closest('.video-toggle');
  if (!toggle) return;
  const video = document.querySelector(`video[data-key="${toggle.dataset.videoKey}"]`);
  if (!video) return;
  if (!video.src && video.dataset.src) video.src = video.dataset.src;
  if (video.paused) {
    video.play().catch(() => {});
    toggle.setAttribute('aria-label', toggle.getAttribute('aria-label').replace(text('play'), text('pause')));
    toggle.classList.remove('is-paused');
  } else {
    video.pause();
    toggle.setAttribute('aria-label', toggle.getAttribute('aria-label').replace(text('pause'), text('play')));
    toggle.classList.add('is-paused');
  }
});

elements.copySelected.addEventListener('click', copySelectedCards);
elements.clearSelected.addEventListener('click', () => {
  state.selectedCards.clear();
  updateSelectionBar();
  render();
});

elements.clearFilters.addEventListener('click', () => {
  state.query = '';
  state.filter = 'all';
  elements.searchInput.value = '';
  elements.filters.querySelectorAll('[data-filter]').forEach((item) => {
    const active = item.dataset.filter === 'all';
    item.classList.toggle('is-active', active);
    item.setAttribute('aria-pressed', String(active));
  });
  render();
});

document.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    elements.searchInput.focus();
  }
  if (event.key === 'Escape' && document.activeElement === elements.searchInput) {
    elements.searchInput.value = '';
    state.query = '';
    render();
    elements.searchInput.blur();
  }
});

function showLoadingCards() {
  const template = document.querySelector('#loadingTemplate');
  elements.library.innerHTML = '';
  for (let index = 0; index < 8; index += 1) {
    elements.library.append(template.content.cloneNode(true));
  }
}

applyLanguage();
showLoadingCards();
loadLibrary();
if (!staticGallery) setInterval(() => loadLibrary({silent: true}), 8000);
