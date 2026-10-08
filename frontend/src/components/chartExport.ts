/* Download the result chart as an image.
 *
 * The exported file is a standalone SVG built here, not a screenshot: the page's stylesheet does
 * not travel with a serialized node, and the bar chart is HTML rather than SVG, so neither could be
 * saved by serializing the DOM alone. Building the document means one code path for both chart
 * kinds, and an image that carries its own title and legend instead of depending on the page
 * around it. PNG is the same document rasterized through a canvas - no extra dependency.
 */

export interface ChartImage {
  /** Plot contents, already positioned inside a `width` x `height` box. */
  body: string;
  width: number;
  height: number;
}

export const EXPORT_FONT =
  "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif";

/** Escapes text for both element content and attribute values. */
export function esc(text: string): string {
  return String(text).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" })[c]!);
}

const TITLE_H = 34;
const PAD = 14;

/** Wrap plot contents in a standalone, self-styled SVG document with a title and optional caption. */
export function svgDocument(image: ChartImage, title: string, caption: string): string {
  const width = image.width + PAD * 2;
  const height = image.height + TITLE_H + PAD + (caption ? 18 : 0);
  return [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" `
      + `viewBox="0 0 ${width} ${height}" font-family="${EXPORT_FONT}">`,
    `<style>`,
    `.chart-grid{stroke:#e9ece9;stroke-width:1}`,
    `.chart-base{stroke:#c6cec8;stroke-width:1}`,
    `.chart-axis{fill:#6d7b74;font-size:10.5px}`,
    `.chart-end{fill:#1d2823;font-size:11px;font-weight:600}`,
    `.x-title{fill:#1d2823;font-size:13px;font-weight:600}`,
    `.x-note{fill:#6d7b74;font-size:10.5px}`,
    `.x-label{fill:#1d2823;font-size:11.5px}`,
    `.x-value{fill:#1d2823;font-size:11px}`,
    `</style>`,
    `<rect width="${width}" height="${height}" fill="#ffffff"/>`,
    `<text class="x-title" x="${PAD}" y="21">${esc(title)}</text>`,
    `<g transform="translate(${PAD},${TITLE_H})">${image.body}</g>`,
    caption ? `<text class="x-note" x="${PAD}" y="${height - 7}">${esc(caption)}</text>` : "",
    `</svg>`,
  ].join("");
}

function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoked on the next frame: Safari cancels the download if the URL dies synchronously.
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

export function saveSvg(svg: string, name: string): void {
  saveBlob(new Blob([svg], { type: "image/svg+xml;charset=utf-8" }), `${name}.svg`);
}

/** Rasterize the SVG document through a canvas. `scale` 2 keeps it sharp on a HiDPI screen. */
export async function savePng(svg: string, name: string, scale = 2): Promise<void> {
  const size = /width="(\d+)" height="(\d+)"/.exec(svg);
  const width = Number(size?.[1] ?? 800);
  const height = Number(size?.[2] ?? 320);
  // A same-origin blob URL does not taint the canvas, so toBlob() stays allowed.
  const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml;charset=utf-8" }));
  try {
    const image = await new Promise<HTMLImageElement>((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("The chart image could not be rendered."));
      img.src = url;
    });
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("This browser cannot render the chart to PNG.");
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!blob) throw new Error("This browser cannot render the chart to PNG.");
    saveBlob(blob, `${name}.png`);
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** "Users by app" -> "users-by-app", so the saved file says what it holds. */
export function fileName(parts: string[]): string {
  const slug = parts.join("-").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return (slug || "chart").slice(0, 80);
}
