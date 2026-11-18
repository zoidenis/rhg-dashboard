# Pulse × Microsoft 365: çfarë u bë

18 nëntor 2026 · gjendja në fund të ditës

## Përmbledhje

Pulse është tani i lidhur me Microsoft 365 në dy pika, me një aplikacion të vetëm në Entra ID dhe me leje të ngushta:

1. **Email-i** dërgohet me Microsoft Graph (OAuth) nga `pulse@rhg.al`, në vend të SMTP me fjalëkalim. Microsoft e çaktivizon SMTP AUTH me fjalëkalim në fund të dhjetorit 2026, ndaj ky ishte hapi urgjent.
2. **SharePoint** lexohet vetëm në tre site, dhe vetëm për lexim:
   - **RHG-Home**: regjistri i navigimit shfaqet në Control center;
   - **RHG-KONTRATA** dhe **RHG-Politika**: faqja e re **Contracts & SOPs** (`/documents`), e hapur vetëm për Fabian Kuka.

Pagat, HR, Financa dhe DocumentHub mbeten të mbyllura për Pulse.

## 1. Analiza e dokumentit "Pulse × Microsoft 365"

Auditi i Gerit (`integration.md`) u shqyrtua dhe u gjykua i drejtë: Pulse mbetet motori i kontrollit, M365 mban identitetin, dokumentet dhe aprovimet. Shtesat kryesore të sugjeruara:

- Email-i të ndahej nga SSO dhe të bëhej menjëherë, për shkak të afatit të dhjetorit.
- SSO me Entra ID: llogaritë të lidhen me `oid`, jo me email; sesione të shkurtra; llogari emergjente.
- Unicorn me procedurat kërkon lexim me lejet e personit (on-behalf-of), jo kopjim të lejeve në Pulse.
- Kolonat kyçe të kontratave të bëhen të detyrueshme në SharePoint, jo të mbështeten te disiplina.
- Tabela LOC/ENT ↔ BC/Simphony mund të nisë menjëherë.
- Rreziku kryesor është pronësia: Fabian është admin i tenant-it, pronar i përmbajtjes dhe dërgues i email-eve.

## 2. Email-i me Microsoft Graph

**Kodi** (commit `0cb558f`):
- Moduli i ri `rhg-purchasing-local/graph_mail.py`. I gjithë email-i i Pulse (Purchasing, Finance, Action Center, raportet) kalon nga një funksion i vetëm, `notify.send_mail`, ndaj ndryshimi u bë vetëm aty.
- Email-et deri rreth 3.5 MB dërgohen me një kërkesë. Ato më të mëdha (eksportet deri në 9.5 MB) kalojnë si draft dhe me upload session; drafti fshihet nëse diçka dështon.
- Settings tregon gjendjen e Graph, datën e skadimit të certifikatës (me paralajmërim 30 ditë para) dhe arsyen kur diçka nuk është gati.

**Konfigurimi në M365:**
- Kutia e përbashkët `pulse@rhg.al`.
- Aplikacioni **RHG Pulse – server** në Entra ID, me certifikatë (pa client secret).
- Leja për email-in jepet në Exchange (RBAC for Applications), vetëm për `pulse@rhg.al`:
  - `Application Mail.Send` dhe `Application Mail.ReadWrite`, me scope "RHG Pulse - kutia";
  - u verifikua: `InScope = True` për `pulse@rhg.al`, `False` për `fkuka@rhg.al`.

**Verifikimi:**
- Dy teste nga serveri: një email i vogël dhe një 4 MB.
- Email testi te Gmail: erdhi te Inbox.
- SPF, DKIM, DMARC dhe MX të `rhg.al` janë në rregull.

## 3. SharePoint

### Lejet
- Në Entra, aplikacioni ka **vetëm `Sites.Selected`**. Kjo leje, më vete, nuk hap asnjë site.
- Leje "read" të dhëna site për site: **RHG-Home**, **RHG-KONTRATA**, **RHG-Politika**.
- Çdo site RHG-* është site më vete (RHG-Home nuk ka nën-site), ndaj një leje nuk kalon kurrë te site-t e tjera.
- U verifikua nga serveri: këto tre hapen (200); FINANCA, HR, HR-Payroll dhe DocumentHub refuzohen (403); shkrimi refuzohet.

