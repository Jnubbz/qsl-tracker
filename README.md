# QSL Tracker

A small Flask app for hams: look up callsigns (one at a time, or in bulk
from an ADIF log) against [QRZ.com](https://www.qrz.com)'s XML Logbook
Data API, and keep track of which operators want a **direct** paper QSL
card -- so you know who to actually mail a card to.

Part of the [KN0BLE.com](https://kn0ble.com) site, callsign KN0BLE.

## How it works

- No accounts. Each visitor gets an anonymous, cookie-based session that
  keeps their results separate from everyone else's.
- Visitors log in with their **own** QRZ XML subscriber username and
  password. Those credentials are used once to fetch a short-lived QRZ
  session key and are never stored -- the key itself lives server-side
  in SQLite, keyed by the anonymous session id; the cookie only ever
  holds that id, never the key.
- Look up a single callsign, or upload an `.adi` / `.adif` log file to
  look up every callsign in it (capped at 200 per upload). Uploads pace
  their QRZ lookups with a short delay between each, and will stop
  early -- with a clear message about how many were completed -- if
  either the request is running long enough to risk a host-level timeout
  or QRZ starts erroring repeatedly in a row. Re-uploading the same file
  picks up the rest (it re-fetches everything, not just what's missing,
  so it's a little wasteful on a very large log, but simple and correct).
- A mailing address is only kept for operators who look like they want a
  direct card: an address is on file, QRZ's `mqsl` field isn't explicitly
  set to "no" (most operators never set it either way, so a blank `mqsl`
  doesn't disqualify them), and the `qslmgr` text doesn't name an actual
  manager to route through. That last one is a free-text field operators
  use two different ways -- naming a real manager ("via N0XYZ") or just
  listing which methods *they* accept ("Direct, LOTW, QRZ.") -- so a
  mention of "direct" in that text counts as a positive signal rather
  than an exclusion, unless it's negated ("no direct"). See the comment
  in `qrz.py` if you want to tune that rule further. Everyone else shows
  up in the list with `not stored` in the address column.
- A toggle switches between "all looked-up contacts" and "direct QSL
  only".
- "Export CSV" downloads every direct-QSL contact with an address on
  file as two columns: callsign, and a single "Mailing Label" cell with
  the name and full address (street, city/state/zip, country) on their
  own lines inside that one cell -- select it, paste, and it's a
  complete label, no reassembling separate columns. Independent of
  whichever table filter is currently active, since a mailing list only
  makes sense for contacts with an address. Clicking the link shows a
  one-time heads-up that those cells have line breaks baked in, since
  most spreadsheet apps don't auto-expand row height to show them --
  turning on "Wrap Text" (or widening the row) reveals the full address.
- Results are stored in SQLite, scoped to your session, and purged
  automatically after 24 hours.
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
- `/admin/photomap/import-adif` -- upload your own ADIF log so the
  upload form below can auto-fill QSO details (date/band/mode/frequency/
  RST) for a callsign instead of typing them by hand. Safe to re-upload
  the same or an overlapping log -- duplicates are skipped. This never
  touches QRZ; it only reads your log file.
- `/admin/photomap/upload` -- pick a callsign, attach one or more
  front-of-card photos, fill in (or accept the auto-filled) QSO details,
  save. A callsign's map location (country/state/grid/lat-lon) is looked
  up from QRZ the first time it's uploaded and cached from then on --
  requires being logged in with your QRZ credentials (same login as the
  rest of the app) the first time a given callsign is used.

Photos are stored in a private S3 bucket and relayed straight through the
Flask app on upload (a normal multipart form post, not a direct-to-S3
presigned upload) -- simple, and avoids needing any S3 CORS
configuration. The public map page never gets a permanent link to a
photo; it gets a short-lived presigned GET URL, regenerated every time
the page loads.

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
    "Statement": [{
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::kn0ble-qsl-.../photocards/*"
    }]
  }
  ```
  (swap in the real bucket name; no `s3:ListBucket` or account-wide
  access needed).
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
- [x] Export results (CSV) for printing mailing labels -- "Export CSV"
      link on the dashboard, always the direct-QSL contacts with an
      address on file regardless of which table filter is active; each
      row's address cell is one paste-ready label block (see above)
- [x] "Request a QSL Card" -- public form (embedded on kn0ble.com) for
      visitors to request a card back, emailed to Josh via Gmail SMTP
      (see "Email notifications" above)
- [x] QSL Photo Map -- public `/photomap` (world map of scanned cards)
      plus an admin-only upload/import flow; see "QSL Photo Map" above

## License

MIT -- see [LICENSE](LICENSE).
