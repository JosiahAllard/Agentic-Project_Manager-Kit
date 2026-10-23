"""The machine schedule window: owner holds, the run view, the division and game mode, all on
kitlib.lock.api. tkinter only; what it shows comes
from kitlib.lockui.model, what it does goes through kitlib.lockui.actions.Client.

The schedule: 7 days in half-hour cells, from today (< Earlier and Later > move it a day at a
time, back to look at past runs), with every owner and game hold on it. The strip down the right
of each day is agent work: runs now (in the run's colour, to its expected end), queued runs where
the queue plans them (grey, hatched), agent holds (violet; one an owner or game hold overlaps
loses the slots that hold covers, "lost") and past runs (teal; red when the run took longer; a
white outline for the time it was expected to take). Click the strip to see what holds or held
that half hour. Drag down a day column to pick a window, choose whole or half, press Reserve.
Click a hold to pick it, then Edit... or Release. Hold... makes any owner hold (whole, half or
named slots) at typed times.

Beside the schedule: the slot map (each cpu and gpu slot, filled in the colour of the run holding
it, outlined when a hold covers it) with memory; each running run's details; the queue; agent
holds. Each waiting run in the queue has a Move to top button: after a confirm it moves that run
to the front of the queue (api.move_to_top; runs holding slots keep them). Game mode, Games...
and Division... are the owner's settings.

The lock is read on a worker thread (a snapshot can take a while); its result comes back to the
Tk thread by a queue the window polls with root.after. A write shows the snapshot it returns.
"""
import datetime as dt
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from kitlib.lockui import actions as act
from kitlib.lockui import model as m

DAYS, ROWS, BACK_DAYS = m.DAYS, m.ROWS, m.BACK_DAYS
ROW_H, COL_W, LEFT, TOP = 12, 120, 48, 34
STRIP_X, STRIP_W = 22, 19   # the agent strip: from COL_W - STRIP_X, STRIP_W wide
POLL_MS = 50                # how often the Tk thread looks for a finished read
SIDE_W = 540                # the slot map beside the schedule
CORE_H, MEM_H = 40, 30
REFRESH_MS = 30_000
COLOURS = m.COLOURS
DARK = {"bg": "#1e1e1e", "panel": "#252526", "fg": "#d4d4d4", "field": "#2d2d30",
        "grid": "#3c3c3c", "grid_hour": "#555555", "past": "#2a2a2a", "pick": "#ffffff",
        "ink": "#1e1e1e"}
TITLE = "Machine schedule"
TIME_FMT = "%Y-%m-%d %H:%M"


def dark_theme(root):
    root.configure(bg=DARK["bg"])
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=DARK["bg"], foreground=DARK["fg"], fieldbackground=DARK["field"],
                 bordercolor=DARK["grid"], lightcolor=DARK["grid"], darkcolor=DARK["grid"])
    st.configure("TButton", background=DARK["field"])
    st.map("TButton", background=[("active", DARK["grid_hour"])])
    for w in ("TRadiobutton", "TCheckbutton"):
        st.map(w, background=[("active", DARK["bg"])],
               indicatorcolor=[("selected", COLOURS["sel"]), ("!selected", DARK["field"])])
    st.configure("TEntry", foreground=DARK["fg"], insertcolor=DARK["fg"])
    st.configure("Warn.TLabel", foreground=COLOURS["warn"])
    st.configure("Dim.TLabel", foreground=DARK["grid_hour"])


