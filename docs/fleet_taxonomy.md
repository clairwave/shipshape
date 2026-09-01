# World-fleet taxonomy → fallback archetypes

Class comes from the AIS static data ship-type code (message 5 / class-B 24),
NOT the MMSI (MMSI's MID prefix only encodes flag state). The time-machine DB
stores this code per MMSI along with dimension fields A/B/C/D → LOA + beam,
which we use to scale (and disambiguate) archetypes.

## AIS ship-type code map (ITU-R M.1371)

| Code | AIS meaning | Archetype folder |
|---|---|---|
| 0 | Not available | by-dims heuristic → `other_generic` |
| 1–19 | Reserved | `other_generic` |
| 20–29 | Wing-in-ground | `highspeed_craft` (rare) |
| 30 | Fishing | `fishing` |
| 31, 32 | Towing (incl. long/wide tow) | `tug_workboat` |
| 33 | Dredging/underwater ops | `dredger_special` |
| 34 | Diving ops | `tug_workboat` |
| 35 | Military ops | `patrol_military` |
| 36 | Sailing | `sailing_yacht` |
| 37 | Pleasure craft | `pleasure_motor` |
| 40–49 | High-speed craft | `highspeed_craft` |
| 50 | Pilot vessel | `patrol_pilot_small` |
| 51 | Search and rescue | `patrol_pilot_small` |
| 52 | Tug | `tug_workboat` |
| 53 | Port tender | `patrol_pilot_small` |
| 54 | Anti-pollution | `tug_workboat` |
| 55 | Law enforcement | `patrol_military` |
| 58 | Medical transport | `passenger_ferry` |
| 56, 57, 59 | Spare/local/noncombatant | `other_generic` |
| 60–69 | Passenger | LOA ≥ 180m → `passenger_cruise`, else `passenger_ferry` |
| 70–79 | Cargo | see cargo split below |
| 80–89 | Tanker | LOA ≥ 180m → `tanker_crude`, else `tanker_product` |
| 90–99 | Other | by-dims heuristic → `other_generic` |

**Cargo split (codes 70–79 can't distinguish container/bulker/general):**
- LOA ≥ 250m → `cargo_container` (ULCV/neo-panamax overwhelmingly)
- 120–250m: beam/LOA < 0.145 → `cargo_container`, else `cargo_bulker`
  (bulkers and tankers are beamier; containerships are slender) — imperfect,
  refined later by photo when one exists
- LOA < 120m → `cargo_general` (coasters, MPVs)

## The 15 archetype folders (assets/archetypes/<name>/)

1.  `cargo_container`   — cellular containership, deck stacks
2.  `cargo_bulker`      — bulk carrier, hatch covers, deck cranes
3.  `cargo_general`     — coaster/MPV, boxy single superstructure aft
4.  `tanker_crude`      — VLCC/suezmax, long flush deck, manifold amidships
5.  `tanker_product`    — MR/handysize product & chem, deck piping
6.  `passenger_cruise`  — high white superstructure, balconies
7.  `passenger_ferry`   — ro-pax, vehicle deck doors, medium size
8.  `fishing`           — trawler/seiner, gantries, net drums
9.  `tug_workboat`      — high bow, low aft deck, towing gear
10. `offshore_supply`   — OSV/PSV: forward house, long open cargo deck
11. `sailing_yacht`     — masts, rigging (mesh challenge: keep simple)
12. `pleasure_motor`    — motoryacht/cabin cruiser
13. `highspeed_craft`   — catamaran ferry/crew boat
14. `patrol_pilot_small`— pilot/SAR/tender small fast utility
15. `patrol_military`   — grey hull, patrol profile
16. `dredger_special`   — dredger/crane/special ops silhouette
17. `other_generic`     — neutral small merchant profile

(15 primary + `patrol_military`/`dredger_special` optional merges if
population is a burden — merge 15→`patrol_pilot_small`, 16→`tug_workboat`.)

`offshore_supply` has no dedicated AIS code — most OSVs broadcast 70s/90s/52;
assign by photo or operator lists later; archetype exists because the
silhouette is distinctive and the class is huge in AIS traffic.

## Population guidance (drop into each folder)

3–6 clean, open-water, broadside-ish photos per folder (the approved-input
profile: single vessel, no port clutter). Each photo in a folder becomes an
archetype VARIANT: we generate one GLB per photo, and photoless vessels of
that class get a variant assigned deterministically (hash(MMSI) % n_variants)
so the fleet doesn't look cloned. AIS dims then scale x/y/z per vessel.

## Assignment precedence per MMSI

1. Own usable photo (passes QC gate) → unique GLB
2. Ship-type code + dims → archetype variant, scaled to AIS dims
3. No type, no dims → `other_generic`, default scale by class-B/A guess
