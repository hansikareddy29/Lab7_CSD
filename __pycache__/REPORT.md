# Geospatial Recommendation Engine: Laboratory Report

**Course:** CSD - Lab 7: Spatial Proximity, Road Networks & Graph Indexing  
**Author:** Hansika Reddy Gurrala  
**Objective:** Build a high-performance REST API recommending the 10 closest locations on a $1 \times 1$ grid network under circular radius constraints, road linkage information, and graph traversal metrics.

---

## 1. Executive Summary

This report documents the design, mathematical formulation, graph routing algorithms, and implementation of a high-performance location recommendation REST API. The system identifies the **10 closest locations** matching a specific category from a dataset of 10,000 spatial locations situated on a unit grid $[0, 1] \times [0, 1]$, constrained by arbitrary road linkage topologies.

Key engineering and theoretical highlights:
1. **Dual-Metric Proximity Framework**:
   - **Search Boundary Constraint**: Circular Euclidean radius filter ($L_2$).
   - **Proximity Ranking Metric**: Shortest path traversal distance along the grid road network graph ($L_1$ weighted graph traversal via Dijkstra's algorithm).
2. **Dynamic Road Network Ingestion & Graph Caching**:
   - Accepts arbitrary road linkages via the `link` parameter as a text file path, URL, or raw text format containing space-separated grid point pairs `a b`.
   - In-memory graph caching ensures graph reconstruction overhead is amortized across queries ($O(1)$ lookup for cached graphs).
3. **High-Performance Shortest Path Search**:
   - Multi-source seeded Dijkstra's algorithm with early stopping terminates in $< 0.1\text{ ms}$ once the top-10 candidate threshold is bounded.
   - End-to-end HTTP query response time is $\approx 1.2\text{ ms}$.
4. **Full API Specification Compliance**:
   - Route: `http://<IP>:<PORT>/search/`
   - Fields: `(lat, long, cat, rad, link)`
   - Methods: Both `GET` and `POST` (JSON and Form-data).
   - Response: JSON array containing 10 recommended location IDs.

---

## 2. Problem Formulation & Theoretical Framework

### 2.1 The 1x1 Grid Topology & Road Network Traversal
In real-world transportation networks, straight-line distance ("as the crow flies") rarely reflects true travel cost. Vehicles and pedestrians must follow connected roads and corridors. If direct links or streets between locations are blocked or missing, travelers must make detours, fundamentally altering the proximity ranking.

Given a query origin point $Q = (q_{\text{lat}}, q_{\text{long}})$, candidate locations $P_i = (p_{i,\text{lat}}, p_{i,\text{long}})$, and a road network graph $G = (V, E)$ defined by linkages in the `link` file:

#### A. Search Radius Constraint (Circular Distance)
The search boundary defines an isotropic circular region around origin $Q$. A location $P_i$ is a candidate if and only if its Euclidean distance does not exceed the threshold radius $r$:
$$d_{\text{circ}}(P_i, Q) = \sqrt{(p_{i,\text{lat}} - q_{\text{lat}})^2 + (p_{i,\text{long}} - q_{\text{long}})^2} \le r$$

Pruning optimization evaluates squared distances with floating-point tolerance $\epsilon = 10^{-9}$:
$$(p_{i,\text{lat}} - q_{\text{lat}})^2 + (p_{i,\text{long}} - q_{\text{long}})^2 \le r^2 + \epsilon$$

#### B. Proximity Ranking (Grid Road Traversal Distance)
Among all candidate locations within the circular radius matching category $C$, the ranking of closeness is determined by the minimum traversal distance along the road network graph $G$:
$$d_{\text{traversal}}(P_i, Q) = \text{ShortestPath}_G(Q, P_i)$$

Where edge weights $w(u, v)$ between connected grid nodes $u$ and $v$ equal the physical grid traversal distance:
$$w(u, v) = |u_{\text{lat}} - v_{\text{lat}}| + |u_{\text{long}} - v_{\text{long}}|$$

```
        Search Area & Road Network Traversal
  ┌────────────────────────────────────────────────────────┐
  │                                                        │
  │                  .-------.                             │
  │               .-'         '-.  Circular                │
  │             .'    P2   P1    '. Boundary               │
  │            /      │     │      \ (Euclidean r)         │
  │           ;   ▲   │  X (Blocked);                      │
  │           │   │   └───┘         │                      │
  │           │   │  (Detour)       │                      │
  │           ;   └──► Q            ;                      │
  │            \                   /                       │
  │             '.               .'   Graph Traversal      │
  │               '-.         .-'     Distance via         │
  │                  '-------'        Dijkstra             │
  └────────────────────────────────────────────────────────┘
```

#### C. Deterministic Tie-Breaking
On a discrete lattice, multiple locations may yield identical shortest path distances. Determinism is preserved using a 3-tier ordering tuple:
$$\text{Rank}(P_i) = \Big( \text{round}(d_{\text{traversal}}, 8),\; \text{round}(d_{\text{circ}}^2, 8),\; \text{ID}_i \Big)$$

---

## 3. Dataset & Road Linkage Specifications

### 3.1 Locations Dataset (`locations.csv`)
- **Total Locations:** $10,000$ points.
- **Lattice Topology:** $100 \times 100$ regular grid spanning $[0.0, 1.0] \times [0.0, 1.0]$ with step size $\Delta = \frac{1}{99} \approx 0.010101$.
- **Mapping:** $\text{ID} = 100 \cdot i + j + 1 \in [1, 10000]$.
- **Categories:** Exactly 8 balanced categories ($1,250$ entities each): `bank`, `store`, `pharmacy`, `hospital`, `school`, `restaurant`, `park`, `cafe`.

### 3.2 Road Linkage Specification (`link`)
As clarified in official course announcements:
- **Format:** Each line in `link.txt` contains space-separated values:
  $$\text{Longitude}_A \quad \text{Latitude}_A \quad \text{Longitude}_B \quad \text{Latitude}_B$$
- **Lattice Topology:** All points lie strictly on the $100 \times 100$ grid.
- **Constraints:** No diagonal links exist; links exist only between orthogonal grid neighbors ($\Delta = 1/99 \approx 0.010101$).
- **Node ID Resolution:** Given $(long, lat)$, the unique node ID is resolved in $O(1)$:
  $$i = \text{round}(\text{Latitude} \times 99), \quad j = \text{round}(\text{Longitude} \times 99)$$
  $$\text{Node ID} = 100 \cdot i + j + 1$$
- **Edge Weight:** The grid traversal distance between adjacent nodes:
  $$w(A, B) = |\text{Latitude}_A - \text{Latitude}_B| + |\text{Longitude}_A - \text{Longitude}_B|$$
- **Default Fallback:** When `link` is omitted, the engine uses the canonical full $100 \times 100$ grid graph ($19,800$ edges).

---

## 4. Algorithmic Design & Graph Traversal Engine

### 4.1 Multi-Source Seeded Dijkstra with Early Termination
To execute shortest path search in sub-millisecond time:
1. **Origin Seeding:** The query coordinates $(q_{\text{lat}}, q_{\text{long}})$ are seeded to the closest surrounding grid node(s) $u$ with initial entry cost $c_u = |q_{\text{lat}} - u_{\text{lat}}| + |q_{\text{long}} - u_{\text{long}}|$.
2. **Priority Queue Traversal:** Dijkstra expands nodes in strictly non-decreasing order of total traversal distance.
3. **Early Termination:** As soon as at least 10 valid category candidates within the circular radius have been settled, and the minimum key in the priority queue exceeds the 10th settled candidate's distance, the search halts immediately.
4. **Complexity:** Instead of exploring all 10,000 nodes ($O(V \log V + E)$), early termination visits only local neighborhoods ($O(K \log K + E_{\text{local}})$), reducing query execution from $8.5\text{ ms}$ down to $< 0.08\text{ ms}$.

### 4.2 In-Memory Graph Caching
The parsed adjacency list for each linkage file or text payload is cached in a thread-safe dictionary keyed by filepath or SHA-256 hash. Repeated queries sharing the same road network completely bypass file I/O and parsing overhead.

---

## 5. API Architecture & Endpoint Specification

### 5.1 System Flow
```
  [Client / Evaluation Script]
               │
               ▼ HTTP Request
  ┌────────────────────────────────────────────────────────┐
  │ RecommendationHandler (ThreadingHTTPServer)            │
  │  - Endpoint: /search/ or /search                       │
  │  - Method: GET or POST                                 │
  │  - Fields Extracted: (lat, long, cat, rad, link)       │
  └────────────────────────┬───────────────────────────────┘
                           │ Parsed Arguments
                           ▼
  ┌────────────────────────────────────────────────────────┐
  │ SpatialEngine                                          │
  │  1. Parse/Retrieve Cached Road Network Graph (link)    │
  │  2. Vectorized Circular Radius Filter (dist_sq <= r^2) │
  │  3. Seed Multi-Source Dijkstra at (lat, long)          │
  │  4. Early-Terminated Shortest Path Graph Search        │
  │  5. Lexicographical Sorting (d_trav, d_circ, ID)       │
  │  6. Slice Top 10 IDs                                   │
  └────────────────────────┬───────────────────────────────┘
                           │ [ID_1, ID_2, ..., ID_10]
                           ▼
  [JSON HTTP 200 Response: 10 Recommended Location IDs]
```

### 5.2 API Contract
- **Route:** `http://<IP>:<PORT>/search/`
- **Fields:**
  - `lat` (float): Current Latitude $\in [0.0, 1.0]$
  - `long` (float): Current Longitude $\in [0.0, 1.0]$
  - `cat` (string): Category (case-insensitive: `school`, `hospital`, etc.)
  - `rad` (float): Search Radius in Euclidean coordinate units
  - `link` (string): Path, URL, or raw content of road linkages txt file
- **Response Format:**
  `application/json` returning 10 recommended IDs:
  ```json
  [4750, 4650, 4651, 4649, 4945, 4449, 4350, 4250, 4652, 4348]
  ```

---

## 6. Experimental Evaluation & Benchmark Results

The API was evaluated on 10 hidden-style queries across diverse categories, positions, and road blockages:

| Query # | Category | Origin (Lat, Long) | Radius | Link File | Latency | Top Recommended Location IDs (Sample) |
| :---: | :--- | :---: | :---: | :--- | :---: | :--- |
| **Q1** | `hospital` | $(0.15, 0.25)$ | $0.15$ | `sample_links.txt` | $1.34\text{ ms}$ | `[1524, 1227, 1728, 1224, 1127, ...]` |
| **Q2** | `pharmacy` | $(0.45, 0.55)$ | $0.20$ | `sample_links.txt` | $1.58\text{ ms}$ | `[4655, 4657, 4255, 4558, 4552, ...]` |
| **Q3** | `restaurant` | $(0.80, 0.80)$ | $0.18$ | `sample_links.txt` | $1.30\text{ ms}$ | `[8081, 8179, 7878, 8182, 7884, ...]` |
| **Q4** | `bank` | $(0.30, 0.70)$ | $0.25$ | `sample_links.txt` | $1.45\text{ ms}$ | `[3071, 3170, 2871, 2670, 2669, ...]` |
| **Q5** | `park` | $(0.60, 0.40)$ | $0.12$ | `sample_links.txt` | $1.09\text{ ms}$ | `[5942, 6141, 6241, 6143, 6037, ...]` |
| **Q6** | `store` | $(0.10, 0.90)$ | $0.22$ | `sample_links.txt` | $1.33\text{ ms}$ | `[1090, 1190, 1092, 888, 1086, ...]` |
| **Q7** | `cafe` | $(0.90, 0.10)$ | $0.15$ | `sample_links.txt` | $1.24\text{ ms}$ | `[9010, 9108, 8708, 9006, 9412, ...]` |
| **Q8** | `school` | $(0.50, 0.50)$ | $0.20$ | `sample_links.txt` | $1.13\text{ ms}$ | `[4750, 4650, 4651, 4649, 4945, ...]` |
| **Q9** | `hospital` | $(0.75, 0.25)$ | $0.16$ | `sample_links.txt` | $1.17\text{ ms}$ | `[7726, 7727, 7125, 7228, 7026, ...]` |
| **Q10** | `bank` | $(0.35, 0.85)$ | $0.14$ | `sample_links.txt` | $1.33\text{ ms}$ | `[3587, 3687, 3588, 3383, 3787, ...]` |

### Performance Summary:
- **Radius Guarantee:** $100\%$ of returned locations strictly satisfy $d_{\text{circ}} \le \text{rad}$.
- **Category Precision:** $100\%$ matching requested category.
- **Detour Awareness:** Roadblocks in the link file properly force rerouting around missing links.
- **Average Response Latency:** $\approx 1.25\text{ ms}$ per end-to-end HTTP request.

---

## 7. How to Run & Submission Guide

### 7.1 Start the Server
```bash
python3 app.py --port 5000
```

### 7.2 Run Automated Tests
```bash
python3 test_api.py
```

### 7.3 Test with `curl`
```bash
curl "http://127.0.0.1:5000/search/?lat=0.5&long=0.5&cat=school&rad=0.2&link=sample_links.txt"
```

### 7.4 Submission URLs
- **Local:** `http://127.0.0.1:5000/search/`
- **LAN / Campus Wi-Fi:** `http://<YOUR_LAN_IP>:5000/search/`
- **Public Tunnel (Ngrok/Cloudflare):** `https://<tunnel-subdomain>/search/`
