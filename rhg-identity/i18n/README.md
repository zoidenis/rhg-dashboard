# pulse in Albanian

`sq.json` is the Albanian of every English text of the pages (Finance, Purchasing, identity), keyed by the English
text with its numbers and values as `#`. identity serves it with `static/i18n-runtime.js` as `/i18n.js`; every page
loads it first, and the runtime translates the page as it is drawn. A person's language (sq, the default, or en)
is kept on their account and switched with SQ / EN in the side menu.

- A wrong or missing word on the server, without a deploy: put `{"English text": "shqip"}` in
  `data/i18n-sq.json`; it is read on top of `sq.json` at the next page load.
- After adding screens: `python3 i18n/extract.py ../ /tmp/keys.json` lists every English text of the pages;
  the keys missing from `sq.json` are the ones to translate (same number of `#`, in the same order).
- Text that must stay as it is (names, codes typed by people): `data-no-i18n` on the element.
