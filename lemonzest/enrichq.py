"""One queue for identification, one worker, one request a second.

Identification is the only part of Lemon Zest that talks to somebody else's
service, and that service asks for one request a second from one client. The
rule used to be kept by each caller separately - a download built a client
and paced itself, a scan built another and paced itself - which meant that
two of them running at once politely went twice as fast as allowed. What
came back was a run of 503s and a handful of files left unidentified for
nobody to notice.

So there is one queue and one worker. Not as a policy the callers agree to
follow, but as the only way in: nothing else holds a MusicBrainz client, so
there is never more than one request in flight and the interval between them
is a property of the program rather than of whoever happened to start first.

What the queue is for, beyond the rate limit:

  * **Retries belong here.** A 503 is almost always the service being busy
    for a minute. The item goes back on the queue with a longer fuse rather
    than being dropped on the floor and waiting for somebody to notice a
    counter.
  * **Priority.** A person who clicks "Identify again" is watching; a sweep
    over two thousand files is not. Manual work goes first.
  * **One place to slow down.** The client widens its own interval when it
    is refused; because everything goes through this worker, that slowdown
    applies to the whole program at once.
  * **It is not lost on a restart.** A file that has never been looked at is
    `raw` in the catalog, so the backlog can be rebuilt by asking the
    catalog rather than by keeping a second copy of it.

Nothing here raises into a caller. Enrichment is optional work done behind
somebody else's download, and a service being down is not a reason for the
download to look like it failed.
"""
import contextlib
import heapq
import threading
import time

# What kind of work jumps which. Lower goes first.
MANUAL, ARRIVAL, SWEEP = 0, 1, 2
PRIORITIES = {"manual": MANUAL, "arrival": ARRIVAL, "sweep": SWEEP}

# How many times one file may come back before it is left alone. Four
# attempts spread over a couple of minutes outlasts an ordinary wobble; a
# service that is down for the afternoon is not worth queueing against.
MAX_ATTEMPTS = 4
# How long after each failure to try again. Index by attempts made so far.
BACKOFF = (20.0, 90.0, 300.0)

# How many finished items to remember for the interface. The queue's own
# history is a progress report, not a log.
RECENT = 40


class _Job:
    """One file waiting to be identified, and what has happened to it."""

    __slots__ = ("key", "priority", "seq", "attempts", "not_before", "label",
                 "on_state")

    def __init__(self, key, priority, seq, label=None, on_state=None):
        self.key = key
        self.priority = priority
        self.seq = seq
        self.attempts = 0
        self.not_before = 0.0
        self.label = label
        self.on_state = on_state

    # Ordered by what should happen next: the most urgent lane, then the
    # thing that has been waiting longest. `not_before` is deliberately not
    # part of this - a deferred item keeps its place in line rather than
    # going to the back for having been unlucky.
    def __lt__(self, other):
        return (self.priority, self.seq) < (other.priority, other.seq)


