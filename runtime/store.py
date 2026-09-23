"""State for an approval-gated agent: a queue, a lock, a set of pending approvals, and the
corpus of human corrections.

SQLite, one connection per call, no ORM. At the volume this shape is built for (tens of
thousands of decisions a month, one reviewer) a database server and a connection pool are
two more things that can be down without buying anything.

THREE CONCURRENCY PRIMITIVES, ONE SQL STATEMENT EACH. This is the part worth reading. Each
of them replaces infrastructure people normally reach for, and each is exactly one
statement because that is what makes it correct: the atomicity is the database's, not ours.

  1. A trailing debounce            INSERT OR REPLACE on a primary key
  2. A distributed lock             INSERT OR IGNORE + a rowcount check
  3. Optimistic concurrency         a conditional UPDATE + a rowcount check

The rowcount check is not decoration. `INSERT OR IGNORE` that inserted nothing and
`INSERT OR IGNORE` that inserted a row are indistinguishable unless you ask how many rows
changed, and that difference is the whole lock.
"""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "state.db"

# Terminal tags a reviewer's verdict can carry. They are constants rather than free text
# because the weekly pass CLUSTERS on this field: one typo and a finding is filed under a
# tag that will never reach a threshold, which fails silently.
TAG_DECLINE_PLAIN = "DECLINED - no reply wanted"
TAG_DECLINE_AFTER_FIX = "DECLINED AFTER FIX - no sendable reply exists"


