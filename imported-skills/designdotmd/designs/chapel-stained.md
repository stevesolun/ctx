---
version: alpha
name: Chapel Stained
description: Chapel hymnal: stained-glass blue, gilt, parchment.
colors:
  primary: "#F3E9CC"
  secondary: "#A89478"
  tertiary: "#E2B44C"
  neutral: "#0F1E4A"
  surface: "#152658"
  on-primary: "#F3E9CC"
typography:
  display:
    fontFamily: Cormorant Garamond
    fontSize: 5rem
    fontWeight: 500
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Cormorant Garamond
    fontSize: 2.5rem
    fontWeight: 500
  body:
    fontFamily: EB Garamond
    fontSize: 1.05rem
    lineHeight: 1.75
  label:
    fontFamily: EB Garamond
    fontSize: 0.78rem
    letterSpacing: "0.22em"
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

A chapel-inspired palette: stained-glass blue surface, gilt accent, parchment body.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F3E9CC`):** Headlines and core text.
- **Secondary (`#A89478`):** Borders, captions, and metadata.
- **Tertiary (`#E2B44C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0F1E4A`):** The page foundation.

## Typography

- **display:** Cormorant Garamond 5rem
- **h1:** Cormorant Garamond 2.5rem
- **body:** EB Garamond 1.05rem
- **label:** EB Garamond 0.78rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
