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
  look up every callsign in it (capped at 200 per upload).
- A mailing address is only kept for operators who look like they want a
  direct card (QRZ's `mqsl` flag set and no QSL manager on file --
  see the comment in `qrz.py` if you want to tune that rule). Everyone
  else shows up in the list with `not stored` in the address column.
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

## Project status

Early / brainstorming-to-working-prototype stage. Next up:

- [ ] Deploy a public instance
- [ ] Rate-limit QRZ lookups more carefully (QRZ throttles heavy use)
- [ ] Consider a server-side session store instead of the signed cookie,
      since it currently holds the QRZ session key
- [ ] Export results (CSV) for printing mailing labels

## License

MIT -- see [LICENSE](LICENSE).
