# RHG Purchasing Dashboard (lokal)

Dashboard blerjesh që xhiron në PC-në tënde, lexon direkt nga Business Central (OData V4) dhe rifreskohet vetë çdo 30 sekonda.

## Instalimi (një herë)
1. Duhet Python 3.10+ (e ke tashmë, se e përdor serveri `C:\bc-mcp`). Nëse jo: python.org, me "Add to PATH".
2. Hape zip-in te p.sh. `C:\rhg-purchasing`.
3. Dy-klik `start.bat`. Herën e parë hap `.env` në Notepad: plotëso `BC_USERNAME` dhe `BC_PASSWORD`
   (të njëjtat kredenciale që ka konfigurimi i `C:\bc-mcp`). Ruaje dhe mbylle.
4. Dy-klik `start.bat` sërish. Instalon paketat (vetëm herën e parë) dhe hap `http://127.0.0.1:8765`.

Nëse nuk lidhet: dy-klik `check_connection.bat` — tregon saktë ku është problemi (login, adresë, rrjet).

## Kredencialet
- BC me "NavUserPassword": `BC_AUTH_MODE=basic`, fjalëkalimi = **Web Service Access Key** e userit.
- BC me Windows login: `BC_AUTH_MODE=ntlm`, username si `DOMAIN\user`, fjalëkalimi i Windows.
Kredencialet qëndrojnë vetëm te `.env` në PC-në tënde.

## Si funksionon
- Merr `ValueEntries` me `Item_Ledger_Entry_Type eq 'Purchase'` për javën e zgjedhur dhe javën para saj.
- Java aktuale krahasohet "like for like" (e hënë → sot, me të njëjtat ditë të javës së kaluar).
- Pas ngarkimit të parë, çdo 30 sek kërkon vetëm hyrjet e reja (`Entry_No` më i madh) — ngarkesë minimale mbi BC.
- Kostoja përfshin mallin e pranuar por ende pa faturë (kosto e pritshme). Për vetëm faturat: `INCLUDE_EXPECTED_COST=false`.

## Pamjet
Overview · Price changes (me kërkim, renditje, eksport CSV) · Location prices (i njëjti artikull me çmime të ndryshme sipas lokacionit) · Exceptions · Latest postings (live).

## Faturat dhe performanca e userave
**Invoices** (punon menjëherë):
- faturat e postuara, kreditë dhe raporti kredit/fatura (nga VendorLedgerEntries, përfshin edhe shërbimet)
- rreshtat për faturë (mesatarja, maksimumi, shpërndarja sipas madhësisë)
- vonesa e postimit (data e faturës së furnitorit → data e postimit), % brenda ditës, faturat e vonuara
- faturat për t'u kontrolluar: rreshta negativë (duhej kredit) ose kosto zero
- furnitorët sipas numrit të faturave dhe vonesës mesatare

**User performance** kërkon një hap në BC, sepse asnjë web service aktual s'e ka User ID:
1. BC → **Web Services** → **New**
2. Object Type `Page`, Object ID `117` (Item Registers), Service Name `ItemRegisters`, **Published** ✔
Pa ndryshuar asgjë këtu, në rifreskimin tjetër del për çdo user: fatura, rreshta, vlera, fatura/ditë, pranime,
kredite, rreshta negativë, kosto zero, vonesa, postime të vonuara, postime me datë mbrapa (backdated),
ora kur poston më shumë, postimi i fundit. Eksport CSV nga çdo ekran.

Kufizim: useri lidhet përmes rreshtave të artikujve, prandaj faturat vetëm-shërbime (pa artikuj) s'kanë user.

## Control Tower (Faza 1)
Ekranet e reja: **Live operations** (feed i dokumenteve dhe porosive, me "data updated at"), **Purchase orders**
(porositë e hapura, mallra të marra pa faturë), **Supplier performance**, **Action center**, **Data availability**, **Settings**.

- **Executive purchasing brief** në krye të Overview: status (Under control / Attention required / Critical intervention
  required), zhvillimet kryesore, impakti financiar, rreziqet operacionale dhe max 5 veprime me prioritet, pronar, afat,
  vlerë në rrezik dhe besueshmëri. Çdo rresht etiketohet fakt / inference / rekomandim dhe hapet te regjistrat e BC.
