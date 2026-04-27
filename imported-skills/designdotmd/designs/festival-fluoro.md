---
version: alpha
name: Festival Fluoro
description: Open-air festival: sunset magenta, lime wash, neon type.
colors:
  primary: "#16082B"
  secondary: "#7A5AA3"
  tertiary: "#FF2D92"
  neutral: "#DAFF4C"
  surface: "#EBFF7A"
  on-primary: "#16082B"
typography:
  display:
    fontFamily: Archivo Black
    fontSize: 5.5rem
    fontWeight: 900
    letterSpacing: "-0.04em"
  h1:
    fontFamily: Archivo Black
    fontSize: 2.8rem
    fontWeight: 900
  body:
    fontFamily: Inter
    fontSize: 0.94rem
    lineHeight: 1.5
  label:
    fontFamily: Archivo Black
    fontSize: 0.72rem
    letterSpacing: "0.16em"
rounded:
  sm: 4px
  md: 8px
  lg: 14px
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

A festival-poster palette: saturated magenta/lime, neon display type, loud but legible.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#16082B`):** Headlines and core text.
- **Secondary (`#7A5AA3`):** Borders, captions, and metadata.
- **Tertiary (`#FF2D92`):** The sole driver for interaction. Reserve it.
- **Neutral (`#DAFF4C`):** The page foundation.

## Typography

- **display:** Archivo Black 5.5rem
- **h1:** Archivo Black 2.8rem
- **body:** Inter 0.94rem
- **label:** Archivo Black 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