class EnrichQueue:
    """The backlog and the one worker that works through it."""

    def __init__(self, db_path, contact=None, stream=None, connect=None,
                 idle_sleep=0.2):
        self.db_path = db_path
        self.contact = contact
        self._connect = connect
        self._stream = stream          # injectable, for tests
        self._idle_sleep = idle_sleep

        self._heap = []
        self._queued = {}              # content_key -> job, for deduping
        self._lock = threading.Condition()
        self._thread = None
        self._stop = False
        self._paused = False

        self.current = None            # the key being worked on now
        self.done = 0
        self.failed = 0
        self.applied = 0
        self.candidates = 0
        self.unmatched = 0
        self.gave_up = 0
        self.recent = []               # newest first: what happened to what
        self.last_error = None

    # ------------------------------------------------------------- putting

    def submit(self, keys, priority="arrival", label=None, on_state=None):
        """Queue content keys. Returns how many were actually added.

        A key already waiting is not added twice - the same track queued by
        a scan and by a download is one lookup - but a second submission may
        promote it: a person asking for a file that a sweep had at the back
        of the queue gets it now rather than in an hour.
        """
        rank = PRIORITIES.get(priority, ARRIVAL)
        added = 0
        with self._lock:
            for key in keys or []:
                if not key:
                    continue
                waiting = self._queued.get(key)
                if waiting is not None:
                    if rank < waiting.priority:
                        # Promote by re-queueing: heapq has no decrease-key,
                        # and the stale entry is skipped when it surfaces.
                        promoted = _Job(key, rank, self._next_seq(),
                                        label or waiting.label,
                                        on_state or waiting.on_state)
                        promoted.attempts = waiting.attempts
                        self._queued[key] = promoted
                        heapq.heappush(self._heap, promoted)
                    elif on_state and not waiting.on_state:
                        waiting.on_state = on_state
                    continue
                job = _Job(key, rank, self._next_seq(), label, on_state)
                self._queued[key] = job
                heapq.heappush(self._heap, job)
                added += 1
            self._lock.notify_all()
        if added:
            self.start()
        return added

    _seq = 0

    def _next_seq(self):
        EnrichQueue._seq += 1
        return EnrichQueue._seq

    # ------------------------------------------------------------- running

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="lz-enrich")
            self._thread.start()

    def stop(self, timeout=5.0):
        """Ask the worker to finish the item in hand and stand down."""
        with self._lock:
            self._stop = True
            self._lock.notify_all()
        thread = self._thread
        if thread:
            thread.join(timeout)

    def pause(self):
        with self._lock:
            self._paused = True

    def resume(self):
        with self._lock:
            self._paused = False
            self._lock.notify_all()
        self.start()

    def clear(self):
        """Drop the backlog. What is in hand finishes; nothing follows it."""
        with self._lock:
            dropped = len(self._queued)
            self._heap = []
            self._queued = {}
            return dropped

    def _take(self):
        """The next item that is allowed to run yet, or None.

        Returns (job, wait): a job to do now, or how long to sleep before
        asking again. Deferred items keep their place, so a queue holding
        only items in backoff waits rather than spinning.
        """
        now = time.time()
        deferred = []
        job = None
        soonest = None
        while self._heap:
            top = heapq.heappop(self._heap)
            if self._queued.get(top.key) is not top:
                continue               # a stale entry from a promotion
            if top.not_before > now:
                deferred.append(top)
                soonest = (top.not_before if soonest is None
                           else min(soonest, top.not_before))
                continue
            job = top
            break
        for item in deferred:
            heapq.heappush(self._heap, item)
        wait = (soonest - now) if (job is None and soonest is not None) \
            else self._idle_sleep
        return job, max(0.05, min(wait, 60.0))

    def _run(self):
        while True:
            with self._lock:
                if self._stop:
                    return
                if self._paused:
                    self._lock.wait(0.5)
                    continue
                job, wait = self._take()
                if job is None:
                    # Nothing to do. The worker stands down rather than
                    # spinning; the next submit starts it again.
                    if not self._heap:
                        self._thread = None
                        return
                    self._lock.wait(wait)
                    continue
                self._queued.pop(job.key, None)
                self.current = job.key
            self._work(job)
            with self._lock:
                self.current = None

    def _work(self, job):
        stream = self._get_stream()
        if stream is None:
            self._note(job, {"state": "failed",
                             "detail": "the catalog could not be opened"})
            return
        self._say(job, "identifying", "looking it up")
        try:
            out = stream.add(job.key)
        except Exception as exc:      # noqa: BLE001 - never raises upward
            out = {"state": "failed", "detail": str(exc)}
        if out.get("state") == "failed" and job.attempts + 1 < MAX_ATTEMPTS:
            # Almost always the service being busy for a minute. Back on the
            # queue with a longer fuse, in its own place in the line.
            job.attempts += 1
            job.not_before = time.time() + BACKOFF[min(job.attempts,
                                                       len(BACKOFF)) - 1]
            self.last_error = out.get("detail")
            self._say(job, "waiting",
                      "%s - trying again shortly" % out.get("detail", "failed"))
            with self._lock:
                self._queued[job.key] = job
                heapq.heappush(self._heap, job)
                self._lock.notify_all()
            return
        self._note(job, out)

    def _note(self, job, out):
        state = out.get("state")
        with self._lock:
            self.done += 1
            if state == "applied":
                self.applied += 1
            elif state == "candidate":
                self.candidates += 1
            elif state == "none":
                self.unmatched += 1
            elif state == "failed":
                self.failed += 1
                if job.attempts + 1 >= MAX_ATTEMPTS:
                    self.gave_up += 1
                self.last_error = out.get("detail")
            self.recent.insert(0, {"key": job.key, "label": job.label,
                                   "state": state,
                                   "detail": out.get("detail"),
                                   "at": time.time()})
            del self.recent[RECENT:]
        self._say(job, "done" if state != "failed" else "failed",
                  out.get("detail"), enrich=state)

    def _say(self, job, state, detail, enrich=None):
        """Tell whoever queued this how it is going, if they asked."""
        if not job.on_state:
            return
        try:
            job.on_state(job.key, state, detail, enrich)
        except Exception:      # noqa: BLE001 - a caller's view is their own
            pass

    def _get_stream(self):
        if self._stream is not None:
            return self._stream
        try:
            from . import db as db_mod
            from . import enrich as en
            # Built on whichever thread asks first - usually the worker,
            # but a hand-picked run borrowing the client can get there
            # before it. Safe because only one of them ever uses it at a
            # time; see EnrichQueue.exclusive.
            con = (self._connect() if self._connect
                   else db_mod.connect(self.db_path, same_thread=False))
            self._stream = en.IngestStream(con, contact=self.contact)
        except Exception:      # noqa: BLE001
            return None
        return self._stream

    def client(self):
        """The one client every request in this program goes out through."""
        stream = self._get_stream()
        return stream.client() if stream else None

    @contextlib.contextmanager
    def exclusive(self, timeout=30.0):
        """Borrow the client with the worker held back.

        For the work the queue cannot do itself: a hand-picked run carries
        options - typed search terms, fingerprinting, whether to write tags
        - that an arrival never has. It still must not open a second client,
        because two clients pacing themselves separately is the whole
        problem this queue exists to solve. So the worker stands down, the
        item in hand is allowed to finish, and the caller gets the client.
        """
        self.pause()
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if self.current is None:
                    break
            time.sleep(0.05)
        try:
            yield self.client()
        finally:
            self.resume()

    # -------------------------------------------------------------- saying

    def status(self):
        with self._lock:
            waiting = len(self._queued)
            deferred = sum(1 for j in self._queued.values() if j.not_before)
            out = {
                "waiting": waiting,
                "deferred": deferred,
                "current": self.current,
                "running": bool(self._thread and self._thread.is_alive()),
                "paused": self._paused,
                "done": self.done,
                "applied": self.applied,
                "candidates": self.candidates,
                "unmatched": self.unmatched,
                "failed": self.failed,
                "gave_up": self.gave_up,
                "last_error": self.last_error,
                "recent": list(self.recent[:12]),
            }
        stream = self._stream
        client = getattr(stream, "_client", None) if stream else None
        out["interval"] = getattr(client, "interval", None)
        return out

    def drain(self, timeout=30.0):
        """Wait for the backlog to empty. For tests and for the CLI."""
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if not self._queued and self.current is None:
                    return True
            time.sleep(0.02)
        return False


# The one queue. Addressed by catalog path, because a second catalog is a
# second library and has nothing to do with this one's backlog.
_QUEUES = {}
_QUEUES_LOCK = threading.Lock()


def get_queue(db_path, contact=None):
    with _QUEUES_LOCK:
        q = _QUEUES.get(db_path)
        if q is None:
            q = EnrichQueue(db_path, contact=contact)
            _QUEUES[db_path] = q
        return q


def reset_queues():
    """Forget every queue. Tests only: a live one owns a worker thread."""
    with _QUEUES_LOCK:
        for q in _QUEUES.values():
            q.stop(timeout=1.0)
        _QUEUES.clear()
