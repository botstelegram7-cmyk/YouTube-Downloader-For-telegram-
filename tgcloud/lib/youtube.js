const YOUTUBE_HOSTS = new Set([
  'youtube.com',
  'www.youtube.com',
  'm.youtube.com',
  'music.youtube.com',
  'youtu.be',
  'www.youtu.be'
]);

export const QUALITIES = Object.freeze(['360p', '480p', '720p', '1080p', '1440p', '2160p']);

export function normalizeYouTubeUrl(value) {
  if (typeof value !== 'string' || !value.trim()) return null;

  try {
    const input = value.trim();
    const candidate = /^[a-z][a-z\d+.-]*:\/\//i.test(input) ? input : `https://${input}`;
    const url = new URL(candidate);

    if (!['http:', 'https:'].includes(url.protocol)) return null;
    if (!YOUTUBE_HOSTS.has(url.hostname.toLowerCase())) return null;
    if (url.username || url.password) return null;
    if (!url.pathname.split('/').some(Boolean)) return null;

    return url.href;
  } catch {
    return null;
  }
}

export function parseCommand(text) {
  const trimmed = typeof text === 'string' ? text.trim() : '';
  if (!trimmed) return { command: '', args: [] };

  const [rawCommand = '', ...args] = trimmed.split(/\s+/);
  return { command: rawCommand.split('@')[0].toLowerCase(), args };
}
