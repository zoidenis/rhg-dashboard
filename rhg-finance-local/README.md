# RHG Finance & Inventory Control Tower (Faza 1)

App i veçantë nga Purchasing, xhiron lokalisht, lexon Business Central vetëm për lexim.

## Nisja
1. Dy-klik `start.bat`. Herën e parë plotëso `BC_USERNAME` dhe `BC_PASSWORD` te `.env` (si te `C:\bc-mcp`).
2. Dy-klik sërish. Hapet te `http://127.0.0.1:8766`.
3. Nëse diçka nuk lidhet, dy-klik `check_connection.bat`: kontrollon login-in, llogaritë, AP, AR, bankat,
   G/L-në, buxhetin dhe dimensionet, dhe të thotë saktesisht cila burim dështoi.

Purchasing rri te porti 8765, Finance te 8766 — të dy mund të xhirojnë njëkohësisht.

## Si merren shifrat
- **P&L dhe Bilanci** nuk lexojnë G/L rresht për rresht: te SALT ka rreth 38,000 hyrje në ditë. Merren nga
  FlowFields-et e Chart of Accounts, që BC i llogarit vetë kur i jepet `Date_Filter eq 'nga..deri'` dhe,
  për lokal, `Global_Dimension_1_Filter`. 207 llogari, një kërkesë e lehtë.
- **Struktura** vjen nga `Account_Category` (Income, Cost of Goods Sold, Expense, Assets, Liabilities, Equity).
  Amortizimi ndahet sipas emrit të llogarisë — kontrolloje para se raporti të shkojë te bordi.
- **AP/AR** nga hyrjet e hapura të furnitorëve dhe klientëve, me aging sipas datës së maturimit.
- **Cash** nga llogaritë e bilancit që identifikohen si arkë ose bankë, plus lëvizjet nga bank ledger.
- **G/L** lexohet vetëm mbi pragun e materialitetit; BC nuk publikon userin as timestamp-et e postimit.

## Faza 2 dhe 3
Inventari (valuation, lëvizjet, inventari fizik, stoku i ngadaltë), Closing Cockpit, Cash Flow, Treasury,
Intercompany, Fixed Assets, Budget & Forecast, konsolidimi dhe rolet.

## Shitjet nga Simphony
Ekrani **Sales · Simphony** lexon shitjet ditore të restoranteve nga Oracle Simphony, nëpërmjet **Business Intelligence API** të Reporting and Analytics. Vetëm lexim: në Simphony nuk shkruhet asgjë.

- **Lidhja:** Settings → **Simphony sales**: Authentication server, Application server, Enterprise short name, Client ID, emri i API account-it dhe fjalëkalimi. Ruhen te `data/simphony.json` në server (leje 600). Fjalëkalimi dhe token-at nuk i kthehen kurrë browser-it. **Test connection** hyn në Simphony dhe tregon lokacionet që sheh account-i.
- **API account-i** krijohet te Reporting and Analytics → Administration → System → API Accounts, me Type "Business Intelligence API", Location Authorization Scope në nivelin që përfshin të gjitha restorantet dhe Data Access "Sales And Operations". **Fjalëkalimi i tij skadon çdo 60 ditë**: pill-i paralajmëron nga dita e 50-të.
- **Hyrja:** OpenID Connect me PKCE. ID token-i zgjat 14 ditë dhe rinovohet 3 ditë para se të skadojë. Refresh token-i zgjat 28 ditë; vetëm pa të bëhet sërish sign-in i plotë.
- **Të dhënat:** `getLocationDimensions`, `getLatestBusDt` dhe `getOperationsDailyTotals`, një thirrje për restorant dhe ditë, në sfond si inventari. Ditët e mbyllura para 2 ditëve të fundit ruhen te `data/simphony_days.json` dhe nuk rilexohen.
- **Shifrat:** net sales (`netSlsTtl`), çeqet, klientët, çeku mesatar, zbritjet, void-et, kthimet dhe çeqet e hapura, për restorant dhe kundrejt periudhës së mëparshme. Monedhat (ALL/EUR) mbahen të ndara dhe nuk konvertohen.
- **Lidhja me BC:** fusha "Simphony location → BC code" (p.sh. `1001=SALT`) përgatit krahasimin e shitjeve me koston nga BC (food cost %), që është hapi tjetër.
