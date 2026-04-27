---
version: alpha
name: Spatial Glass
description: AR OS: frosted lens, aurora gradient, depth hairline.
colors:
  primary: "#F4F6FB"
  secondary: "#8F99B0"
  tertiary: "#7FE6D6"
  neutral: "#10131B"
  surface: "#1A1F2C"
  on-primary: "#10131B"
typography:
  display:
    fontFamily: Outfit
    fontSize: 4rem
    fontWeight: 300
    letterSpacing: "-0.04em"
  h1:
    fontFamily: Outfit
    fontSize: 2.2rem
    fontWeight: 400
  body:
    fontFamily: Outfit
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.72rem
    letterSpacing: "0.1em"
rounded:
  sm: 10px
  md: 18px
  lg: 30px
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

A spatial/AR OS palette: frosted-lens translucency, aurora gradient accent, tight hairlines.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F4F6FB`):** Headlines and core text.
- **Secondary (`#8F99B0`):** Borders, captions, and metadata.
- **Tertiary (`#7FE6D6`):** The sole driver for interaction. Reserve it.
- **Neutral (`#10131B`):** The page foundation.

## Typography

- **display:** Outfit 4rem
- **h1:** Outfit 2.2rem
- **body:** Outfit 0.95rem
- **label:** IBM Plex Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
