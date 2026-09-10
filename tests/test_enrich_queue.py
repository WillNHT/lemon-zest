"""The identification queue: one worker, one pace, and retries that hold.

No network and no catalog: the queue is handed a stand-in for the
identification pass, so what is tested here is the queue's own behaviour -
what it does first, what it does twice, and what it does when the service
says no.
"""
import os
import time
import unittest

os.environ["LEMONZEST_AUTO_ENRICH"] = "0"

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lemonzest import enrich, enrichq  # noqa: E402


class FakeStream:
    """Stands in for IngestStream. Answers however the test says to."""

    def __init__(self, answers=None, fail_times=0):
        self.answers = answers or {}
        self.fail_times = fail_times
        self.seen = []
        self.attempts = {}

    def add(self, key):
        self.seen.append(key)
        self.attempts[key] = self.attempts.get(key, 0) + 1
        if self.attempts[key] <= self.fail_times:
            return {"state": "failed", "detail": "MusicBrainz returned 503"}
        return self.answers.get(key, {"state": "applied",
                                      "detail": "identified"})

    def client(self):
        return None


def queue(stream, **kw):
    q = enrichq.EnrichQueue("test.db", stream=stream, idle_sleep=0.01, **kw)
    return q


class QueueTests(unittest.TestCase):
    def tearDown(self):
        enrichq.reset_queues()

    def test_what_goes_in_comes_out_identified(self):
        stream = FakeStream()
        q = queue(stream)
        q.submit(["a", "b", "c"])
        self.assertTrue(q.drain(timeout=5))
        self.assertEqual(sorted(stream.seen), ["a", "b", "c"])
        self.assertEqual(q.applied, 3)
        self.assertEqual(q.status()["waiting"], 0)

    def test_the_same_track_twice_is_one_lookup(self):
        """A scan and a download naming the same file must not ask twice."""
        stream = FakeStream()
        q = queue(stream)
        q.pause()
        self.assertEqual(q.submit(["a", "a", "b"]), 2)
        self.assertEqual(q.submit(["a"]), 0)
        q.resume()
        self.assertTrue(q.drain(timeout=5))
        self.assertEqual(sorted(stream.seen), ["a", "b"])

    def test_a_person_waiting_goes_first(self):
        stream = FakeStream()
        q = queue(stream)
        q.pause()
        q.submit(["sweep1", "sweep2"], priority="sweep")
        q.submit(["arrival"], priority="arrival")
        q.submit(["manual"], priority="manual")
        q.resume()
        self.assertTrue(q.drain(timeout=5))
        self.assertEqual(stream.seen[0], "manual")
        self.assertEqual(stream.seen[1], "arrival")

    def test_asking_again_by_hand_promotes_what_was_already_waiting(self):
        stream = FakeStream()
        q = queue(stream)
        q.pause()
        q.submit(["a", "b", "c"], priority="sweep")
        q.submit(["c"], priority="manual")
        q.resume()
        self.assertTrue(q.drain(timeout=5))
        self.assertEqual(stream.seen[0], "c")
        # Promoted, not duplicated.
        self.assertEqual(len(stream.seen), 3)

    def test_a_refusal_is_tried_again(self):
        """A 503 is the service being busy, not the file being unknowable."""
        stream = FakeStream(fail_times=1)
        q = queue(stream)
        # The real fuse is twenty seconds; a test should not sit through it.
        original, enrichq.BACKOFF = enrichq.BACKOFF, (0.05, 0.05, 0.05)
        try:
            q.submit(["a"])
            self.assertTrue(q.drain(timeout=5))
        finally:
            enrichq.BACKOFF = original
        self.assertEqual(stream.attempts["a"], 2)
        self.assertEqual(q.applied, 1)
        self.assertEqual(q.failed, 0)

    def test_a_file_that_keeps_failing_is_eventually_left_alone(self):
        stream = FakeStream(fail_times=99)
        q = queue(stream)
        original, enrichq.BACKOFF = enrichq.BACKOFF, (0.01, 0.01, 0.01)
        try:
            q.submit(["a"])
            self.assertTrue(q.drain(timeout=5))
        finally:
            enrichq.BACKOFF = original
        self.assertEqual(stream.attempts["a"], enrichq.MAX_ATTEMPTS)
        self.assertEqual(q.gave_up, 1)
        self.assertEqual(q.status()["last_error"],
                         "MusicBrainz returned 503")

    def test_whoever_queued_it_is_told_how_it_went(self):
        stream = FakeStream()
        q = queue(stream)
        said = []
        q.submit(["a"], on_state=lambda k, state, detail, enrich=None:
                 said.append((state, detail, enrich)))
        self.assertTrue(q.drain(timeout=5))
        self.assertEqual([s[0] for s in said], ["identifying", "done"])
        self.assertEqual(said[-1][2], "applied")

    def test_a_caller_that_throws_does_not_take_the_worker_with_it(self):
        stream = FakeStream()
        q = queue(stream)

        def rude(*args, **kw):
            raise RuntimeError("the page went away")

        q.submit(["a"], on_state=rude)
        q.submit(["b"])
        self.assertTrue(q.drain(timeout=5))
        self.assertEqual(sorted(stream.seen), ["a", "b"])

    def test_a_pass_that_throws_is_a_failure_and_not_a_crash(self):
        class Broken:
            def add(self, key):
                raise RuntimeError("no catalog")

            def client(self):
                return None

        q = queue(Broken())
        original, enrichq.BACKOFF = enrichq.BACKOFF, (0.01, 0.01, 0.01)
        try:
            q.submit(["a"])
            self.assertTrue(q.drain(timeout=5))
        finally:
            enrichq.BACKOFF = original
        # One file, one failure - the attempts in between were retries,
        # not four separate files going wrong.
        self.assertEqual(q.failed, 1)
        self.assertEqual(q.gave_up, 1)
        self.assertIn("no catalog", q.status()["last_error"])

    def test_pausing_stops_it_and_resuming_starts_it_again(self):
        stream = FakeStream()
        q = queue(stream)
        q.pause()
        q.submit(["a"])
        time.sleep(0.1)
        self.assertEqual(stream.seen, [])
        self.assertTrue(q.status()["paused"])
        q.resume()
        self.assertTrue(q.drain(timeout=5))
        self.assertEqual(stream.seen, ["a"])

    def test_the_backlog_can_be_dropped(self):
        stream = FakeStream()
        q = queue(stream)
        q.pause()
        q.submit(["a", "b", "c"])
        self.assertEqual(q.clear(), 3)
        q.resume()
        time.sleep(0.1)
        self.assertEqual(stream.seen, [])

    def test_the_status_says_enough_to_draw_a_progress_bar(self):
        stream = FakeStream()
        q = queue(stream)
        q.pause()
        q.submit(["a", "b"])
        st = q.status()
        self.assertEqual(st["waiting"], 2)
        self.assertTrue(st["paused"])
        q.resume()
        self.assertTrue(q.drain(timeout=5))
        st = q.status()
        self.assertEqual(st["done"], 2)
        self.assertEqual(st["waiting"], 0)
        self.assertEqual(len(st["recent"]), 2)

    def test_the_queue_is_one_per_catalog(self):
        a = enrichq.get_queue("one.db")
        self.assertIs(a, enrichq.get_queue("one.db"))
        self.assertIsNot(a, enrichq.get_queue("two.db"))