- **Drill-down**: çdo kartë KPI dhe çdo përjashtim hap dritaren me rreshtat përkatës të BC, me eksport CSV.
- **Rregullat SLA** (Settings): mall i marrë pa faturë, PO e hapur gjatë, faturë e postuar vonë, rritje çmimi mbi %,
  impakt mbi X ALL, diferencë çmimi mes lokacioneve, përqendrim furnitori. Ruhen te `data/settings.json`.
- **Action center**: veprime me pronar, afat, status, komente dhe histori. Ruhen te `data/actions.json`;
  BC mbetet vetëm për lexim. Çdo ndryshim regjistrohet te `data/audit.json` (shihet te Settings → Audit history).
- **Data availability**: KPI-të që BC nuk i jep (kërkesa, aprovime, blerësi te PO, çmimi kontraktual, UOM) me kërkesën
  teknike përkatëse për Onetech. Asgjë nuk vlerësohet me hamendje.

## Kërkesat e blerjes (Requisitions)
Ekrani **Requisitions** aktivizohet sapo të publikohet worksheet-i në BC:
1. BC → **Web Services** → **New**
2. Object Type `Page`, zgjidh **Req. Worksheet** nga lista e objekteve (mos shkruaj ID me dorë)
3. Service Name `RequisitionLines`, shëno **Published**

Nëse ekipi përdor një faqe tjetër worksheet-i, vendos emrin e atij web service te `BC_REQ_ENTITY` në `.env`.

Rëndësishme: BC e fshin rreshtin e worksheet-it në momentin që kthehet në porosi dhe nuk mban asnjë gjurmë.
Prandaj koha kërkesë→porosi nuk rindërtohet dot për të kaluarën. App-i vendos vulë kohore për çdo rresht që sheh
(`data/history.json`) dhe e mat vetë kohën nga momenti që nis ndjekja. E njëjta logjikë mat edhe sa orë
qëndron një porosi në status Open ose Released, meqë as këtë BC nuk e regjistron.

Pragjet: 24 orë warning, 48 orë critical, të ndryshueshme te Settings.

## Faza 2
- **Comparisons**: java e kaluar, e njëjta periudhë muajin e kaluar (28 ditë), e njëjta periudhë vitin e kaluar (364 ditë)
  dhe mesatarja rrotulluese 4-javore. Të gjitha me ditë të njëjta të javës, që ditët e tregtimit të përputhen.
- **Budget**: nga G/L Budget Entries, e ndarë pro-rata sipas ditëve; krahasohet me koston reale të mallit nga G/L Entries
  të të njëjtave llogari. Emri i buxhetit dhe llogaritë e kostos vendosen te Settings (default 6051, 60111).
- **Contracted price**: kërkon publikimin e listës së çmimeve (Web Services → Page → *Purchase Prices* ose *Price List Lines*
  → emri `PurchasePrices`). Pastaj çdo artikull i blerë mbi çmimin e rënë dakord del si përjashtim, me tolerancë te Settings.
- **Data quality**: lokacion që mungon, kosto zero, kosto pa sasi, data postimi në të ardhmen, përshkrime jokonsistente,
  artikuj të dyfishuar, fatura të mundshme të dyfishta, fatura pa furnitor, ndryshim çmimi mbi 5x (gabim i mundshëm njësie),
  rreshta pa kod artikulli. Çdo gjetje hapet me regjistrat dhe kthehet në veprim.
- **Reports**: Daily brief dhe Weekly report, me eksport CSV dhe printim (Ctrl+P → Save as PDF).

## Opsione
- Të tjerët në zyrë ta hapin: `APP_HOST=0.0.0.0`, pastaj `http://IP-e-PC-së:8765` (lejo portin 8765 në Windows Firewall). Pa login — vetëm në rrjet të besuar.
- Nisje automatike me Windows: shkurtore e `start.bat` te `shell:startup`.
- Pragjet (minimumi i shpenzimit, % e ndryshimit, alarmet) ndryshohen te `.env`.
