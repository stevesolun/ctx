---
version: alpha
name: Quantum Lab
description: Physics-lab dark: cold plasma blue, superfluid teal.
colors:
  primary: "#E6F3FF"
  secondary: "#6A8AA8"
  tertiary: "#4FD6E0"
  neutral: "#050A14"
  surface: "#0A1220"
  on-primary: "#050A14"
typography:
  display:
    fontFamily: Space Grotesk
    fontSize: 3.5rem
    fontWeight: 500
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Space Grotesk
    fontSize: 1.85rem
    fontWeight: 500
  body:
    fontFamily: IBM Plex Sans
    fontSize: 0.92rem
    lineHeight: 1.55
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.7rem
    letterSpacing: "0.12em"
rounded:
  sm: 2px
  md: 4px
  lg: 8px
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

A quantum-computing console aesthetic: cold blues, teal accents, monospace readouts.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E6F3FF`):** Headlines and core text.
- **Secondary (`#6A8AA8`):** Borders, captions, and metadata.
- **Tertiary (`#4FD6E0`):** The sole driver for interaction. Reserve it.
- **Neutral (`#050A14`):** The page foundation.

## Typography

- **display:** Space Grotesk 3.5rem
- **h1:** Space Grotesk 1.85rem
- **body:** IBM Plex Sans 0.92rem
- **label:** IBM Plex Mono 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