class PacingTests(unittest.TestCase):
    """The client's own manners, which the queue relies on."""

    def slept(self):
        naps = []
        client = enrich.MusicBrainz(interval=1.0, sleep=naps.append)
        return client, naps

    def test_a_refusal_widens_the_gap_between_requests(self):
        client, _ = self.slept()
        base = client.interval
        client._refused()
        self.assertGreater(client.interval, base)
        client._refused()
        self.assertLessEqual(client.interval, enrich.MB_MAX_INTERVAL)

    def test_a_run_of_answers_eases_it_back(self):
        client, _ = self.slept()
        client._refused()
        widened = client.interval
        for _ in range(enrich.MB_EASE_AFTER):
            client._answered()
        self.assertLess(client.interval, widened)

    def test_the_service_is_asked_when_to_come_back(self):
        class Exc(Exception):
            headers = {"Retry-After": "7"}

        client, _ = self.slept()
        self.assertEqual(client._retry_after(Exc(), 0), 7.0)

    def test_without_a_header_it_backs_off_with_jitter(self):
        class Exc(Exception):
            headers = {}

        client, _ = self.slept()
        waits = {round(client._retry_after(Exc(), 2), 3) for _ in range(6)}
        self.assertGreater(len(waits), 1, "every client would retry in step")
        self.assertTrue(all(w <= 30.0 for w in waits))


if __name__ == "__main__":
    unittest.main()
