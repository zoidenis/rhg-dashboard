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
Çdo push te `main` i `fabiankuka01-stack/rhg-tower` bën deploy vetë: imazhet ndërtohen në runner-in e GitHub-it dhe
ngarkohen në server me `docker save | docker load`, vetëm ato që ndryshuan. Në server nuk ndërtohet më, sepse ndërtimi
e lë pa memorie. `data/` është volume — përditësimi nuk prek users, actions, settings, audit.

## Memoria (serveri ka 1 GB RAM)
Më 6 nëntor 2026 serveri ngeci sepse RAM-i dhe swap-i u mbushën. Mbrojtjet tani:
- **Swap 2.5 GB:** `/swapfile` (2 GB) mbi `/dev/sdb` (496 MB), që memoria e papërdorur të mos nxjerrë kodin nga RAM-i.
- **`vm.swappiness = 10`** (`/etc/sysctl.d/90-rhg-memory.conf`): aplikacionet qëndrojnë në RAM dhe lirohet më parë cache-i i skedarëve.
- **`earlyoom`:** vret aplikacionin më të madh para se host-i të ngecë, dhe Docker e rindez (`/etc/default/earlyoom`).
- **Kufij për kontejner:** `mem_limit` dhe `memswap_limit` te `docker-compose.yml`. Një aplikacion i rëndë ngadalëson veten, jo gjithë serverin.

Kontroll: `free -m`, `docker stats --no-stream`, `sar -r` (historiku), `journalctl -u earlyoom`.
Nëse serveri rritet, ngriji kufijtë te `docker-compose.yml`.

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

## Email me Microsoft Graph (në vend të SMTP AUTH)

Microsoft e çaktivizon SMTP AUTH me fjalëkalim (Basic) si parazgjedhje për tenant-ët ekzistues në
fund të dhjetorit 2026. Pulse dërgon atëherë me Microsoft Graph: një aplikacion në Entra ID me
**certifikatë** (asnjë fjalëkalim apo client secret), që mund të dërgojë **vetëm** nga kutia e Pulse.
I gjithë email-i (Purchasing, Finance, Action Center, raportet) kalon nga Purchasing, ndaj
konfigurohet vetëm aty. Asnjë çelës nuk kalon në chat apo në git.

**1. Kutia (Microsoft 365 admin center).** Krijoni një kuti të përbashkët (shared mailbox),
p.sh. `pulse@rhg.al`. Nuk kërkon licencë.

**2. Certifikata, në server.** Çelësi privat krijohet në server dhe nuk del prej andej:

```bash
sudo mkdir -p /opt/rhg/secrets/mail && cd /opt/rhg/secrets/mail
sudo openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes -subj "/CN=RHG Pulse mail" \
     -keyout pulse-graph.pem -out pulse-graph.crt
sudo sh -c 'cat pulse-graph.crt >> pulse-graph.pem'     # pem = çelësi + certifikata
UID_RHG=$(docker compose -f /opt/rhg/deploy/docker-compose.yml exec -T purchasing id -u)
sudo chown "$UID_RHG" pulse-graph.pem && sudo chmod 400 pulse-graph.pem
sudo chmod 711 /opt/rhg/secrets /opt/rhg/secrets/mail
```

`pulse-graph.crt` është vetëm pjesa publike: atë e shkarkoni për hapin 3. `pulse-graph.pem` nuk
kopjohet askund.

**3. Aplikacioni (Entra admin center → App registrations → New registration).**
- Emri `RHG Pulse – server`, *Single tenant*, pa redirect URI.
- Shënoni **Application (client) ID** dhe **Directory (tenant) ID**.
- *Certificates & secrets → Certificates → Upload certificate*: `pulse-graph.crt`.
- **Mos** shtoni `Mail.Send` te *API permissions*. Leja atje vlen për çdo kuti të tenant-it.
  Leja jepet në hapin 4, vetëm për kutinë e Pulse.
- Te *Enterprise applications → RHG Pulse – server* shënoni **Object ID** (i service principal-it,
  jo ai i app registration).

**4. Leja vetëm për kutinë e Pulse (Exchange Online PowerShell, RBAC for Applications).**