### Regjistri i navigimit në Control center (commit `995fd87`)
- Karta **"RHG Home · SharePoint"**, si pemë: Portal → site → brand → njësi, plus lidhja "RHG Home ↗" në menu.
- Ndjek rregullat e vetë regjistrit: nuk shfaqen destinacionet e çaktivizuara, të paverifikuara ose pa adresë (sot: OPA Shkodër, OPA Food Truck, OPA Centralizim).
- Lidhje bëhen vetëm adresat `https://…sharepoint.com`.
- E sheh kush ka lejen `m365.home.view` (sot vetëm roli Administrator).

### Contracts & SOPs (commit `9e1059e`)
- Faqja `/documents` lexon çdo skedar të RHG-KONTRATA dhe RHG-Politika sipas kolonave të tij, **kurrë përmbajtjen**. Skedari hapet në SharePoint, i cili kontrollon aksesin e personit.
- **Kontratat:** entiteti, biblioteka, pala tjetër, lloji, statusi, datat, afati i njoftimit, pronari; çfarë mungon; çfarë kërkon vëmendje:
  - në bibliotekë të nënshkruar, por me status Draft;
  - afati i njoftimit brenda 60/30 ditëve, ose i kaluar;
  - e mbaruar, por ende Aktiv.
- **Politikat dhe SOP-të:** kodi, versioni (0.x = draft), statusi, aprovimi, data e rishikimit. Paralajmërimet:
  - versioni draft;
  - i publikuar, por i paaprovuar;
  - rishikimi i vonuar.
- **Aksesi:** vetëm llogaritë e emërtuara në `M365_DOCS_USERS`, sipas username-it (sot `fkuka@rhg.al`). Pulse i lexon me aplikacionin e vet, ndaj SharePoint nuk e kontrollon personin që shikon. "Read again" regjistrohet në audit.
- Lexohet çdo 10 minuta; nëse SharePoint nuk përgjigjet, mbahet leximi i fundit i mirë.

## 4. Gjetjet nga të dhënat (18.11.2026)

| | Gjendja |
|---|---|
| Kontrata gjithsej | 102 (93 RHG SHPK të nënshkruara, 2 RHG SHPK në punë, 7 RHG KS të nënshkruara) |
| Kontrata me metadata të plotë | 1 nga 102 |
| Në bibliotekë "Të nënshkruara", por me status "Draft" | 96, me shumë mundësi vlera parazgjedhje e pandryshuar |
| Politika dhe SOP në RHG-Politika | 1 skedar, pa metadata; 36 biblioteka bosh |
| Site të tjera jashtë modelit RHG-* | rreth 30 (Purchasing, Finance, SALT TEAM, "Departamenti Blerjeve…" dy herë etj.) |

## 5. Konfigurimi aktual

| Çfarë | Vlera |
|---|---|
| Aplikacioni | RHG Pulse – server |
| Client ID | `9c030146-4e2b-4f7f-b123-1e13cd44998c` |
| Tenant ID | `bc05fb9a-76dc-437e-9832-f9ca3c87272f` |
| Object ID (Enterprise app) | `eee66328-2491-43c7-9248-9ca5e98d0b02` |
| SharePoint | `revoal.sharepoint.com` |
| Certifikata | `/opt/rhg/secrets/mail/pulse-graph.pem` në server; thumbprint `F78DEE762011285F2083034DF7CBB9FAB9A5EB5E`; **skadon më 18.11.2027** |
| Kutia dërguese | `pulse@rhg.al` (Settings → Email notifications → From) |
| `.env` i Purchasing | `MAIL_TRANSPORT=graph`, `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CERT_PATH=/app/secrets/pulse-graph.pem`; kopje e vjetër: `.env.bak-20261118-131918` |
| `.env` i identity (opsionale) | `M365_DOCS_USERS`: parazgjedhja `fkuka@rhg.al` |
| Lejet në Pulse | `m365.home.view` (regjistri i navigimit); Contracts & SOPs sipas username-it |

