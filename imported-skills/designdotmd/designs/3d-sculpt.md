---
version: alpha
name: 3D Sculpt
description: 3D viewport: studio grey, mesh cyan, normal magenta.
colors:
  primary: "#E8E8E6"
  secondary: "#8C8B88"
  tertiary: "#00BFCF"
  neutral: "#1C1C1E"
  surface: "#252527"
  on-primary: "#1C1C1E"
typography:
  display:
    fontFamily: Space Grotesk
    fontSize: 3.5rem
    fontWeight: 600
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Space Grotesk
    fontSize: 1.85rem
    fontWeight: 600
  body:
    fontFamily: Inter
    fontSize: 0.92rem
    lineHeight: 1.55
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.7rem
    letterSpacing: "0.06em"
rounded:
  sm: 3px
  md: 6px
  lg: 10px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

A 3D-tool palette: studio-grey viewport, mesh-cyan highlight, normal-magenta axis accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E8E8E6`):** Headlines and core text.
- **Secondary (`#8C8B88`):** Borders, captions, and metadata.
- **Tertiary (`#00BFCF`):** The sole driver for interaction. Reserve it.
- **Neutral (`#1C1C1E`):** The page foundation.

## Typography

- **display:** Space Grotesk 3.5rem
- **h1:** Space Grotesk 1.85rem
- **body:** Inter 0.92rem
- **label:** IBM Plex Mono 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
