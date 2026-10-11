// Serverless worker connection settings.
// This file is committed with safe placeholders so tgcloud can resolve the import
// and compile the handler on a fresh clone. Replace both values before enabling
// downloads, then deploy with: npx tgcloud push tgcloud/lib/config.js
//
// IMPORTANT: never commit real values to GitHub. This file is a deploy-time config
// module because the Serverless handler runtime does not read this repository's
// .env file automatically.
export const WORKER_URL = '';
export const WORKER_SECRET = '';
