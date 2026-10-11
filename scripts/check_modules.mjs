#!/usr/bin/env node
/**
 * Validates the deployable Serverless project layout.
 *
 * The platform only deploys `tgcloud/`, and it has stricter rules than Node does:
 *   - every source file under tgcloud/ is a module, and module names must be
 *     identifier-like (letters, digits, `_`, not starting with a digit), so a file
 *     such as `config.example.js` breaks a deploy;
 *   - `handlers/` and `endpoints/` are flat — one level, no subdirectories;
 *   - relative imports must carry the `.js` extension;
 *   - the runtime is a V8 sandbox with **no node builtins and no npm packages**, so
 *     importing `node:fs` or `axios` there fails at deploy or at run time.
 *
 * Run it directly (`npm run check:modules`) or from the test suite.
 */

import { readdirSync, readFileSync, existsSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const MODULE_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*\.js$/;
const ALLOWED_SDK_SPECIFIERS = /^sdk(\/[\w./-]+)?$/;
const NODE_BUILTIN = /^node:/;
const FLAT_DIRECTORIES = ['handlers', 'endpoints'];

/**
 * @param {string} root Repository root.
 * @returns {{ failures: string[], checked: number, files: string[] }}
 */
export function checkModules(root) {
  const moduleRoot = path.join(root, 'tgcloud');
  const failures = [];
  const files = [];

  if (!existsSync(moduleRoot) || !statSync(moduleRoot).isDirectory()) {
    return { failures: ['tgcloud/ directory is missing'], checked: 0, files: [] };
  }

  if (!existsSync(path.join(moduleRoot, 'schema.js'))) {
    failures.push('tgcloud/schema.js is missing (the database schema is a required module)');
  }

  for (const directory of FLAT_DIRECTORIES) {
    const full = path.join(moduleRoot, directory);
    if (!existsSync(full)) continue;
    for (const entry of readdirSync(full, { withFileTypes: true })) {
      if (entry.isDirectory()) {
        failures.push(`tgcloud/${directory}/ must be flat, but contains the directory "${entry.name}"`);
      }
    }
  }

  walk(moduleRoot);

  return { failures, checked: files.length, files };

  function walk(directory) {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const full = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        walk(full);
        continue;
      }
      if (!entry.isFile()) continue;

      const relative = path.relative(root, full);
      files.push(relative);

      if (!MODULE_NAME_PATTERN.test(entry.name)) {
        failures.push(`invalid module filename: ${relative} (expected identifier.js)`);
        continue;
      }

      const source = readFileSync(full, 'utf8');

      for (const specifier of readSpecifiers(source)) {
        if (NODE_BUILTIN.test(specifier)) {
          failures.push(`${relative} imports ${specifier}, but the runtime has no node builtins`);
          continue;
        }
        if (specifier.startsWith('.')) {
          if (!specifier.endsWith('.js')) {
            failures.push(`${relative} must import "${specifier}" with an explicit .js extension`);
            continue;
          }
          const resolved = path.resolve(path.dirname(full), specifier);
          if (!existsSync(resolved)) failures.push(`${relative} imports a missing file: ${specifier}`);
          continue;
        }
        if (!ALLOWED_SDK_SPECIFIERS.test(specifier)) {
          failures.push(`${relative} imports "${specifier}", but only "sdk" and relative modules exist in the runtime`);
        }
      }

      if (/\brequire\s*\(/.test(source)) {
        failures.push(`${relative} uses require(); the runtime is ESM only`);
      }
    }
  }
}

/** All static and dynamic import specifiers in a module. */
function readSpecifiers(source) {
  const specifiers = new Set();
  const patterns = [
    /(?:^|[\s;])import\s+[^'"]*?from\s+['"]([^'"]+)['"]/g,
    /(?:^|[\s;])import\s+['"]([^'"]+)['"]/g,
    /import\s*\(\s*['"]([^'"]+)['"]\s*\)/g,
    /export\s+[^'"]*?from\s+['"]([^'"]+)['"]/g
  ];
  for (const pattern of patterns) {
    for (const match of source.matchAll(pattern)) specifiers.add(match[1]);
  }
  return specifiers;
}

function main() {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const { failures, checked } = checkModules(root);

  if (failures.length > 0) {
    console.error(`Serverless module check failed:\n- ${failures.join('\n- ')}`);
    process.exit(1);
  }

  console.log(`Serverless module check passed (${checked} modules under tgcloud/).`);
}

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  main();
}

export { readSpecifiers };
