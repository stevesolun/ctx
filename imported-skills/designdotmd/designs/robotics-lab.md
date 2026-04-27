---
version: alpha
name: Robotics Lab
description: Lab-bench white, safety-orange kill switch.
colors:
  primary: "#17191C"
  secondary: "#636870"
  tertiary: "#FF6A00"
  neutral: "#F2F3F5"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Space Grotesk
    fontSize: 3.75rem
    fontWeight: 600
  h1:
    fontFamily: Space Grotesk
    fontSize: 2rem
    fontWeight: 600
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.72rem
    letterSpacing: "0.08em"
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

A robotics-control UI: bench-white surface, neutral grey, single hot-orange for STOP.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#17191C`):** Headlines and core text.
- **Secondary (`#636870`):** Borders, captions, and metadata.
- **Tertiary (`#FF6A00`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F2F3F5`):** The page foundation.

## Typography

- **display:** Space Grotesk 3.75rem
- **h1:** Space Grotesk 2rem
- **body:** Inter 0.95rem
- **label:** IBM Plex Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
