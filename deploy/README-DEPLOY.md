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
I gjithë state-i i aplikacionit është në dy volumet; nuk ka databazë.
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
