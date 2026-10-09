"""
Geospatial Recommendation API with Road Network Linkages
Route: /search/
Query Parameters / Body:
  - lat: float (Current Latitude)
  - long: float (Current Longitude)
  - cat: str (Category)
  - rad: float (Search Radius)
  - link: str / file (Road linkage information txt file)

Ranking:
  - Radius filter: Circular (Euclidean) distance <= rad
  - Proximity ranking: Grid traversal distance along the road network graph
  - Returns: 10 closest recommended location IDs
"""

import sys
import os
import csv
import json
import math
import heapq
import hashlib
import argparse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request
from collections import defaultdict
import numpy as np


class SpatialEngine:
    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        self.cat_data = {}
        self.loc_coords = {}     # id -> (lat, lon, cat)
        self.coord_to_id = {}    # (round(lat,6), round(lon,6)) -> id
        self.default_adj = {}    # Full grid graph
        self.graph_cache = {}    # Cache for parsed linkage graphs: cache_key -> adj
        self.load_data()
        self.build_default_grid()

    def load_data(self):
        raw_cats = {}
        with open(self.csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cat = row["Category"].strip().lower()
                loc_id = int(row["ID"])
                lat = float(row["Latitude"])
                lon = float(row["Longitude"])
                
                self.loc_coords[loc_id] = (lat, lon, cat)
                self.coord_to_id[(round(lat, 6), round(lon, 6))] = loc_id
                
                if cat not in raw_cats:
                    raw_cats[cat] = {"ids": [], "lats": [], "longs": []}
                raw_cats[cat]["ids"].append(loc_id)
                raw_cats[cat]["lats"].append(lat)
                raw_cats[cat]["longs"].append(lon)

        for cat, data in raw_cats.items():
            self.cat_data[cat] = {
                "ids": np.array(data["ids"], dtype=np.int32),
                "lats": np.array(data["lats"], dtype=np.float64),
                "longs": np.array(data["longs"], dtype=np.float64),
            }
        print(f"Indexed {len(self.loc_coords)} locations across {len(self.cat_data)} categories.")

    def build_default_grid(self):
        """Build standard 100x100 full grid graph (each node connected to 4 cardinal neighbors)."""
        print("Constructing default 100x100 grid road network...")
        adj = {nid: [] for nid in self.loc_coords}
        for i in range(100):
            for j in range(100):
                u = i * 100 + j + 1
                lat_u, lon_u, _ = self.loc_coords[u]
                # Right neighbor
                if j + 1 < 100:
                    v = i * 100 + (j + 1) + 1
                    lat_v, lon_v, _ = self.loc_coords[v]
                    w = abs(lat_u - lat_v) + abs(lon_u - lon_v)
                    adj[u].append((v, w))
                    adj[v].append((u, w))
                # Down neighbor
                if i + 1 < 100:
                    v = (i + 1) * 100 + j + 1
                    lat_v, lon_v, _ = self.loc_coords[v]
                    w = abs(lat_u - lat_v) + abs(lon_u - lon_v)
                    adj[u].append((v, w))
                    adj[v].append((u, w))
        self.default_adj = adj

    def get_node_id(self, lat: float, lon: float) -> int:
        i = int(round(lat * 99.0))
        j = int(round(lon * 99.0))
        if 0 <= i < 100 and 0 <= j < 100:
            return i * 100 + j + 1
        return self.coord_to_id.get((round(lat, 6), round(lon, 6)))

    def parse_links(self, link_source: str) -> dict:
        """Parse into adjacency graph.
        """
        if not link_source:
            # Fallback to local link.txt if available, else default grid
            if os.path.isfile("link.txt"):
                link_source = "link.txt"
            else:
                return self.default_adj

        # Check if cached
        cache_key = link_source if len(link_source) < 512 else hashlib.md5(link_source.encode()).hexdigest()
        if cache_key in self.graph_cache:
            return self.graph_cache[cache_key]

        content = ""
        # 1. Check if it's a file on disk
        candidates_paths = [
            link_source,
            os.path.join(os.getcwd(), link_source),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), link_source),
            os.path.join("/tmp", link_source)
        ]
        file_found = False
        for path in candidates_paths:
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    file_found = True
                    break
                except Exception as e:
                    print(f"Error reading file {path}: {e}")

        # 2. Check if it's a URL
        if not file_found and (link_source.startswith("http://") or link_source.startswith("https://")):
            try:
                req = urllib.request.Request(link_source, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    content = resp.read().decode("utf-8", errors="ignore")
                file_found = True
            except Exception as e:
                print(f"Error fetching linkage URL {link_source}: {e}")

        # 3. Otherwise treat link_source as raw text content if not a path
        if not file_found:
            content = link_source

        if not content.strip():
            return self.default_adj

        adj = {nid: [] for nid in self.loc_coords}
        edge_count = 0

        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            
            # Format: 4 floats -> Longitude_A Latitude_A Longitude_B Latitude_B
            if len(parts) >= 4:
                try:
                    lon_a, lat_a = float(parts[0]), float(parts[1])
                    lon_b, lat_b = float(parts[2]), float(parts[3])
                    u = self.get_node_id(lat_a, lon_a)
                    v = self.get_node_id(lat_b, lon_b)
                    if u and v and u in self.loc_coords and v in self.loc_coords:
                        w = abs(lat_a - lat_b) + abs(lon_a - lon_b)
                        adj[u].append((v, w))
                        adj[v].append((u, w))
                        edge_count += 1
                except ValueError:
                    pass
            # Format: 2 integers -> ID_A ID_B
            elif len(parts) >= 2:
                try:
                    u, v = int(parts[0]), int(parts[1])
                    if u in self.loc_coords and v in self.loc_coords:
                        lat_u, lon_u, _ = self.loc_coords[u]
                        lat_v, lon_v, _ = self.loc_coords[v]
                        w = abs(lat_u - lat_v) + abs(lon_u - lon_v)
                        adj[u].append((v, w))
                        adj[v].append((u, w))
                        edge_count += 1
                except ValueError:
                    pass

        print(f"Loaded road network graph from '{link_source[:60]}' with {edge_count} linkages.")
        self.graph_cache[cache_key] = adj
        return adj

    def query(self, lat: float, lon: float, cat: str, rad: float, link: str = None, top_k: int = 10):
        cat_key = cat.strip().lower()
        if cat_key not in self.cat_data:
            return []

        # Step 1: Candidate filtering by circular radius and category
        data = self.cat_data[cat_key]
        dlat = data["lats"] - lat
        dlon = data["longs"] - lon

        dist_sq = dlat * dlat + dlon * dlon
        rad_sq = rad * rad + 1e-9
        circ_mask = dist_sq <= rad_sq

        if not np.any(circ_mask):
            return []

        valid_candidate_ids = set(data["ids"][circ_mask].tolist())
        id_to_circ_sq = dict(zip(data["ids"][circ_mask].tolist(), dist_sq[circ_mask].tolist()))

        # Step 2: Retrieve road network graph
        adj = self.parse_links(link)

        # Step 3: Find starting grid nodes for the query origin (lat, lon)
        # multi-source Dijkstra from the 4 closest surrounding grid corners
        # On a 100x100 grid:
        grid_i = lat * 99.0
        grid_j = lon * 99.0
        i0, i1 = max(0, min(99, math.floor(grid_i))), max(0, min(99, math.ceil(grid_i)))
        j0, j1 = max(0, min(99, math.floor(grid_j))), max(0, min(99, math.ceil(grid_j)))
        
        corner_coords = set([(i0, j0), (i0, j1), (i1, j0), (i1, j1)])
        start_nodes = []
        for ci, cj in corner_coords:
            nid = ci * 100 + cj + 1
            nlat, nlon, _ = self.loc_coords[nid]
            init_cost = abs(nlat - lat) + abs(nlon - lon)
            start_nodes.append((init_cost, nid))

        # Step 4: Shortest path traversal via Dijkstra's algorithm
        dist = {}
        pq = []
        for init_cost, nid in start_nodes:
            dist[nid] = init_cost
            heapq.heappush(pq, (init_cost, nid))

        found_candidates = {}  # cand_id -> shortest_traversal_dist
        
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue

            if u in valid_candidate_ids and u not in found_candidates:
                found_candidates[u] = d
                # Early stopping: once we have >= top_k candidates and the next distance
                # in pq is strictly greater than the top_k-th found distance, we can stop
                if len(found_candidates) >= top_k:
                    # Check distance of top_k-th candidate
                    kth_dist = sorted(found_candidates.values())[top_k - 1]
                    if d > kth_dist + 1e-9:
                        break

            for v, w in adj.get(u, []):
                nd = d + w
                if v not in dist or nd < dist[v]:
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))

        # Step 5: Rank valid candidates
        # If any candidate was not reached by Dijkstra (disconnected component), assign infinity
        ranked = []
        for cid in valid_candidate_ids:
            trav_dist = found_candidates.get(cid, float("inf"))
            c_sq = id_to_circ_sq[cid]
            ranked.append((round(trav_dist, 8), round(c_sq, 8), cid))

        # Multi-tier sort:
        # 1. Traversal distance on grid road network
        # 2. Circular distance (Euclidean)
        # 3. Location ID (ascending)
        ranked.sort()

        top_ids = [item[2] for item in ranked[:top_k]]
        return top_ids