## 6. Siguria: çfarë ndodhi dhe çfarë mësuam

- **`Sites.Read.All` u shtua gabimisht** bashkë me `Sites.Selected`. Për pak minuta Pulse mund të lexonte çdo site të kompanisë, përfshirë pagat. Pulse nuk lexoi asnjë dokument; u kontrollua vetëm nëse site-t përgjigjeshin. Leja u hoq.
- **Mësimi:** heqja e lejes vetëm te *App registrations* nuk mjafton; miratimi duhet hequr te *Enterprise applications → Permissions*. Pas çdo ndryshimi në Entra, kontrollohet që token-i të ketë vetëm `Sites.Selected`.
- **Pse aksesi sipas emrit:** 4 llogari kanë rolin Administrator (`admin`, `fkuka@rhg.al`, `temp`, `techi`), dhe Administrator merr automatikisht çdo leje të re.

## 7. Çfarë mbetet

**Pas 2–3 ditësh pa probleme me email-in:**
- [ ] Hiq fjalëkalimin e vjetër SMTP: Settings → "Remove saved App Password" → Save.
- [ ] Mbyll SMTP AUTH te kutia e vjetër, nëse asgjë tjetër nuk e përdor: `Set-CASMailbox -Identity fkuka@rhg.al -SmtpClientAuthenticationDisabled $true`.

**Llogaritë:**
- [ ] Çaktivizo `temp` (Temporary Administrator), nëse nuk duhet më.
- [ ] Verifiko llogarinë `techi`.

**SharePoint:**
- [ ] Ndrysho statusin e 96 kontratave "Draft" te "Të nënshkruara" dhe plotëso datat, palën tjetër dhe pronarin.
- [ ] Bëji kolonat kyçe të kontratave të detyrueshme te bibliotekat "Të nënshkruara".
- [ ] Zhvendos SOP-të dhe manualet nga DocumentHub në RHG-Politika, me aprovim.
- [ ] Vendos për rreth 30 site-t e vjetra jashtë modelit RHG-*.

**Fazat e ardhshme:**
- [ ] Hyrja në Pulse me Entra ID (SSO).
- [ ] Njoftime në Teams.
- [ ] Leximi i RHG-FINANCA (REG-Lokacione, REG-Entitete) dhe tabela LOC/ENT ↔ BC/Simphony.
- [ ] Kërkesat PRO dhe ASR në Action Center.

**Afate:**
- [ ] **18.11.2027:** rinovo certifikatën.

## 8. Veprime të zakonshme

**Hap Contracts & SOPs për një person tjetër:** në `/opt/rhg/rhg-identity/.env` shto ose ndrysho `M365_DOCS_USERS=fkuka@rhg.al,<username>`, pastaj `docker compose up -d identity`.

**Hap regjistrin e navigimit për një rol:** Admin → Roles & permissions → "RHG Home: see the SharePoint navigation register…".

**Jep "read" për një site të ri:** Fabian hyn me një kod pajisjeje (device code) me `Sites.FullControl.All`, dhe jepet `POST /sites/{id}/permissions` me `roles: ["read"]` vetëm për aplikacionin RHG Pulse – server. Pastaj verifikohet nga serveri që site-i hapet dhe që të tjerat mbeten 403. Kurrë për RHG-HR, RHG-HR-Payroll ose OneDrive.

**Rinovo certifikatën:** hapat te `deploy/README-DEPLOY.md`, seksioni "Email me Microsoft Graph". Pulse e lexon skedarin e ri pa rinisje.

**Kthim te SMTP (vetëm deri sa Microsoft ta mbyllë):** `MAIL_TRANSPORT=smtp` te `.env` i Purchasing, From përsëri adresa e vjetër, pastaj `docker compose up -d purchasing`.

## Commit-et

| Commit | Çfarë |
|---|---|
| `0cb558f` | Email through Microsoft Graph (OAuth) in place of SMTP AUTH |
| `995fd87` | Control center: RHG Home's navigation register from SharePoint |
| `9e1059e` | Contracts & SOPs: RHG-KONTRATA and RHG-Politika read from SharePoint |
