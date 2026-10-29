# QSL Tracker

A small Flask app for hams, part of [KN0BLE.com](https://kn0ble.com).

**QSL Tracker** is the site name. Each tool has one name, used in the
navigation, the page title, and the heading:

| Tool | Who | Path |
| --- | --- | --- |
| ADIF Address Extractor | public (QRZ login) | `/raw-address` |
| QSL Photo Map | public | `/photomap` |
| Request a Card | public, no login | `/request-qsl` |
| Logbook | admin | `/admin/log` |
| QSO Labels | admin | `/admin/qso-label` |
| QSO Map | admin | `/admin/qso-map` |
| QSL Cards | admin | `/admin/photomap/manage` |
| Card requests | admin | `/admin/requests` |

Visitors see ADIF Address Extractor, QSL Photo Map, Request a Card, and Admin.
After admin log in, the nav adds Logbook, QSO Labels, QSO Map, QSL
Cards, and Card requests, and keeps the public pages in a second group.
Log out of QRZ and log out of admin are separate, in the header.

- **ADIF Address Extractor** -- log in with your own QRZ XML subscriber account,
  look up one callsign or upload an ADIF log, and print the mailing
  addresses QRZ has on file onto Avery 8160 labels. Shows a heads-up
  when a station has opted out of paper cards or lists a QSL manager.
  Nothing looked up or uploaded is stored.
- **QSL Photo Map** -- scanned QSL cards on a public world map.
- **Request a Card** -- someone worked KN0BLE and wants a card mailed
  back. No QRZ login. Details under "Email notifications" below.
- **Logbook** -- the copy of your log behind QSO Labels and QSL Cards
  auto-fill. One page, three tasks: **Sync from QRZ**, **Find missing
  QSOs**, and **Import ADIF**. Counts (this log, QRZ's total, last sync,
  anything not matched) stay at the top. "Not matched" links straight
  to Find missing QSOs. Filled by the QRZ Logbook API or an ADIF upload.
  The old import URL `/admin/photomap/import-adif` opens Import ADIF.
- **QSO Labels** -- your own logged QSOs, filterable by callsign or
  DXCC entity, printed onto Avery 5163 labels to stick on the card, or
  printed **straight onto the 6"x4" QSL card** ("Print on the card"):
  one 6x4 page per QSO, the same details grid placed in the blank
  lower-left of the card back. "Proof" draws it over a scan of the card
  (`static/card/qsl_card_back.jpg`) for checking on screen; a saved
  right/down nudge corrects for where the printer actually lands it.
  Extract address on a row adds that station to the ADIF Address Extractor batch.
- **QSO Map** -- a world map of your log by DXCC entity, shaded by QSO
  count. Click a country (or a small island's dot) to list its QSOs,
  check some, and add them to the QSO Labels sheet. Map outlines are
  Natural Earth 1:50m map subunits (public domain), which already split
  out most separate DXCC entities (Alaska, Hawaii, England/Scotland/Wales,
  European/Asiatic Russia, Canary Islands, Sardinia ...); entity
  reference points come from AD1C's cty.dat. Both are pre-built into
  `static/qso_map/areas.geojson` and `qso_map_entities.json` by
  `tools/build_qso_map.py` (run by hand only to refresh them; the app
  never fetches anything).
- **QSL Cards** -- admin upload and edit of scanned cards shown on the
  QSL Photo Map. All cards and Upload are the two sections;
  `/admin/photomap/upload` is Upload.

## How it works

- No accounts. Each visitor gets an anonymous, cookie-based session.
- Visitors log in with their **own** QRZ XML subscriber username and
  password, used once to fetch a short-lived QRZ session key; the
  password is never stored and the key lives server-side in SQLite,
  keyed by the anonymous session id.
- ADIF Address Extractor uploads are capped at 200 distinct callsigns, paced
  between QRZ requests, and stop early (with a clear message) on a long
  request or repeated QRZ failures. Any upload (ADIF or card photo) is
  also capped at 8 MB.
- Session cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` (HTTPS
  only, unless `SESSION_COOKIE_SECURE=0`). Every form that posts to this
  app includes a per-session CSRF token, except `/request-qsl`.
- **QRZ Logbook sync** pages through the whole logbook with
  `OPTION=MAX:250,AFTERLOGID:n` (QRZ's documented pagination), saving
  the cursor after every page, so the first sync walks everything and
  later ones fetch only new QSOs. A sync that stops partway resumes
  from that log id on the next press, including QSOs whose dates are
  older than the newest row already stored. "Find missing QSOs" can
  record a second QRZ copy of a contact already here, so those ids
  stop showing up as missing. A QSO date in the future is ignored when
  the first sync picks its date window, so one bad date cannot freeze
  sync. It deliberately never uses QRZ's `CALL:` filter, which returned
  nothing in testing. Needs `QRZ_LOGBOOK_API_KEY` (QRZ.com -> Logbook
  -> Settings -> API).
- `/request-qsl` is a separate, public, unauthenticated form (no QRZ
  login needed) for the reverse case: someone worked KN0BLE and wants a
  card mailed back to *them*. It's embedded directly on kn0ble.com
  (index and awards pages) via a plain HTML `<form>` posting here, and
  also reachable directly. Callsign is required. Note, mailing address
  (`address`: street, city, postal code, country, up to 500 characters),
  and email are optional. The email is only so Josh can reach the
  operator; it is stored and shown on Card requests, and it is included
  in the notification body, but it is **not** set as Reply-To. On submit
  the request is saved to `qsl_requests` (an `address` column is added
  for databases created before that field existed) and, if Resend is
  configured (see below), emailed to Josh. Admin reads them at
  `/admin/requests`. A hidden honeypot (`hp_url`, and the older field
  name `website`) and a light per-session rate limit (3 requests /
  10 minutes) guard against bots and spam. This form is the one POST
  exempt from the CSRF token, because the copy embedded on kn0ble.com
  is plain HTML and cannot carry a token minted here. The session
  cookie is `SameSite=Lax`, so that cross-site POST does not include
  the cookie: the honeypot still runs, but the rate limit only sees
  requests made on this site itself.

## Setup

Requires a paid **QRZ XML Logbook Data** subscription (that's a
QRZ.com feature, not something this app provides).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# optional: pin a stable Flask session signing key across restarts
export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

# Session cookies are Secure (HTTPS-only). For local http://127.0.0.1
# the browser would drop them, so turn that off here. Leave this unset
# on Render.
export SESSION_COOKIE_SECURE=0

python3 app.py
```

Then open http://127.0.0.1:5000 and log in with your QRZ credentials.

Admin login (`/admin/login`) checks `ADMIN_PASSWORD`. The compare is
constant-time and accepts a non-ASCII password. Pick something you
don't use anywhere else.

## Deploying to Render

A `render.yaml` blueprint is included, so Render can pick up the whole
config automatically:

1. In the [Render dashboard](https://dashboard.render.com), **New +** ->
   **Blueprint**, and point it at this repo.
2. Render reads `render.yaml`, provisions a free web service running
   `gunicorn app:app --workers 1 --timeout 120`, and auto-generates a
   stable `SECRET_KEY` env var (important: without a fixed `SECRET_KEY`,
   each gunicorn worker process would get its own random key and session
   cookies would break intermittently depending on which worker handles
   a request).
3. Deploy. Render gives you a `*.onrender.com` URL; a custom domain
   (e.g. a `qsl` subdomain of kn0ble.com) can be attached afterward from
   the service's Settings tab. Re-applying the blueprint prompts for
   `QRZ_LOGBOOK_API_KEY` if it is not already set. It does not overwrite
   a value already saved in the dashboard.

The start command pins **one worker** and a **120 second** timeout.
One worker is deliberate: the log is one JSON object in S3. Overlapping
saves retry (S3 `If-Match`, which needs boto3 1.37 or newer --
`requirements.txt` already requires that, and no extra IAM action
beyond `s3:PutObject`), so a lost race does not erase QSOs, but a
second worker is not useful on the free plan. The longer timeout is so
gunicorn does not kill a sync mid-page. The sync still stops itself
well inside that limit and asks you to press Sync again. If you change
the start command, keep `--workers 1` and a timeout of at least 120.

**Storage note:** the free plan's disk is ephemeral -- a redeploy or a
spin-down after inactivity wipes `instance/qsl_tracker.db`. Given results
already auto-purge after 24h and there are no accounts, that's a fairly
soft loss (an active visitor mid-session on a restart would just need to
re-search). It also now means a visitor's *login* resets on
redeploy/spin-down too, since the QRZ session key moved into that same
database (see "server-side session store" below) -- that trade is worth
it (nothing sensitive sits in the browser's cookie anymore) but it's a
real behavior change from before, when the cookie alone kept someone
logged in across a restart. That's still an accepted tradeoff for
`contacts`/`auth_sessions`/`qsl_requests`, which are meant to be
short-lived. If it ever needs to stop being ephemeral too, add a
persistent disk to `render.yaml` (Starter plan or above; the free plan
doesn't support disks):

```yaml
    disk:
      name: qsl-data
      mountPath: /opt/render/project/src/instance
      sizeGB: 1
```

**The QSL Photo Map's data does *not* depend on this disk at all** --
see "QSL Photo Map" below for why (it's stored in S3 as JSON instead),
so cards uploaded through `/admin/photomap/upload` survive Render
redeploys/spin-downs on the free plan just fine, no disk upgrade needed.

## Running on Unraid

Run the app on the Unraid box as a container. `render.yaml` is
unchanged and still describes the Render service. A push to `main`
(or a manual run of the "Publish container image" workflow from
`main`) builds `ghcr.io/jnubbz/qsl-tracker` for amd64 and tags it
`latest` plus the short git sha. The image runs gunicorn with one
worker and a 120 second timeout, listens on port 8000, and keeps
SQLite at `/data/qsl_tracker.db`. Cards and the QSO log stay in S3,
so they do not move with the container.

Cloudflare Tunnel is the public entry. `cloudflared` only connects
outbound, and the compose file publishes no host ports. That needs
no router port forward. kn0ble.com's DNS zone has to be hosted on
Cloudflare for the tunnel hostname to exist. The free plan is enough;
a paid Zero Trust plan is not required. If the zone is still on
another DNS host, move the nameservers to Cloudflare first.

The photo map and the log already live in S3, not on Render's disk.
Turning Render off does not delete them. Do that once
`https://qsl.kn0ble.com` is serving from Unraid, so two copies are
not syncing the log at the same time.

1. Create the appdata folders before the first `docker compose up`.
   The process is uid 99, gid 100 (Unraid's `nobody:users`). If the
   data folder is missing, Compose creates it as root and the app
   cannot write its database:

   ```bash
   mkdir -p /mnt/cache/appdata/qsl-tracker/data
   chown nobody:users /mnt/cache/appdata/qsl-tracker/data
   ```

2. Copy `docker-compose.yml` and `.env.example` into
   `/mnt/cache/appdata/qsl-tracker/`. Copy the example to `.env` and
   replace every placeholder. Nothing in `.env.example` is a real
   credential. Leave `QSL_DB_PATH=/data/qsl_tracker.db`. Generate
   `SECRET_KEY` with
   `python3 -c 'import secrets; print(secrets.token_hex(32))'`.
   Leave `SESSION_COOKIE_SECURE` at `1` (or delete the line) once
   visitors use HTTPS. The home test below is the one time it is `0`.

3. In the Cloudflare dashboard: Zero Trust, Networks, Tunnels, create
   a tunnel, choose Docker, and put the token in `.env` as
   `TUNNEL_TOKEN`. Add a public hostname `qsl.kn0ble.com` whose
   service is `http://qsl-tracker:8000`. That name is the app
   container on the compose network, not an address on the Unraid host.

4. After the first green run on `main`, the image is at
   `ghcr.io/jnubbz/qsl-tracker`. If a pull on Unraid is denied, the
   package is still private: either make it public (GitHub, Packages,
   package settings, change visibility) or run `docker login ghcr.io`
   on Unraid with a token that can read packages.

5. Start it from the appdata directory:

   ```bash
   cd /mnt/cache/appdata/qsl-tracker
   docker compose up -d
   ```

   To pick up a new `latest`, schedule the same two commands. The
   User Scripts plugin can run them daily. The app service sets
   `pull_policy: always`, so `up -d` recreates the container when
   the image changed:

   ```bash
   cd /mnt/cache/appdata/qsl-tracker
   docker compose pull
   docker compose up -d
   ```

6. Backups. Point the Appdata Backup plugin at
   `/mnt/cache/appdata/qsl-tracker` so the SQLite file and `.env`
   are copied. `.env` has secrets, so keep that backup private.
   In the AWS console, turn on versioning for the photo-map bucket.
   Versioning is what lets you restore the log and the card index
   after a bad write. The appdata copy does not back up S3.

7. A browser on the LAN, over plain HTTP, drops the session cookie
   while it is marked Secure. To click through before the hostname
   works, set `SESSION_COOKIE_SECURE=0` in `.env`, temporarily add
   `ports: ["8000:8000"]` under the `qsl-tracker` service, and run
   `docker compose up -d`. Open `http://<unraid-ip>:8000`. When
   `https://qsl.kn0ble.com` loads, remove that `ports` entry, set
   `SESSION_COOKIE_SECURE` back to `1` (or delete the line), and run
   `docker compose up -d` again. Normal operation publishes no ports.


## Email notifications for QSL card requests

The `/request-qsl` form emails a notification via the
[Resend](https://resend.com) HTTP API when someone requests a card.
**Not Gmail SMTP** -- that was the original approach and it doesn't
work on Render's free plan (see "Why not Gmail SMTP" below). It needs
two env vars set in the Render dashboard (Settings -> Environment) --
`render.yaml` declares them with `sync: false` so Render prompts for
values instead of storing them in the repo:

- `RESEND_API_KEY` -- an API key from a free [resend.com](https://resend.com)
  account. Sign up, skip domain verification (not needed for this),
  and grab an API key from the dashboard.
- `NOTIFY_EMAIL` -- where the notification should land, e.g. Josh's own
  Gmail address. **Must be the same email address the Resend account
  was created with**, unless a custom sending domain has been
  verified -- Resend's free/unverified tier only allows sending to
  your own address, as an anti-abuse measure. That's exactly what this
  feature needs (notifications to yourself), so it's not actually a
  limitation here.

If these aren't set, the form still works and requests are still saved
to the database -- they just won't trigger an email until the Resend
setup is done.

**Why not Gmail SMTP:** the original version of this feature used
Gmail SMTP directly. Render's free plan silently blocks outbound SMTP
entirely -- both port 465 (implicit TLS) and port 587 (STARTTLS)
reliably timed out from a live deployment (confirmed 2026-08-21), which
is a common anti-spam policy on free-tier hosts. Plain HTTPS isn't
blocked (the app already depends on it for the QRZ API), so switching
to an HTTP-based email API sidesteps the problem entirely rather than
fighting it. Along the way an unrelated IPv6 issue was also found and
worked around (`_force_ipv4_dns()` in `mailer.py`'s git history) --
Render's containers advertise an IPv6 address but don't actually route
it outbound, which caused a separate instant `Network is unreachable`
error before the SMTP-port-blocking issue was even reached. That fix is
no longer needed now that SMTP isn't used at all, but the lesson (check
IPv4-only if a Render outbound connection fails instantly rather than
timing out) is worth remembering for anything else this app might ever
connect to.

## QSL Photo Map

`/photomap` is a public world map (Leaflet + OpenStreetMap tiles, clustered
markers) plotting scanned QSL cards by station. Click a pin to see the
card photo(s) and QSO details for that callsign.

Uploading is admin-only (just Josh) -- there's no public upload:

- `/admin/login` -- a single shared password, checked against the
  `ADMIN_PASSWORD` env var. Separate from the QRZ login system the rest
  of the app uses (that's per-visitor and anonymous; this is one
  person's admin area).
- `/admin/log` (Logbook) -- Sync from QRZ, Find missing QSOs, or Import
  ADIF, so the QSL Cards upload form can auto-fill QSO details
  (date/band/mode/frequency/RST) for a callsign instead of typing them
  by hand. Safe to mix and repeat -- duplicates are skipped.
- `/admin/photomap/upload` (QSL Cards, Upload) -- pick a callsign,
  attach one or more front-of-card photos, fill in (or accept the
  auto-filled) QSO details, save. A callsign's map location
  (country/state/grid/lat-lon) is looked up from QRZ the first time it's
  uploaded and cached from then on -- requires being logged in with your
  QRZ credentials (same login as the rest of the app) the first time a
  given callsign is used. Cards already uploaded are listed and edited
  from `/admin/photomap/manage`.

Photos are stored in a private S3 bucket and relayed straight through the
Flask app both ways: on upload (a normal multipart form post, not a
direct-to-S3 presigned upload) and on view (`/photomap/image/<key>`
streams the object's bytes through Flask rather than handing the
browser a presigned S3 URL). Both choices avoid needing any S3 CORS
configuration. The presigned-URL approach was tried first for viewing
and dropped after it ran into a persistent, never-fully-explained
`SignatureDoesNotMatch` in production -- see `qsl-tracker-status.md` in
the project for the full debugging trail if this ever needs revisiting.

`/photomap/image/<key>` only serves a card photo:
`photocards/<callsign>/<32-hex>.jpg|jpeg|png|webp|gif` (the callsign may
contain a slash, as in `FP/KJ1V`). It responds with the type for that
extension and `X-Content-Type-Options: nosniff`. It will not serve
`photocards/_index.json` or any other object. Uploads are limited to
those same image types; the browser's content type is not trusted.

**All of this data lives in S3, not in Render's SQLite disk** --
`photomap_store.py` keeps every callsign location, photo card, and
imported QSO in one JSON object at `photocards/_index.json` in the same
bucket (deliberately inside the `photocards/` prefix the IAM policy
below already covers, so this needed no extra AWS setup). Each save is
conditional on the object's ETag, so two requests writing at once
retry instead of the later one wiping the earlier one's QSOs. This is the
one part of the app that can't afford Render's free-tier ephemeral
disk -- everything else (`contacts`, `auth_sessions`, `qsl_requests`)
is fine being short-lived, but the whole point of the QSL Photo Map is
that uploaded cards stick around, so its data intentionally never
touches SQLite at all. A Render redeploy or spin-down wipes
`instance/qsl_tracker.db` same as always, but the photo map doesn't
notice or care.

**Setup:** four env vars, in addition to what's already needed above.
`render.yaml` sets `S3_BUCKET` and `AWS_REGION` directly (not secrets),
and declares the rest with `sync: false` so Render prompts for them in
the dashboard instead of storing them in the repo:

- `S3_BUCKET` / `AWS_REGION` -- already set in `render.yaml` for Josh's
  bucket (`kn0ble-qsl-...-us-east-2`, region `us-east-2`).
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` -- credentials for an IAM
  user or role scoped to just this bucket. Minimal policy:
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
        "Resource": "arn:aws:s3:::kn0ble-qsl-.../photocards/*"
      },
      {
        "Effect": "Allow",
        "Action": "s3:ListBucket",
        "Resource": "arn:aws:s3:::kn0ble-qsl-...",
        "Condition": {
          "StringLike": { "s3:prefix": "photocards/*" }
        }
      }
    ]
  }
  ```
  (swap in the real bucket name; no account-wide access needed).
  `s3:DeleteObject` is required, or removing a photo or a card leaves
  the object in the bucket (the metadata row goes away, the file does
  not). The `ListBucket` statement is scoped to just the `photocards/*` prefix
  via the `StringLike` condition -- it's needed because S3 returns
  `AccessDenied` instead of a clean 404 on `GetObject` for a
  not-yet-existing key (like `photocards/_index.json` on first run)
  when the caller lacks `ListBucket`, which otherwise looks exactly
  like a permissions problem instead of "nothing's been uploaded yet."
- `ADMIN_PASSWORD` -- whatever password gates `/admin/login`. Pick
  something you don't use anywhere else -- it's checked with a
  constant-time compare but is otherwise a plain shared password, not
  hashed at rest.

If any of the AWS vars are missing, the admin upload form will show an
S3 error when you try to save a card rather than failing silently.

## Project status

Early / brainstorming-to-working-prototype stage. Next up:

- [x] Deploy a public instance -- `render.yaml` added; run through Render
      dashboard to go live (see above)
- [x] Rate-limit QRZ lookups more carefully -- uploads now pace requests,
      cap themselves to a time budget, and back off on repeated failures
      (see the note above)
- [x] Server-side session store for the QRZ session key -- it now lives
      in SQLite (`auth_sessions`, keyed by the anonymous session id) and
      the cookie only ever carries that id, never the key itself
- [x] Print-ready mailing labels -- ADIF Address Extractor (replaced the
      old Dashboard + CSV export on 2026-10-04)
- [x] QRZ Logbook sync for QSO labels (AFTERLOGID paging, 2026-10-04)
- [x] "Request a Card" -- public form (embedded on kn0ble.com) for
      visitors to request a card back, emailed to Josh via Gmail SMTP
      (see "Email notifications" above)
- [x] QSL Photo Map -- public `/photomap` (world map of scanned cards)
      plus an admin-only upload/import flow; see "QSL Photo Map" above

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

The tests use a throwaway SQLite file and stand-ins for S3 and QRZ.
They do not contact AWS, QRZ, or Resend. `QSL_DB_PATH` and
`SESSION_COOKIE_SECURE` are set by the test harness; you don't need
to export them yourself.

## License

MIT -- see [LICENSE](LICENSE).
