import test from 'node:test';
import assert from 'node:assert/strict';

import { DuplicateJobError, JobQueue, QueueFullError } from '../src/queue.js';

const deferred = () => {
  let resolve;
  const promise = new Promise((res) => {
    resolve = res;
  });
  return { promise, resolve };
};

function makeJob(overrides = {}) {
  return {
    id: overrides.id ?? `job-${Math.random().toString(36).slice(2, 10)}`,
    chatId: overrides.chatId ?? 1,
    userId: overrides.userId ?? 1,
    url: 'https://youtu.be/jNQXAC9IVRw',
    quality: '720p',
    mode: 'video',
    ...overrides
  };
}

test('runs up to `concurrency` jobs and queues the rest', async () => {
  const started = [];
  const release = deferred();
  const queue = new JobQueue({
    concurrency: 2,
    run: async (job) => {
      started.push(job.id);
      await release.promise;
    }
  });

  const first = queue.enqueue(makeJob({ id: 'a', chatId: 10, userId: 10 }));
  const second = queue.enqueue(makeJob({ id: 'b', chatId: 11, userId: 11 }));
  const third = queue.enqueue(makeJob({ id: 'c', chatId: 12, userId: 12 }));

  assert.equal(first.position, 0);
  assert.equal(second.position, 0);
  assert.equal(third.position, 1);
  assert.equal(queue.running, 2);
  assert.equal(queue.pending, 1);

  release.resolve();
  await queue.drain(2_000);

  assert.deepEqual(started.sort(), ['a', 'b', 'c']);
  assert.equal(queue.completed, 3);
  assert.equal(queue.stats().failed, 0);
});

test('one job per chat and per user', () => {
  const queue = new JobQueue({ concurrency: 2, run: async () => new Promise(() => {}) });
  queue.enqueue(makeJob({ id: 'x', chatId: 100, userId: 1 }));

  assert.throws(() => queue.enqueue(makeJob({ id: 'y', chatId: 100, userId: 2 })), DuplicateJobError);
  assert.throws(() => queue.enqueue(makeJob({ id: 'z', chatId: 200, userId: 1 })), DuplicateJobError);

  const other = queue.enqueue(makeJob({ id: 'w', chatId: 300, userId: 3 }));
  assert.ok(other.job.id === 'w');
});

test('the waiting list is bounded', () => {
  const queue = new JobQueue({ concurrency: 1, maxWaiting: 1, run: async () => new Promise(() => {}) });
  queue.enqueue(makeJob({ id: 'run', chatId: 1, userId: 1 }));
  queue.enqueue(makeJob({ id: 'wait', chatId: 2, userId: 2 }));
  assert.throws(() => queue.enqueue(makeJob({ id: 'overflow', chatId: 3, userId: 3 })), QueueFullError);
});

test('cancelling a queued job prevents it from ever running', async () => {
  const started = [];
  const release = deferred();
  const queue = new JobQueue({
    concurrency: 1,
    run: async (job) => {
      started.push(job.id);
      await release.promise;
      return job.signal.aborted ? 'cancelled' : undefined;
    }
  });

  queue.enqueue(makeJob({ id: 'first', chatId: 1, userId: 1 }));
  queue.enqueue(makeJob({ id: 'second', chatId: 2, userId: 2 }));

  assert.equal(queue.cancel('second'), 'cancelled');
  assert.equal(queue.cancel('does-not-exist'), 'not_found');
  assert.equal(queue.pending, 0);

  release.resolve();
  await queue.drain(2_000);

  assert.deepEqual(started, ['first']);
  assert.equal(queue.cancelled, 1);
});

test('cancelling a running job aborts its signal', async () => {
  let observed = false;
  const queue = new JobQueue({
    concurrency: 1,
    run: async (job) => {
      job.signal.addEventListener('abort', () => {
        observed = true;
      });
      await new Promise((resolve) => setTimeout(resolve, 50));
      return job.signal.aborted ? 'cancelled' : undefined;
    }
  });

  queue.enqueue(makeJob({ id: 'running', chatId: 1, userId: 1 }));
  assert.equal(queue.cancel('running', 'test'), 'cancelled');
  await queue.drain(2_000);

  assert.equal(observed, true);
  assert.equal(queue.cancelled, 1);
  assert.equal(queue.completed, 0);
});

test('a failing job is counted and does not stop the queue', async () => {
  const queue = new JobQueue({
    concurrency: 1,
    run: async (job) => {
      if (job.id === 'boom') throw new Error('kaboom');
    }
  });

  queue.enqueue(makeJob({ id: 'boom', chatId: 1, userId: 1 }));
  queue.enqueue(makeJob({ id: 'fine', chatId: 2, userId: 2 }));
  await queue.drain(2_000);

  assert.equal(queue.failed, 1);
  assert.equal(queue.completed, 1);
});

test('positionOf and findActiveByChat describe the queue accurately', () => {
  const queue = new JobQueue({ concurrency: 1, run: async () => new Promise(() => {}) });
  queue.enqueue(makeJob({ id: 'go', chatId: 5, userId: 5 }));
  queue.enqueue(makeJob({ id: 'next', chatId: 6, userId: 6 }));

  assert.equal(queue.positionOf('next'), 1);
  assert.equal(queue.positionOf('go'), 0);
  assert.equal(queue.findActiveByChat(5).id, 'go');
  assert.equal(queue.findActiveByChat(999), null);
  assert.equal(queue.findActiveByUser(6).id, 'next');
});

test('cancelAll aborts running and waiting jobs', async () => {
  const queue = new JobQueue({ concurrency: 1, run: async () => new Promise(() => {}) });
  queue.enqueue(makeJob({ id: 'a', chatId: 1, userId: 1 }));
  queue.enqueue(makeJob({ id: 'b', chatId: 2, userId: 2 }));

  assert.equal(queue.cancelAll('shutdown'), 2);
  assert.equal(queue.pending, 0);
});
