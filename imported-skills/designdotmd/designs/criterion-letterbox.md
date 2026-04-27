---
version: alpha
name: Criterion Letterbox
description: Arthouse film label: cream sleeve, spine red, 4:3 crops.
colors:
  primary: "#141210"
  secondary: "#6E6A60"
  tertiary: "#A6262B"
  neutral: "#EFE8D8"
  surface: "#F8F1E1"
  on-primary: "#F8F1E1"
typography:
  display:
    fontFamily: EB Garamond
    fontSize: 5.5rem
    fontWeight: 600
    letterSpacing: "-0.015em"
  h1:
    fontFamily: EB Garamond
    fontSize: 2.6rem
    fontWeight: 600
  body:
    fontFamily: EB Garamond
    fontSize: 1.05rem
    lineHeight: 1.7
  label:
    fontFamily: Inter
    fontSize: 0.7rem
    fontWeight: 600
    letterSpacing: "0.22em"
rounded:
  sm: 0px
  md: 0px
  lg: 2px
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

A film-label system inspired by boxed editions. Cream card stock, spine-red accent, plate-numbered hierarchy.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#141210`):** Headlines and core text.
- **Secondary (`#6E6A60`):** Borders, captions, and metadata.
- **Tertiary (`#A6262B`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EFE8D8`):** The page foundation.

## Typography

- **display:** EB Garamond 5.5rem
- **h1:** EB Garamond 2.6rem
- **body:** EB Garamond 1.05rem
- **label:** Inter 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
