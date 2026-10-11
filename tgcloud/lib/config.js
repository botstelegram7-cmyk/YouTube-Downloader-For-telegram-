/**
 * Non-secret configuration for the Serverless bot.
 *
 * This file is committed, so it must never hold a token or a secret. The worker URL
 * and shared secret live in the database and are set once with /setup — that keeps
 * credentials out of git and lets you rotate them without redeploying.
 *
 * Every value here can be overridden at runtime by a row in `bot_settings` with the
 * same lower-case name, so you can tune the bot from Telegram without a redeploy.
 */

export const CONFIG = Object.freeze({
  /**
   * Telegram user id that may run /setup, /stats and /unset.
   * Leave 0 and the first person to send /setup becomes the owner — convenient for a
   * personal bot. Set it to your numeric id for anything public.
   */
  OWNER_USER_ID: 0,

  /** Optional compile-time fallback used only when the database has no value. */
  WORKER_URL: '',
  WORKER_SECRET: '',

  /** Defaults applied to users who never run /quality. */
  DEFAULT_QUALITY: '720p',
  DEFAULT_MODE: 'video',

  /** Flood control. */
  COOLDOWN_SECONDS: 20,
  MAX_JOBS_PER_DAY: 20,

  /** How long to wait for the worker to accept a job (it answers immediately). */
  SUBMIT_TIMEOUT_MS: 12_000,
  SUBMIT_ATTEMPTS: 3,
  SUBMIT_BACKOFF_MS: [1_500, 4_000],

  /** Legal note shown on /start, /about and around every download. */
  DISCLAIMER:
    'Only download videos you own or are allowed to save. Respect copyright, YouTube’s terms and local law.'
});

/** Setting keys stored in the database. */
export const SETTING_KEYS = Object.freeze({
  WORKER_URL: 'worker_url',
  WORKER_SECRET: 'worker_secret',
  OWNER_USER_ID: 'owner_user_id',
  DISABLED: 'disabled'
});
