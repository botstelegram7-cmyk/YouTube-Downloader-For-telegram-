/**
 * Registers the `sdk` stub loader. Usage:
 *
 *   node --import ./tests/stubs/register.mjs --test tests/
 *
 * (The npm `test` script already does this.)
 */

import { register } from 'node:module';

register('./loader.mjs', import.meta.url);
