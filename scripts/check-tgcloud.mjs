import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const moduleRoot = path.join(root, 'tgcloud');
const failures = [];

function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walk(full);
      continue;
    }
    if (!entry.isFile()) continue;

    // Serverless treats every source file under tgcloud/ as a module. Names must
    // be identifiers; names such as config.example.js are invalid module names.
    if (!/^[A-Za-z_][A-Za-z0-9_]*\.js$/.test(entry.name)) {
      failures.push(`Invalid Serverless module filename: ${path.relative(root, full)}`);
    }

    if (!entry.name.endsWith('.js')) continue;
    const source = fs.readFileSync(full, 'utf8');
    const imports = source.matchAll(/(?:from\s*|import\s*)['"]([^'"]+)['"]/g);
    for (const match of imports) {
      const specifier = match[1];
      if (!specifier.startsWith('.')) continue;
      const resolved = path.resolve(path.dirname(full), specifier);
      if (!fs.existsSync(resolved) || !fs.statSync(resolved).isFile()) {
        failures.push(`Missing relative import in ${path.relative(root, full)}: ${specifier}`);
      }
    }
  }
}

walk(moduleRoot);
if (failures.length) {
  console.error('Telegram Serverless structure check failed:\n- ' + failures.join('\n- '));
  process.exit(1);
}
console.log('Telegram Serverless structure check passed.');
