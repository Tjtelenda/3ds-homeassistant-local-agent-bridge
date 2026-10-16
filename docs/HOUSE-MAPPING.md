# Mapping a house for the bridge

How the author's house became a walkable sprite map, what you would
need to do to make one of your own, and — honestly — what state all of
this is in today.

This repository contains the bridge only. The author's level spec,
compiler, diagrams, sprites, and house layout are **not** published:
they are literally his house. This guide documents the method so you
can produce your own.

## What we did

The house was reduced to a tile grid and a JSON level spec. The spec —
not any drawing — is the single source of truth; the map, the
collision, and the bridge's interactables all derive from it.

- **Grid.** 16 tiles wide × 32 tiles tall, 16×16 px source tiles,
  roughly 2 ft per tile — the whole single-story house on one map.
  Origin at the north-west corner; x runs east, y runs south.
- **Rooms as zones.** Every tile belongs to a room zone (15 in the
  current spec). Each room declares a rectangle (a couple are unions of
  rectangles, for a notched office and a wide landing) and a floor
  type.
- **Walls by rule, not by hand.** A wall exists on every edge between
  two different zones, except where a door, an opening, or an open-plan
  join crosses it. Nobody drew walls tile by tile; the rule derives
  them, and the same rule blocks movement.
- **Doors.** 10 door/opening entries. On screen a door is a gap in the
  wall plus a mat — never a swing arc, in top-down style. Swing and
  hinge direction exist only as movement logic. Some openings are
  cased openings with no door at all.
- **Objects.** 74 placed objects, each with an id, a room, a tile
  rectangle, a layer, and a blocking flag:
  - layer 0 — floors and rug decals (never blocking)
  - layer 1 — furniture and fixtures (blocking)
  - layer 2 — overhead (reserved; unused in this house)
  - layer 3 — wall-mounted items: TVs, the thermostat, light switches
- **Interactables.** One small wall switch plate per room or area
  (16 of them), each grouping that room's lights and lamps, so the
  player presses A at the switch instead of hunting for individual
  bulbs. TVs, the front-door lock, the vacuum, and two PCs are direct
  targets. Each interactable maps to real Home Assistant entities in
  the bridge's `config.json` — the spec holds logical ids, the config
  holds entity ids, and anything with no real entity behind it
  honestly reports `mapped: false`.
- **Provenance.** Every object and asset is flagged *photo-verified*,
  *user-confirmed layout*, or *assumed*. Assumed placements (office
  chairs, some bathroom fixtures, switch positions) are styled freely
  and never presented as fact. Rooms were photographed before art was
  commissioned.
- **Verification.** Collision is precomputed from blocking objects
  plus the wall rule, and a reachability check confirms every
  interactable can actually be approached from the player's spawn
  point before a build ships.

## What you would need to do

1. **Get a floor plan.** A builder plan, a listing schematic, or your
   own tape-measure sketch. Decide your scale: ~2 ft per tile put a
   whole house on a small handheld screen. A much larger or multi-story
   home will want scrolling maps or one map per floor.
2. **Draw the zone map.** Assign every tile a room code. Mark doors,
   open cased openings, and open-plan joins — the wall rule does the
   rest.
3. **Write your level spec.** A JSON file with the grid, the rooms
   (rectangle + floor), the doors, and the objects (id, room, tile
   rectangle, layer, blocking, interactable binding if any). This is
   the main piece of work, and it is data entry, not programming.
4. **Decide your interactables.** List the smart devices you actually
   have and how they should group (the one-switch-per-room grouping is
   worth copying). Put the Home Assistant entity ids in the bridge's
   `config.json`, following `config.example.json`.
5. **Create the assets** (next section).
6. **Build or adapt a client.** Your client renders the spec, handles
   movement and collision from the same wall rule, and talks to the
   bridge endpoints documented in the README. Read state from
   `/v1/worldstate`, send interactions to `/v1/interact`, and poll
   `/v1/reply` if you use chat. **The author's client is not
   published** — see "What state is this in" below.
7. **Verify before you call it done.** Check every interactable is
   reachable from spawn, and round-trip one real device through the
   bridge (`/v1/worldstate` → `/v1/interact` → state changed) before
   mapping the rest.

## Assets you would need created

Style: top-down ¾ view (Pokémon LeafGreen-like). Source tiles are
16×16 px; an object's canvas is its footprint in tiles × 16 px;
everything is upscaled with nearest-neighbor, never smoothed.

- **Floor tiles** — one tileable design per flooring type in your home
  (2–3 seamless variants each), plus large rug decals if you want them.
- **A wall system** — exterior and interior wall tiles in north, south,
  east, and west facings, plus corners and T-junctions, window
  variants, a sliding-door piece, and a garage-door piece if relevant.
- **Doorway pieces** — a gap treatment and a mat per door. No swing
  arcs.
- **Furniture and fixtures, per room, at their real footprints** —
  sofas, beds, desks, kitchen counters and island, tub, shower,
  toilet, washer/dryer, shelves. Blocking pieces need to look right
  from the camera at their actual size, not icon size.
- **Wall items (layer 3)** — TVs (with on and off screen states), a
  thermostat, and one switch plate per room with on/off artwork.
- **State variants for anything that visibly changes** — a TV that's
  on, a lamp that's lit. If a device changes in the real house, its
  sprite needs both looks or a readable indicator.
- **Characters** — a player sprite sheet (the author's is 32×36 px
  frames, four directions, idle/walk/run), plus any household members
  or pets you want wandering, with sit/sleep/walk variants for pets.
- **Exterior (optional)** — house front and roof edge, driveway, lawn,
  street, mailbox, patio and fence, if your map extends outside.

**The one art rule that matters most:** commission or draw every piece
*from the camera position it will actually be seen from, in its actual
place*. A piece on a north wall is drawn front-on; a piece on an east
or west wall is drawn in side/¾ profile. A front-view sprite pasted
onto a side wall reads as furniture floating in the middle of the
floor — this was the single most common defect in early builds. State
the cardinal facing and the wall for every asset you request, and
audit placed pieces against room photos before ordering more.

## What state is this in — what can you use now?

- **The bridge: usable today.** It runs in production, daily, at the
  author's house. If you run Home Assistant and can write (or adapt) a
  client against the documented protocol, you can use it now, as-is.
  It is also useful without any game client at all — it is a small,
  authenticated, config-driven HTTP front-end to Home Assistant with a
  chat path to a local agent.
- **The mapping method: documented, not packaged.** This guide plus
  the bridge's config example are what exist. The author's spec file,
  compiler, and diagrams are private, and there is no map editor — you
  write your spec as data, following the structure above.
- **The game client: not published.** No renderer, no compiler, no
  asset pack, and none of the author's sprites or art are in this
  repository. A turnkey "walk around your own house" app does not
  exist yet; the client is the largest remaining piece of work for
  anyone who wants the full experience.
- **Scope and limits.** Built for one household on a trusted home
  network: shared-key header auth, no multi-user support, no accounts.
  Still a work in progress — endpoints and config evolve as the client
  does.
