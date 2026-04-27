---
version: alpha
name: Space Mission
description: Mission-control amber on black. Telemetry forever.
colors:
  primary: "#FFB347"
  secondary: "#8A6A38"
  tertiary: "#FF6B35"
  neutral: "#050505"
  surface: "#0D0B08"
  on-primary: "#050505"
typography:
  display:
    fontFamily: IBM Plex Mono
    fontSize: 3.25rem
    fontWeight: 500
    letterSpacing: "0"
  h1:
    fontFamily: IBM Plex Mono
    fontSize: 1.7rem
    fontWeight: 500
  body:
    fontFamily: IBM Plex Mono
    fontSize: 0.9rem
    lineHeight: 1.55
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.68rem
    letterSpacing: "0.1em"
rounded:
  sm: 0px
  md: 2px
  lg: 4px
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

A 1970s mission-control palette: amber glow on black CRT, cold grids, monospace data.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#FFB347`):** Headlines and core text.
- **Secondary (`#8A6A38`):** Borders, captions, and metadata.
- **Tertiary (`#FF6B35`):** The sole driver for interaction. Reserve it.
- **Neutral (`#050505`):** The page foundation.

## Typography

- **display:** IBM Plex Mono 3.25rem
- **h1:** IBM Plex Mono 1.7rem
- **body:** IBM Plex Mono 0.9rem
- **label:** IBM Plex Mono 0.68rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