```powershell
Connect-ExchangeOnline
New-ServicePrincipal -AppId <CLIENT_ID> -ObjectId <OBJECT_ID_E_ENTERPRISE_APP> -DisplayName "RHG Pulse"
New-ManagementScope -Name "RHG Pulse - kutia" -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'pulse@rhg.al'"
New-ManagementRoleAssignment -App <CLIENT_ID> -Role "Application Mail.Send" -CustomResourceScope "RHG Pulse - kutia"
# email-et mbi ~3.5 MB (eksportet deri në 9.5 MB) kalojnë si draft në po atë kuti:
New-ManagementRoleAssignment -App <CLIENT_ID> -Role "Application Mail.ReadWrite" -CustomResourceScope "RHG Pulse - kutia"
# kontrolli: duhet të dalë InScope = True për pulse@rhg.al, dhe False për një kuti tjetër
Test-ServicePrincipalAuthorization -Identity <CLIENT_ID> -Resource pulse@rhg.al
Test-ServicePrincipalAuthorization -Identity <CLIENT_ID> -Resource fkuka@rhg.al
```

Ndryshimet e roleve mund të duan nga 30 minuta deri në 2 orë që të vlejnë.

**5. Pulse.** Te `/opt/rhg/rhg-purchasing-local/.env`:

```
GRAPH_TENANT_ID=<Directory (tenant) ID>
GRAPH_CLIENT_ID=<Application (client) ID>
GRAPH_CERT_PATH=/app/secrets/pulse-graph.pem
```

Pastaj `docker compose up -d purchasing`. Te Purchasing → Settings → Email notifications:
*From address* = `pulse@rhg.al`, **Save mail server**, pastaj **Send a test**. Pill-i lart duhet
të thotë "Microsoft 365 · Graph · from pulse@rhg.al". Nëse nuk është gati, aty shfaqet arsyeja
(certifikata nuk u gjet, mungon tenant-i, refuzim nga Microsoft). Logu i nisjes e thotë po ashtu.

**6. Pastrimi, pasi testi del mirë.** Hiqni `SMTP_PASSWORD` nga `.env` dhe fjalëkalimin e ruajtur
(Settings → "Remove saved App Password" → Save). Pastaj, nëse asgjë tjetër nuk dërgon me SMTP
nga ajo kuti, mbylleni SMTP AUTH për kutinë e vjetër:
`Set-CASMailbox -Identity <kutia e vjetër> -SmtpClientAuthenticationDisabled $true`.

**Rrotullimi (çdo 12 muaj).** Settings tregon datën e skadimit dhe paralajmëron 30 ditë para
afatit. Krijoni certifikatën e re me hapin 2 (mbi të njëjtin skedar), ngarkoni `.crt` e re te
aplikacioni (e vjetra mund të mbetet deri sa të skadojë). Pulse e lexon vetë skedarin e ri, pa rinisje.

**Kthim prapa.** `MAIL_TRANSPORT=smtp` te `.env` dhe `docker compose up -d purchasing`. Vlen vetëm
deri sa Microsoft ta mbyllë SMTP AUTH.

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
- Deploy-i automatik (GitHub Actions) e mban `reporter`-in lart vetë. Imazhi (Chromium, ~400 MB), si të tjerët,
  ndërtohet në runner-in e GitHub-it dhe dërgohet me
  `docker save | docker load`, vetëm kur ndryshon kodi te `rhg-identity/reporter` (etiketa `rhg.tree` e imazhit mban
  hash-in e dosjes në git). Në çdo deploy tjetër hapi vetëm kontrollon që kontejneri të jetë `Up`. Hapi vjen pas
  verifikimit të faqes, kështu që një dështim i tij nuk e lë faqen poshtë.

  Me dorë (p.sh. pa GitHub) mund të ndërtohet edhe në server, me kujdes për memorien:

      cd /opt/rhg/deploy
      df -h / && free -m                      # duhen ~2 GB disk të lirë dhe memorie për build-in
      docker compose --profile reports build reporter
      docker compose --profile reports up -d reporter
      docker compose logs --tail=20 reporter

  Kontejneri ka `mem_limit: 700m`, nën RAM-in e serverit (1 GB): një faqe shumë e rëndë e ndal Chromium-in brenda
  kontejnerit dhe raporti del si i dështuar, në vend që kernel-i të vrasë Finance ose Purchasing.
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
