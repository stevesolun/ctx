---
version: alpha
name: Farmsim Harvest
description: Cozy farm sim: golden wheat, rosy blush, soft sky.
colors:
  primary: "#3D2A16"
  secondary: "#B09674"
  tertiary: "#E67E5C"
  neutral: "#FFF3DC"
  surface: "#FFF9EA"
  on-primary: "#FFF9EA"
typography:
  display:
    fontFamily: Chewy
    fontSize: 4.5rem
    fontWeight: 400
  h1:
    fontFamily: Fredoka
    fontSize: 2.3rem
    fontWeight: 600
  body:
    fontFamily: Fredoka
    fontSize: 1rem
    lineHeight: 1.6
  label:
    fontFamily: Fredoka
    fontSize: 0.78rem
    fontWeight: 600
    letterSpacing: "0.04em"
rounded:
  sm: 8px
  md: 16px
  lg: 26px
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

Cozy simulation-game palette. Warm wheat surfaces, rosy primary, handpainted vibe.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#3D2A16`):** Headlines and core text.
- **Secondary (`#B09674`):** Borders, captions, and metadata.
- **Tertiary (`#E67E5C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#FFF3DC`):** The page foundation.

## Typography

- **display:** Chewy 4.5rem
- **h1:** Fredoka 2.3rem
- **body:** Fredoka 1rem
- **label:** Fredoka 0.78rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
