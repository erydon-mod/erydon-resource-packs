# Portorium branding

The polished stone plaques, metal inlays and Cinzel lettering form the shared
ERYDON family branding. Wide spaces use the full logo; compact square slots use
the emblem. The 32x and 64x resource packs retain separate resolution lettering.

## Written names

Use **ERYDON**, **ERYDON Themelios**, **ERYDON Daedalon** and
**ERYDON Metrakron** for official mod names, listings and full titles.
Themelios, Daedalon and Metrakron may be shortened in descriptive prose.
Always write ERYDON in capitals. Pack names are **ERYDON 32x Resource Pack**
and **ERYDON 64x Resource Pack**. **ERYDON Collection** refers to the whole
family of mods and resource packs.

The approved plaque artwork retains its engraved capital-letter typography.
Written names follow the convention above. Technical mod IDs, namespaces,
commands, URLs and artifact filenames retain their existing spelling and case.

Oliver owns the source photograph of real Portoro marble. GPT was used to
upscale that photograph; it was not generated as a new stone texture. Oliver
confirmed this provenance on 13 September 2026. The approved plaque geometry,
lettering and lighting were assembled and rendered in Blender with Codex
assistance. Exported images retain the approved appearance and texture crops.

Lettering uses Cinzel SemiBold by the Cinzel Project Authors (Natanael Gama),
under the SIL Open Font License 1.1: https://github.com/NDISCOVER/Cinzel.
These branding exports contain rendered lettering, not an embedded font file.
Existing repository asset licences and third-party notices continue to apply.

Creative-tab animation uses transparent 64-pixel square PNG frames and one
Minecraft tick per frame, without interpolation. GUI logos use a filtered
20-fps sheet drawn only while the relevant menu is visible. Mod Menu icons
and resource-pack selection icons use static square PNGs.

## Resource-pack README artwork

The README uses the approved animated web logos supplied by Oliver in the
ERYDON logo integration pack:

- `docs/branding/erydon-32x-logo.gif` is an exact copy of
  `erydon-32x-logo-web-black.gif`.
- `docs/branding/erydon-64x-logo.gif` is an exact copy of
  `erydon-64x-logo-web-black.gif`.

These documentation GIFs retain their original frames and metadata without
conversion or re-encoding. They do not replace either pack's static `pack.png`
selection icon or the existing animated PNG emblems used by the family mods.
The Portoro and Cinzel provenance above and the existing restricted licence
continue to apply.

## Branding validation

Use the approved animated full logo for wide README and custom GUI headers,
and the animated emblem for compact custom GUI slots wherever animation is
supported. Keep static square emblems in standard Fabric, Mod Menu and resource-
pack selection slots. Preserve aspect ratio and the approved playback timing.
Use purpose-sized 8-bit runtime exports; keep full-resolution artwork masters
outside runtime resource directories. Documentation GIFs stay in `docs/branding`.

Run `python -B tools/audit_branding.py` for the portable source audit. Its
`config/branding-assets.json` records approved hashes, dimensions, frame counts,
timing and consumers. Update that contract only when deliberately replacing an
approved export; never resize an entire animation atlas as if it were one frame.
The matching `tools/test_branding.py` checks rejection of damaged, mistimed,
unreferenced or oversized branding assets. Gradle source/resource checks run
these gates automatically; resource-pack CI runs the same source audit.
