/* Tiny MIDI file player for the /oot song buttons.
 *
 * Browsers can't play .mid in <audio>: a MIDI file is a list of notes,
 * not sound. This parses Standard MIDI Files (format 0/1, tempo changes,
 * running status) and plays the notes with Web Audio using a soft
 * sine-ish tone, which happens to sound close to an ocarina.
 * Channel 10 (drums) is skipped. No dependencies.
 *
 * The same file lives in qsl-tracker (static/oot/midi-player.js) for the
 * setup wizard's preview. Keep the two copies identical.
 *
 *   MidiPlayer.isMidi(name)            -> true for .mid / .midi
 *   MidiPlayer.unlock()                -> call inside a click (iOS)
 *   MidiPlayer.load(url)               -> Promise<song>
 *   MidiPlayer.play(song, onEnded)     -> { stop() }
 */
(function () {
  "use strict";

  let ctx = null;

  function audioCtx() {
    if (!ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      ctx = new AC();
    }
    return ctx;
  }

  function unlock() {
    const c = audioCtx();
    if (c.state === "suspended") c.resume();
    return c;
  }

  function isMidi(name) {
    return /\.midi?(\?|$)/i.test(name || "");
  }

  // ---- parsing -------------------------------------------------------

  function parse(buf) {
    const d = new DataView(buf);
    let p = 0;
    const str = (n) => { let s = ""; for (let i = 0; i < n; i++) s += String.fromCharCode(d.getUint8(p + i)); return s; };
    const u32 = () => { const v = d.getUint32(p); p += 4; return v; };
    const u16 = () => { const v = d.getUint16(p); p += 2; return v; };

    if (str(4) !== "MThd") throw new Error("not a MIDI file");
    p += 4;
    const hdrLen = u32();
    const hdrStart = p;
    u16(); // format
    const nTracks = u16();
    const division = u16();
    p = hdrStart + hdrLen;
    if (division & 0x8000) throw new Error("SMPTE-timed MIDI isn't supported");
    const tpq = division;

    const tempos = [];   // {tick, uspq}
    const raw = [];      // {tick, on, ch, note, vel}

    for (let t = 0; t < nTracks && p + 8 <= d.byteLength; t++) {
      const id = str(4); p += 4;
      const len = u32();
      const end = Math.min(p + len, d.byteLength);
      if (id !== "MTrk") { p = end; continue; }
      let tick = 0, status = 0;
      const vlq = () => { let v = 0, b; do { b = d.getUint8(p++); v = (v << 7) | (b & 0x7f); } while (b & 0x80 && p < end); return v; };
      while (p < end) {
        tick += vlq();
        let b = d.getUint8(p);
        if (b & 0x80) { status = b; p++; } // else running status
        if (status === 0xff) {
          const type = d.getUint8(p++);
          const l = vlq();
          if (type === 0x51 && l === 3) {
            tempos.push({ tick, uspq: (d.getUint8(p) << 16) | (d.getUint8(p + 1) << 8) | d.getUint8(p + 2) });
          }
          p += l;
          if (type === 0x2f) break;
        } else if (status === 0xf0 || status === 0xf7) {
          p += vlq();
        } else {
          const kind = status & 0xf0, ch = status & 0x0f;
          const a = d.getUint8(p++);
          const two = kind !== 0xc0 && kind !== 0xd0;
          const v = two ? d.getUint8(p++) : 0;
          if (kind === 0x90 && v > 0) raw.push({ tick, on: true, ch, note: a, vel: v });
          else if (kind === 0x80 || (kind === 0x90 && v === 0)) raw.push({ tick, on: false, ch, note: a, vel: 0 });
        }
      }
      p = end;
    }

    // Ticks -> seconds through the tempo map (default 120 bpm).
    tempos.sort((x, y) => x.tick - y.tick);
    const map = [{ tick: 0, sec: 0, uspq: 500000 }];
    for (const tm of tempos) {
      const last = map[map.length - 1];
      const sec = last.sec + ((tm.tick - last.tick) * last.uspq) / tpq / 1e6;
      if (tm.tick === last.tick) last.uspq = tm.uspq;
      else map.push({ tick: tm.tick, sec, uspq: tm.uspq });
    }
    const toSec = (tick) => {
      let i = map.length - 1;
      while (i > 0 && map[i].tick > tick) i--;
      const m = map[i];
      return m.sec + ((tick - m.tick) * m.uspq) / tpq / 1e6;
    };

    // Pair note-ons with note-offs.
    raw.sort((x, y) => x.tick - y.tick || (x.on === y.on ? 0 : x.on ? 1 : -1));
    const open = new Map();
    const notes = [];
    for (const e of raw) {
      if (e.ch === 9) continue; // drums
      const key = e.ch * 128 + e.note;
      if (e.on) {
        if (!open.has(key)) open.set(key, []);
        open.get(key).push(e);
      } else {
        const stack = open.get(key);
        const on = stack && stack.shift();
        if (on) {
          const t = toSec(on.tick);
          notes.push({ t, dur: Math.max(0.05, toSec(e.tick) - t), midi: e.note, vel: on.vel / 127 });
        }
      }
    }
    for (const stack of open.values()) {
      for (const on of stack) notes.push({ t: toSec(on.tick), dur: 0.5, midi: on.note, vel: on.vel / 127 });
    }
    notes.sort((x, y) => x.t - y.t);
    const duration = notes.reduce((m, n) => Math.max(m, n.t + n.dur), 0);
    return { notes, duration };
  }

  function load(url) {
    return fetch(url).then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.arrayBuffer();
    }).then(parse);
  }

  // ---- playback ------------------------------------------------------

  function play(song, onEnded) {
    const c = unlock();
    const master = c.createGain();
    master.gain.value = 0.22;
    master.connect(c.destination);
    const start = c.currentTime + 0.08;
    const nodes = [];

    for (const n of song.notes) {
      const t0 = start + n.t;
      const t1 = t0 + n.dur;
      const freq = 440 * Math.pow(2, (n.midi - 69) / 12);
      const peak = 0.25 + 0.75 * n.vel;

      const g = c.createGain();
      g.gain.setValueAtTime(0, t0);
      g.gain.linearRampToValueAtTime(peak, t0 + 0.025);
      g.gain.setTargetAtTime(peak * 0.8, t0 + 0.025, 0.15);
      g.gain.setValueAtTime(peak * 0.8, Math.max(t0 + 0.03, t1 - 0.001));
      g.gain.linearRampToValueAtTime(0, t1 + 0.09);
      g.connect(master);

      const o = c.createOscillator();
      o.type = "sine";
      o.frequency.value = freq;
      o.connect(g);
      o.start(t0);
      o.stop(t1 + 0.12);

      // A little breathy second harmonic and a slow vibrato.
      const o2 = c.createOscillator();
      o2.type = "sine";
      o2.frequency.value = freq * 2;
      const g2 = c.createGain();
      g2.gain.value = 0.08;
      o2.connect(g2).connect(g);
      o2.start(t0);
      o2.stop(t1 + 0.12);

      const lfo = c.createOscillator();
      const depth = c.createGain();
      lfo.frequency.value = 5;
      depth.gain.value = freq * 0.004;
      lfo.connect(depth);
      depth.connect(o.frequency);
      lfo.start(t0);
      lfo.stop(t1 + 0.12);

      nodes.push(o, o2, lfo);
    }

    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      if (onEnded) onEnded();
    };
    const timer = setTimeout(finish, (song.duration + 0.4) * 1000);

    return {
      stop() {
        if (done) return;
        done = true;
        clearTimeout(timer);
        const now = c.currentTime;
        master.gain.cancelScheduledValues(now);
        master.gain.setValueAtTime(master.gain.value, now);
        master.gain.linearRampToValueAtTime(0, now + 0.05);
        for (const node of nodes) { try { node.stop(now + 0.06); } catch (e) { /* not started yet */ } }
        setTimeout(() => master.disconnect(), 200);
      },
    };
  }

  window.MidiPlayer = { isMidi, unlock, load, play, parse };
})();
