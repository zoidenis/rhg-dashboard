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
