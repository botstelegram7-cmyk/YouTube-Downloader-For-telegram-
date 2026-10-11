import { table, integer, text } from 'sdk/db';

export const userPreferences = table('user_preferences', {
  userId: integer('user_id').primaryKey(),
  quality: text('quality').notNull().default('720p'),
});
