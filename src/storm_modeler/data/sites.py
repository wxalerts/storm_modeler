"""WSR-88D (NEXRAD) site table and nearest-site resolution.

``SITE_COORDS`` ships the complete operational WSR-88D network — ICAO
identifier mapped to ``(latitude, longitude, tower_elevation_m, name)``. The
table is plain data so it can be lifted wholesale into the production
``renderer.py`` later (Section 6). Coordinates are the published feedhorn
locations, accurate to roughly a hundred metres, which is far finer than the
nearest-site decision needs.

``SiteResolver`` maps a warning polygon to the single nearest covering radar
(Phase A: nearest to the polygon centroid).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import Polygon

# icao -> (lat, lon, elevation_m, name)
SITE_COORDS: dict[str, tuple[float, float, float, str]] = {
    # --- Northeast --------------------------------------------------------
    "KCBW": (46.0391, -67.8066, 227, "Caribou/Houlton, ME"),
    "KGYX": (43.8913, -70.2566, 125, "Portland, ME"),
    "KCXX": (44.5111, -73.1669, 97, "Burlington, VT"),
    "KTYX": (43.7558, -75.6800, 562, "Montague/Fort Drum, NY"),
    "KBGM": (42.1997, -75.9847, 490, "Binghamton, NY"),
    "KBUF": (42.9489, -78.7367, 211, "Buffalo, NY"),
    "KENX": (42.5864, -74.0639, 556, "Albany, NY"),
    "KOKX": (40.8656, -72.8639, 26, "Upton/New York City, NY"),
    "KBOX": (41.9558, -71.1369, 36, "Boston/Taunton, MA"),
    "KGSP": (34.8833, -82.2200, 287, "Greer/Greenville-Spartanburg, SC"),
    "KCCX": (40.9231, -78.0036, 733, "State College, PA"),
    "KDIX": (39.9469, -74.4108, 45, "Mount Holly/Philadelphia, NJ"),
    "KPBZ": (40.5317, -80.2181, 361, "Pittsburgh, PA"),
    "KCLE": (41.4131, -81.8597, 233, "Cleveland, OH"),
    "KLWX": (38.9753, -77.4778, 83, "Sterling/Washington DC, VA"),
    # --- Southeast / Mid-Atlantic ----------------------------------------
    "KAKQ": (36.9839, -77.0072, 33, "Wakefield/Norfolk, VA"),
    "KFCX": (37.0242, -80.2739, 874, "Roanoke/Blacksburg, VA"),
    "KRLX": (38.3111, -81.7228, 329, "Charleston, WV"),
    "KRAX": (35.6656, -78.4897, 113, "Raleigh/Durham, NC"),
    "KMHX": (34.7758, -76.8761, 9, "Morehead City/Newport, NC"),
    "KLTX": (33.9892, -78.4292, 20, "Wilmington, NC"),
    "KCAE": (33.9486, -81.1183, 70, "Columbia, SC"),
    "KCLX": (32.6556, -81.0422, 30, "Charleston, SC"),
    "KFFC": (33.3636, -84.5658, 263, "Atlanta/Peachtree City, GA"),
    "KJGX": (32.6753, -83.3511, 159, "Robins AFB/Macon, GA"),
    "KVAX": (30.8903, -83.0017, 54, "Moody AFB/Valdosta, GA"),
    "KTLH": (30.3975, -84.3289, 19, "Tallahassee, FL"),
    "KJAX": (30.4847, -81.7019, 10, "Jacksonville, FL"),
    "KMLB": (28.1133, -80.6542, 11, "Melbourne, FL"),
    "KAMX": (25.6111, -80.4128, 4, "Miami, FL"),
    "KBYX": (24.5975, -81.7031, 3, "Key West, FL"),
    "KTBW": (27.7056, -82.4017, 12, "Tampa Bay, FL"),
    "KTGH": (27.7056, -82.4017, 12, "Tampa Bay (alt), FL"),
    "KEVX": (30.5644, -85.9214, 43, "Eglin AFB, FL"),
    "KMOB": (30.6794, -88.2397, 63, "Mobile, AL"),
    "KEOX": (31.4603, -85.4592, 132, "Fort Rucker, AL"),
    "KBMX": (33.1722, -86.7697, 197, "Birmingham, AL"),
    "KHTX": (34.9306, -86.0833, 537, "Huntsville/Hytop, AL"),
    "KMXX": (32.5367, -85.7897, 122, "Maxwell AFB/Montgomery, AL"),
    "KGWX": (33.8967, -88.3289, 145, "Columbus AFB, MS"),
    "KDGX": (32.2800, -89.9844, 151, "Jackson/Brandon, MS"),
    "KNQA": (35.3447, -89.8733, 86, "Memphis/Millington, TN"),
    "KOHX": (36.2472, -86.5625, 176, "Nashville, TN"),
    "KMRX": (36.1686, -83.4017, 408, "Morristown/Knoxville, TN"),
    # --- Gulf Coast / Deep South -----------------------------------------
    "KLIX": (30.3367, -89.8256, 7, "New Orleans/Slidell, LA"),
    "KLCH": (30.1253, -93.2158, 4, "Lake Charles, LA"),
    "KPOE": (31.1556, -92.9758, 124, "Fort Polk, LA"),
    "KSHV": (32.4508, -93.8414, 83, "Shreveport, LA"),
    "KLZK": (34.8364, -92.2622, 173, "Little Rock, AR"),
    "KSRX": (35.2906, -94.3619, 195, "Fort Smith, AR"),
    # --- Texas ------------------------------------------------------------
    "KHGX": (29.4719, -95.0792, 5, "Houston/Galveston, TX"),
    "KCRP": (27.7842, -97.5111, 14, "Corpus Christi, TX"),
    "KBRO": (25.9161, -97.4189, 7, "Brownsville, TX"),
    "KDFX": (29.2728, -100.2806, 345, "Del Rio/Laughlin AFB, TX"),
    "KEWX": (29.7039, -98.0289, 193, "Austin/San Antonio, TX"),
    "KGRK": (30.7219, -97.3831, 164, "Fort Hood/Central Texas, TX"),
    "KFWS": (32.5731, -97.3031, 208, "Dallas/Fort Worth, TX"),
    "KDYX": (32.5383, -99.2542, 462, "Dyess AFB/Abilene, TX"),
    "KSJT": (31.3714, -100.4925, 576, "San Angelo, TX"),
    "KMAF": (31.9433, -102.1894, 874, "Midland/Odessa, TX"),
    "KLBB": (33.6542, -101.8142, 993, "Lubbock, TX"),
    "KAMA": (35.2333, -101.7092, 1093, "Amarillo, TX"),
    "KEPZ": (31.8731, -106.6981, 1251, "El Paso, TX"),
    "KFDX": (34.6342, -103.6189, 1417, "Cannon AFB/Clovis, NM"),
    "KHDX": (33.0764, -106.1228, 1287, "Holloman AFB/Alamogordo, NM"),
    "KABX": (35.1497, -106.8239, 1789, "Albuquerque, NM"),
    # --- Southern Plains --------------------------------------------------
    "KTLX": (35.3331, -97.2778, 370, "Oklahoma City/Twin Lakes, OK"),
    "KFDR": (34.3622, -98.9764, 386, "Frederick, OK"),
    "KVNX": (36.7406, -98.1278, 369, "Vance AFB/Enid, OK"),
    "KINX": (36.1750, -95.5644, 209, "Tulsa, OK"),
    "KICT": (37.6544, -97.4431, 407, "Wichita, KS"),
    "KDDC": (37.7608, -99.9689, 789, "Dodge City, KS"),
    "KGLD": (39.3669, -101.7003, 1113, "Goodland, KS"),
    "KTWX": (38.9969, -96.2325, 417, "Topeka, KS"),
    "KEAX": (38.8103, -94.2644, 303, "Kansas City/Pleasant Hill, MO"),
    "KSGF": (37.2353, -93.4006, 390, "Springfield, MO"),
    "KLSX": (38.6989, -90.6828, 185, "St. Louis, MO"),
    "KPAH": (37.0683, -88.7719, 119, "Paducah, KY"),
    "KHPX": (36.7369, -87.2853, 175, "Fort Campbell, KY"),
    "KLVX": (37.9753, -85.9439, 219, "Louisville, KY"),
    "KJKL": (37.5908, -83.3131, 416, "Jackson, KY"),
    # --- Midwest / Great Lakes -------------------------------------------
    "KILN": (39.4203, -83.8217, 322, "Cincinnati/Wilmington, OH"),
    "KILX": (40.1506, -89.3369, 177, "Lincoln/Central Illinois, IL"),
    "KLOT": (41.6044, -88.0847, 202, "Chicago/Romeoville, IL"),
    "KIWX": (41.3589, -85.7000, 293, "Northern Indiana, IN"),
    "KIND": (39.7075, -86.2803, 241, "Indianapolis, IN"),
    "KVWX": (38.2603, -87.7247, 173, "Evansville/Owensville, IN"),
    "KGRR": (42.8939, -85.5450, 237, "Grand Rapids, MI"),
    "KDTX": (42.6997, -83.4717, 327, "Detroit/Pontiac, MI"),
    "KAPX": (44.9072, -84.7197, 446, "Gaylord, MI"),
    "KMQT": (46.5311, -87.5483, 430, "Marquette, MI"),
    "KGRB": (44.4983, -88.1114, 208, "Green Bay, WI"),
    "KMKX": (42.9678, -88.5506, 292, "Milwaukee/Sullivan, WI"),
    "KARX": (43.8228, -91.1911, 389, "La Crosse, WI"),
    "KDLH": (46.8369, -92.2097, 435, "Duluth, MN"),
    "KMPX": (44.8489, -93.5656, 288, "Minneapolis/Chanhassen, MN"),
    "KDMX": (41.7311, -93.7228, 299, "Des Moines, IA"),
    "KDVN": (41.6117, -90.5808, 230, "Davenport/Quad Cities, IA"),
    "KFSD": (43.5878, -96.7294, 436, "Sioux Falls, SD"),
    "KABR": (45.4558, -98.4131, 397, "Aberdeen, SD"),
    "KUDX": (44.1250, -102.8297, 919, "Rapid City, SD"),
    "KOAX": (41.3203, -96.3669, 350, "Omaha/Valley, NE"),
    "KLNX": (41.9578, -100.5764, 916, "North Platte, NE"),
    "KUEX": (40.3208, -98.4419, 602, "Hastings, NE"),
    "KGID": (40.2653, -98.3742, 605, "Grand Island, NE"),
    # --- Northern / High Plains ------------------------------------------
    "KBIS": (46.7708, -100.7606, 505, "Bismarck, ND"),
    "KMVX": (47.5278, -97.3258, 300, "Grand Forks/Mayville, ND"),
    "KMBX": (48.3925, -100.8644, 455, "Minot AFB, ND"),
    "KFGF": (47.6800, -97.0019, 273, "Grand Forks AFB, ND"),
    "KGGW": (48.2064, -106.6253, 694, "Glasgow, MT"),
    "KTFX": (47.4597, -111.3853, 1132, "Great Falls, MT"),
    "KMSX": (47.0411, -113.9861, 2394, "Missoula, MT"),
    "KBLX": (45.8538, -108.6067, 1097, "Billings, MT"),
    "KCYS": (41.1519, -104.8061, 1868, "Cheyenne, WY"),
    "KRIW": (43.0661, -108.4772, 1712, "Riverton, WY"),
    "KFTG": (39.7867, -104.5458, 1675, "Denver/Front Range, CO"),
    "KGJX": (39.0622, -108.2136, 3046, "Grand Junction, CO"),
    "KPUX": (38.4595, -104.1814, 1600, "Pueblo, CO"),
    # --- Desert Southwest / Intermountain --------------------------------
    "KFSX": (34.5744, -111.1981, 2261, "Flagstaff, AZ"),
    "KIWA": (33.2892, -111.6700, 412, "Phoenix, AZ"),
    "KEMX": (31.8936, -110.6303, 1586, "Tucson, AZ"),
    "KYUX": (32.4953, -114.6567, 53, "Yuma, AZ"),
    "KICX": (37.5908, -112.8622, 3231, "Cedar City, UT"),
    "KMTX": (41.2628, -112.4478, 1969, "Salt Lake City, UT"),
    "KESX": (35.7011, -114.8914, 1483, "Las Vegas, NV"),
    "KRGX": (39.7542, -119.4622, 2530, "Reno, NV"),
    "KLRX": (40.7397, -116.8025, 2056, "Elko/Battle Mountain, NV"),
    "KCBX": (43.4906, -116.2353, 933, "Boise, ID"),
    "KSFX": (43.1056, -112.6861, 1364, "Pocatello/Idaho Falls, ID"),
    # --- West Coast -------------------------------------------------------
    "KMUX": (37.1553, -121.8983, 1057, "San Francisco/Monterey Bay, CA"),
    "KDAX": (38.5011, -121.6778, 9, "Sacramento, CA"),
    "KBBX": (39.4961, -121.6317, 53, "Beale AFB/Chico, CA"),
    "KBHX": (40.4986, -124.2919, 732, "Eureka, CA"),
    "KHNX": (36.3142, -119.6322, 73, "San Joaquin Valley/Hanford, CA"),
    "KVTX": (34.4117, -119.1797, 831, "Los Angeles/Oxnard, CA"),
    "KSOX": (33.8178, -117.6361, 933, "Santa Ana Mountains, CA"),
    "KNKX": (32.9189, -117.0419, 291, "San Diego, CA"),
    "KEYX": (35.0978, -117.5608, 877, "Edwards AFB, CA"),
    "KMAX": (42.0811, -122.7172, 2290, "Medford, OR"),
    "KRTX": (45.7150, -122.9656, 479, "Portland, OR"),
    "KPDT": (45.6906, -118.8528, 462, "Pendleton, OR"),
    "KATX": (48.1947, -122.4956, 151, "Seattle/Camano, WA"),
    "KOTX": (47.6803, -117.6267, 727, "Spokane, WA"),
    "KLGX": (47.1167, -124.1067, 100, "Langley Hill/Hoquiam, WA"),
    # --- Alaska -----------------------------------------------------------
    "PABC": (60.7919, -161.8767, 49, "Bethel, AK"),
    "PAPD": (65.0351, -147.5014, 794, "Fairbanks/Pedro Dome, AK"),
    "PAHG": (60.7259, -151.3514, 74, "Anchorage/Kenai, AK"),
    "PAKC": (58.6794, -156.6294, 19, "King Salmon, AK"),
    "PAIH": (59.4614, -146.3031, 20, "Middleton Island, AK"),
    "PAEC": (64.5114, -165.2950, 16, "Nome, AK"),
    "PACG": (56.8528, -135.5292, 33, "Sitka/Biorka Island, AK"),
    "PGUA": (13.4558, 144.8111, 80, "Andersen AFB, Guam"),
    # --- Hawaii / Pacific -------------------------------------------------
    "PHKI": (21.8939, -159.5524, 55, "Kauai/South Kauai, HI"),
    "PHKM": (20.1253, -155.7783, 1162, "Kohala/Kamuela, HI"),
    "PHMO": (21.1328, -157.1803, 416, "Molokai, HI"),
    "PHWA": (19.0950, -155.5689, 421, "South Shore/Hawaii, HI"),
    # --- Puerto Rico ------------------------------------------------------
    "TJUA": (18.1156, -66.0781, 851, "San Juan, PR"),
}


@dataclass(frozen=True)
class Site:
    icao: str
    lat: float
    lon: float
    elevation_m: float
    name: str


def get_site(icao: str) -> Site:
    lat, lon, elev, name = SITE_COORDS[icao]
    return Site(icao=icao, lat=lat, lon=lon, elevation_m=elev, name=name)


def all_sites() -> list[Site]:
    return [get_site(icao) for icao in SITE_COORDS]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class SiteResolver:
    """Resolve a warning polygon to the nearest covering WSR-88D site.

    Phase A: nearest site to the polygon centroid, single site. The full CONUS
    table is consulted so any CONUS warning resolves deterministically.
    """

    def __init__(self, coords: dict | None = None) -> None:
        self._coords = coords or SITE_COORDS

    def nearest(self, lat: float, lon: float) -> Site:
        best_icao = min(
            self._coords,
            key=lambda icao: _haversine_km(
                lat, lon, self._coords[icao][0], self._coords[icao][1]
            ),
        )
        return get_site(best_icao)

    def for_polygon(self, polygon: Polygon) -> Site:
        c = polygon.centroid
        return self.nearest(lat=float(c.y), lon=float(c.x))
