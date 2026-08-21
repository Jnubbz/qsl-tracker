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
  session key and are never stored -- only the session key lives in the
  cookie for the rest of the visit.
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
- Results are stored in SQLite, scoped to your session, and purged
  automatically after 24 hours.

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
re-search), but if that ever matters, add a persistent disk to
`render.yaml` (Starter plan or above; the free plan doesn't support
disks):

```yaml
    disk:
      name: qsl-data
      mountPath: /opt/render/project/src/instance
      sizeGB: 1
```

## Project status

Early / brainstorming-to-working-prototype stage. Next up:

- [x] Deploy a public instance -- `render.yaml` added; run through Render
      dashboard to go live (see above)
- [x] Rate-limit QRZ lookups more carefully -- uploads now pace requests,
      cap themselves to a time budget, and back off on repeated failures
      (see the note above)
- [ ] Consider a server-side session store instead of the signed cookie,
      since it currently holds the QRZ session key
- [ ] Export results (CSV) for printing mailing labels

## License

MIT -- see [LICENSE](LICENSE).