class App:
    """threaded False reads on the Tk thread (tests, the screenshot demo)."""

    def __init__(self, root, client=None, auto_refresh=True, threaded=True):
        self.root = root
        self.client = client or act.Client()
        self.auto_refresh = auto_refresh
        self.threaded = threaded
        self.busy = False        # a read is on the worker thread
        self.gen = 0             # bumped by each write: a read begun before it is stale
        self.results = queue.Queue()
        self.timer = None        # the next refresh's after id
        root.title(TITLE)
        dark_theme(root)
        self.day0 = None
        self.offset = 0          # days before today the schedule starts (0 or less)
        self.sel = None          # (day index, first row, last row)
        self.drag = None
        self.picked = None       # hold id
        self.boxes = []          # (x0, y0, x1, y1, hold id)
        self.snap, self.hist = {}, ()
        self.view = m.build({})

        bar = ttk.Frame(root, padding=(6, 6, 6, 2))
        bar.pack(fill="x")
        self.scope = tk.StringVar(value="whole")
        ttk.Radiobutton(bar, text="Whole machine", variable=self.scope, value="whole").pack(side="left")
        ttk.Radiobutton(bar, text="Half (the least used half)", variable=self.scope,
                        value="half").pack(side="left", padx=(6, 12))
        ttk.Label(bar, text="Note").pack(side="left")
        self.note = tk.StringVar()
        ttk.Entry(bar, textvariable=self.note, width=22).pack(side="left", padx=4)
        for text, cmd in (("Reserve", self.reserve), ("Hold...", self.open_hold),
                          ("Edit...", self.open_edit), ("Release", self.release),
                          ("Refresh", lambda: self.refresh(schedule=False))):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=3)
        ttk.Button(bar, text="< Earlier", command=lambda: self.move(-1)).pack(side="left", padx=(16, 3))
        ttk.Button(bar, text="Later >", command=lambda: self.move(1)).pack(side="left", padx=3)

        bar2 = ttk.Frame(root, padding=(6, 0, 6, 2))
        bar2.pack(fill="x")
        self.game_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar2, text="Game mode", variable=self.game_on,
                        command=self.toggle_game).pack(side="left")
        self.game_state = tk.StringVar()
        ttk.Label(bar2, textvariable=self.game_state).pack(side="left", padx=(6, 12))
        ttk.Button(bar2, text="Games...", command=self.open_games).pack(side="left", padx=3)
        ttk.Button(bar2, text="Division...", command=self.open_division).pack(side="left", padx=3)

        wrap = LEFT + DAYS * COL_W + SIDE_W
        self.info = tk.StringVar(value="Drag down a day to pick a window. Click a hold to pick "
                                       "it, or the strip at a day's right for what runs or ran then.")
        ttk.Label(root, textvariable=self.info, padding=(6, 0), wraplength=wrap).pack(fill="x")
        self.warn = tk.StringVar()
        ttk.Label(root, textvariable=self.warn, padding=(6, 0), wraplength=wrap,
                  style="Warn.TLabel").pack(fill="x")

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", anchor="n")
        self.cv = tk.Canvas(left, width=LEFT + DAYS * COL_W + 1, height=TOP + ROWS * ROW_H + 1,
                            bg=DARK["bg"], highlightthickness=0)
        self.cv.pack(padx=6, pady=6)
        self.cv.bind("<ButtonPress-1>", self.press)
        self.cv.bind("<B1-Motion>", self.motion)
        self.cv.bind("<ButtonRelease-1>", self.up)
        ttk.Label(left, padding=(6, 0), font=("Segoe UI", 8), text=(
            "Strip: a running run in its colour (hatched: no expected end); grey hatched: queued, "
            "where the queue plans it; violet: agent hold; teal: ran (white outline: expected time; "
            "red: took longer).")).pack(anchor="w")

        side = ttk.Frame(body)
        side.pack(side="left", fill="both", expand=True, padx=(0, 6), pady=6)
        self.core_head = tk.StringVar(value="Slots now")
        ttk.Label(side, textvariable=self.core_head, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.cm = tk.Canvas(side, width=SIDE_W, height=2 * CORE_H + MEM_H, bg=DARK["bg"],
                            highlightthickness=0)
        self.cm.pack(anchor="w", pady=(2, 6))
        self.details = self.panel(side, "Running now", 10)
        self.queue = self.panel(side, "Queue", 7)
        self.booked = self.panel(side, "Agent holds", 3)
        self.refresh()

    def panel(self, parent, title, lines):
        ttk.Label(parent, text=title, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        t = tk.Text(parent, height=lines, width=76, font=("Consolas", 9), relief="flat", wrap="word",
                    bg=DARK["panel"], fg=DARK["fg"])
        t.pack(fill="both", expand=True, pady=(0, 6))
        t.tag_configure("bad", foreground=COLOURS["late"])
        t.tag_configure("warn", foreground=COLOURS["warn"])
        t.tag_configure("dim", foreground=DARK["grid_hour"])
        t.config(state="disabled")
        return t

    # ---------------------------------------------------------------- data
    def read(self, gen):
        """(gen, snapshot, history lines, error): one look at the lock; never raises."""
        try:
            return gen, self.client.snapshot(), self.client.history(days=BACK_DAYS + 1), None
        except Exception as e:          # the window stays up and says why
            return gen, None, None, e

    def refresh(self, schedule=True):
        """Reads the lock and shows it; with schedule, the next refresh follows REFRESH_MS
        later whatever happened. Threaded, the read runs on a worker and poll() shows it."""
        if schedule:
            self.timer = None
        if not self.threaded:
            self.apply(self.read(self.gen), schedule)
            return
        if self.busy:                   # a read is out; it schedules the next
            return
        self.busy = True
        threading.Thread(target=lambda g=self.gen: self.results.put(self.read(g)),
                         daemon=True).start()
        self.root.after(POLL_MS, self.poll, schedule)

    def poll(self, schedule):
        try:
            got = self.results.get_nowait()
        except queue.Empty:
            self.root.after(POLL_MS, self.poll, schedule)
            return
        self.busy = False
        self.apply(got, schedule or self.auto_refresh)

    def apply(self, got, schedule):
        """Shows a read's result (a read begun before a write is not shown, nor its error: the
        write's own snapshot is newer); anything that fails to show is said, and the refresh
        goes on."""
        gen, snap, hist, err = got
        try:
            if gen != self.gen:
                pass
            elif err is not None:
                self.info.set("the lock could not be read: %s" % err)
            else:
                self.load(snap, hist)
            self.draw()
        except Exception as e:
            self.info.set("the lock's state could not be shown: %s: %s" % (type(e).__name__, e))
        if schedule and self.auto_refresh and self.timer is None:
            self.timer = self.root.after(REFRESH_MS, self.refresh)

    def load(self, snap, hist=None):
        """Shows snapshot snap; hist None keeps the history lines last read (a write's snapshot
        carries none)."""
        if hist is None:
            hist = self.hist
        view = m.build(snap or {}, hist)
        self.snap, self.hist, self.view = snap or {}, hist, view
        if self.picked and not any(h["id"] == self.picked for h in self.view["holds"]):
            self.picked = None
        for rid, c in self.view["colour"].items():
            self.details.tag_configure("c:" + rid, foreground=c)
        self.fill(self.details, self.view["details"], "nothing running")
        self.fill(self.queue, self.view["queue"], "nothing waiting", self.queue_button)
        self.fill(self.booked, self.view["bookings"], "no agent holds")
        self.warn.set("  |  ".join(self.view["warnings"]))
        g = self.snap.get("game") or {}
        self.game_on.set(bool(g.get("enabled")))
        run = g.get("running")
        self.game_state.set(("running: %s" % (run.get("name") or run.get("exe"))
                             if isinstance(run, dict) else "no game running")
                            + ("; %d game%s listed" % (len(g.get("games") or []),
                                                       "" if len(g.get("games") or []) == 1 else "s")))

    @staticmethod
    def fill(t, parts, empty, button=None):
        """Writes parts [(text, tags)] into panel t. A part tagged "move:<run id>" is where
        button(t, run id) goes (a widget embedded in the text); the widgets the last fill made
        go first."""
        t.config(state="normal")
        for w in t.winfo_children():
            w.destroy()
        t.delete("1.0", "end")
        for text, tags in parts or [(empty, ("dim",))]:
            mark = [x[5:] for x in tags if x.startswith("move:")]
            if mark:
                if button:
                    t.window_create("end", window=button(t, mark[0]), padx=2)
                continue
            t.insert("end", text, tags)
        t.config(state="disabled")

    def queue_button(self, t, rid):
        return ttk.Button(t, text="Move to top", width=12, command=lambda: self.move_to_top(rid))

    # ---------------------------------------------------------------- slot map
    def draw_cores(self):
        cv, cores = self.cm, self.view["cores"]
        cv.delete("all")
        self.core_head.set(cores["head"])
        n = len(cores["cpu"])
        cols = max(1, min(8, (n + 1) // 2))
        bw = (SIDE_W - 4) // cols

        def box(x, y, b):
            rid = b["holder"]
            fill = self.view["colour"].get(rid, COLOURS["agent"]) if rid else DARK["field"]
            outline, width = DARK["grid"], 1
            if b["outline"]:
                outline = COLOURS["booked"] if b["outline"] == "agent" else COLOURS.get(
                    b["outline"], COLOURS["whole"])
                width = 3
            cv.create_rectangle(x + 2, y + 2, x + bw - 2, y + CORE_H - 2, fill=fill,
                                outline=outline, width=width)
            ink = DARK["ink"] if rid else DARK["fg"]
            cv.create_text(x + 6, y + 5, anchor="nw", text=b["title"], font=("Segoe UI", 8), fill=ink)
            cv.create_text(x + 6, y + CORE_H - 5, anchor="sw", text=b["label"][:max(4, bw // 7)],
                           font=("Segoe UI", 8, "bold" if rid else "normal"), fill=ink)

        for i, b in enumerate(cores["cpu"]):
            box((i % cols) * bw, (i // cols) * CORE_H, b)
        y = -(-n // cols) * CORE_H
        for i, b in enumerate(cores["gpu"]):
            box((i % cols) * bw, y + (i // cols) * CORE_H, b)
        y += -(-len(cores["gpu"]) // cols) * CORE_H if cores["gpu"] else 0
        mem = self.view["memory"]
        if mem and mem.get("free_mb") is not None:
            used = 1 - mem["free_mb"] / mem["total_mb"]
            cv.create_rectangle(2, y + 4, SIDE_W - 2, y + 14, fill=DARK["field"], outline=DARK["grid"])
            cv.create_rectangle(2, y + 4, 2 + (SIDE_W - 4) * used, y + 14,
                                fill=COLOURS["late"] if used > 0.9 else COLOURS["agent"], outline="")
            cv.create_text(2, y + 16, anchor="nw", font=("Segoe UI", 8), fill=DARK["fg"],
                           text="memory: %s MB free of %s MB (%d%% in use)" % (
                               format(mem["free_mb"], ","), format(mem["total_mb"], ","), used * 100))
        else:
            cv.create_text(2, y + 4, anchor="nw", font=("Segoe UI", 8), fill=DARK["fg"],
                           text="memory: %s" % ("%s MB, free not reported" % format(mem["total_mb"], ",")
                                                if mem else "not reported"))
        cv.config(height=y + MEM_H + 2)

    # ---------------------------------------------------------------- drawing
    def cell_time(self, day, row):
        return m.cell_time(self.day0, day, row)

    def move(self, days):
        """< Earlier / Later >: the schedule starts `days` later, between BACK_DAYS ago and today."""
        self.offset = m.clamp_offset(self.offset + days)
        self.sel = self.drag = None
        self.draw()

    def draw(self):
        cv, now = self.cv, dt.datetime.now().astimezone()
        today = m.day_start(now)
        self.day0 = m.day_start(now, self.offset)
        cv.delete("all")
        for d in range(DAYS):
            x = LEFT + d * COL_W
            day = self.day0 + dt.timedelta(days=d)
            cv.create_text(x + COL_W / 2, TOP / 2, text=("Today " if day.date() == now.date() else "")
                           + day.strftime("%a %d %b"), font=("Segoe UI", 9, "bold"), fill=DARK["fg"])
            cv.create_line(x, TOP, x, TOP + ROWS * ROW_H, fill=DARK["grid_hour"])
        for r in range(ROWS + 1):
            y = TOP + r * ROW_H
            cv.create_line(LEFT, y, LEFT + DAYS * COL_W, y, fill=DARK["grid"] if r % 2 else DARK["grid_hour"])
            if r % 2 == 0 and r < ROWS:
                cv.create_text(LEFT - 4, y, text="%02d:00" % (r // 2), anchor="e",
                               font=("Segoe UI", 8), fill=DARK["fg"])
        for d in range(DAYS):                       # the past is greyed
            for r in range(ROWS):
                if self.cell_time(d, r + 1) <= now:
                    x, y = LEFT + d * COL_W, TOP + r * ROW_H
                    cv.create_rectangle(x + 1, y + 1, x + COL_W, y + ROW_H, fill=DARK["past"], outline="")
        if self.sel:
            d, a, b = self.sel
            x, y0, y1 = LEFT + d * COL_W, TOP + a * ROW_H, TOP + (b + 1) * ROW_H
            cv.create_rectangle(x + 2, y0 + 1, x + COL_W - 2, y1, fill=COLOURS["sel"], outline="")
        self.boxes = []
        lanes = self.view["lanes"]
        lw = STRIP_W / lanes
        for it in self.view["strip"]:
            lane = it.get("_lane", 0) % lanes
            fill = {"run": self.view["colour"].get(it["id"], COLOURS["agent"]), "wait": COLOURS["agent"],
                    "booking": COLOURS["booked"],
                    "hist": COLOURS["late"] if it.get("late") else COLOURS["ran"]}[it["type"]]
            for x, y0, y1 in self.segments(it["_s"], it["_e"]):
                x0 = x + COL_W - STRIP_X + lane * lw
                cv.create_rectangle(x0, y0 + 1, x0 + lw - (1 if lanes > 1 else 0), max(y1 - 1, y0 + 2),
                                    fill=fill, outline="",
                                    stipple="gray50" if it["open"] or it["type"] == "wait" else "")
            if it.get("_x"):
                for x, y0, y1 in self.segments(it["_s"], it["_x"]):
                    x0 = x + COL_W - STRIP_X + lane * lw
                    cv.create_rectangle(x0, y0 + 1, x0 + lw - 1, max(y1 - 1, y0 + 2),
                                        outline=COLOURS["expect"], width=1)
        for h in self.view["holds"]:
            key = "game" if h.get("owner") == "game" else h.get("scope")
            for x, y0, y1 in self.segments(h["_s"], h["_e"]):
                picked = h["id"] == self.picked
                cv.create_rectangle(x + 4, y0 + 1, x + COL_W - 26, y1 - 1,
                                    fill=COLOURS.get(key, COLOURS["whole"]),
                                    outline=DARK["pick"] if picked else "", width=3 if picked else 1)
                if y1 - y0 >= ROW_H:
                    cv.create_text(x + (COL_W - 22) / 2, y0 + 7, text="%s %s-%s" % (
                        key, h["_s"].strftime("%H:%M"), h["_e"].strftime("%H:%M")),
                        font=("Segoe UI", 8), fill="white")
                if key != "whole" and y1 - y0 >= 2 * ROW_H:
                    cv.create_text(x + (COL_W - 22) / 2, y0 + 7 + ROW_H, text=m.slots_text(h, short=True),
                                   font=("Segoe UI", 8), fill="white")
                self.boxes.append((x, y0, x + COL_W - 26, y1, h["id"]))
        d = (today - self.day0).days                # now line, in today's column when shown
        if 0 <= d < DAYS:
            y = TOP + (now - today).total_seconds() / 1800 * ROW_H
            cv.create_line(LEFT + d * COL_W, y, LEFT + (d + 1) * COL_W, y, fill=COLOURS["now"], width=2)
        self.draw_cores()

    def segments(self, s, e):
        """(x, y0, y1) pixel pieces of [s, e) clipped to the visible days (model.segments)."""
        return [(LEFT + d * COL_W, TOP + a * ROW_H, TOP + b * ROW_H)
                for d, a, b in m.segments(self.day0, s, e)]

    # ---------------------------------------------------------------- mouse
    def at(self, ev):
        d, r = int((ev.x - LEFT) // COL_W), int((ev.y - TOP) // ROW_H)
        return (d, r) if 0 <= d < DAYS and 0 <= r < ROWS and ev.x >= LEFT and ev.y >= TOP else None

    def hold(self, hid):
        return next((h for h in self.view["holds"] if h["id"] == hid), None)

    def pick(self, hid):
        h = self.hold(hid)
        self.picked, self.sel, self.drag = hid, None, None
        if h:
            self.info.set("Picked %s. Edit... changes it, Release ends it." % m.hold_line(h))
        self.draw()

    def press(self, ev):
        for x0, y0, x1, y1, hid in self.boxes:
            if x0 <= ev.x <= x1 and y0 <= ev.y <= y1:
                self.pick(hid)
                return
        c = self.at(ev)
        self.picked = None
        if c and ev.x >= LEFT + c[0] * COL_W + COL_W - STRIP_X - 2:
            s = self.cell_time(*c)                  # the strip: what holds, or held, that half hour
            hits = m.holders(self.view, s, s + dt.timedelta(minutes=30), owner=False)
            if hits:
                self.sel = self.drag = None
                self.info.set("%s: %s" % (s.strftime("%a %H:%M"), "; ".join(hits)))
                self.draw()
                return
        if c is None or self.cell_time(c[0], c[1] + 1) <= dt.datetime.now().astimezone():
            self.sel = self.drag = None
        else:
            self.drag = c
            self.sel = (c[0], c[1], c[1])
        self.draw()

    def motion(self, ev):
        if not self.drag:
            return
        d, r0 = self.drag
        r = min(max(int((ev.y - TOP) // ROW_H), 0), ROWS - 1)
        self.sel = (d, min(r0, r), max(r0, r))
        self.draw()

    def up(self, ev):
        if self.sel:
            s, e = self.window()
            hits = m.holders(self.view, s, e)
            self.info.set("Picked %s to %s. %s Press Reserve." % (
                s.strftime("%a %H:%M"), e.strftime("%a %H:%M"),
                ("Holding it: " + "; ".join(hits) + ".") if hits else "Nothing holds it."))
        self.drag = None

    def window(self):
        return m.drag_window(self.day0, self.sel, dt.datetime.now().astimezone())

    # ---------------------------------------------------------------- actions
    def done(self, title, res, quiet_ok=False):
        """Says how a write went; True when it went through. The window then shows the snapshot
        the write returned (no second read); a read already out is not shown."""
        text = act.result_text(res)
        self.gen += 1
        if res.get("snapshot"):
            self.apply((self.gen, res["snapshot"], None, None), False)
        elif res.get("ok"):                 # it went through but brought no snapshot back
            self.refresh(schedule=False)
        if res.get("ok"):
            if text or not quiet_ok:
                messagebox.showinfo(title, text or "done", parent=self.root)
        else:
            messagebox.showerror(title, text, parent=self.root)
        return bool(res.get("ok"))

    def reserve(self):
        if not self.sel:
            messagebox.showinfo(TITLE, "Drag down a day column to pick a window first.", parent=self.root)
            return
        s, e = self.window()
        hits = m.holders(self.view, s, e)
        if hits and not messagebox.askyesno("Reserve", "This window is held by:\n\n" + "\n".join(hits)
                                            + "\n\nReserve over it? Running work finishes; new runs and "
                                            "agent holds lose the slots your hold covers.", parent=self.root):
            return
        res = act.attempt(lambda: self.client.reserve(self.scope.get(), s, e, self.note.get()))
        if self.done("Reserve", res):
            self.sel = None
            self.draw()

    def release(self):
        h = self.hold(self.picked)
        if not h:
            messagebox.showinfo(TITLE, "Click a hold on the schedule first.", parent=self.root)
            return
        if not messagebox.askyesno("Release", m.release_prompt(h), parent=self.root):
            return
        self.picked = None
        self.done("Release", act.attempt(lambda: self.client.release(h["id"])))

    def move_to_top(self, rid):
        q = next((x for x in (self.snap.get("queue") or {}).get("entries") or []
                  if x.get("id") == rid), {})
        if not messagebox.askyesno("Move to top", (
                "Move run %s (%s/%s, %s) to the front of the queue?\n\nRuns holding slots keep "
                "them; it starts as soon as what it needs is free." % (
                    rid, q.get("repo") or "?", q.get("slug") or "?", q.get("kind") or "?")),
                parent=self.root):
            return
        self.done("Move to top", act.attempt(lambda: self.client.move_to_top(rid)), quiet_ok=True)

    def toggle_game(self):
        self.done("Game mode", act.attempt(lambda: self.client.game_mode(self.game_on.get())),
                  quiet_ok=True)

    def open_hold(self):
        return HoldDialog(self)

    def open_edit(self):
        h = self.hold(self.picked)
        if not h:
            messagebox.showinfo(TITLE, "Click a hold on the schedule first.", parent=self.root)
            return None
        return EditDialog(self, h)

    def unread(self):
        """True (and says so) while no snapshot has been shown: a settings dialog opened on an
        empty snapshot would save empty settings over the owner's."""
        if self.snap:
            return False
        messagebox.showinfo(TITLE, "The lock is not read yet; try again once the schedule shows.",
                            parent=self.root)
        return True

    def open_games(self):
        return None if self.unread() else GamesDialog(self)

    def open_division(self):
        return None if self.unread() else DivisionDialog(self)


class Dialog(tk.Toplevel):
    """A small modal window over the schedule: body() fills it, ok() acts."""

    def __init__(self, app, title):
        tk.Toplevel.__init__(self, app.root)
        self.app = app
        self.title(title)
        self.configure(bg=DARK["bg"])
        self.transient(app.root)
        self.frame = ttk.Frame(self, padding=10)
        self.frame.pack(fill="both", expand=True)
        self.body(self.frame)
        btns = ttk.Frame(self, padding=(10, 0, 10, 10))
        btns.pack(fill="x")
        for text, cmd in self.buttons():
            ttk.Button(btns, text=text, command=cmd).pack(side="right", padx=3)
        self.bind("<Escape>", lambda _e: self.destroy())

    def buttons(self):
        return [("Cancel", self.destroy), ("OK", self.ok)]

    def row(self, r, label, var, width=24, hint=""):
        ttk.Label(self.frame, text=label).grid(row=r, column=0, sticky="w", pady=2)
        e = ttk.Entry(self.frame, textvariable=var, width=width)
        e.grid(row=r, column=1, sticky="w", padx=6, pady=2)
        if hint:
            ttk.Label(self.frame, text=hint, style="Dim.TLabel").grid(row=r, column=2, sticky="w")
        return e

    def act(self, title, call):
        """Runs call() (a parser plus a Client write); closes on success. Any exception is shown
        (act.attempt) and the dialog stays open."""
        res = act.attempt(call)
        if "snapshot" not in res and not res.get("ok"):     # refused before any write
            messagebox.showerror(title, act.result_text(res), parent=self)
            return
        if self.app.done(title, res):
            self.destroy()


class HoldDialog(Dialog):
    """Hold...: an owner hold at typed times: whole, half or named slots (a whole hold is
    the owner's; slots names cpu and gpu slots)."""

    def __init__(self, app):
        Dialog.__init__(self, app, "New owner hold")

    def body(self, f):
        self.scope = tk.StringVar(value=self.app.scope.get())
        sc = ttk.Frame(f)
        sc.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        for text, val in (("Whole machine", "whole"), ("Half", "half"), ("Named slots", "slots")):
            ttk.Radiobutton(sc, text=text, variable=self.scope, value=val).pack(side="left", padx=(0, 10))
        start, end = ("", "+1h")
        if self.app.sel:
            s, e = self.app.window()
            start = "" if s <= dt.datetime.now().astimezone() + act.NOW_SLACK else s.strftime(TIME_FMT)
            end = e.strftime(TIME_FMT)
        self.start, self.end = tk.StringVar(value=start), tk.StringVar(value=end)
        self.note = tk.StringVar(value=self.app.note.get())
        self.cpu, self.gpu = tk.StringVar(), tk.StringVar()
        lay = self.app.snap.get("layout") or {}
        self.row(1, "Start", self.start, hint="blank: now; HH:MM or YYYY-MM-DD HH:MM")
        self.row(2, "End", self.end, hint="a time (HH:MM before the start: the next day), "
                                          "or +90m / +2h after the start")
        self.row(3, "Note", self.note, width=32)
        self.row(4, "CPU slots", self.cpu, hint="named slots only: 0-3, 6 (of 0-%s)" % (
            (lay.get("cpu_slots") or 1) - 1))
        self.row(5, "GPU slots", self.gpu, hint="named slots only (of %s)" % (
            m.ranges(range(lay.get("gpu_slots") or 0)) or "none"))

    def buttons(self):
        return [("Cancel", self.destroy), ("Create", self.ok)]

    def ok(self):
        self.act("Hold", lambda: self.app.client.hold(act.hold_spec(
            self.scope.get(), self.start.get(), self.end.get(), self.note.get(), self.cpu.get(),
            self.gpu.get(), dt.datetime.now().astimezone())))


class EditDialog(Dialog):
    """Edit...: the picked hold's start (an owner hold before it starts), end and note."""

    def __init__(self, app, hold):
        self.hold = hold
        Dialog.__init__(self, app, "Edit hold %s" % hold["id"])

    def body(self, f):
        h = self.hold
        ttk.Label(f, text=m.hold_line(h), wraplength=480).grid(row=0, column=0, columnspan=3,
                                                                sticky="w", pady=(0, 6))
        self.s0, self.e0 = h["_s"].strftime(TIME_FMT), h["_e"].strftime(TIME_FMT)
        self.start, self.end = tk.StringVar(value=self.s0), tk.StringVar(value=self.e0)
        self.note = tk.StringVar(value=h.get("note") or "")
        e = self.row(1, "Start", self.start, hint="HH:MM (on the hold's day) or YYYY-MM-DD HH:MM")
        if h.get("owner") != "owner" or h["_s"] <= dt.datetime.now().astimezone():
            e.state(["disabled"])
        self.row(2, "End", self.end, hint="a time, or +90m / +2h from now")
        self.row(3, "Note", self.note, width=32)

    def buttons(self):
        return [("Cancel", self.destroy), ("Save", self.ok)]

    def ok(self):
        def call():
            ch = act.edit_changes(self.hold, "" if self.start.get() == self.s0 else self.start.get(),
                                  "" if self.end.get() == self.e0 else self.end.get(), self.note.get(),
                                  dt.datetime.now().astimezone())
            if not ch:
                raise ValueError("nothing changed")
            return self.app.client.edit(self.hold["id"], ch)
        self.act("Edit hold", call)


class GamesDialog(Dialog):
    """Games...: the games list game mode watches for, one a line."""

    def __init__(self, app):
        Dialog.__init__(self, app, "Games")

    def body(self, f):
        g = self.app.snap.get("game") or {}
        ttk.Label(f, wraplength=480, text=(
            "One game a line: Exe.exe [pin|nopin] [name]. Left out, pin is off for a title known "
            "to use an anti-cheat, else on. Game mode %s." % ("is on" if g.get("enabled") else "is off"))
                  ).pack(anchor="w", pady=(0, 6))
        self.text = tk.Text(f, height=10, width=64, font=("Consolas", 9), bg=DARK["panel"],
                            fg=DARK["fg"], insertbackground=DARK["fg"])
        self.text.insert("1.0", act.games_text(g.get("games")))
        self.text.pack(fill="both", expand=True)
        ac = [x for x in g.get("games") or [] if x.get("anti_cheat")]
        if ac:
            ttk.Label(f, style="Dim.TLabel", text="anti-cheat: " + ", ".join(
                "%s (%s)" % (x.get("exe"), x["anti_cheat"]) for x in ac)).pack(anchor="w")

    def buttons(self):
        return [("Cancel", self.destroy), ("Save", self.ok)]

    def ok(self):
        self.act("Games", lambda: self.app.client.games(act.games_from_text(self.text.get("1.0", "end"))))


class DivisionDialog(Dialog):
    """Division...: the hardware the lock found and how it divides it: a field per
    division key, blank for its default; Defaults sets every default."""

    def __init__(self, app):
        Dialog.__init__(self, app, "Division")

    def body(self, f):
        snap = self.app.snap
        hw, lay = snap.get("hardware") or {}, snap.get("layout") or {}
        gpus = ", ".join("%s (%s MB)" % (g.get("name"), g.get("vram_mb")) for g in hw.get("gpus") or [])
        mem = (hw.get("memory_mb") or {}).get("total")
        lines = ["Hardware: %s threads, %d cores, %d CCD%s%s; gpus: %s" % (
            hw.get("threads"), len(hw.get("cores") or []), len(hw.get("ccds") or []),
            "" if len(hw.get("ccds") or []) == 1 else "s",
            ", %s MB memory" % format(mem, ",") if mem else "", gpus or "none"),
            "In force: %s cpu slots of %s threads, %s gpu slots, half = %s, agent cap %s, medium %s "
            "cores (from %s)" % (lay.get("cpu_slots"), lay.get("cpu_slot_threads"), lay.get("gpu_slots"),
                                 m.needs_text((lay.get("half") or {}).get("cpu") or 0,
                                              (lay.get("half") or {}).get("gpu") or 0),
                                 lay.get("agent_cap"),
                                 lay.get("medium_cores"), lay.get("source"))]
        wait = m.layout_wait(lay)
        if wait:
            lines.append(wait[0].upper() + wait[1:] + ("" if wait.endswith(".") else "."))
        lines += ["hardware: %s" % p for p in hw.get("problems") or []]
        ttk.Label(f, text="\n".join(lines), wraplength=560, justify="left").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        default = self.default = lay.get("default_division") or {}
        self.vars = {}          # only the keys the owner set (machine.json); blank: the default
        for r, (k, v) in enumerate(act.division_fields(lay.get("owner_division")).items(), start=1):
            self.vars[k] = tk.StringVar(value=v)
            d = default.get(k)
            self.row(r, k, self.vars[k], width=12, hint="default %s" % (
                ", ".join(map(str, d)) if isinstance(d, list) else d))

    def buttons(self):
        return [("Cancel", self.destroy), ("Save", self.ok), ("Defaults", self.defaults)]

    def ok(self):
        """Saves the fields. Every field blank writes the default division rather than removing
        machine.json (Defaults does that), and says so. Notes in machine.json (keys starting with
        _) are kept by the lock."""
        def call():
            d = act.division_from_fields({k: v.get() for k, v in self.vars.items()})
            if d is not None:
                return self.app.client.division(d)
            d = {k: v for k, v in self.default.items() if k in act.DIVISION_KEYS}
            if not d:
                raise ValueError("every field is blank and the default division is not known; "
                                 "Defaults removes the owner's division")
            res = self.app.client.division(d)
            if res.get("ok"):
                r = dict(res.get("result") or {})
                r["notes"] = list(r.get("notes") or []) + [
                    "Every field was blank, so machine.json now holds the default division (%s) "
                    "instead of being removed; Defaults removes it." % ", ".join(
                        "%s %s" % (k, ", ".join(map(str, v)) if isinstance(v, list) else v)
                        for k, v in d.items())]
                res = dict(res, result=r)
            return res
        self.act("Division", call)

    def defaults(self):
        self.act("Division", lambda: self.app.client.division(None))


def main(client=None):
    root = tk.Tk()
    App(root, client)
    root.mainloop()
