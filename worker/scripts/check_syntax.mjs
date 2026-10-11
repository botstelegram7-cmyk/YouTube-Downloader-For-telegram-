#!/usr/bin/env node
/**
 * Dependency-free syntax + hygiene check for the worker.
 *
 *   node scripts/check_syntax.mjs
 *
 * Parses every .js file with `node --check` (so it also fails on files that are never
 * imported) and fails on a few conventions we care about in review.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const roots = ['src', 'test', 'scripts'];
const failures = [];

function walk(dir) {
  let entries = [];
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules') continue;
      walk(full);
      continue;
    }
    if (entry.isFile() && entry.name.endsWith('.js')) check(full);
  }
}

function check(file) {
  const relative = path.relative(root, file);
  const result = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8' });
  if (result.status !== 0) {
    failures.push(`syntax error in ${relative}: ${(result.stderr || '').split('\n').slice(0, 4).join(' ')}`);
    return;
  }

  const source = readFileSync(file, 'utf8');
  const lines = source.split('\n');

  lines.forEach((line, index) => {
    if (/[ \t]+$/.test(line)) failures.push(`trailing whitespace at ${relative}:${index + 1}`);
    if (line.includes('\t')) failures.push(`tab character at ${relative}:${index + 1}`);
  });

  if (source.length > 0 && !source.endsWith('\n')) failures.push(`${relative} must end with a newline`);

  // `import ... from 'node:fs'` style is required for builtins in this codebase.
  const bareBuiltin = /from\s+'((fs|path|os|http|https|util|crypto|stream|url|child_process|events))'/.exec(source);
  if (bareBuiltin) failures.push(`${relative} should import the builtin as 'node:${bareBuiltin[1]}'`);
}

for (const dir of roots) walk(path.join(root, dir));

// Files in the repo root that must stay parseable too.
for (const name of ['package.json']) {
  try {
    JSON.parse(readFileSync(path.join(root, name), 'utf8'));
  } catch (error) {
    failures.push(`${name} is not valid JSON: ${error.message}`);
  }
}

if (failures.length > 0) {
  console.error(`Worker checks failed:\n- ${failures.join('\n- ')}`);
  process.exit(1);
}

const stats = statSync(path.join(root, 'src'));
console.log(`Worker checks passed (src present: ${stats.isDirectory()}).`);
