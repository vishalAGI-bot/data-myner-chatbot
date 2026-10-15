/* The /ask page never shows the technical thinking steps (they name the model, tables and queries).
 * These turn the same steps into plain words, so /ask still shows what is happening and what was done. */

import type { ThinkingStep } from "./api/journey";

/** The step in progress, in plain words. */
export function plainProgress(step?: ThinkingStep): string {
  if (!step) return "Reading your question…";
  const question = /^Question (\d+)\b/.exec(step.text)?.[1];
  switch (step.stage) {
    case "understand":
      return "Reading your question…";
    case "plan":
      return question ? `Planning question ${question}…` : "Working out how to answer…";
    case "query": {
      if (question) return `Answering question ${question}…`;
      const by = /^Scorecard by (.+)$/.exec(step.text)?.[1];
      return by ? `Comparing apps by ${by.toLowerCase()}…` : "Reading the data…";
    }
    case "verify":
      return step.ok ? "Checking the figures…" : "Double-checking a result…";
    case "write":
      return "Writing the answer…";
    case "stop":
      return "Finishing with what is ready…";
    default:
      return "Working on it…";
  }
}

/** What was done for an answer, in plain words (for "How this was calculated" on /ask). */
export function plainSummary(steps: ThinkingStep[], timedOut: boolean): string[] {
  // "Gender: 6 rows" reports a read, not a check of the figures.
  const counted = steps.filter((s) => !(s.stage === "verify" && /: \d+ rows\b/.test(s.text)));
  const count = (stage: string, ok?: boolean) =>
    counted.filter((s) => s.stage === stage && (ok === undefined || s.ok === ok)).length;
  const times = (n: number) => `${n} time${n === 1 ? "" : "s"}`;
  const lines: string[] = [];
  if (count("understand")) lines.push("Read your question and restated it in full (shown under “Understood as”).");
  if (count("query")) lines.push(`Read the data ${times(count("query"))}; every figure comes from those reads.`);
  if (count("verify", true)) lines.push(`Checked the figures against the data ${times(count("verify", true))}.`);
  const doubts = count("verify", false);
  if (doubts) lines.push(`${doubts} check${doubts === 1 ? "" : "s"} raised a doubt or needed a second try; `
    + "anything still in doubt is marked “Check before relying on this”.");
  if (timedOut) lines.push("Stopped at the time limit; the answer shows what was ready.");
  return lines;
}
