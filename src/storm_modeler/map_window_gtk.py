"""Standalone GTK/WebKit Leaflet map window (separate process).

Qt WebEngine (Chromium) renders a black surface on this project's target GL
stack, but WebKitGTK renders fine — so the map runs as its own GTK process and
the Qt app drives it over a pipe. This module is intentionally dependency-light:
**stdlib + PyGObject only** (no storm_modeler imports, no numpy), so it runs
under the system ``python3`` that carries the gi/WebKit2 bindings, independent
of the app's virtualenv.

Protocol: one JSON object per line on stdin. Commands:
    {"cmd": "radar",     "url": "data:image/png;base64,...", "bounds": [[s,w],[n,e]]}
    {"cmd": "warning",   "geojson": {...}}
    {"cmd": "cells",     "geojson": {...}}
    {"cmd": "highlight", "geojson": {...}}
    {"cmd": "fit",       "bounds": [[s,w],[n,e]]}
    {"cmd": "clear"}
    {"cmd": "snapshot",  "path": "/tmp/x.png"}   # test aid
Prints ``MAP_READY`` to stdout once the page has loaded.
"""

import json
import os
import sys

# XWayland + WebKit software compositing: no GDK GL context needed (it fails
# on the offscreen/Wayland path), and rendering is reliable.
os.environ.setdefault("GDK_BACKEND", "x11")
os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

HTML = """<!DOCTYPE html><html><head><meta charset='utf-8'>
<link rel='stylesheet' href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css'/>
<script src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'></script>
<style>html,body,#map{height:100%;margin:0;background:#111;}
#prod{position:absolute;top:10px;right:10px;z-index:1000;background:rgba(0,0,0,.6);
 color:#fff;padding:4px 10px;font-family:sans-serif;font-size:14px;border-radius:4px;}
</style></head>
<body><div id='map'></div><div id='prod'></div><script>
// Animations off: WebKit software compositing (no GL) does not paint Leaflet's
// CSS transform animations, which leaves tiles blank after a zoom/fit.
var map = L.map('map',{attributionControl:false, zoomAnimation:false,
                       fadeAnimation:false, markerZoomAnimation:false})
            .setView([39,-98], 5);
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
            {maxZoom:18}).addTo(map);
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
            {maxZoom:18}).addTo(map);
var radarLayer=null, warningLayer=null, cellsLayer=null, hiLayer=null;
function setRadar(url, b){ if(radarLayer)map.removeLayer(radarLayer);
  radarLayer=L.imageOverlay(url,b,{opacity:0.7}).addTo(map);}
function setWarning(gj){ if(warningLayer)map.removeLayer(warningLayer);
  warningLayer=L.geoJSON(gj,{style:{color:'#fff',weight:2.5,fill:false}}).addTo(map);}
function setCells(gj){ if(cellsLayer)map.removeLayer(cellsLayer);
  cellsLayer=L.geoJSON(gj,{style:{color:'#ff0',weight:2,fill:false}}).addTo(map);}
function highlightCell(gj){ if(hiLayer)map.removeLayer(hiLayer);
  hiLayer=L.geoJSON(gj,{style:{color:'#fff',weight:4,fill:false}}).addTo(map);}
function fitBounds(b){ map.fitBounds(b,{padding:[25,25], animate:false}); }
function panTo(lat, lon){ map.panTo([lat,lon],{animate:false}); }
function setProduct(p){ document.getElementById('prod').textContent = p; }
function clearAll(){ [radarLayer,warningLayer,cellsLayer,hiLayer].forEach(
  function(l){if(l)map.removeLayer(l);}); radarLayer=warningLayer=cellsLayer=hiLayer=null;}
</script></body></html>"""


_GEOM_PATH = os.environ.get("STORM_MODELER_MAP_GEOM", "")


def _load_geom():
    try:
        with open(_GEOM_PATH) as fh:
            g = json.load(fh)
        return int(g["w"]), int(g["h"]), int(g["x"]), int(g["y"])
    except Exception:  # noqa: BLE001
        return None


def _save_geom(win) -> None:
    if not _GEOM_PATH:
        return
    try:
        w, h = win.get_size()
        x, y = win.get_position()
        with open(_GEOM_PATH, "w") as fh:
            json.dump({"w": w, "h": h, "x": x, "y": y}, fh)
    except Exception:  # noqa: BLE001
        pass


class MapApp:
    def __init__(self) -> None:
        self.win = Gtk.Window(title="Storm Modeler — Map")
        geom = _load_geom()
        if geom:
            w, h, x, y = geom
            self.win.set_default_size(w, h)
            self.win.move(x, y)
        else:
            self.win.set_default_size(1000, 760)
        self.win.connect("destroy", self._on_destroy)
        self.view = WebKit2.WebView()
        self.win.add(self.view)
        self.win.show_all()
        self._ready = False
        self._pending: list[str] = []
        self.view.connect("load-changed", self._on_load)
        self.view.load_html(HTML, "https://storm-modeler.local/")

    def _on_destroy(self, *_a) -> None:
        _save_geom(self.win)
        Gtk.main_quit()

    def _on_load(self, view, event) -> None:
        if event == WebKit2.LoadEvent.FINISHED and not self._ready:
            self._ready = True
            for js in self._pending:
                self._js(js)
            self._pending.clear()
            sys.stdout.write("MAP_READY\n")
            sys.stdout.flush()

    def _js(self, script: str) -> None:
        if not self._ready:
            self._pending.append(script)
            return
        self.view.run_javascript(script, None, None, None)

    def handle(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "radar":
            self._js(f"setRadar({json.dumps(msg['url'])}, {json.dumps(msg['bounds'])});")
            if msg.get("product"):
                self._js(f"setProduct({json.dumps(msg['product'])});")
        elif cmd == "warning":
            self._js(f"setWarning({json.dumps(msg['geojson'])});")
        elif cmd == "cells":
            self._js(f"setCells({json.dumps(msg['geojson'])});")
        elif cmd == "highlight":
            self._js(f"highlightCell({json.dumps(msg['geojson'])});")
        elif cmd == "fit":
            self._js(f"fitBounds({json.dumps(msg['bounds'])});")
        elif cmd == "pan":
            self._js(f"panTo({msg['lat']}, {msg['lon']});")
        elif cmd == "clear":
            self._js("clearAll();")
        elif cmd == "snapshot":
            self._snapshot(msg.get("path", "/tmp/map_snapshot.png"))

    def _snapshot(self, path: str) -> None:
        def done(v, res, _):
            try:
                surf = v.get_snapshot_finish(res)
                surf.write_to_png(path)
                sys.stdout.write(f"SNAPSHOT {path}\n")
                sys.stdout.flush()
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"snapshot failed: {e}\n")
        self.view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE,
                               WebKit2.SnapshotOptions.NONE, None, done, None)


def main() -> None:
    app = MapApp()

    def on_stdin(fd, condition):
        line = sys.stdin.readline()
        if not line:  # EOF: parent closed the pipe → save geometry and exit
            _save_geom(app.win)
            Gtk.main_quit()
            return False
        line = line.strip()
        if line:
            try:
                app.handle(json.loads(line))
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"bad command: {e}\n")
        return True

    GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN, on_stdin)
    Gtk.main()


if __name__ == "__main__":
    main()
