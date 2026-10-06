# QSL Tracker

A small Flask app for hams, part of [KN0BLE.com](https://kn0ble.com):

- **Raw Address Ripper** (`/raw-address`, public) -- log in with your own
  QRZ XML subscriber account, look up one callsign or upload an ADIF log,
  and print the mailing addresses QRZ has on file onto Avery 8160 labels.
  Shows a heads-up when a station has opted out of paper cards or lists
  a QSL manager. Nothing looked up or uploaded is stored.
- **QSO Labels** (`/admin/qso-label`, admin) -- your own logged QSOs,
  filterable by callsign or DXCC entity, printed onto Avery 5163 labels
  to stick on the card.
- **QSO Map** (`/admin/qso-map`, admin) -- a world map of your log by
  DXCC entity, shaded by QSO count. Click a country (or a small island's
  dot) to list its QSOs, check some, and add them to the QSO Labels
  sheet. Map outlines are Natural Earth 1:50m map subunits (public
  domain), which already split out most separate DXCC entities (Alaska,
  Hawaii, England/Scotland/Wales, European/Asiatic Russia, Canary
  Islands, Sardinia ...); entity reference points come from AD1C's
  cty.dat. Both are pre-built into `static/qso_map/areas.geojson` and
  `qso_map_entities.json` by `tools/build_qso_map.py` (run by hand only
  to refresh them; the app never fetches anything).
- **QSL Cards / Photo Map** -- admin upload of scanned cards, shown on
  the public `/photomap`.
- **Your log** (`/admin/log`, admin) -- the copy of your log behind QSO
  Labels, filled by "Sync from QRZ" (QRZ Logbook API) or an ADIF upload.

## How it works

- No accounts. Each visitor gets an anonymous, cookie-based session.
- Visitors log in with their **own** QRZ XML subscriber username and
  password, used once to fetch a short-lived QRZ session key; the
  password is never stored and the key lives server-side in SQLite,
  keyed by the anonymous session id.
- Ripper ADIF uploads are capped at 200 distinct callsigns, paced
  between QRZ requests, and stop early (with a clear message) on a long
  request or repeated QRZ failures.
- **QRZ Logbook sync** pages through the whole logbook with
  `OPTION=MAX:250,AFTERLOGID:n` (QRZ's documented pagination), saving
  the cursor after every page, so the first sync walks everything and
  later ones fetch only new QSOs. It deliberately never uses QRZ's
  `CALL:` filter, which returned nothing in testing. Needs
  `QRZ_LOGBOOK_API_KEY` (QRZ.com -> Logbook -> Settings -> API).
- `/request-qsl` is a separate, public, unauthenticated form (no QRZ
  login needed) for the reverse case: someone worked KN0BLE and wants a
  card mailed back to *them*. It's embedded directly on kn0ble.com
  (index and awards pages) via a plain HTML `<form>` posting here, and
  also reachable directly. On submit it's saved to a `qsl_requests`
  table and, if Gmail is configured (see below), emailed straight to
  Josh with the callsign, optional note, and optional contact email so
  he can look the callsign up here the normal way and mail a card. A
  hidden honeypot field and a light per-session rate limit (3 requests /
  10 minutes) guard against bots and spam.

## Setup

Requires a paid **QRZ XML Logbook Data** subscription (that's a
QRZ.com feature, not something this app provides).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# optional: pin a stable Flask session signing key across restarts
export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

python3 app.py
```

Then open http://127.0.0.1:5000 and log in with your QRZ credentials.

## Deploying to Render

A `render.yaml` blueprint is included, so Render can pick up the whole
config automatically:

1. In the [Render dashboard](https://dashboard.render.com), **New +** ->
   **Blueprint**, and point it at this repo.
2. Render reads `render.yaml`, provisions a free web service running
   `gunicorn app:app`, and auto-generates a stable `SECRET_KEY` env var
   (important: without a fixed `SECRET_KEY`, each gunicorn worker process
   would get its own random key and session cookies would break
   intermittently depending on which worker handles a request).
3. Deploy. Render gives you a `*.onrender.com` URL; a custom domain
   (e.g. a `qsl` subdomain of kn0ble.com) can be attached afterward from
   the service's Settings tab.

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

**Self-hosting on Unraid was considered and built out** (see the git
history around 2026-08-21 -- `Dockerfile`, `docker-compose.yml`,
`.env.example`, an nginx config example) as a way to get a real
persistent disk instead of working around Render's ephemeral one. That
plan is currently on hold: Josh's Starlink failover doesn't play nicely
with the reverse-proxy setup that'd be needed to expose a self-hosted
container reliably. Render stays the deployment target for now. The
Docker files are left in the repo in case that changes -- they're inert
otherwise (nothing about the Render deployment depends on them).

If this comes back up later, the previously-written steps (get the repo
onto the box, copy `.env.example` to `.env`, check the volume path in
`docker-compose.yml`, `docker compose up -d --build`, reverse proxy
`qsl.kn0ble.com` to the published port using
`nginx-qsl.kn0ble.com.conf.example` as a starting point, then repoint
DNS) are all still valid -- nothing about them changed, they're just
not the active plan right now. One thing that *did* change since they
were written: there's no need to migrate any QSL Photo Map data off of
Render before a future cutover -- it already lives in S3 (see below),
not in Render's SQLite disk, so it'd already be there regardless of
where the app itself runs.

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
- `/admin/log` -- sync your QRZ Logbook or upload an ADIF log so the
  upload form below can auto-fill QSO details (date/band/mode/frequency/
  RST) for a callsign instead of typing them by hand. Safe to mix and
  repeat -- duplicates are skipped.
- `/admin/photomap/upload` -- pick a callsign, attach one or more
  front-of-card photos, fill in (or accept the auto-filled) QSO details,
  save. A callsign's map location (country/state/grid/lat-lon) is looked
  up from QRZ the first time it's uploaded and cached from then on --
  requires being logged in with your QRZ credentials (same login as the
  rest of the app) the first time a given callsign is used.

Photos are stored in a private S3 bucket and relayed straight through the
Flask app both ways: on upload (a normal multipart form post, not a
direct-to-S3 presigned upload) and on view (`/photomap/image/<key>`
streams the object's bytes through Flask rather than handing the
browser a presigned S3 URL). Both choices avoid needing any S3 CORS
configuration. The presigned-URL approach was tried first for viewing
and dropped after it ran into a persistent, never-fully-explained
`SignatureDoesNotMatch` in production -- see `qsl-tracker-status.md` in
the project for the full debugging trail if this ever needs revisiting.

**All of this data lives in S3, not in Render's SQLite disk** --
`photomap_store.py` keeps every callsign location, photo card, and
imported QSO in one JSON object at `photocards/_index.json` in the same
bucket (deliberately inside the `photocards/` prefix the IAM policy
below already covers, so this needed no extra AWS setup). This is the
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
        "Action": ["s3:PutObject", "s3:GetObject"],
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
  (swap in the real bucket name; no account-wide access needed). The
  `ListBucket` statement is scoped to just the `photocards/*` prefix
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
- [x] Print-ready mailing labels -- the Raw Address Ripper (replaced the
      old Dashboard + CSV export on 2026-10-04)
- [x] QRZ Logbook sync for QSO labels (AFTERLOGID paging, 2026-10-04)
- [x] "Request a QSL Card" -- public form (embedded on kn0ble.com) for
      visitors to request a card back, emailed to Josh via Gmail SMTP
      (see "Email notifications" above)
- [x] QSL Photo Map -- public `/photomap` (world map of scanned cards)
      plus an admin-only upload/import flow; see "QSL Photo Map" above

## License

MIT -- see [LICENSE](LICENSE).
