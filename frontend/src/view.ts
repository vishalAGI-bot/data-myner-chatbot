/* Which of the two pages this browser is on.
 *
 * `/`     the working page: agent chooser, data source, connection settings, thinking controls,
 *         the SQL behind every answer.
 * `/ask`  the page for the people who just want the answer. No model, no agent, no SQL, no table
 *         names - the question, the figures, and what the question was taken to mean.
 *
 * A path rather than a flag so the two audiences get different links: someone handed /ask has no
 * control in the page that leads back to the working one. Serving it needs the SPA fallback that
 * `vite dev`, `vite preview` and the nginx `try_files $uri /index.html` line already provide.
 */

export const USER_PATH = "/ask";

/** True on the simplified page. Trailing slashes and a deploy sub-path are both tolerated. */
export function isUserView(pathname: string = window.location.pathname): boolean {
  return pathname.replace(/\/+$/, "").toLowerCase().endsWith(USER_PATH);
}
