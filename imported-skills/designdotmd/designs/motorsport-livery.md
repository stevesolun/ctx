---
version: alpha
name: Motorsport Livery
description: F1 livery: carbon black, racing red, pit-lane yellow.
colors:
  primary: "#F2F2F2"
  secondary: "#8C8C8C"
  tertiary: "#E10600"
  neutral: "#0B0B0B"
  surface: "#151515"
  on-primary: "#0B0B0B"
typography:
  display:
    fontFamily: Oswald
    fontSize: 5rem
    fontWeight: 700
    letterSpacing: "0.02em"
  h1:
    fontFamily: Oswald
    fontSize: 2.5rem
    fontWeight: 700
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.5
  label:
    fontFamily: Oswald
    fontSize: 0.78rem
    letterSpacing: "0.16em"
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

A motorsport-livery palette: carbon black, racing red, pit-lane yellow accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F2F2F2`):** Headlines and core text.
- **Secondary (`#8C8C8C`):** Borders, captions, and metadata.
- **Tertiary (`#E10600`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0B0B0B`):** The page foundation.

## Typography

- **display:** Oswald 5rem
- **h1:** Oswald 2.5rem
- **body:** Inter 0.95rem
- **label:** Oswald 0.78rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
