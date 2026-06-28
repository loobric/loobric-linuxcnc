"""GladeVCP handler for the Smooth status panel.

Surfaces the smooth-linuxcnc sync client's state to the operator inside a
running LinuxCNC GUI. It is READ-ONLY with respect to tools: it never edits the
tool table and never intercepts M6. It only reflects the `summary` the sync
client persists to state-<machine>.json, and offers a button to run a sync.

    green   - fully in sync
    yellow  - in sync, but a tool bind is still pending
    red     - a tool mount is REQUESTED: the operator must mount it
    stale   - no sync yet, or the last sync is too old to trust

The button runs `smooth-linuxcnc sync` - the same command the cron job runs -
so all server I/O stays in one place. This panel only displays and requests;
it does not duplicate any sync logic.
"""
import os
import sys
import time
import subprocess

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib

# Reuse the client's own config/state-path logic so the panel and the client
# can never disagree about WHERE the state lives or WHICH machine it is. When
# the client is pip-installed it is importable directly; running from a source
# checkout, fall back to the repo root (three levels up from this example).
try:
    import smooth_linuxcnc as sl
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    import smooth_linuxcnc as sl

POLL_MS = 2000            # how often to re-read the state file
STALE_SECONDS = 15 * 60   # a "good" sync older than this is shown as stale

COLORS = {
    "green": "#2e7d32",
    "yellow": "#c9a000",
    "red": "#c62828",
    "stale": "#777777",
}
NO_SYNC_MESSAGE = "No sync yet - press Sync"


class HandlerClass:
    def __init__(self, halcomp, builder):
        self.hal = halcomp
        self.builder = builder
        self.led = builder.get_object('led')
        self.message = builder.get_object('message')
        self.sync_button = builder.get_object('sync_button')
        self._syncing = False
        self._proc = None

        # One CSS provider, installed once, recoloured on each state change
        # (re-adding a provider every poll would leak providers).
        self._css = Gtk.CssProvider()
        if self.led is not None:
            self.led.get_style_context().add_provider(
                self._css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        try:
            self.config = sl.load_config()
        except Exception:
            self.config = {}
        self.machine = self.config.get("MACHINE_NAME") or "default"
        try:
            self.state_file = sl._state_path(self.config, self.machine)
        except Exception:
            self.state_file = None

        self.refresh()
        GLib.timeout_add(POLL_MS, self._tick)

    # --- display -----------------------------------------------------------
    def _tick(self):
        # While a sync is in flight, poll for its completion instead of
        # refreshing (which would clobber the "Syncing..." message); poll()
        # also reaps the child so it never lingers as a zombie.
        if self._syncing:
            if self._proc is None or self._proc.poll() is not None:
                self._proc = None
                self._syncing = False
                if self.sync_button is not None:
                    self.sync_button.set_sensitive(True)
                self.refresh()
        else:
            self.refresh()
        return True  # keep the timer alive

    def refresh(self):
        summary = self._read_summary()
        health = summary.get("health") or "stale"
        message = summary.get("message") or NO_SYNC_MESSAGE

        # A green/yellow health that is stale can't be trusted (e.g. the server
        # was unreachable since, so the client never updated the file).
        if health in ("green", "yellow") and self._is_stale(summary):
            health = "stale"
            message = "Last sync stale (%s) - press Sync" % \
                summary.get("last_sync_local", "unknown")

        self._set_led(health)
        if self.message is not None:
            self.message.set_text(message)
            self.message.set_tooltip_text(message)

    def _read_summary(self):
        if not self.state_file:
            return {}
        try:
            return sl._load_state(self.state_file).get("summary") or {}
        except Exception:
            return {}

    def _is_stale(self, summary):
        last = summary.get("last_sync")
        if not last:
            return True
        try:
            return (time.time() - float(last)) > STALE_SECONDS
        except (TypeError, ValueError):
            return True

    def _set_led(self, health):
        color = COLORS.get(health, COLORS["stale"])
        if self.led is not None:
            self.led.set_text(health.upper())
            css = ("label { background-color: %s; color: #ffffff; "
                   "font-weight: bold; padding: 6px; }" % color)
            self._css.load_from_data(css.encode("utf-8"))

    # --- sync action -------------------------------------------------------
    def on_sync_clicked(self, widget, data=None):
        if self._syncing or not self.state_file:
            return
        # Run the same module the cron job runs, with the same interpreter,
        # non-blocking so the GUI stays responsive. _tick() polls for the
        # child's exit and then refreshes from the freshly written state.
        argv = [sys.executable, os.path.abspath(sl.__file__),
                "sync", self.machine]
        try:
            self._proc = subprocess.Popen(argv)
        except Exception as e:
            if self.message is not None:
                self.message.set_text("Sync failed to start: %s" % e)
            return
        self._syncing = True
        if self.sync_button is not None:
            self.sync_button.set_sensitive(False)
        if self.message is not None:
            self.message.set_text("Syncing...")


def get_handlers(halcomp, builder, useropts=None):
    return [HandlerClass(halcomp, builder)]
