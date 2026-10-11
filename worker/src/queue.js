/**
 * Bounded job queue.
 *
 * Rules that keep a small (512 MB) instance alive:
 *  - at most `concurrency` jobs run at once;
 *  - one job per chat and one per user, so a single person cannot fill the queue;
 *  - a hard cap on the waiting list, so memory never grows without bound;
 *  - every job owns an AbortController, which is how /jobs/:id/cancel and the
 *    shutdown handler stop real work (child processes and sockets, not just promises).
 */

export class QueueFullError extends Error {
  constructor(limit) {
    super(`The worker queue is full (${limit} waiting jobs). Try again in a few minutes.`);
    this.name = 'QueueFullError';
    this.code = 'QUEUE_FULL';
    this.status = 503;
  }
}

export class DuplicateJobError extends Error {
  constructor(reason) {
    super(reason);
    this.name = 'DuplicateJobError';
    this.code = 'DUPLICATE';
    this.status = 409;
  }
}

export class JobQueue {
  /**
   * @param {object} options
   * @param {number} options.concurrency How many jobs may run simultaneously.
   * @param {number} [options.maxWaiting] Cap on queued (not running) jobs.
   * @param {(job: object) => Promise<void>} options.run Worker function.
   * @param {import('./logger.js').Logger} [options.log]
   */
  constructor({ concurrency, maxWaiting = 50, run, log }) {
    this.concurrency = Math.max(1, concurrency);
    this.maxWaiting = maxWaiting;
    this.run = run;
    this.log = log;
    /** @type {object[]} */
    this.waiting = [];
    /** @type {Map<string, object>} */
    this.active = new Map();
    this.completed = 0;
    this.failed = 0;
    this.cancelled = 0;
  }

  get running() {
    return this.active.size;
  }

  get pending() {
    return this.waiting.length;
  }

  /** Position of a job in the waiting line (1 = next). 0 when unknown or running. */
  positionOf(jobId) {
    const index = this.waiting.findIndex((job) => job.id === jobId);
    return index === -1 ? 0 : index + 1;
  }

  findActiveByChat(chatId) {
    for (const job of this.active.values()) {
      if (job.chatId === chatId) return job;
    }
    return this.waiting.find((job) => job.chatId === chatId) ?? null;
  }

  findActiveByUser(userId) {
    for (const job of this.active.values()) {
      if (job.userId === userId) return job;
    }
    return this.waiting.find((job) => job.userId === userId) ?? null;
  }

  /**
   * Adds a job and starts it as soon as a slot is free.
   * @returns {{ job: object, position: number }}
   * @throws {DuplicateJobError | QueueFullError}
   */
  enqueue(job) {
    if (this.findActiveByChat(job.chatId)) {
      throw new DuplicateJobError('This chat already has a download in progress.');
    }
    if (job.userId && this.findActiveByUser(job.userId)) {
      throw new DuplicateJobError('You already have a download in progress. Wait for it or press Cancel.');
    }
    if (this.pending >= this.maxWaiting) {
      throw new QueueFullError(this.maxWaiting);
    }

    const controller = new AbortController();
    const record = {
      ...job,
      controller,
      signal: controller.signal,
      enqueuedAt: Date.now(),
      startedAt: null,
      finishedAt: null
    };

    if (this.active.size < this.concurrency) {
      this.#start(record);
      return { job: record, position: 0 };
    }

    this.waiting.push(record);
    this.log?.info('Job queued', { jobId: record.id, chatId: record.chatId, pending: this.pending });
    return { job: record, position: this.pending };
  }

  #start(record) {
    record.startedAt = Date.now();
    this.active.set(record.id, record);

    this.run(record)
      .then((outcome) => {
        if (outcome === 'cancelled') this.cancelled += 1;
        else this.completed += 1;
      })
      .catch((error) => {
        this.failed += 1;
        this.log?.error('Job crashed', { jobId: record.id, message: error?.message, stack: error?.stack });
      })
      .finally(() => {
        record.finishedAt = Date.now();
        this.active.delete(record.id);
        this.#drain();
      });
  }

  #drain() {
    while (this.active.size < this.concurrency && this.waiting.length > 0) {
      const next = this.waiting.shift();
      if (!next) return;
      if (next.signal.aborted) {
        next.finishedAt = Date.now();
        this.cancelled += 1;
        continue;
      }
      this.#start(next);
    }
  }

  /**
   * Cancels a waiting or running job.
   * @param {string} jobId
   * @param {string} [reason]
   * @returns {'cancelled' | 'not_found'}
   */
  cancel(jobId, reason = 'cancelled by user') {
    const waitingIndex = this.waiting.findIndex((job) => job.id === jobId);
    if (waitingIndex !== -1) {
      const [job] = this.waiting.splice(waitingIndex, 1);
      job.cancelReason = reason;
      job.finishedAt = Date.now();
      this.cancelled += 1;
      this.log?.info('Queued job cancelled', { jobId, reason });
      return 'cancelled';
    }

    const active = this.active.get(jobId);
    if (active) {
      active.cancelReason = reason;
      active.controller.abort(reason);
      this.log?.info('Running job asked to stop', { jobId, reason });
      return 'cancelled';
    }
    return 'not_found';
  }

  /** Aborts everything (used on SIGTERM). Returns the number of jobs signalled. */
  cancelAll(reason) {
    let signalled = 0;
    for (const job of [...this.active.values()]) {
      job.cancelReason = reason;
      job.controller.abort(reason);
      signalled += 1;
    }
    for (const job of this.waiting.splice(0)) {
      job.cancelReason = reason;
      job.controller.abort(reason);
      signalled += 1;
    }
    return signalled;
  }

  /** Waits until every active job finished or `timeoutMs` elapsed. */
  async drain(timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (this.active.size > 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    return this.active.size === 0;
  }

  stats() {
    return {
      concurrency: this.concurrency,
      running: this.active.size,
      pending: this.pending,
      completed: this.completed,
      failed: this.failed,
      cancelled: this.cancelled,
      activeJobs: [...this.active.values()].map((job) => ({
        id: job.id,
        chatId: job.chatId,
        quality: job.quality,
        mode: job.mode,
        startedAt: job.startedAt
      }))
    };
  }
}