# Global engine instance
ENGINE = None


class RecommendationHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")

    def _send_json_response(self, status_code: int, data):
        response_bytes = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(response_bytes)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _extract_request_params(self) -> dict:
        parsed_url = urlparse(self.path)
        params = {}

        # 1. First extract URL query string params
        if parsed_url.query:
            query_params = parse_qs(parsed_url.query)
            for k, v in query_params.items():
                params[k] = v[0]

        # 2. Next, check if there is a request body (e.g. POST, or GET with body)
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            body = self.rfile.read(content_length)
            content_type = self.headers.get("Content-Type", "")
            
            # Try parsing JSON body
            body_params = {}
            if "application/json" in content_type or body.strip().startswith(b"{"):
                try:
                    body_params = json.loads(body.decode("utf-8"))
                except Exception:
                    pass
            else:
                try:
                    form_params = parse_qs(body.decode("utf-8"))
                    body_params = {k: v[0] for k, v in form_params.items()}
                except Exception:
                    pass

            # Body parameters override or supplement query parameters
            for k, v in body_params.items():
                params[k] = v

        return params

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path

        if path in ("/search/", "/search"):
            params = self._extract_request_params()
            self._handle_search(params)
        elif path in ("/", "/health", "/health/"):
            self._send_json_response(200, {
                "status": "healthy",
                "service": "Location Recommendation API",
                "route": "/search/",
                "fields": ["lat", "long", "cat", "rad", "link"]
            })
        else:
            self._send_json_response(404, {"error": f"Path '{path}' not found. Please use '/search/'."})

    def do_POST(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path

        if path not in ("/search/", "/search"):
            self._send_json_response(404, {"error": f"Path '{path}' not found. Please use '/search/'."})
            return

        params = self._extract_request_params()
        self._handle_search(params)

    def _handle_search(self, params: dict):
        # Extract lat
        lat_val = params.get("lat") or params.get("latitude")
        # Extract long
        lon_val = params.get("long") or params.get("lon") or params.get("lng") or params.get("longitude")
        # Extract cat
        cat_val = params.get("cat") or params.get("category")
        # Extract rad
        rad_val = params.get("rad") or params.get("radius")
        # Extract link
        link_val = params.get("link") or params.get("linkage") or params.get("links") or ""

        # Validate required fields
        missing = []
        if lat_val is None: missing.append("lat")
        if lon_val is None: missing.append("long")
        if cat_val is None: missing.append("cat")
        if rad_val is None: missing.append("rad")

        if missing:
            self._send_json_response(400, {
                "error": f"Missing required parameter(s): {', '.join(missing)}",
                "expected_fields": ["lat", "long", "cat", "rad", "link"]
            })
            return

        try:
            lat = float(lat_val)
            lon = float(lon_val)
            rad = float(rad_val)
            cat = str(cat_val).strip()
            link = str(link_val).strip() if link_val else None
        except ValueError as e:
            self._send_json_response(400, {
                "error": f"Invalid numerical format for coordinates or radius: {str(e)}"
            })
            return

        # Perform recommendation query with road network linkages
        top_ids = ENGINE.query(lat=lat, lon=lon, cat=cat, rad=rad, link=link, top_k=10)

        # Check format preference
        format_pref = params.get("format", "").lower()
        if format_pref == "dict":
            response_payload = {"locations": top_ids}
        else:
            response_payload = top_ids

        self._send_json_response(200, response_payload)


def run_server(host="0.0.0.0", port=5000, csv_path="locations.csv"):
    global ENGINE
    ENGINE = SpatialEngine(csv_path)

    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, RecommendationHandler)
    print(f"\n=======================================================")
    print(f" Location Recommendation API Server Running")
    print(f" Host: {host}  |  Port: {port}")
    print(f" Route: http://{host}:{port}/search/")
    print(f" Parameters: ?lat=<float>&long=<float>&cat=<str>&rad=<float>&link=<txt_path>")
    print(f"=======================================================\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        httpd.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Location Recommendation API Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)), help="Port (default: 5000)")
    parser.add_argument("--csv", default="locations.csv", help="Path to locations CSV file (default: locations.csv)")
    args = parser.parse_args()

    run_server(host=args.host, port=args.port, csv_path=args.csv)