@contextmanager
def _conn(path=None):
    c = sqlite3.connect(str(path or DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    # WAL so a long read (the weekly pass scanning the corpus) cannot block the webhook
    # thread from writing. The default rollback journal takes a write lock over the whole
    # database and would make the two contend.
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init(path=None):
    with _conn(path) as c:
        # --- 1. The debounce queue -------------------------------------------------
        # PRIMARY KEY on the subject is what makes the trailing debounce work: see
        # `queue_inbound` below.
        c.execute("""CREATE TABLE IF NOT EXISTS pending (
            subject_id TEXT PRIMARY KEY,
            payload    TEXT NOT NULL,
            fire_at    REAL NOT NULL
        )""")

        # --- 2. The claim table (a distributed lock) --------------------------------
        # One row per unit of work that must be processed exactly once. The PRIMARY KEY is
        # the lock.
        c.execute("""CREATE TABLE IF NOT EXISTS claims (
            claim_key  TEXT PRIMARY KEY,
            claimed_at REAL NOT NULL
        )""")

        # --- 3. Approvals ----------------------------------------------------------
        # `version` is the optimistic-concurrency token. `card_version` is which version
        # the human can actually SEE on the review surface. They come apart whenever a
        # corrected card fails to post, and the difference between "the row is on v2" and
        # "a human can act on v2" is a draft that is sendable but invisible.
        c.execute("""CREATE TABLE IF NOT EXISTS approvals (
            request_id   TEXT PRIMARY KEY,
            kind         TEXT NOT NULL DEFAULT 'reply',
            subject_id   TEXT,
            data         TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            version      INTEGER NOT NULL DEFAULT 1,
            card_version INTEGER,
            decided_by   TEXT,
            decided_at   REAL,
            created_at   REAL NOT NULL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_appr_created ON approvals(created_at)")

        # --- 4. The review corpus --------------------------------------------------
        # One append-only row per human intervention. THIS IS THE TABLE THE WHOLE SYSTEM
        # EXISTS TO FILL. Deliberately denormalised: the classifier's verdict and the
        # prompt hashes are copied in, so the weekly pass is a plain query and never has
        # to join back into a blob whose prompt has since changed.
        c.execute("""CREATE TABLE IF NOT EXISTS revisions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id      TEXT NOT NULL,
            version         INTEGER NOT NULL,
            subject_id      TEXT,
            created_at      REAL NOT NULL,
            kind            TEXT,
            reviewer_reason TEXT,
            reviewer_id     TEXT,
            violated        TEXT,
            policy_quote    TEXT,
            tag_resolved    INTEGER,
            explanation     TEXT,
            draft_before    TEXT,
            draft_after     TEXT,
            intent          TEXT,
            confidence      TEXT,
            prompts         TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_rev_created ON revisions(created_at)")

        # --- 5. Prompt-edit candidates ---------------------------------------------
        # The proposed edit is stored as an exact anchor -> replacement PAIR, never as a
        # unified diff. A diff needs context matching and can apply in the wrong place or
        # ambiguously; an exact string either matches once or refuses. That refusal is
        # also the staleness guard: if the prompt changed between the proposal and the
        # Apply press, the anchor is gone and the edit declines to land.
        c.execute("""CREATE TABLE IF NOT EXISTS candidates (
            id            TEXT PRIMARY KEY,
            week          TEXT NOT NULL,
            created_at    REAL NOT NULL,
            tag           TEXT NOT NULL,
            recurrence    INTEGER NOT NULL,
            evidence      TEXT,
            prompt_file   TEXT,
            anchor        TEXT,
            replacement   TEXT,
            rationale     TEXT,
            status        TEXT NOT NULL DEFAULT 'pending',
            decided_by    TEXT,
            decided_at    REAL,
            reject_reason TEXT
        )""")


# --------------------------------------------------------------- 1. trailing debounce

def queue_inbound(subject_id, payload, delay_seconds, path=None):
    """Schedule work for a subject, resetting the timer if it is already scheduled.

    A person sends three messages in ninety seconds. Answering the first is answering a
    question they have already refined twice, so every arrival must push the deadline
    out and the work must run ONCE, against the final state.

    That whole behaviour is `INSERT OR REPLACE` on a table whose primary key is the
    subject. The second message overwrites the first row rather than adding a second, and
    `fire_at` moves forward with it. No scheduler, no cancellation bookkeeping, no
    per-subject timer object to leak.
    """
    with _conn(path) as c:
        c.execute("INSERT OR REPLACE INTO pending (subject_id, payload, fire_at) VALUES (?, ?, ?)",
                  (subject_id, json.dumps(payload), time.time() + delay_seconds))


def due_inbounds(path=None):
    """Rows whose timer has expired. The caller deletes each one as it takes it."""
    with _conn(path) as c:
        rows = c.execute("SELECT subject_id, payload FROM pending WHERE fire_at <= ?",
                         (time.time(),)).fetchall()
    return [(r["subject_id"], json.loads(r["payload"])) for r in rows]


def drop_inbound(subject_id, path=None):
    with _conn(path) as c:
        c.execute("DELETE FROM pending WHERE subject_id = ?", (subject_id,))


# ------------------------------------------------------------------ 2. distributed lock

def claim(claim_key, path=None):
    """True exactly once per `claim_key`, across every process and thread.

    Two workers can pick up the same inbound: the webhook fires twice, or a retry lands
    while the first attempt is still running. Both would draft, both would post a card,
    and the human would see one subject twice.

    `INSERT OR IGNORE` plus `rowcount` settles it without a lock table, a Redis, or a
    queue broker. The database serialises the two inserts; the loser inserted zero rows
    and learns that from the rowcount. Without the rowcount check the two calls are
    indistinguishable and the guard does nothing.

    The key is CONTENT-derived by the caller (subject + a hash of the newest message), not
    a random id, so a genuinely new message from the same subject gets a new key and is
    not swallowed by the guard.
    """
    with _conn(path) as c:
        cur = c.execute("INSERT OR IGNORE INTO claims (claim_key, claimed_at) VALUES (?, ?)",
                        (claim_key, time.time()))
        return cur.rowcount == 1


def release_claim(claim_key, path=None):
    """Only for a failed attempt that should be retried. A SUCCESSFUL run keeps its claim
    forever: the claim is the duplicate guard, and releasing it re-opens the duplicate."""
    with _conn(path) as c:
        c.execute("DELETE FROM claims WHERE claim_key = ?", (claim_key,))


# ----------------------------------------------------------- 3. optimistic concurrency

def save_approval(data, kind="reply", subject_id=None, path=None):
    request_id = uuid.uuid4().hex
    with _conn(path) as c:
        c.execute("INSERT INTO approvals (request_id, kind, subject_id, data, created_at, "
                  "version, card_version) VALUES (?, ?, ?, ?, ?, 1, 1)",
                  (request_id, kind, subject_id, json.dumps(data), time.time()))
    return request_id


def get_approval(request_id, path=None):
    with _conn(path) as c:
        row = c.execute("SELECT * FROM approvals WHERE request_id = ?", (request_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["data"] = json.loads(out["data"])
    return out


def set_status(request_id, status, by=None, expect="pending", expect_version=None, path=None):
    """Move an approval to a terminal state, and return False if someone got there first.

    A double-click is not a hypothetical: the review surface is a chat client, the button
    stays live while the send is in flight, and a reviewer who sees no immediate feedback
    clicks again. Read-then-write loses that race. A CONDITIONAL UPDATE does not:

        UPDATE ... SET status = 'approved' WHERE request_id = ? AND status = 'pending'

    The second click matches zero rows and returns False, so the caller sends nothing. The
    database did the mutual exclusion; there is no application-level lock to forget.

    `expect_version` closes the other half. A card posted at v1 is still sitting in the
    channel with working buttons after a Fix produces v2, and approving THERE would send
    superseded text. The version travels in the button payload, so a stale click carries
    a stale version and matches nothing.
    """
    sql = "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ? WHERE request_id = ? AND status = ?"
    args = [status, by, time.time(), request_id, expect]
    if expect_version is not None:
        sql += " AND version = ?"
        args.append(expect_version)
    with _conn(path) as c:
        return c.execute(sql, args).rowcount == 1


def bump_version(request_id, new_text, path=None):
    """Store a corrected draft as a new version, keeping the original readable.

    `data['draft_v1']` is never overwritten: the rejected draft and the correction are
    only a training pair while BOTH are present. `data['text']` is always the CURRENT
    draft, so Approve sends what the reviewer is looking at.
    """
    with _conn(path) as c:
        row = c.execute("SELECT data, version FROM approvals WHERE request_id = ?",
                        (request_id,)).fetchone()
        if not row:
            return None
        data = json.loads(row["data"])
        data.setdefault("draft_v1", data.get("text"))
        data["text"] = new_text
        version = (row["version"] or 1) + 1
        c.execute("UPDATE approvals SET data = ?, version = ? WHERE request_id = ?",
                  (json.dumps(data), version, request_id))
    return version


def set_card_version(request_id, version, path=None):
    """Record which version the human can actually SEE. Called only after the surface
    confirms the corrected card is up. If this never runs, the row is on v2 while the
    reviewer is still looking at v1, and the invariant `card_version == version` is how
    that is detected rather than discovered from a lead's confused answer."""
    with _conn(path) as c:
        c.execute("UPDATE approvals SET card_version = ? WHERE request_id = ?",
                  (version, request_id))


def orphaned_cards(path=None):
    """Approvals whose newest draft is not the one on display. Should always be empty."""
    with _conn(path) as c:
        return [dict(r) for r in c.execute(
            "SELECT request_id, version, card_version FROM approvals "
            "WHERE status = 'pending' AND COALESCE(card_version, 0) <> version")]


# ------------------------------------------------------------------- the review corpus

def record_revision(request_id, version, **fields):
    """Append one human intervention. Called at DECISION time, in the same transaction as
    the verdict, because a label written later is a label written from memory."""
    cols = ["request_id", "version", "created_at"]
    vals = [request_id, version, time.time()]
    for k, v in fields.items():
        cols.append(k)
        vals.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
    q = f"INSERT INTO revisions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    with _conn(fields.pop("path", None)) as c:
        c.execute(q, vals)


def revisions_since(ts, path=None):
    with _conn(path) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM revisions WHERE created_at >= ? ORDER BY created_at", (ts,))]


def acceptance_stats(since_ts, path=None):
    """First-pass acceptance as a COHORT on one denominator: of the approvals DECIDED
    since `since_ts`, how many were approved while still on v1.

    Deliberately not an event count over interventions. A numerator with no denominator
    cannot improve, and reporting one is how a metric ends up wrong by an order of
    magnitude while looking like it is being watched.
    """
    with _conn(path) as c:
        row = c.execute(
            "SELECT COUNT(*) n, "
            " SUM(CASE WHEN status='approved' AND version=1 THEN 1 ELSE 0 END) clean, "
            " SUM(CASE WHEN version > 1 THEN 1 ELSE 0 END) fixed, "
            " SUM(CASE WHEN status='declined' AND version=1 THEN 1 ELSE 0 END) unwanted "
            "FROM approvals WHERE created_at >= ? AND status IN ('approved','declined')",
            (since_ts,)).fetchone()
    n = row["n"] or 0
    return {
        "decided": n,
        "clean": row["clean"] or 0,
        "fixed": row["fixed"] or 0,
        "unwanted": row["unwanted"] or 0,
        "acceptance": round(100.0 * (row["clean"] or 0) / n, 1) if n else None,
    }


# ------------------------------------------------------------------------- candidates

def save_candidate(cand_id, week, tag, recurrence, **fields):
    cols = ["id", "week", "created_at", "tag", "recurrence"]
    vals = [cand_id, week, time.time(), tag, recurrence]
    for k, v in fields.items():
        cols.append(k)
        vals.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
    q = (f"INSERT OR REPLACE INTO candidates ({', '.join(cols)}) "
         f"VALUES ({', '.join('?' * len(cols))})")
    with _conn() as c:
        c.execute(q, vals)


def get_candidate(cand_id, path=None):
    with _conn(path) as c:
        row = c.execute("SELECT * FROM candidates WHERE id = ?", (cand_id,)).fetchone()
    return dict(row) if row else None


def decide_candidate(cand_id, status, by=None, reason=None, path=None):
    """Same conditional-UPDATE guard as an approval: applying a prompt edit twice would
    either fail on a missing anchor or, worse, apply to text the first pass already
    changed."""
    with _conn(path) as c:
        return c.execute(
            "UPDATE candidates SET status = ?, decided_by = ?, decided_at = ?, "
            "reject_reason = ? WHERE id = ? AND status = 'pending'",
            (status, by, time.time(), reason, cand_id)).rowcount == 1
