# Deploy me Docker — 139.162.169.104

Dy control tower-at xhirojnë si kontejnerë pas një nginx-i që mban TLS-in.
Vetëm nginx publikon porta; aplikacionet flasin vetëm brenda rrjetit të Docker-it.

```
Internet ──▶ NPM :443 ──▶ nginx :80 ──┬─▶ /finance/    ──▶ finance:8766
             (TLS, GUI)  (rewriting)  └─▶ /purchasing/ ──▶ purchasing:8765
                                               │
                                               └──▶ BC OData 92.205.182.52:8348 (read-only)
```

Nginx Proxy Manager owns 80/443 and TLS, with its GUI on **:81**. The internal
nginx keeps the path rewriting (sub_filter + cookie scoping) that NPM's GUI
cannot express.

## Nginx Proxy Manager

- GUI: `http://139.162.169.104:81`
- The proxy host forwards `139.162.169.104` to `nginx:80`.
- `nginx/custom/http_top.conf` inside the `npm-data` volume holds a catch-all
  `default_server`. Without it NPM rejects a bare-IP TLS handshake with
  "unrecognized name", because it matches hosts by SNI. A local copy is in
  `deploy/npm/http_top.conf`.
- **When the domain arrives:** point an A record at the server, then in the GUI
  add the domain to the proxy host and request a Let's Encrypt certificate
  (SSL tab → Request a new certificate). That removes the browser warning and
  auto-renews. Nothing else changes.

## Hapat

### 1. Serveri, një herë
```bash
scp deploy/server-setup.sh root@139.162.169.104:/tmp/
ssh root@139.162.169.104 'bash /tmp/server-setup.sh'
```
Instalon Docker, mbyll firewall-in përveç 22/80/443, dhe krijon certifikatën
self-signed për IP-në.

### 2. Dërgo kodin (nga Mac-u)
```bash
./deploy/push.sh
```
`.env`, `.venv`, `__pycache__` dhe `data/` nuk dërgohen nga rsync-u.

### 3. Krijo `.env` në server, një herë
Përmbajnë kredencialet e vërteta të BC, pra krijohen direkt në server:
```bash
ssh root@139.162.169.104
cp /opt/rhg/rhg-finance-local/.env.example    /opt/rhg/rhg-finance-local/.env
cp /opt/rhg/rhg-purchasing-local/.env.example /opt/rhg/rhg-purchasing-local/.env
nano /opt/rhg/rhg-finance-local/.env     # BC_USERNAME + BC_PASSWORD
nano /opt/rhg/rhg-purchasing-local/.env  # BC_USERNAME + BC_PASSWORD
chmod 600 /opt/rhg/rhg-*/.env
```
`APP_HOST` dhe `APP_PORT` **mos i ndrysho** — compose i mbishkruan vetë.

Për emailet e Purchasing (Faturat pa PO), hap **Purchasing → Settings → Email notifications → Mail server** dhe plotëso hostin SMTP, portën, sigurinë, përdoruesin, adresën From, fjalëkalimin e aplikacionit dhe URL-në publike. Ruajtja zbatohet pa rinisje. Vlerat `SMTP_*` dhe `PUBLIC_URL` në `.env` mbeten si konfigurim fillestar derisa të ruhet formulari. Fjalëkalimi ruhet në volumin `purchasing-data`, nuk kthehet nga API-ja dhe përfshihet në backup.

### 4. Nis
```bash
cd /opt/rhg/deploy && ./deploy.sh
```

### 5. Hyr
`https://139.162.169.104/` — prano warning-un e certifikatës një herë.
Fjalëkalimi i admin-it shfaqet vetëm në log-un e nisjes së parë:
```bash
docker compose logs finance    | grep -A4 "First run"
docker compose logs purchasing | grep -A4 "First run"
```

## Përditësim kodi
```bash
./deploy/push.sh                                   # nga Mac-u
ssh root@139.162.169.104 'cd /opt/rhg/deploy && docker compose build && docker compose up -d'
```
`data/` është volume — përditësimi nuk prek users, actions, settings, audit.

