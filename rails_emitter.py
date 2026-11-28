"""Optional in-process worker observer. Durable JSONL/outbox remain authoritative."""

from contextlib import closing, contextmanager
import json
import sys
import threading
from pathlib import Path

# The R02 package uses absolute imports. Resolve the pinned local copy both
# from a checkout and a wheel; no checkout of BundleBees or PYTHONPATH needed.
_vendor = str(Path(__file__).resolve().parent / "contrib")
if _vendor not in sys.path:
    sys.path.insert(0, _vendor)
from rizzstack_rails import RailsLedger, contracts, privacy
from rails_log_source import ATTEMPT, Cursor, LogSource, candidate, diagnostic


class RailsEmitter:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "rails.sqlite"
        self._lock = threading.RLock()
        self._errors = {}
        self._partial = set()
        self._ready = False
        # Attempt initialization even if there are no journals. Failure never
        # changes execution state and retains the original files for replay.
        try:
            with closing(self._writer()) as db:
                db._conn.execute(
                    "CREATE TABLE IF NOT EXISTS hyvv_log_cursor (attempt TEXT PRIMARY KEY, cursor TEXT NOT NULL)"
                )
        except Exception:
            self._errors["init"] = "storage_unavailable"

    def _writer(self):
        return RailsLedger(str(self.path), allowed_authorities={"worker_report"})

    @contextmanager
    def recovery_writer(self):
        """One ledger writer shared by startup journal ingest and receipt replay.

        Recovery used to open a fresh writer per journal and per receipt. Keeping
        one connection for the whole pass makes restart cost independent of the
        attempt count without changing what is written.
        """
        db = None
        try:
            db = self._writer()
        except Exception:
            self._errors.setdefault("init", "storage_unavailable")
        try:
            yield db
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def recover(self, attempts, *, writer=None):
        if writer is None:
            with self.recovery_writer() as db:
                self._recover_attempts(attempts, db)
        else:
            self._recover_attempts(attempts, writer)

    def _recover_attempts(self, attempts, writer):
        seen = set()
        try:
            # Path.glob may swallow directory listing errors; partial evidence
            # must never be reported as an empty, fully recorded source.
            for path in (self.root / "attempts").iterdir():
                if path.name.endswith(".events.jsonl"):
                    aid = path.name.removesuffix(".events.jsonl")
                    seen.add(aid)
                    self.ingest(aid, expected_seq=attempts.get(aid, {}).get("lseq"), writer=writer)
        except OSError:
            self._errors["recovery"] = "journal_listing_unavailable"
        expected = {aid for aid, a in attempts.items() if a.get("lseq")}
        try:
            # A journal-only fixture (or a lost attempt manifest) still has a
            # recorded cursor. Its disappearance on restart is not clean EOF.
            with closing(RailsLedger(str(self.path), read_only=True)) as db:
                expected.update(row["attempt"] for row in db.query("SELECT attempt FROM hyvv_log_cursor"))
        except Exception:
            self._errors["cursor"] = "storage_unavailable"
        for aid in expected - seen:
            self._errors[("log", aid)] = "missing_journal"
        self._ready = True

    def final_receipts(self):
        """Recorded final usage dedupe keys, or None when the ledger is unreadable.

        This is a read-only check used to skip replay of receipts that already
        exist. None means "unknown", so callers must not treat it as "none".
        """
        try:
            with closing(RailsLedger(str(self.path), read_only=True)) as db:
                rows = db.query(
                    "SELECT dedupe_key FROM dedupe_index WHERE dedupe_key LIKE ?", ("%:final",)
                )
            return {row["dedupe_key"] for row in rows
                    if isinstance(row.get("dedupe_key"), str)}
        except Exception:
            return None

    @staticmethod
    def _writer_args(event):
        if event["schema"] != "rizzstack.rails-candidate/1" or event["authority"] != "worker_report":
            raise ValueError("invalid worker candidate")
        args = dict(event)
        del args["schema"]
        args["lane"] = {k: args.pop(k) for k in contracts.LANE_FIELDS}
        return args

    def journal_write_failed(self, aid):
        # A later successful ingest of surviving bytes cannot resolve a failed
        # native append/sync. Do not clear this with the transient reader error.
        with self._lock:
            self._errors[("write", aid)] = "journal_write_unconfirmed"

    def ingest(self, aid, *, expected_seq=None, writer=None):
        """Identical entry point for just-flushed native logs and startup replay."""
        with self._lock:
            error_key = ("log", aid)
            own = writer is None
            db = writer
            try:
                if db is None:
                    db = self._writer()
                source = LogSource(self.root / "attempts" / f"{aid}.events.jsonl", aid)

                def work(conn):
                    prev = conn.execute(
                        "SELECT cursor FROM hyvv_log_cursor WHERE attempt=?", (aid,)
                    ).fetchone()
                    token = prev[0] if prev else None
                    cursor = Cursor.decode(token)
                    page = source.read_batch(token)
                    next_c = Cursor.decode(page["next_cursor"])
                    # Pin parser limits and authority to the versioned R02
                    # primitive. All appends and the offset advance share
                    # this transaction (including a missing-range notice).
                    for event in page["records"]:
                        db._append_locked(conn, **self._writer_args(event))
                    if next_c != cursor:
                        if next_c.offset != cursor.offset or next_c.generation != cursor.generation:
                            mark = diagnostic(
                                aid,
                                next_c,
                                "source.checkpoint",
                                "bytes_consumed",
                                next_c.offset,
                                next_c.offset,
                                "offset",
                                next_c.offset,
                            )
                            db._append_locked(conn, **self._writer_args(mark))
                        conn.execute(
                            "INSERT OR REPLACE INTO hyvv_log_cursor VALUES (?,?)",
                            (aid, page["next_cursor"]),
                        )
                    return page

                while True:
                    page = db._write_tx(work)
                    if not page["has_more"]:
                        break
                if page["diagnostics"]:
                    self._partial.add(aid)
                else:
                    self._partial.discard(aid)
                if type(expected_seq) is int and expected_seq > Cursor.decode(page["next_cursor"]).seq:
                    # A clean EOF behind a retained native manifest isn't a
                    # complete replay, even if ingest never saw the lost tail.
                    self._errors[("write", aid)] = "journal_shorter_than_manifest"
                self._errors.pop(error_key, None)
            except Exception:
                self._errors[error_key] = "replay_required"
            finally:
                if own and db is not None:
                    try:
                        db.close()
                    except Exception:
                        pass

    def receipt(self, aid, block, models, *, agy=False, writer=None):
        """Use only the persisted landing receipt's allowlisted count block."""
        if not block:
            return
        with self._lock:
            key = ("usage", aid)
            own = writer is None
            db = writer
            try:
                if not ATTEMPT.fullmatch(aid):
                    raise ValueError("unsupported attempt identity")

                def count(name):
                    v = block.get(name)
                    # Legacy agy usage receipts default missing counts to 0. An
                    # ambiguous zero from that backend cannot be asserted here.
                    return v if type(v) is int and int(agy) <= v < 10**9 else None

                provider = block.get("route")
                model = block.get("model")
                payload = {
                    "usage_record_id": aid + ":final",
                    "metric_scope": aid,
                    "usage_basis": "final",
                    "provider": provider if provider in ("go", "deepseek", "zoo", "free", "agy") else None,
                    "model": model if model in models else None,
                    "input_tokens": count("input"),
                    "output_tokens": count("output"),
                    "thinking_tokens": count("thinking") if "thinking" in block else count("reasoning"),
                }
                if db is None:
                    db = self._writer()
                row = db.query("SELECT cursor FROM hyvv_log_cursor WHERE attempt=?", (aid,))
                c = Cursor(**json.loads(row[0]["cursor"])) if row else Cursor()
                event = candidate(aid, c.generation, 0, "receipt", "usage.observed", payload, run=c.run)
                event["dedupe_key"] = aid + ":final"
                db.append(**self._writer_args(event))
                self._errors.pop(key, None)
            except Exception:
                self._errors[key] = "receipt_replay_required"
            finally:
                if own and db is not None:
                    try:
                        db.close()
                    except Exception:
                        pass

    def health(self):
        with self._lock:
            complete = self._ready and not self._errors and not self._partial
            return {
                "recording": "degraded" if self._errors or self._partial else "ready",
                "completeness": "complete" if complete else "partial",
                # Clean EOF proves coverage of retained journals only. Older
                # writers, unavailable directory barriers and lost host state
                # cannot be ruled out by opening the ledger after a restart.
                "completeness_scope": "retained_journals",
                "power_loss_durability": "unproven",
                "warnings": sorted(
                    set(self._errors.values()) | ({"partial_journal"} if self._partial else set())
                ),
            }
