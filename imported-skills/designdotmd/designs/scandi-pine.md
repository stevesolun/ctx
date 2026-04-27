---
version: alpha
name: Scandi Pine
description: Scandi interior: birch, oat, one forest pine.
colors:
  primary: "#1B1B1A"
  secondary: "#8A8578"
  tertiary: "#4C7A55"
  neutral: "#F0EADD"
  surface: "#FBF5E8"
  on-primary: "#FBF5E8"
typography:
  display:
    fontFamily: Manrope
    fontSize: 4rem
    fontWeight: 500
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Manrope
    fontSize: 2.1rem
    fontWeight: 500
  body:
    fontFamily: Manrope
    fontSize: 0.98rem
    lineHeight: 1.6
  label:
    fontFamily: Manrope
    fontSize: 0.72rem
    fontWeight: 500
    letterSpacing: "0.2em"
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

A Scandinavian furniture palette: birch-white surface, oat-warm secondary, forest-pine accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1B1B1A`):** Headlines and core text.
- **Secondary (`#8A8578`):** Borders, captions, and metadata.
- **Tertiary (`#4C7A55`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F0EADD`):** The page foundation.

## Typography

- **display:** Manrope 4rem
- **h1:** Manrope 2.1rem
- **body:** Manrope 0.98rem
- **label:** Manrope 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
