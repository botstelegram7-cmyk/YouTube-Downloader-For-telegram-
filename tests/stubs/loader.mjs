/**
 * Module resolution hook: every `sdk` / `sdk/*` import inside the handlers is
 * redirected to the local stub, which is what makes the Serverless code testable
 * outside Telegram.
 */

const SDK_URL = new URL('./sdk.js', import.meta.url).href;

export function resolve(specifier, context, nextResolve) {
  if (specifier === 'sdk' || specifier.startsWith('sdk/')) {
    return { url: SDK_URL, shortCircuit: true, format: 'module' };
  }
  return nextResolve(specifier, context);
}
