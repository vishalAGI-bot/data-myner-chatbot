import { useEffect, useMemo, useRef, useState } from "react";

/* The opening screen of DataMyner Intelligence (the /ask page).
 *
 * It holds for MIN_MS so the product introduces itself, but it is not only a delay: opening the SSH
 * tunnel, connecting to PostgreSQL and reading the catalogue genuinely take seconds, and this is the
 * window that covers them. So it waits for the longer of the two - the animation never cuts off a
 * connection that is still being made, and a connection that finishes early still gets its full
 * introduction. MAX_MS stops it hanging forever when the database cannot be reached; the chat's own
 * "not available right now" message explains that better than a spinner can.
 */
const MIN_MS = 10_000;
const MAX_MS = 25_000;
const TICK_MS = 100;

/** What is actually happening behind the screen, in the order it happens. */
const STAGES = [
  { at: 0.00, label: "Starting DataMyner Intelligence" },
  { at: 0.16, label: "Opening a secure read-only connection" },
  { at: 0.38, label: "Reading the data catalogue" },
  { at: 0.58, label: "Checking data quality" },
  { at: 0.76, label: "Preparing the vetted analyses" },
  { at: 0.92, label: "Almost ready" },
];

/** Nodes on a ring plus a hub: a small network that wakes up as the progress passes each one. */
const NODES = Array.from({ length: 10 }, (_, i) => {
  const angle = (i / 10) * Math.PI * 2 - Math.PI / 2;
  return { x: 110 + Math.cos(angle) * 74, y: 110 + Math.sin(angle) * 74, delay: i * 0.18 };
});

export default function SplashScreen({ ready, onDone }: { ready: boolean; onDone: () => void }) {
  const [elapsed, setElapsed] = useState(0);
  const started = useRef(Date.now());
  const done = useRef(false);
  // Read through a ref so the timer is created once and still sees the latest connection state.
  const readyRef = useRef(ready);
  readyRef.current = ready;

  useEffect(() => {
    const timer = window.setInterval(() => {
      const ms = Date.now() - started.current;
      setElapsed(ms);
      if (done.current) return;
      if ((ms >= MIN_MS && readyRef.current) || ms >= MAX_MS) {
        done.current = true;
        window.clearInterval(timer);
        onDone();
      }
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, [onDone]);

  const progress = Math.min(1, elapsed / MIN_MS);
  const waiting = elapsed >= MIN_MS && !ready;
  const stage = useMemo(() => {
    if (waiting) return "Still connecting to your data…";
    return [...STAGES].reverse().find((s) => progress >= s.at)?.label ?? STAGES[0].label;
  }, [progress, waiting]);

  return (
    <div className="splash" role="status" aria-live="polite" aria-label="Starting DataMyner Intelligence">
      <div className="splash-inner">
        <svg className="splash-art" viewBox="0 0 220 220" aria-hidden="true">
          <defs>
            <linearGradient id="splash-edge" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="#5eead4" />
              <stop offset="50%" stopColor="#a3e635" />
              <stop offset="100%" stopColor="#c4b5fd" />
            </linearGradient>
            <radialGradient id="splash-hub">
              <stop offset="0%" stopColor="#ffffff" />
              <stop offset="100%" stopColor="#5eead4" />
            </radialGradient>
          </defs>
          <circle className="splash-orbit" cx="110" cy="110" r="94" />
          <circle className="splash-sweep" cx="110" cy="110" r="94" />
          {/* A steady web, with a travelling pulse over it: the structure stays readable at any
              instant instead of existing only while a dash happens to be passing. */}
          {NODES.map((n, i) => (
            <line key={`b${i}`} className="splash-edge-base" x1="110" y1="110" x2={n.x} y2={n.y} />
          ))}
          {NODES.map((n, i) => (
            <line key={`e${i}`} className="splash-edge" x1="110" y1="110" x2={n.x} y2={n.y}
                  stroke="url(#splash-edge)" style={{ animationDelay: `${n.delay}s` }} />
          ))}
          {NODES.map((n, i) => (
            <circle key={`n${i}`} className={`splash-node${progress > (i + 1) / 12 ? " on" : ""}`}
                    cx={n.x} cy={n.y} r="6" style={{ animationDelay: `${n.delay}s` }} />
          ))}
          <circle className="splash-hub" cx="110" cy="110" r="22" fill="url(#splash-hub)" />
          <text className="splash-hub-text" x="110" y="118" textAnchor="middle">D</text>
        </svg>

        <h1 className="splash-name">DataMyner Intelligence</h1>
        <p className="splash-company">Market Xcel Data Matrix Pvt Ltd</p>

        <div className="splash-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100}
             aria-valuenow={Math.round(progress * 100)}>
          <span style={{ width: `${progress * 100}%` }} />
        </div>
        <p className={`splash-stage${waiting ? " waiting" : ""}`}>{stage}</p>
      </div>
    </div>
  );
}
