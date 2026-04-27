---
version: alpha
name: Cyber Matrix
description: Cyberpunk neon grid meets operator terminal.
colors:
  primary: "#E8FDFF"
  secondary: "#7BD3E0"
  tertiary: "#FF2A9A"
  neutral: "#070A12"
  surface: "#0E131F"
  on-primary: "#070A12"
typography:
  display:
    fontFamily: Orbitron
    fontSize: 3.5rem
    fontWeight: 800
    letterSpacing: "0.02em"
  h1:
    fontFamily: Orbitron
    fontSize: 1.8rem
    fontWeight: 700
  body:
    fontFamily: IBM Plex Mono
    fontSize: 0.9rem
    lineHeight: 1.55
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.7rem
    letterSpacing: "0.12em"
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

Dense, high-voltage UI: pitch-black surface, magenta/cyan duotone, hard-edged panels.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E8FDFF`):** Headlines and core text.
- **Secondary (`#7BD3E0`):** Borders, captions, and metadata.
- **Tertiary (`#FF2A9A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#070A12`):** The page foundation.

## Typography

- **display:** Orbitron 3.5rem
- **h1:** Orbitron 1.8rem
- **body:** IBM Plex Mono 0.9rem
- **label:** IBM Plex Mono 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
