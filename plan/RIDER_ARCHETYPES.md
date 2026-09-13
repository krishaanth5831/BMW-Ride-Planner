# Future rider archetypes

> Idea backlog only. These archetypes are not implemented and must not replace
> capability limits learned from real rider telemetry.

Possible high-level rider types for future onboarding, route defaults, filters,
and explanations:

| Archetype | Likely route priorities |
|---|---|
| Commuter | Reliable arrival, direct routing, predictable traffic |
| Touring rider | Long-distance comfort, scenery, fuel and rest stops |
| Adventure rider | Exploration, remote places, mixed-surface capability |
| Sport rider | Curves, technical roads, performance within safety limits |
| Cruiser rider | Relaxed pace, open roads, social destinations |
| Urban rider | Short trips, maneuverability, parking and city access |
| Off-road / enduro rider | Trails, gravel, surface difficulty and elevation |
| Café / heritage rider | Short scenic outings, landmarks and social stops |
| Track rider | Circuit-focused performance; not public-road optimization |
| Social / group rider | Meetups, regrouping stops and group-manageable routes |

For a simple first version, these could be condensed into four customer-facing
presets: **Relaxed**, **Touring**, **Adventure**, and **Sportive**.

## Guardrails for later implementation

- Treat an archetype as a preference preset, never as proof of riding ability.
- Keep the telemetry-derived capability ceiling as a hard safety constraint.
- Let riders change or combine preference presets.
- Explain which route choices came from the preset versus observed behavior.
- Validate the taxonomy with riders before presenting it as personalization.
