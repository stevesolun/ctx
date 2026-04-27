---
version: alpha
name: Realty Open House
description: Open-house listing: linen warm, brass accents.
colors:
  primary: "#1E1914"
  secondary: "#8A7E6C"
  tertiary: "#A57D46"
  neutral: "#F2ECDE"
  surface: "#FBF5E6"
  on-primary: "#FBF5E6"
typography:
  display:
    fontFamily: Cormorant Garamond
    fontSize: 5rem
    fontWeight: 400
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Cormorant Garamond
    fontSize: 2.6rem
    fontWeight: 400
  body:
    fontFamily: Inter
    fontSize: 1rem
    lineHeight: 1.65
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    fontWeight: 500
    letterSpacing: "0.2em"
rounded:
  sm: 2px
  md: 4px
  lg: 6px
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

A premium real-estate palette: linen warmth, brass accent, tall serif display.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1E1914`):** Headlines and core text.
- **Secondary (`#8A7E6C`):** Borders, captions, and metadata.
- **Tertiary (`#A57D46`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F2ECDE`):** The page foundation.

## Typography

- **display:** Cormorant Garamond 5rem
- **h1:** Cormorant Garamond 2.6rem
- **body:** Inter 1rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
