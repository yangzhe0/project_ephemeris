const languagePreference = 'ephemeris-language';
let pageLanguage = 'zh-CN';

try {
    if (localStorage.getItem(languagePreference) === 'en') pageLanguage = 'en';
} catch {
    // Language switching still works if storage is disabled.
}

const originalTranslations = new WeakMap();
const localizedAttributes = ['text', 'title', 'placeholder', 'label', 'aria-label', 'alt'];

function translate(chinese, english) {
    return pageLanguage === 'en' ? english : chinese;
}

function stationName(name, code) {
    if (pageLanguage !== 'en') return name || '测站 ' + code;
    const option = document.querySelector('#observatorySelect option[value="' + CSS.escape(code) + '"]');
    const presetName = option?.getAttribute('data-en-text');
    if (presetName) return presetName.slice(code.length + 3);
    return name && !/\p{Script=Han}/u.test(name) ? name : 'Station ' + code;
}

function targetName(name, code) {
    if (pageLanguage !== 'en') return name || '天体 ' + code;
    if (!name || name.startsWith('天体 ')) return 'Target ' + code;
    // Known targets already carry an English name after their Chinese name.
    return name.replace(/^\p{Script=Han}+\s+/u, '').replace(' (中心)', ' Center');
}

function localizeMarkup(root = document) {
    for (const attribute of localizedAttributes) {
        for (const element of root.querySelectorAll('[data-en-' + attribute + ']')) {
            if (!originalTranslations.has(element)) originalTranslations.set(element, {});
            const original = originalTranslations.get(element);
            if (!(attribute in original)) {
                original[attribute] = attribute === 'text' ? element.textContent.trim() : element.getAttribute(attribute);
            }
            const value = translate(original[attribute], element.getAttribute('data-en-' + attribute));
            if (attribute === 'text') element.textContent = value;
            else element.setAttribute(attribute, value);
        }
    }
}

function localizePage() {
    document.documentElement.lang = pageLanguage;
    localizeMarkup();
    document.querySelectorAll('[data-guide-lang]').forEach(guide => {
        guide.hidden = guide.lang !== pageLanguage;
    });
    const toggleLabel = document.querySelector('#languageToggle span');
    toggleLabel.lang = translate('en', 'zh-CN');
    toggleLabel.textContent = translate('English', '中文');
}

function toggleLanguage() {
    pageLanguage = translate('en', 'zh-CN');
    try {
        localStorage.setItem(languagePreference, pageLanguage);
    } catch {
        // Keep the preference for the current page.
    }
    localizePage();
    refreshLocalizedResults();
}
