---
version: alpha
name: Linen
description: Hand-woven textures, tea stains, slow.
colors:
  primary: "#2A2420"
  secondary: "#7A6E64"
  tertiary: "#A47D4E"
  neutral: "#EEE7D9"
  surface: "#F7F1E3"
  on-primary: "#F7F1E3"
typography:
  display:
    fontFamily: EB Garamond
    fontSize: 4.75rem
    fontWeight: 500
    letterSpacing: "-0.01em"
  h1:
    fontFamily: EB Garamond
    fontSize: 2.75rem
    fontWeight: 500
  body:
    fontFamily: EB Garamond
    fontSize: 1.15rem
    lineHeight: 1.7
  label:
    fontFamily: Inter
    fontSize: 0.75rem
    letterSpacing: "0.1em"
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

A domestic, tactile palette. Everything a half-shade warmer than you'd expect. Quiet ink, warm parchment.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2A2420`):** Headlines and core text.
- **Secondary (`#7A6E64`):** Borders, captions, and metadata.
- **Tertiary (`#A47D4E`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EEE7D9`):** The page foundation.

## Typography

- **display:** EB Garamond 4.75rem
- **h1:** EB Garamond 2.75rem
- **body:** EB Garamond 1.15rem
- **label:** Inter 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
