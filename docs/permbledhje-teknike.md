# rhg-dashboard — përmbledhje teknike

_28 shtator 2026_

## Çfarë përmban

Monorepo me tre shërbime Python dhe një shtresë deploy-i. Të tre shërbimet janë pa framework: `ThreadingHTTPServer` nga libraria standarde, routing me `if url.path == ...`, dhe frontend SPA në një skedar HTML me JavaScript të pastër. Varësitë e vetme: `requests` dhe `requests_ntlm`. Gjithsej ~15.000 rreshta kod.

| Shërbimi | Porti | Roli |
|---|---|---|
| `rhg-purchasing-local` | 8765 | Purchasing Control Tower: value entries, fatura, performanca e userave, PO, requisitions, stock, compliance, krahasime, buxhet, cilësi të dhënash, brief ekzekutiv |
| `rhg-finance-local` | 8766 | Finance & Inventory: P&L dhe bilanc nga FlowFields e Chart of Accounts (207 llogari), AP/AR aging, cash, treasury, assets, closing cockpit |
| `rhg-identity` | 8767 | Login qendror: llogari, role, sesione, audit në SQLite; permissions `<app>.<area>.<action>`; `/internal/validate` i mbrojtur me `SERVICE_TOKEN` |

### Burimi i të dhënave
Business Central OData V4 (kompania SALT), vetëm lexim, auth basic ose NTLM. Çdo tower mban cache SQLite (`data/cache.db`, tabela `entries`/`periods`) me logjikë append-only: një periudhë lexohet një herë, pastaj kërkohen vetëm entries me `Entry_No` më të madh. State-i i aplikacionit (actions, settings, audit, users, history, closing) është JSON në `data/`.

### Deploy
Docker Compose në 139.162.169.104. Nginx Proxy Manager mban 80/443 dhe TLS (self-signed mbi IP); nginx i brendshëm bën path rewriting me `sub_filter` për `/finance/` dhe `/purchasing/` dhe kufizon cookie-t sipas prefiksit; aplikacionet nuk publikojnë porta. GitHub Actions bën deploy në çdo push në `main`: kontroll `.env`, `compileall`, rsync me SSH, `compose build/up`, health-check, verifikim 200.

## Gjetje që duan vëmendje

1. **Integrimi me identity është gjysmë.** Compose u jep tower-ave `IDENTITY_URL`, `SERVICE_TOKEN`, `RHG_APP`, `COOKIE_NAME`, por kodi i Finance/Purchasing nuk i përdor: `auth.py` ende bën login lokal me `data/users.json` dhe cookie `session`.
2. **`data/` është i gjurmuar në git** (users.json me hash-e, cache.db ~48 MB, history.json), megjithëse `.gitignore` e përjashton. Duhet `git rm --cached -r */data` dhe mundësisht pastrim i historikut.
3. **CRLF/LF:** checkout-i në Windows/OneDrive i shfaq ~49 skedarë si të ndryshuar. Shtoni `.gitattributes` me `* text=auto eol=lf` (skriptet `.sh` me CRLF nuk ekzekutohen në server).
4. **`backup.sh` nuk përfshin `identity-data`**, ku jetojnë llogaritë, rolet dhe sesionet.
5. **Dockerfile i dublikuar** katër herë; `deploy/Dockerfile` nuk përdoret nga compose.
6. **Pa teste automatike**; CI kontrollon vetëm sintaksën.
7. **TLS self-signed** derisa të ketë domain.

## Pikat e forta
Dokumentim i mirë që shpjegon vendimet; PBKDF2 me 240k iteracione, cookies HttpOnly, CSRF te identity, lockout pas 5 dështimeve, kontejnerë non-root, firewall vetëm 22/80/443; strategji cache-i e menduar mirë për një G/L me ~38.000 hyrje në ditë.
