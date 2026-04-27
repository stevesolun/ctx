---
version: alpha
name: Denim Workwear
description: Heritage workwear: selvedge indigo, rivet copper.
colors:
  primary: "#F6F1E7"
  secondary: "#A89878"
  tertiary: "#C96F2E"
  neutral: "#1C2B45"
  surface: "#243351"
  on-primary: "#F6F1E7"
typography:
  display:
    fontFamily: Oswald
    fontSize: 4.75rem
    fontWeight: 700
    letterSpacing: "0.02em"
  h1:
    fontFamily: Oswald
    fontSize: 2.5rem
    fontWeight: 700
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.6
  label:
    fontFamily: Oswald
    fontSize: 0.8rem
    letterSpacing: "0.14em"
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

A heritage workwear system: deep indigo, copper-rivet accent, thick slab weights.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F6F1E7`):** Headlines and core text.
- **Secondary (`#A89878`):** Borders, captions, and metadata.
- **Tertiary (`#C96F2E`):** The sole driver for interaction. Reserve it.
- **Neutral (`#1C2B45`):** The page foundation.

## Typography

- **display:** Oswald 4.75rem
- **h1:** Oswald 2.5rem
- **body:** Inter 0.95rem
- **label:** Oswald 0.8rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
