---
version: alpha
name: DevOps Graphite
description: Prod-green, staging-amber, build-pipeline blue.
colors:
  primary: "#E8ECF1"
  secondary: "#8893A1"
  tertiary: "#3EC893"
  neutral: "#121418"
  surface: "#1A1D22"
  on-primary: "#0A0B0D"
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
    fontSize: 0.72rem
    letterSpacing: "0.04em"
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

An infrastructure-console palette built for at-a-glance status.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E8ECF1`):** Headlines and core text.
- **Secondary (`#8893A1`):** Borders, captions, and metadata.
- **Tertiary (`#3EC893`):** The sole driver for interaction. Reserve it.
- **Neutral (`#121418`):** The page foundation.

## Typography

- **display:** Space Grotesk 3.5rem
- **h1:** Space Grotesk 1.85rem
- **body:** Inter 0.92rem
- **label:** IBM Plex Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