## Backup
State-i i aplikacionit është në volumet `finance-data`, `purchasing-data` dhe `identity-data`. Arkivat përmbajnë edhe kredenciale; `backup.sh` i kufizon për root.
```bash
/opt/rhg/deploy/backup.sh                 # → /opt/rhg/backups, mban 30 ditë
echo "15 2 * * * /opt/rhg/deploy/backup.sh" | crontab -   # çdo natë 02:15
```

Restore:
```bash
docker compose stop finance
docker run --rm -v deploy_finance-data:/data -v /opt/rhg/backups:/in alpine \
  sh -c 'rm -rf /data/* && tar xzf /in/deploy_finance-data-<STAMP>.tar.gz -C /data'
docker compose start finance
```

## Komanda të dobishme
```bash
docker compose ps                  # health i të dyve
docker compose logs -f finance     # log live
docker compose restart finance
docker compose down                # ndal (volumet mbeten)
```

## Certifikata e fiskalizimit (faturat e fiskalizuara kundrejt BC)

Finance lexon e-faturat nga shërbimi i Tatimeve me certifikatën e fiskalizimit të NIPT-it
(vetëm lexim, asnjë regjistrim). Certifikata **nuk** shkon në git dhe nuk futet në ekran:

```bash
sudo mkdir -p /opt/rhg/secrets
sudo cp fisk.p12 /opt/rhg/secrets/fisk.p12          # skedari .p12 i certifikatës
# përdoruesi brenda kontejnerit duhet ta lexojë: merr uid-in e tij
UID_RHG=$(docker compose -f /opt/rhg/deploy/docker-compose.yml exec -T finance id -u)
sudo chown "$UID_RHG" /opt/rhg/secrets/fisk.p12 && sudo chmod 400 /opt/rhg/secrets/fisk.p12
sudo chmod 711 /opt/rhg/secrets
```

Në `/opt/rhg/rhg-finance-local/.env` shtoni (fjalëkalimin e fut vetë administratori):

```
FISK_CERT_PASSWORD=...
FISK_NIPT=<NIPT-i i kompanisë që rakordohet, i njëjti me certifikatën>
FISK_COMPANY=SALT
# FISK_EINVOICE_URL bosh = https://einvoice.tatime.gov.al/EinvoiceService-v1 (prodhim)
```

Certifikata duhet të jetë e **po atij NIPT-i** si kompania e BC-së që rakordohet: shërbimi i
Tatimeve kthen vetëm faturat e mbajtësit të certifikatës (certifikata e RHG-së, L91520027A,
lexon faturat e RHG-së, jo të SALT-it). Për të zëvendësuar një certifikatë: kopjoni të renë mbi
`/opt/rhg/secrets/fisk.p12` me po ato komanda (chown/chmod), ndryshoni `FISK_CERT_PASSWORD` dhe
`FISK_NIPT` te `.env`, dhe rindizni Finance.

Pastaj `docker compose up -d finance` (rikrijon kontejnerin, që të lexojë `.env` e re; një
`restart` i thjeshtë nuk e lexon). Te Finance → Fiscal invoices vs BC, karta
"Connection to the Tax Administration" tregon nëse certifikata u hap dhe deri kur vlen.
`/opt/rhg/secrets` është jashtë dosjeve që deploy-i i zëvendëson, kështu që nuk preket.

## Unicorn (analisti i biznesit brenda pulse)

Unicorn shkruan çdo ditë në 08:00 brief-in e mëngjesit (shifrat e djeshme nga Finance dhe Purchasing) dhe u
përgjigjet pyetjeve në `/unicorn`, vetëm për rolet me të drejtën **unicorn.use** (Admin → Roles & permissions).

1. Krijo një API key në console.anthropic.com (organizata e RHG) dhe vendose **vetëm në server**:
   ```
   nano /opt/rhg/rhg-identity/.env
   ANTHROPIC_API_KEY=sk-ant-...
   UNICORN_MODEL=claude-sonnet-5-5      # opsionale
   UNICORN_TIME=08:00                    # opsionale (ora e serverit, Europe/Tirane)
   ```
   Mos e vendos kurrë në repo.
2. `cd /opt/rhg/deploy && docker compose up -d identity` (rinis identity që të lexojë çelësin).
3. Hap `/unicorn` → "Write today's brief now" për provën e parë (disa minuta, lexon Finance dhe Purchasing).

