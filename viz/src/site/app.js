/**
 * The one script every page loads (see src/site/router.js: the pages are one
 * document that swaps its content, so every page's styles are here).
 *
 * The order is the cascade the pages are written against: the Find and Query
 * explorer sheets before the shared ones, the others after. Each page sheet is
 * scoped to its page's body class.
 */
// Inter, Estuary's typeface, self-hosted: no font request leaves the site.
import '@fontsource-variable/inter';
import '../pages/fit.css';
import '../pages/queries.css';
import '../style.css';
import './site.css';
import '../pages/index.css';
import '../pages/method.css';
import '../pages/engine.css';
import '../pages/compare.css';
import { start } from './boot.js';

start();
