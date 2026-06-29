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
    {"cmd": "lightning", "points": [[lat, lon, t_frac], ...]}  # GLM flashes
    {"cmd": "lightning_clear"}
    {"cmd": "satellite", "url": "data:image/png;base64,...", "bounds": [[s,w],[n,e]]}
    {"cmd": "satellite_clear"}
    {"cmd": "satellite_opacity", "value": 0.55}  # BT overlay opacity 0..1
    {"cmd": "cloudtops",  "geojson": {...}}  # cloud-top cells (anvil/OT markers)
    {"cmd": "stormtrack", "geojson": {...}}  # one storm's history (line + dots)
    {"cmd": "stormtrack_clear"}
    {"cmd": "fit",       "bounds": [[s,w],[n,e]]}
    {"cmd": "clear"}
    {"cmd": "snapshot",  "path": "/tmp/x.png"}   # test aid
Prints ``MAP_READY`` to stdout once the page has loaded.
"""

import json
import os
import signal
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
var radarLayer=null, warningLayer=null, cellsLayer=null, hiLayer=null, lightningLayer=null;
var satLayer=null, cloudtopLayer=null, trackLayer=null;
// Canvas renderer: thousands of flash markers paint far faster than SVG.
var lightningCanvas = L.canvas({padding:0.5});
function setRadar(url, b){ if(radarLayer)map.removeLayer(radarLayer);
  radarLayer=L.imageOverlay(url,b,{opacity:0.7}).addTo(map);}
function setWarning(gj){ if(warningLayer)map.removeLayer(warningLayer);
  warningLayer=L.geoJSON(gj,{style:{color:'#fff',weight:2.5,fill:false}}).addTo(map);}
function setCells(gj){ if(cellsLayer)map.removeLayer(cellsLayer);
  cellsLayer=L.geoJSON(gj,{style:{color:'#ff0',weight:2,fill:false}}).addTo(map);}
function highlightCell(gj){ if(hiLayer)map.removeLayer(hiLayer);
  hiLayer=L.geoJSON(gj,{style:{color:'#fff',weight:4,fill:false}}).addTo(map);}
// GLM flashes: small dots ramped oldest(blue)->newest(red) by time fraction.
function setLightning(pts){ if(lightningLayer)map.removeLayer(lightningLayer);
  if(!pts||!pts.length){lightningLayer=null;return;}
  var ms=pts.map(function(p){var hue=Math.round(240*(1-p[2]));
    return L.circleMarker([p[0],p[1]],{renderer:lightningCanvas,radius:3,
      stroke:false,fillColor:'hsl('+hue+',100%,55%)',fillOpacity:0.85});});
  lightningLayer=L.layerGroup(ms).addTo(map);}
function clearLightning(){ if(lightningLayer)map.removeLayer(lightningLayer);
  lightningLayer=null;}
// GOES ABI Band-13 brightness temperature: a translucent image overlay under
// the cloud-top markers (sits above radar so cold tops read on the IR). The
// opacity is user-controlled and persists across scene updates.
var satOpacity=0.55;
function setSatellite(url,b){ if(satLayer)map.removeLayer(satLayer);
  satLayer=L.imageOverlay(url,b,{opacity:satOpacity}).addTo(map);}
function setSatelliteOpacity(v){ satOpacity=v; if(satLayer)satLayer.setOpacity(v);}
function clearSatellite(){ if(satLayer)map.removeLayer(satLayer); satLayer=null;
  if(cloudtopLayer)map.removeLayer(cloudtopLayer); cloudtopLayer=null;}
// Cloud-top cells: anvil/footprint polygons + coldest-pixel markers (overshooting
// tops magenta + larger, cores cyan). Popups carry BT and storm tilt.
function setCloudtops(gj){ if(cloudtopLayer)map.removeLayer(cloudtopLayer);
  cloudtopLayer=L.geoJSON(gj,{
    pointToLayer:function(f,ll){var ot=f.properties.kind==='ot';
      return L.circleMarker(ll,{radius:ot?7:4,
        color:ot?'#f0f':'#0ff',weight:2,fillOpacity:0.55});},
    style:function(f){return {color:f.properties.kind==='anvil'?'#88f':'#0cf',
      weight:1.5,fill:false};},
    onEachFeature:function(f,l){var p=f.properties;var s='BT '+p.min_bt_k+' K';
      if(p.tilt_km!==undefined)s+=' · tilt '+p.tilt_km+' km @ '+p.tilt_bearing_deg+'°';
      l.bindPopup(s);}
  }).addTo(map);}
// Storm track: an orange polyline through the storm's history with a dot per
// volume (overshooting-top volumes larger + red). Popups carry time/dBZ/top.
function setStormTrack(gj){ if(trackLayer)map.removeLayer(trackLayer);
  trackLayer=L.geoJSON(gj,{
    style:function(f){return {color:'#ff8c1a',weight:3,opacity:0.9};},
    pointToLayer:function(f,ll){var ot=f.properties.kind==='ot';
      return L.circleMarker(ll,{radius:ot?7:4,color:ot?'#ff3030':'#ff8c1a',
        weight:2,fillColor:ot?'#ff3030':'#ffd24a',fillOpacity:0.9});},
    onEachFeature:function(f,l){var p=f.properties;if(p.kind==='line')return;
      var s=p.time+' · '+p.dbz+' dBZ · top '+p.top_km+' km';
      if(p.kind==='ot')s+=' · OT';l.bindPopup(s);}
  }).addTo(map);}
function clearStormTrack(){ if(trackLayer)map.removeLayer(trackLayer);
  trackLayer=null;}
function fitBounds(b){ map.fitBounds(b,{padding:[25,25], animate:false}); }
function panTo(lat, lon){ map.panTo([lat,lon],{animate:false}); }
function setProduct(p){ document.getElementById('prod').textContent = p; }
function clearAll(){ [radarLayer,warningLayer,cellsLayer,hiLayer,lightningLayer,
  satLayer,cloudtopLayer,trackLayer].forEach(function(l){if(l)map.removeLayer(l);});
  radarLayer=warningLayer=cellsLayer=hiLayer=lightningLayer=null;
  satLayer=cloudtopLayer=trackLayer=null;}
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
        elif cmd == "lightning":
            self._js(f"setLightning({json.dumps(msg.get('points', []))});")
        elif cmd == "lightning_clear":
            self._js("clearLightning();")
        elif cmd == "satellite":
            self._js(f"setSatellite({json.dumps(msg['url'])}, {json.dumps(msg['bounds'])});")
        elif cmd == "satellite_clear":
            self._js("clearSatellite();")
        elif cmd == "satellite_opacity":
            self._js(f"setSatelliteOpacity({float(msg['value'])});")
        elif cmd == "cloudtops":
            self._js(f"setCloudtops({json.dumps(msg['geojson'])});")
        elif cmd == "stormtrack":
            self._js(f"setStormTrack({json.dumps(msg['geojson'])});")
        elif cmd == "stormtrack_clear":
            self._js("clearStormTrack();")
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

    def _shutdown(*_a) -> bool:
        # Single graceful exit path: persist window geometry, then quit. Used by
        # both stdin-EOF and the SIGTERM/SIGINT handlers below.
        _save_geom(app.win)
        Gtk.main_quit()
        return False  # one-shot: remove the GLib source

    # The parent reaps us with SIGTERM on app exit (see MapClient.shutdown).
    # The OS default for SIGTERM would kill us *before* the stdin-EOF watch runs,
    # losing the geometry — so route both signals through the GLib main loop so
    # geometry is written first. (Plain signal.signal won't fire promptly while
    # blocked in the C-level Gtk.main(); GLib.unix_signal_add integrates it.)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _shutdown)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _shutdown)

    def on_stdin(fd, condition):
        # IO_HUP fires when the parent closes the pipe with no pending data; must
        # be handled (and watched) explicitly — a plain IO_IN watch never wakes on
        # hangup, so the EOF-save path silently never ran.
        if condition & GLib.IO_HUP:
            return _shutdown()
        line = sys.stdin.readline()
        if not line:  # EOF: parent closed the pipe → save geometry and exit
            return _shutdown()
        line = line.strip()
        if line:
            try:
                app.handle(json.loads(line))
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"bad command: {e}\n")
        return True

    GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN | GLib.IO_HUP, on_stdin)
    Gtk.main()


if __name__ == "__main__":
    main()