Çfarë del nga serveri: vetëm shifrat përmbledhëse të brief-it (shitje, P&L, cash, AP/AR, blerje, emrat e
furnitorëve/klientëve më të mëdhenj) drejt Anthropic API. Prompt-i kryesor është `rhg-identity/unicorn_prompt.md`;
një kopje në `identity-data` si `data/unicorn_prompt.md` e zëvendëson pa deploy.

## Raportet e planifikuara (shërbimi `reporter`)

Çdo faqe (tab) e pulse mund të dërgohet me email sipas një orari: Finance/Purchasing → Settings → *Scheduled reports*,
ose butoni *Schedule as report* në krye të çdo faqeje (për rolet me `reports.manage`).

- Identity mban raportet (`data/reports.json` në volumin e identity-t) dhe vendos kur është radha e secilit.
- Kontejneri `reporter` (kodi te `rhg-identity/reporter`, sepse deploy-i kopjon vetëm tre dosjet e aplikacioneve; Chromium headless, pa porta, pa të dhëna) pyet identity çdo 30 sekonda, hap faqen **si
  personi që e merr** (sesion vetëm-lexim 25 minutësh, i krijuar me SERVICE_TOKEN dhe i fshirë në fund), merr një
  foto (në trupin e email-it) dhe një PDF të plotë, dhe e dërgon përmes serverit të mail-it të Purchasing.
- Nuk ka `.env` të ri: `reporter` merr SERVICE_TOKEN nga `deploy/.env`, si kullat. `PUBLIC_URL` (identity, opsionale)
  është adresa e lidhjes "Hape në pulse" (parazgjedhur `https://dashboard.rhg.al`).
- Deploy-i i parë pas këtij ndryshimi zgjat disa minuta më shumë: shkarkohen Chromium dhe libraritë e tij (~400 MB
  imazh). Kontejneri ka `mem_limit: 1200m`.
- Kontroll: `docker compose logs reporter | tail` duhet të tregojë "pulse report sender: ...". Te Settings, shenja
  *Report sender on* tregon që ai është lidhur në 3 minutat e fundit.

## Vendime të zbatuara

**TLS self-signed.** Let's Encrypt nuk lëshon certifikata për IP, pra pa domain
warning-u i browserit është i pashmangshëm. Kriptimi është real; vetëm besimi mungon.
Nëse merrni domain: shtoni A record → 139.162.169.104, pastaj certbot e heq warning-un.

**Prefiksi dhe cookie-t.** Aplikacionet emetojnë path-e rrënjë-absolute (`/api/...`,
`/brand.css`, `location.href="/login"`). Nginx i heq prefiksin drejt aplikacionit dhe i
rishkruan path-et në kthim me `sub_filter`, pa ndryshuar asnjë rresht kodi.
`proxy_cookie_path` shton `Secure` — që aplikacioni vetë nuk e vendos ([auth.py](../rhg-finance-local/auth.py#L8))
— dhe e kufizon cookie-n në prefiksin e vet, kështu sesioni i Finance nuk vlen në Purchasing.

**`APP_HOST=0.0.0.0` brenda kontejnerit** është e detyrueshme që nginx t'i arrijë.
Nuk është ekspozim publik: kontejnerët e aplikacioneve nuk publikojnë porta.

**`--no-browser`** në CMD — pa të `webbrowser.open()` në `main()` dështon në server headless.

## E verifikuar lokalisht para deploy-it
Stack-u u ngrit dhe u testua në Docker; të gjitha kaluan:
- Të dy kontejnerët `healthy`; HTTP→HTTPS redirect 301
- `/finance/` dhe `/purchasing/` kthejnë login 200; asset-et rishkruhen saktë
- Login real → `Set-Cookie: Path=/finance/; Secure; HttpOnly; SameSite=Strict`
- Endpoint i mbrojtur: 200 me sesion, 401 pa sesion
- Izolim: cookie e Finance → 401 në Purchasing
- Persistencë: restart pa humbje të `data/` (admin-i nuk u rikrijua)
- Backup: arkiva përmban `users.json`, `audit.json`
