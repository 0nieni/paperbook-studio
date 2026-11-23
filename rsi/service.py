"""Durable local review queue. Commit a report and its resulting prompt changes together."""
from contextlib import closing, contextmanager
import json
import os
import sqlite3
from threading import Event, Thread
import time

import providers as api
from . import evidence, review
from .instructions import LABELS

REVIEW_PROTOCOL = "studio-rsi-v1"


class StudioRSI:
    def __init__(self,data, model=None, background=True):
        self.data = data
        self.path = data/"rsi.sqlite3"
        self.model = model or review.Model()
        self.signal = Event()
        self.closed = Event()
        with closing(self.connect()) as db, db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, book TEXT NOT NULL, edition TEXT NOT NULL,
                source TEXT NOT NULL, feedback TEXT NOT NULL, draft TEXT, state TEXT NOT NULL,
                error TEXT, report TEXT, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS pending ON events(state, created);
            CREATE INDEX IF NOT EXISTS by_book ON events(book, created);
            """)
            if not db.execute("SELECT 1 FROM policy").fetchone():
                db.execute("INSERT INTO policy VALUES (1,?)",(review.dump(review.pack(review.unpack({}))),))
        # Reviews are saved independently of model availability. Reconcile the outbox after a restart.
        for path in data.glob("*/reviews.json"):
            reviews = evidence.read_json(path)
            for version in {r["edition"] for r in reviews}:
                self.enqueue(path.parent,version,reviews)
        for path in data.glob("*/editions/*/制作稿.json"):
            # New editions carry their own immutable record even if power failed just after export.
            if "rsi_assigned_at" in evidence.read_json(path) and (path.parent/"book.js").is_file():
                root = path.parents[2]
                feedback_path = root/"reviews.json"
                self.enqueue(root,path.parent.name,evidence.read_json(feedback_path) if feedback_path.is_file() else [])
        for path in data.glob("*/project.json"):
            book=evidence.read_json(path)
            for scene in book.get("scenes",[]):
                if scene.get("redraw_edit") and scene["redraw_edit"]["frame"]==scene.get("frame"):
                    self.enqueue_redraw(path.parent,book,scene)
        self.thread = None
        if background:
            self.thread = Thread(target=self.run,name="picturebook-rsi",daemon=True)
            self.thread.start()

    def connect(self):
        db = sqlite3.connect(self.path,timeout=12)
        db.row_factory = sqlite3.Row
        return db

    def load(self,db):
        return review.unpack(json.loads(db.execute("SELECT value FROM policy WHERE id=1").fetchone()[0]))

    def select(self,book):
        if book.get("rsi_prompts"):
            return
        with closing(self.connect()) as db:
            book["rsi_prompts"] = review.selection(self.load(db),book.get("creation",{}).get("kind","pdf"))
            book["rsi_assigned_at"] = time.time()

    def enqueue(self,root,edition="",reviews=(),draft=None):
        matched = [r for r in reviews if r.get("edition")==edition] if edition else []
        source = "家长评价" if matched else ("播放版本" if edition else "分镜草稿")
        if not edition and not draft:
            raise ValueError("评估需要已保存的分镜。")
        edit = (draft or {}).get("redraw")
        if edit:
            key = api.digest(REVIEW_PROTOCOL,root.name,"redraw",edit["id"])
            source = "重画分镜 · "+edit["title"]
        else:
            key = api.digest(REVIEW_PROTOCOL,root.name,edition,matched,draft)
        with closing(self.connect()) as db,db:
            db.execute("INSERT OR IGNORE INTO events(id,book,edition,source,feedback,draft,state,created) VALUES (?,?,?,?,?,?,'queued',?)",
                       (key,root.name,edition,source,review.dump(matched),review.dump(draft) if draft else None,time.time()))
        self.signal.set()
        return key

    def enqueue_redraw(self,root,book,scene):
        edit = scene["redraw_edit"]
        key = api.digest(REVIEW_PROTOCOL,root.name,"redraw",edit["id"])
        with closing(self.connect()) as db:
            if db.execute("SELECT 1 FROM events WHERE id=?",(key,)).fetchone():
                return key
        draft = evidence.snapshot(root,{**book,"scenes":[scene]})
        draft["redraw"] = {**edit,"position":book["scenes"].index(scene)+1}
        return self.enqueue(root,draft=draft)

    def retry(self,root,key):
        with closing(self.connect()) as db, db:
            changed = db.execute("UPDATE events SET state='queued',error=NULL WHERE book=? AND id=? AND state='error'",
                                 (root.name,key)).rowcount
            if not changed:
                raise ValueError("请选择这本书中评估失败的记录。")
        self.signal.set()

    @contextmanager
    def worker_slot(self):
        # OS releases the lock on process exit. Multiple localhost ports may share the same library.
        with (self.data/"rsi-worker.lock").open("a+b") as lock:
            lock.seek(0)
            try:
                if os.name=="nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
            else:
                yield True

    def step(self):
        with self.worker_slot() as acquired:
            return self.process_next() if acquired else False

    def process_next(self):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM events WHERE state IN ('queued','running') ORDER BY created LIMIT 1").fetchone()
            if not job:
                return False
            db.execute("UPDATE events SET state='running' WHERE id=?",(job["id"],))
            store = self.load(db)
        try:
            root = self.data/job["book"]
            data = json.loads(job["draft"]) if job["draft"] else evidence.snapshot(root,{},job["edition"])
            feedback = json.loads(job["feedback"])
            report = review.cycle(root,store,data,feedback,self.model)
            with closing(self.connect()) as db, db:
                db.execute("UPDATE policy SET value=? WHERE id=1",(review.dump(review.pack(store)),))
                db.execute("UPDATE events SET state='done', report=?,error=NULL WHERE id=?",(review.dump(report),job["id"]))
        except Exception as exc:
            with closing(self.connect()) as db, db:
                db.execute("UPDATE events SET state='error',error=? WHERE id=?",(str(exc)[:500] or type(exc).__name__,job["id"]))
        return True

    def run(self):
        while not self.closed.is_set():
            self.signal.clear()
            if not self.step():
                self.signal.wait(30)

    def view(self,root):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT id,edition,source,state,error,report,created FROM events WHERE book=? ORDER BY created DESC",
                              (root.name,)).fetchall()
            store = self.load(db)
        records = [{**dict(row),"report":json.loads(row["report"]) if row["report"] else None} for row in rows]
        kind = evidence.read_json(root/"project.json").get("creation",{}).get("kind","pdf")
        policies = []
        for role,label in LABELS.items():
            for s in ("active","challenger"):
                versions = store.find(role,"user:"+kind,s)
                for v in versions:
                    if v.text:
                        policies.append({"role":label,"status":s,"id":v.id,"text":v.text,"books":v.n})
        return {"events":records,"policies":policies}
